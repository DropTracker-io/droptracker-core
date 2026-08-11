"""Group mini-sites on ``{sub}.SITES_DOMAIN`` (sites-v1).

Builder (session + group admin; writes additionally require the
``custom_site`` entitlement — the ``custom_embeds`` double-gate pattern):

  GET    /api/v1/groups/{id}/site                       full draft state
  GET    /api/v1/groups/{id}/site/meta                  block catalog + limits
  POST   /api/v1/groups/{id}/site/claim                 claim a subdomain (+ToS)
  PUT    /api/v1/groups/{id}/site                       theme/palette/nav/css
  POST   /api/v1/groups/{id}/site/pages                 add page
  PUT    /api/v1/groups/{id}/site/pages/{page_id}       save draft blocks
  POST   /api/v1/groups/{id}/site/pages/{page_id}/publish | /unpublish
  POST   /api/v1/groups/{id}/site/publish | /unpublish  site-level flag
  DELETE /api/v1/groups/{id}/site/pages/{page_id}       (admin, NO entitlement)
  DELETE /api/v1/groups/{id}/site                       (admin, NO entitlement)
  POST   /api/v1/groups/{id}/site/preview-token         HMAC draft-preview token

Public render projections (anonymous — called only by the Next BFF; return
structure only, never hydrated stats, so the existing public group endpoints
keep owning privacy filtering and caching):

  GET /api/v1/sites/resolve?host={sub}
  GET /api/v1/sites/{sub}/pages/{slug}[?preview={token}]

Render gate: ``db.entitlements.group_has_entitlement(gid, "custom_site")``
(60s cache, fail-closed) AND site.published AND not suspended. A lapsed
subscription therefore takes the site down within a minute while the stored
content stays intact; DELETE needs no entitlement so downgraded groups can
clean up.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from typing import Any

from quart import Blueprint, jsonify, request

from db import AuditLog
from db.entitlements import group_has_entitlement, invalidate_entitlement_cache  # noqa: F401
from db.models import Group, GroupSite, GroupSitePage
from utils.redis import redis_client
from web_api.common import abort_problem, db_session, private_no_store, with_cache_headers
from web_api.deps import (
    assert_group_admin,
    assert_group_entitlement,
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
)
from web_api.site_sanitizer import CssValidationError, sanitize_css, sanitize_html
from web_api.sites_shared import (
    ALLOWED_PALETTE_KEYS,
    BLOCK_TYPES,
    HOME_SLUG,
    MAX_BLOCKS_PER_PAGE,
    MAX_CUSTOM_CSS_BYTES,
    MAX_CUSTOM_HTML_BLOCKS_PER_PAGE,
    MAX_CUSTOM_HTML_BYTES,
    MAX_NAV_ITEMS,
    MAX_PAGES_PER_SITE,
    MAX_PAGE_JSON_BYTES,
    RESERVED_SUBDOMAINS,
    SCHEMA_VERSION,
    page_slug_error,
    palette_value_ok,
    subdomain_error,
)

sites_bp = Blueprint("v1_sites", __name__)

ENTITLEMENT = "custom_site"
# Same first-party asset host the rest of the API hands out; allowed by the
# tenant CSP's img-src.
IMG_BASE = "https://www.droptracker.io/img"
TOS_VERSION = "2026-08-07"
THEME_KEYS = ("dusk", "parchment", "wilderness")

# What a claimed subdomain does. "builder" renders the block pages; the other
# two make the address a pure redirect, which is all a lot of clans want.
SITE_MODES = ("builder", "group_page", "redirect")


# Fields that only mean something for a builder site. Touching any of these,
# or selecting builder mode, is what requires the subscription — claiming an
# address and pointing it somewhere is free for every group.
BUILDER_ONLY_FIELDS = ("theme_key", "palette", "nav", "custom_css_source", "roster_public")


def _assert_builder_access(s, user_id: int, group_id: int, user) -> None:
    """Group admin AND the custom_site entitlement (superadmins bypass)."""
    assert_group_entitlement(
        s, user_id, group_id, ENTITLEMENT,
        manage_guild_ids=manageable_guild_ids(user_id), user=user,
    )


def _redirect_url_error(url: str) -> str | None:
    """Reject anything that isn't a plain https:// link, and anything pointing
    back at the sites domain — a subdomain redirecting to itself (or to another
    tenant) is a redirect loop, not a feature."""
    if not isinstance(url, str) or not url.strip():
        return "A destination URL is required for a redirect site."
    url = url.strip()
    if len(url) > 500:
        return "That URL is too long (500 characters max)."
    if not url.lower().startswith("https://"):
        return "The destination must be a full https:// URL."
    host = url[len("https://"):].split("/", 1)[0].split("@")[-1].split(":", 1)[0].lower()
    if not host or "." not in host:
        return "That doesn't look like a valid web address."
    domain = (os.getenv("SITES_DOMAIN") or "").strip().lower()
    if domain and (host == domain or host.endswith("." + domain)):
        return f"A redirect can't point back at {domain} — that would loop."
    return None


def _resolved_redirect_target(site: GroupSite) -> str | None:
    """Absolute URL this subdomain should bounce visitors to, or None when it
    renders its own pages."""
    if site.mode == "redirect":
        return (site.redirect_url or "").strip() or None
    if site.mode == "group_page":
        # Numeric id is always valid; the profile itself canonicalises to the
        # slug, so this survives clan renames.
        base = (os.getenv("WEB_SITE_URL") or "https://www.droptracker.io/").rstrip("/")
        return f"{base}/groups/{site.group_id}"
    return None

_PREVIEW_TTL_SECONDS = 15 * 60
_SECRET = os.getenv("JWT_TOKEN_KEY") or os.getenv("ENCRYPTION_KEY") or "dev-insecure-web-secret"

# Publish/draft-save rate limits (Redis fixed windows, suggestions.py pattern).
_PUBLISH_PER_HOUR = int(os.getenv("WEB_SITE_PUBLISHES_PER_HOUR", "30"))
# Draft saves are cheap and an active building session is bursty — a single
# afternoon of iterating on one page blows past 60, which reads to the author
# as "the editor stopped saving".
_SAVES_PER_HOUR = int(os.getenv("WEB_SITE_SAVES_PER_HOUR", "240"))
_SLUG_CHANGES_PER_30D = int(os.getenv("WEB_SITE_SLUG_CHANGES_PER_30D", "2"))


def _rc():
    return getattr(redis_client, "client", None)


def _rate_limited(bucket: str, key_id: int, limit: int, window_s: int) -> bool:
    conn = _rc()
    if conn is None:
        return False
    try:
        key = f"web:ratelimit:site:{bucket}:{key_id}"
        count = conn.incr(key)
        if count == 1:
            conn.expire(key, window_s)
        return count > limit
    except Exception:
        return False


# --- serialization ----------------------------------------------------------


def _page_summary(p: GroupSitePage) -> dict:
    return {
        "page_id": p.page_id,
        "slug": p.slug,
        "title": p.title,
        "position": p.position,
        "published": bool(p.published),
        "has_draft_changes": bool(p.published_blocks != p.draft_blocks),
        "custom_css_source": p.custom_css_source or "",
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        "published_at": p.published_at.isoformat() if p.published_at else None,
    }


def _site_admin_view(site: GroupSite, s=None) -> dict:
    roster_public = _roster_enabled(s, site.group_id) if s is not None else False
    return {
        "roster_public": roster_public,
        "site_id": site.site_id,
        "group_id": site.group_id,
        "subdomain": site.subdomain,
        "mode": site.mode or "builder",
        "redirect_url": site.redirect_url or "",
        "redirect_target": _resolved_redirect_target(site),
        "theme_key": site.theme_key,
        "palette": json.loads(site.palette) if site.palette else {},
        "nav": json.loads(site.nav) if site.nav else [],
        "custom_css_source": site.custom_css_source or "",
        "published": bool(site.published),
        "needs_review": bool(site.needs_review),
        "suspended": site.suspended_at is not None,
        "suspend_reason": site.suspend_reason,
        "site_url": _site_url(site.subdomain),
        "pages": [_page_summary(p) for p in sorted(site.pages, key=lambda x: x.position)],
    }


def _site_url(subdomain: str) -> str:
    domain = (os.getenv("SITES_DOMAIN") or "").strip()
    return f"https://{subdomain}.{domain}/" if domain else ""


def _load_site(s, group_id: int) -> GroupSite | None:
    return s.query(GroupSite).filter(GroupSite.group_id == group_id).first()


def _audit(s, user_id: int | None, group_id: int, action: str, target: str,
           before=None, after=None) -> None:
    s.add(
        AuditLog(
            actor_user_id=user_id,
            group_id=group_id,
            action=action,
            target=target,
            before=json.dumps(before) if before is not None else None,
            after=json.dumps(after) if after is not None else None,
        )
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


# --- block validation --------------------------------------------------------


def _require_str(block: dict, key: str, i: int, *, max_len: int, required: bool = True) -> None:
    v = block.get(key)
    if v is None or v == "":
        if required:
            abort_problem(422, "Invalid block", f"Block #{i + 1} needs a non-empty '{key}'.")
        return
    if not isinstance(v, str) or len(v) > max_len:
        abort_problem(
            422, "Invalid block",
            f"Block #{i + 1} '{key}' must be a string of at most {max_len} characters.",
        )


def _validate_blocks(blocks) -> list[dict]:
    """Structural validation + custom_html sanitation. Returns the normalized
    list ready to be JSON-serialized into draft_blocks."""
    if not isinstance(blocks, list):
        abort_problem(422, "Invalid page", "'blocks' must be an array.")
    if len(blocks) > MAX_BLOCKS_PER_PAGE:
        abort_problem(422, "Invalid page", f"At most {MAX_BLOCKS_PER_PAGE} blocks per page.")

    html_blocks = 0
    out: list[dict] = []
    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            abort_problem(422, "Invalid block", f"Block #{i + 1} must be an object.")
        btype = block.get("type")
        if btype not in BLOCK_TYPES:
            abort_problem(
                422, "Invalid block",
                f"Block #{i + 1} has unknown type '{btype}'.",
            )
        bid = block.get("id")
        if not isinstance(bid, str) or not bid or len(bid) > 32:
            abort_problem(422, "Invalid block", f"Block #{i + 1} needs an 'id' (<=32 chars).")

        b = dict(block)
        if btype == "hero":
            _require_str(b, "heading", i, max_len=80)
            _require_str(b, "tagline", i, max_len=200, required=False)
            _require_str(b, "image_url", i, max_len=300, required=False)
        elif btype == "markdown":
            _require_str(b, "body", i, max_len=8000)
        elif btype == "image":
            _require_str(b, "url", i, max_len=300)
            _require_str(b, "alt", i, max_len=200, required=False)
            _require_str(b, "caption", i, max_len=300, required=False)
        elif btype == "buttons":
            items = b.get("items")
            if not isinstance(items, list) or not 1 <= len(items) <= 6:
                abort_problem(
                    422, "Invalid block", f"Block #{i + 1} needs 1-6 button items."
                )
            for item in items:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("label"), str)
                    or not item["label"].strip()
                    or len(item["label"]) > 40
                    or not isinstance(item.get("href"), str)
                    or len(item["href"]) > 300
                ):
                    abort_problem(
                        422, "Invalid block",
                        f"Block #{i + 1} buttons need label (<=40) and href (<=300).",
                    )
                href = item["href"].strip()
                if not (href.startswith("https://") or href.startswith("/")):
                    abort_problem(
                        422, "Invalid block",
                        f"Block #{i + 1} button links must be https:// or site-relative.",
                    )
        elif btype == "custom_html":
            html_blocks += 1
            if html_blocks > MAX_CUSTOM_HTML_BLOCKS_PER_PAGE:
                abort_problem(
                    422, "Invalid page",
                    f"At most {MAX_CUSTOM_HTML_BLOCKS_PER_PAGE} custom HTML blocks per page.",
                )
            source = b.get("source")
            if not isinstance(source, str) or not source.strip():
                abort_problem(
                    422, "Invalid block", f"Block #{i + 1} needs non-empty 'source' HTML."
                )
            if len(source.encode("utf-8", "replace")) > MAX_CUSTOM_HTML_BYTES:
                abort_problem(
                    422, "Invalid block",
                    f"Custom HTML is limited to {MAX_CUSTOM_HTML_BYTES // 1024} KB per block.",
                )
            # The sanitized output is what renders; source is editor round-trip.
            b["html"] = sanitize_html(source)
        # Data blocks (top_players, lootboard, ...) carry small enum/limit
        # settings the renderer clamps again server-side at render time — the
        # stored config is advisory, so structural checks above suffice.
        out.append(b)

    payload = json.dumps(out)
    if len(payload.encode("utf-8", "replace")) > MAX_PAGE_JSON_BYTES:
        abort_problem(
            422, "Invalid page",
            f"Page content is limited to {MAX_PAGE_JSON_BYTES // 1024} KB.",
        )
    return out


def _page_has_custom_html(page: GroupSitePage) -> bool:
    try:
        return any(
            b.get("type") == "custom_html" for b in json.loads(page.draft_blocks or "[]")
        )
    except Exception:
        return False


# --- preview tokens ----------------------------------------------------------


def make_preview_token(site_id: int, now: float | None = None) -> str:
    exp = int((now or time.time()) + _PREVIEW_TTL_SECONDS)
    sig = hmac.new(_SECRET.encode(), f"site-preview:{site_id}:{exp}".encode(), hashlib.sha256)
    return f"{exp}.{sig.hexdigest()}"


def preview_token_valid(site_id: int, token: str, now: float | None = None) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < (now or time.time()):
        return False
    expected = hmac.new(
        _SECRET.encode(), f"site-preview:{site_id}:{exp}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


# --- builder endpoints -------------------------------------------------------


@sites_bp.get("/groups/<int:group_id>/site")
async def get_group_site(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            site = _load_site(s, group_id)
            # The tab is open to every group (redirect modes are free); this
            # tells the UI whether to offer the builder half or upsell it.
            from web_api.deps import is_superadmin

            can_build = bool(
                is_superadmin(user) or group_has_entitlement(group_id, ENTITLEMENT)
            )
            return {
                "site": _site_admin_view(site, s) if site else None,
                "tos_version": TOS_VERSION,
                "can_build": can_build,
            }

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@sites_bp.get("/groups/<int:group_id>/site/meta")
async def get_site_meta(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            return {
                "block_types": list(BLOCK_TYPES),
                "theme_keys": list(THEME_KEYS),
                "palette_keys": sorted(ALLOWED_PALETTE_KEYS),
                "limits": {
                    "max_pages": MAX_PAGES_PER_SITE,
                    "max_blocks_per_page": MAX_BLOCKS_PER_PAGE,
                    "max_custom_html_blocks_per_page": MAX_CUSTOM_HTML_BLOCKS_PER_PAGE,
                    "max_custom_html_bytes": MAX_CUSTOM_HTML_BYTES,
                    "max_custom_css_bytes": MAX_CUSTOM_CSS_BYTES,
                    "max_nav_items": MAX_NAV_ITEMS,
                },
                "reserved_subdomains": sorted(RESERVED_SUBDOMAINS),
                "schema_version": SCHEMA_VERSION,
                "tos_version": TOS_VERSION,
                "sites_domain": (os.getenv("SITES_DOMAIN") or "").strip(),
            }

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@sites_bp.post("/groups/<int:group_id>/site/claim")
async def claim_site(group_id: int):
    user_id = current_user_id()
    body = await json_body()
    sub = str(body.get("subdomain") or "").strip().lower()
    if not body.get("accept_tos"):
        abort_problem(422, "ToS required", "The hosted-content terms must be accepted to claim.")
    err = subdomain_error(sub)
    if err:
        abort_problem(422, "Invalid subdomain", err)

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            # Claiming an address is open to every group: a subdomain that just
            # redirects is useful on its own. Building pages on it is what the
            # custom_site entitlement gates (see _assert_builder_access).
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            if _load_site(s, group_id) is not None:
                abort_problem(409, "Already claimed", "This group already has a site.")
            taken = s.query(GroupSite).filter(GroupSite.subdomain == sub).first()
            if taken is not None:
                abort_problem(409, "Subdomain taken", f"'{sub}' is already claimed.")

            group = s.query(Group).filter(Group.group_id == group_id).first()
            site = GroupSite(
                group_id=group_id,
                subdomain=sub,
                theme_key="dusk",
                tos_version=TOS_VERSION,
                tos_accepted_at=datetime.utcnow(),
                tos_user_id=user_id,
            )
            s.add(site)
            s.flush()
            home = GroupSitePage(
                site_id=site.site_id,
                slug=HOME_SLUG,
                title=(group.group_name if group else "Home") or "Home",
                position=0,
                draft_blocks=json.dumps(
                    [
                        {"id": "hero-1", "type": "hero",
                         "heading": (group.group_name if group else "Our clan") or "Our clan"},
                        {"id": "stats-1", "type": "stats_row",
                         "stats": ["members", "monthly_loot", "rank"]},
                        {"id": "drops-1", "type": "recent_drops", "limit": 10},
                    ]
                ),
                schema_version=SCHEMA_VERSION,
            )
            s.add(home)
            _audit(s, user_id, group_id, "site.claim", f"group_sites.{sub}",
                   after={"subdomain": sub, "tos_version": TOS_VERSION})
            s.commit()
            return _site_admin_view(site, s)

    saved = await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True, "site": saved}))


@sites_bp.put("/groups/<int:group_id>/site")
async def update_site(group_id: int):
    user_id = current_user_id()
    body = await json_body()
    if _rate_limited("save", group_id, _SAVES_PER_HOUR, 3600):
        abort_problem(429, "Slow down", "Too many saves this hour; try again shortly.")

    theme_key = body.get("theme_key")
    if theme_key is not None and theme_key not in THEME_KEYS:
        abort_problem(422, "Invalid theme", f"theme_key must be one of {', '.join(THEME_KEYS)}.")

    palette = body.get("palette")
    if palette is not None:
        if not isinstance(palette, dict):
            abort_problem(422, "Invalid palette", "'palette' must be an object.")
        cleaned = {}
        for k, v in palette.items():
            if k not in ALLOWED_PALETTE_KEYS:
                continue
            if not palette_value_ok(v):
                abort_problem(422, "Invalid palette", f"Palette value for {k} is not allowed.")
            cleaned[k] = v
        palette = cleaned

    nav = body.get("nav")
    if nav is not None:
        if not isinstance(nav, list) or len(nav) > MAX_NAV_ITEMS:
            abort_problem(422, "Invalid nav", f"'nav' must be an array of at most {MAX_NAV_ITEMS}.")
        for item in nav:
            if not isinstance(item, dict) or not isinstance(item.get("label"), str) \
                    or not item["label"].strip() or len(item["label"]) > 40:
                abort_problem(422, "Invalid nav", "Each nav item needs a label (<=40 chars).")
            has_page = isinstance(item.get("page_slug"), str) and item["page_slug"]
            href = item.get("href")
            has_href = isinstance(href, str) and (
                href.startswith("https://") or href.startswith("/")
            )
            if not has_page and not has_href:
                abort_problem(
                    422, "Invalid nav",
                    "Each nav item needs a page_slug or an https://" " or site-relative href.",
                )

    roster_public = body.get("roster_public")
    if roster_public is not None and not isinstance(roster_public, bool):
        abort_problem(422, "Invalid setting", "'roster_public' must be a boolean.")

    mode = body.get("mode")
    if mode is not None and mode not in SITE_MODES:
        abort_problem(
            422, "Invalid mode", f"'mode' must be one of {', '.join(SITE_MODES)}."
        )
    redirect_url = body.get("redirect_url")
    if redirect_url is not None and not isinstance(redirect_url, str):
        abort_problem(422, "Invalid setting", "'redirect_url' must be a string.")
    # Validate the destination whenever the resulting site would be a custom
    # redirect — either because this request sets that mode, or because it
    # only changes the URL on a site already in it.
    if mode == "redirect" or (redirect_url and mode is None):
        err = _redirect_url_error(redirect_url or "")
        if err:
            abort_problem(422, "Invalid destination", err)

    css_source = body.get("custom_css_source")
    css_out = None
    if css_source is not None:
        if not isinstance(css_source, str):
            abort_problem(422, "Invalid CSS", "'custom_css_source' must be a string.")
        if len(css_source.encode("utf-8", "replace")) > MAX_CUSTOM_CSS_BYTES:
            abort_problem(
                422, "Invalid CSS",
                f"Custom CSS is limited to {MAX_CUSTOM_CSS_BYTES // 1024} KB.",
            )
        if css_source.strip():
            try:
                css_out = sanitize_css(css_source)
            except CssValidationError as e:
                abort_problem(
                    422, "CSS rejected", " | ".join(e.problems[:20]),
                    extra={"problems": e.problems[:50]},
                )
        else:
            css_out = ""

    # Only the builder half is subscription-gated. A request that just points
    # the address somewhere needs group admin; one that configures pages,
    # theming or selects builder mode needs the entitlement too.
    touches_builder = any(body.get(f) is not None for f in BUILDER_ONLY_FIELDS) or mode == "builder"

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            if touches_builder:
                _assert_builder_access(s, user_id, group_id, user)
            else:
                assert_group_admin(
                    s, user_id, group_id, manageable_guild_ids(user_id), user=user
                )
            site = _load_site(s, group_id)
            if site is None:
                abort_problem(404, "No site", "Claim a subdomain first.")

            changed = {}
            if theme_key is not None and theme_key != site.theme_key:
                changed["theme_key"] = theme_key
                site.theme_key = theme_key
            if palette is not None:
                changed["palette"] = True
                site.palette = json.dumps(palette)
            if nav is not None:
                changed["nav"] = True
                site.nav = json.dumps(nav)
            if css_source is not None:
                changed["custom_css_sha"] = _sha(css_source)
                site.custom_css_source = css_source
                site.custom_css = css_out
            if mode is not None and mode != site.mode:
                changed["mode"] = mode
                site.mode = mode
                # A redirect has no draft to review, so requiring a separate
                # Publish click would just be a step that looks broken.
                # Choosing the mode (with a valid destination) is the intent.
                if mode in ("group_page", "redirect") and not site.published:
                    site.published = True
                    if site.published_at is None:
                        site.published_at = datetime.utcnow()
                    changed["auto_published"] = True
            if redirect_url is not None:
                changed["redirect_url"] = redirect_url.strip()
                site.redirect_url = redirect_url.strip() or None
            # Switching INTO redirect mode without ever supplying a URL would
            # leave a dead subdomain; catch it here rather than at render.
            if (site.mode == "redirect") and not (site.redirect_url or "").strip():
                abort_problem(
                    422, "Invalid destination",
                    "A redirect site needs a destination URL.",
                )
            if roster_public is not None:
                from db.models import GroupConfiguration

                row = (
                    s.query(GroupConfiguration)
                    .filter(
                        GroupConfiguration.group_id == group_id,
                        GroupConfiguration.config_key == ROSTER_CONFIG_KEY,
                    )
                    .first()
                )
                value = "1" if roster_public else "0"
                if row is None:
                    s.add(
                        GroupConfiguration(
                            group_id=group_id,
                            config_key=ROSTER_CONFIG_KEY,
                            config_value=value,
                            updated_at=datetime.utcnow(),
                        )
                    )
                else:
                    row.config_value = value
                changed["roster_public"] = roster_public
            if changed:
                _audit(s, user_id, group_id, "site.update",
                       f"group_sites.{site.subdomain}", after=changed)
            s.commit()
            return _site_admin_view(site, s)

    saved = await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True, "site": saved}))


@sites_bp.post("/groups/<int:group_id>/site/pages")
async def create_page(group_id: int):
    user_id = current_user_id()
    body = await json_body()
    slug = str(body.get("slug") or "").strip().lower()
    title = str(body.get("title") or "").strip()
    err = page_slug_error(slug)
    if err:
        abort_problem(422, "Invalid page slug", err)
    if not title or len(title) > 80:
        abort_problem(422, "Invalid title", "Pages need a title of at most 80 characters.")

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_entitlement(
                s, user_id, group_id, ENTITLEMENT,
                manage_guild_ids=manageable_guild_ids(user_id), user=user,
            )
            site = _load_site(s, group_id)
            if site is None:
                abort_problem(404, "No site", "Claim a subdomain first.")
            if len(site.pages) >= MAX_PAGES_PER_SITE:
                abort_problem(422, "Page limit", f"At most {MAX_PAGES_PER_SITE} pages per site.")
            if any(p.slug == slug for p in site.pages):
                abort_problem(409, "Slug in use", f"A page '{slug}' already exists.")
            page = GroupSitePage(
                site_id=site.site_id,
                slug=slug,
                title=title,
                position=max((p.position for p in site.pages), default=0) + 1,
                draft_blocks=json.dumps(
                    [{"id": "md-1", "type": "markdown", "body": f"# {title}\n"}]
                ),
                schema_version=SCHEMA_VERSION,
            )
            s.add(page)
            _audit(s, user_id, group_id, "site.page_create", f"site_pages.{slug}")
            s.commit()
            return _page_summary(page)

    saved = await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True, "page": saved}))


def _load_page(s, site: GroupSite, page_id: int) -> GroupSitePage:
    page = next((p for p in site.pages if p.page_id == page_id), None)
    if page is None:
        abort_problem(404, "No such page", "That page does not exist on this site.")
    return page


@sites_bp.get("/groups/<int:group_id>/site/pages/<int:page_id>")
async def get_page(group_id: int, page_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            site = _load_site(s, group_id)
            if site is None:
                abort_problem(404, "No site", "Claim a subdomain first.")
            page = _load_page(s, site, page_id)
            out = _page_summary(page)
            out["draft_blocks"] = json.loads(page.draft_blocks or "[]")
            return out

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify({"page": payload}))


@sites_bp.put("/groups/<int:group_id>/site/pages/<int:page_id>")
async def update_page(group_id: int, page_id: int):
    user_id = current_user_id()
    body = await json_body()
    if _rate_limited("save", group_id, _SAVES_PER_HOUR, 3600):
        abort_problem(429, "Slow down", "Too many saves this hour; try again shortly.")

    blocks = _validate_blocks(body.get("blocks")) if body.get("blocks") is not None else None
    title = body.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 80):
        abort_problem(422, "Invalid title", "Pages need a title of at most 80 characters.")
    position = body.get("position")
    if position is not None and (not isinstance(position, int) or not 0 <= position <= 100):
        abort_problem(422, "Invalid position", "'position' must be an integer 0-100.")

    # Page-scoped stylesheet — same validate-and-reject contract as the
    # site-level sheet, so an author sees why a save failed rather than
    # silently losing rules.
    page_css_source = body.get("custom_css_source")
    page_css_out = None
    if page_css_source is not None:
        if not isinstance(page_css_source, str):
            abort_problem(422, "Invalid CSS", "'custom_css_source' must be a string.")
        if len(page_css_source.encode("utf-8", "replace")) > MAX_CUSTOM_CSS_BYTES:
            abort_problem(
                422, "Invalid CSS",
                f"Page CSS is limited to {MAX_CUSTOM_CSS_BYTES // 1024} KB.",
            )
        if page_css_source.strip():
            try:
                page_css_out = sanitize_css(page_css_source)
            except CssValidationError as e:
                abort_problem(
                    422, "CSS rejected", " | ".join(e.problems[:20]),
                    extra={"problems": e.problems[:50]},
                )
        else:
            page_css_out = ""

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_entitlement(
                s, user_id, group_id, ENTITLEMENT,
                manage_guild_ids=manageable_guild_ids(user_id), user=user,
            )
            site = _load_site(s, group_id)
            if site is None:
                abort_problem(404, "No site", "Claim a subdomain first.")
            page = _load_page(s, site, page_id)

            if title is not None:
                page.title = title.strip()
            if position is not None:
                page.position = position
            if page_css_source is not None:
                page.custom_css_source = page_css_source
                page.custom_css = page_css_out
            if blocks is not None:
                page.draft_blocks = json.dumps(blocks)
                page.schema_version = SCHEMA_VERSION
                _audit(
                    s, user_id, group_id, "site.page_save", f"site_pages.{page.slug}",
                    after={"blocks_sha": _sha(page.draft_blocks), "count": len(blocks)},
                )
            s.commit()
            out = _page_summary(page)
            out["draft_blocks"] = json.loads(page.draft_blocks or "[]")
            return out

    saved = await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True, "page": saved}))


@sites_bp.post("/groups/<int:group_id>/site/pages/<int:page_id>/publish")
async def publish_page(group_id: int, page_id: int):
    return await _set_page_published(group_id, page_id, True)


@sites_bp.post("/groups/<int:group_id>/site/pages/<int:page_id>/unpublish")
async def unpublish_page(group_id: int, page_id: int):
    return await _set_page_published(group_id, page_id, False)


async def _set_page_published(group_id: int, page_id: int, publish: bool):
    user_id = current_user_id()
    if publish and _rate_limited("publish", group_id, _PUBLISH_PER_HOUR, 3600):
        abort_problem(429, "Slow down", "Too many publishes this hour; try again shortly.")

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_entitlement(
                s, user_id, group_id, ENTITLEMENT,
                manage_guild_ids=manageable_guild_ids(user_id), user=user,
            )
            site = _load_site(s, group_id)
            if site is None:
                abort_problem(404, "No site", "Claim a subdomain first.")
            page = _load_page(s, site, page_id)
            if publish:
                page.published_blocks = page.draft_blocks
                page.published = True
                page.published_at = datetime.utcnow()
                # First publish of raw HTML/CSS => review queue (noindex until
                # a superadmin clears it). Publishing stays instant.
                if not site.reviewed_at and not site.needs_review and (
                    _page_has_custom_html(page) or (site.custom_css or "").strip()
                ):
                    site.needs_review = True
            else:
                page.published = False
            _audit(
                s, user_id, group_id,
                "site.page_publish" if publish else "site.page_unpublish",
                f"site_pages.{page.slug}",
                after={"blocks_sha": _sha(page.published_blocks or "")} if publish else None,
            )
            s.commit()
            return _page_summary(page)

    saved = await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True, "page": saved}))


@sites_bp.post("/groups/<int:group_id>/site/publish")
async def publish_site(group_id: int):
    return await _set_site_published(group_id, True)


@sites_bp.post("/groups/<int:group_id>/site/unpublish")
async def unpublish_site(group_id: int):
    return await _set_site_published(group_id, False)


async def _set_site_published(group_id: int, publish: bool):
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            site = _load_site(s, group_id)
            if site is None:
                abort_problem(404, "No site", "Claim a subdomain first.")
            # Publishing pages is the gated act; taking a redirect address
            # on or offline is not.
            if (site.mode or "builder") == "builder":
                _assert_builder_access(s, user_id, group_id, user)
            site.published = publish
            if publish and site.published_at is None:
                site.published_at = datetime.utcnow()
            _audit(s, user_id, group_id,
                   "site.publish" if publish else "site.unpublish",
                   f"group_sites.{site.subdomain}")
            s.commit()
            return _site_admin_view(site, s)

    saved = await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True, "site": saved}))


@sites_bp.delete("/groups/<int:group_id>/site/pages/<int:page_id>")
async def delete_page(group_id: int, page_id: int):
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            # Admin only, NO entitlement — downgraded groups can clean up.
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            site = _load_site(s, group_id)
            if site is None:
                abort_problem(404, "No site", "This group has no site.")
            page = _load_page(s, site, page_id)
            if page.slug == HOME_SLUG:
                abort_problem(422, "Cannot delete home", "The landing page cannot be deleted.")
            _audit(s, user_id, group_id, "site.page_delete", f"site_pages.{page.slug}")
            s.delete(page)
            s.commit()
            return True

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))


@sites_bp.delete("/groups/<int:group_id>/site")
async def delete_site(group_id: int):
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            # Admin only, NO entitlement — releasing the subdomain must always work.
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            site = _load_site(s, group_id)
            if site is None:
                abort_problem(404, "No site", "This group has no site.")
            _audit(s, user_id, group_id, "site.delete", f"group_sites.{site.subdomain}",
                   before={"subdomain": site.subdomain, "pages": len(site.pages)})
            s.delete(site)
            s.commit()
            return True

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))


@sites_bp.post("/groups/<int:group_id>/site/preview-token")
async def preview_token(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_entitlement(
                s, user_id, group_id, ENTITLEMENT,
                manage_guild_ids=manageable_guild_ids(user_id), user=user,
            )
            site = _load_site(s, group_id)
            if site is None:
                abort_problem(404, "No site", "Claim a subdomain first.")
            return {"token": make_preview_token(site.site_id), "site_url": _site_url(site.subdomain)}

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


# --- public member roster -----------------------------------------------------

ROSTER_CONFIG_KEY = "public_members_list"
_ROSTER_MAX = 100


def _roster_enabled(s, group_id: int) -> bool:
    from db.models import GroupConfiguration

    row = (
        s.query(GroupConfiguration)
        .filter(
            GroupConfiguration.group_id == group_id,
            GroupConfiguration.config_key == ROSTER_CONFIG_KEY,
        )
        .first()
    )
    return bool(row and str(row.config_value).strip().lower() in ("1", "true", "yes", "on"))


@sites_bp.get("/groups/<int:group_id>/site-roster")
async def site_roster(group_id: int):
    """Public member roster for the member_roster site block.

    Deliberately opt-in: gated on the ``public_members_list`` group config
    (default OFF; the points_leaderboard_public pattern — an unregistered key
    written only by the site settings PUT, so generic config PATCH can't flip
    it). Honors both privacy layers: the global hidden_player_ids() union and
    the group's own IgnoredPlayer rows.
    """
    try:
        limit = min(max(int(request.args.get("limit", 25)), 1), _ROSTER_MAX)
    except ValueError:
        limit = 25
    sort = (request.args.get("sort") or "monthly").strip().lower()
    if sort not in ("monthly", "all_time", "name"):
        sort = "monthly"

    def _load():
        from db.models import IgnoredPlayer, Player
        from web_api.common import hidden_player_ids, money, player_month_totals
        from utils.partitions import resolve_period

        with db_session() as s:
            if not _roster_enabled(s, group_id):
                abort_problem(
                    404, "Roster not public", "This group does not share its member list."
                )
            ignored = {
                pid
                for (pid,) in s.query(IgnoredPlayer.player_id)
                .filter(IgnoredPlayer.group_id == group_id)
                .all()
            }
            hidden = hidden_player_ids()
            rows = (
                s.query(Player.player_id, Player.player_name)
                .join(Player.groups)
                .filter(Group.group_id == group_id)
                .all()
            )
            visible = [
                (pid, name)
                for pid, name in rows
                if pid not in ignored and pid not in hidden and name
            ]
            ids = [pid for pid, _ in visible]
            # Two batched Redis passes (each is pipelined internally), not 2·N.
            monthly = player_month_totals(ids, resolve_period("month"))
            all_time = player_month_totals(ids, resolve_period("all"))

            members = [
                {
                    "id": pid,
                    "name": name,
                    "monthly_loot": money(int(monthly.get(pid, 0) or 0)),
                    "all_time_loot": money(int(all_time.get(pid, 0) or 0)),
                }
                for pid, name in visible
            ]
            # Rank is always by monthly GP and is assigned BEFORE the display
            # sort, so "#3" means third in the clan this month no matter how
            # the visitor chooses to order the list.
            members.sort(key=lambda m: m["monthly_loot"]["value"], reverse=True)
            for i, m in enumerate(members):
                m["rank"] = i + 1

            if sort == "name":
                members.sort(key=lambda m: m["name"].lower())
            elif sort == "all_time":
                members.sort(key=lambda m: m["all_time_loot"]["value"], reverse=True)

            return {"members": members[:limit], "total": len(members), "sort": sort}

    payload = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(payload), max_age=300)


# --- Wise Old Man group achievements -----------------------------------------

_WOM_ACH_CACHE_TTL = 1800  # 30 min — WOM API budget is shared with the sync jobs
_WOM_ACH_NEG_TTL = 300  # remember failures briefly so a WOM outage can't hammer it

# --- achievement icons -------------------------------------------------------
# WOM reports a `metric` per achievement ("araxxor", "overall",
# "tombs_of_amascut_expert"). Three sources, in order, all first-party assets
# under /img so the tenant CSP needs no new host:
#
#   1. static/assets/img/metrics/{metric}.png — the skill + boss + activity art
#      already used by the event task tiles; keyed by the WOM metric namespace.
#   2. the same file under a singular alias: WOM says `clue_scrolls_elite`
#      while the asset is `clue_scroll_elite.png`.
#   3. npcdb/{npc_id}.png for bosses newer than the metrics art (Yama, Doom of
#      Mokhaiotl, Maggot King …), resolved through npc_match_key so spelling and
#      alias differences fold the same way they do everywhere else.
#
# Anything still unresolved renders without an icon rather than a broken image.
_ASSET_ROOT = "/store/droptracker/disc/static/assets/img"
_METRIC_ITEM_ICONS = {
    # Activities with no metric art but an obvious in-game item.
    "collections_logged": 22711,  # Collection log
    "soul_wars_zeal": 25344,  # Soul cape
}
_icon_cache: dict[str, Any] = {"metrics": None, "npc": None, "npc_files": None, "at": 0.0}
_ICON_CACHE_TTL = 3600


def _icon_tables() -> tuple[set, dict, set]:
    """(metric asset names, npc match key -> npc_id, npc icon ids) — cached."""
    now = time.time()
    if _icon_cache["metrics"] is not None and now - _icon_cache["at"] < _ICON_CACHE_TTL:
        return _icon_cache["metrics"], _icon_cache["npc"], _icon_cache["npc_files"]

    try:
        metrics = {
            f[:-4] for f in os.listdir(f"{_ASSET_ROOT}/metrics") if f.endswith(".png")
        }
    except OSError:
        metrics = set()
    try:
        npc_files = {
            f[:-4] for f in os.listdir(f"{_ASSET_ROOT}/npcdb") if f.endswith(".png")
        }
    except OSError:
        npc_files = set()

    npc_by_key: dict[str, int] = {}
    try:
        from utils.npc_names import npc_match_key

        from db.models import NpcList

        with db_session() as s:
            for npc_id, name in s.query(NpcList.npc_id, NpcList.npc_name).all():
                key = npc_match_key(name or "")
                if key and key not in npc_by_key:
                    npc_by_key[key] = npc_id
    except Exception:
        npc_by_key = {}

    _icon_cache.update(
        {"metrics": metrics, "npc": npc_by_key, "npc_files": npc_files, "at": now}
    )
    return metrics, npc_by_key, npc_files


def achievement_icon_url(metric: str) -> str | None:
    """First-party icon for a WOM achievement metric, or None."""
    metric = (metric or "").strip().lower()
    if not metric:
        return None
    metrics, npc_by_key, npc_files = _icon_tables()

    if metric in metrics:
        return f"{IMG_BASE}/metrics/{metric}.png"

    # WOM pluralises the clue-scroll metrics; the asset names are singular.
    alias = metric.replace("clue_scrolls_", "clue_scroll_")
    if alias in metrics:
        return f"{IMG_BASE}/metrics/{alias}.png"

    item_id = _METRIC_ITEM_ICONS.get(metric)
    if item_id:
        return f"{IMG_BASE}/itemdb/{item_id}.png"

    try:
        from utils.npc_names import npc_match_key

        npc_id = npc_by_key.get(npc_match_key(metric.replace("_", " ")))
    except Exception:
        npc_id = None
    if npc_id and str(npc_id) in npc_files:
        return f"{IMG_BASE}/npcdb/{npc_id}.png"
    return None


@sites_bp.get("/groups/<int:group_id>/wom-achievements")
async def wom_group_achievements(group_id: int):
    """Recent WOM achievements for a group (public; the wom_achievements site
    block). One upstream call per group per 30 minutes regardless of traffic;
    fails soft to an empty list so a WOM outage never breaks a page render."""
    try:
        limit = min(max(int(request.args.get("limit", 10)), 1), 25)
    except ValueError:
        limit = 10

    def _load():
        conn = _rc()
        cache_key = f"wom:group_achievements:{group_id}"
        if conn is not None:
            try:
                cached = conn.get(cache_key)
                if cached:
                    return json.loads(cached)
            except Exception:
                pass

        with db_session() as s:
            group = s.query(Group).filter(Group.group_id == group_id).first()
            wom_id = getattr(group, "wom_id", None) if group else None
        items: list[dict] = []
        if wom_id:
            try:
                import requests as rq

                headers = {"User-Agent": "DropTracker.io sites-v1"}
                api_key = (os.getenv("WOM_API_KEY") or "").strip()
                if api_key:
                    headers["x-api-key"] = api_key
                resp = rq.get(
                    f"https://api.wiseoldman.net/v2/groups/{int(wom_id)}/achievements",
                    params={"limit": 25},
                    headers=headers,
                    timeout=15,
                )
                resp.raise_for_status()
                for a in resp.json():
                    player = a.get("player") or {}
                    metric = a.get("metric") or ""
                    items.append(
                        {
                            "player_name": player.get("displayName") or "",
                            "name": a.get("name") or "",
                            "metric": metric,
                            # Resolved once per cache fill, not per request.
                            "icon_url": achievement_icon_url(metric),
                            "created_at": a.get("createdAt") or "",
                        }
                    )
            except Exception:
                items = []
        if conn is not None:
            try:
                ttl = _WOM_ACH_CACHE_TTL if items else _WOM_ACH_NEG_TTL
                conn.setex(cache_key, ttl, json.dumps(items))
            except Exception:
                pass
        return items

    items = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify({"items": items[:limit]}), max_age=300)


# --- abuse reports -----------------------------------------------------------


@sites_bp.post("/sites/report")
async def report_site():
    """Anonymous abuse report for a tenant site (the footer link's target).

    Stored as an AuditLog row (actor NULL) so it lands in the existing admin
    audit browser; per-IP rate limited. Deliberately returns ok even for
    unknown sites so the endpoint can't be used to enumerate subdomains.
    """
    body = await json_body()
    sub = str(body.get("site") or "").strip().lower()
    reason = str(body.get("reason") or "").strip()
    if not reason or len(reason) > 2000:
        abort_problem(422, "Invalid report", "A reason of at most 2000 characters is required.")

    ip = (request.headers.get("X-Real-IP") or request.remote_addr or "unknown").strip()
    ip_key = int(hashlib.sha256(ip.encode()).hexdigest()[:8], 16)
    if _rate_limited("report", ip_key, 5, 3600):
        abort_problem(429, "Slow down", "Too many reports from this address; try again later.")

    def _apply():
        with db_session() as s:
            site = s.query(GroupSite).filter(GroupSite.subdomain == sub).first()
            _audit(
                s, None, site.group_id if site else 0, "site.report_received",
                f"group_sites.{sub or 'unknown'}",
                after={"reason": reason[:2000], "known_site": site is not None},
            )
            s.commit()

    await asyncio.to_thread(_apply)
    return jsonify({"ok": True})


# --- public render projections ----------------------------------------------


def _render_gate(site: GroupSite) -> str:
    """'ok' | 'suspended' | 'unavailable'. Fail-closed: entitlement resolver
    errors read as no entitlement (db.entitlements caches 60s)."""
    if site.suspended_at is not None:
        return "suspended"
    if not site.published:
        return "unavailable"
    # Redirect addresses keep working for every group; only rendered pages
    # need the subscription. A lapsed builder site therefore goes dark, but
    # the group can switch it to a redirect and keep the address alive.
    if (site.mode or "builder") == "builder" and not group_has_entitlement(
        site.group_id, ENTITLEMENT
    ):
        return "unavailable"
    return "ok"


@sites_bp.get("/sites/resolve")
async def resolve_site():
    sub = (request.args.get("host") or "").strip().lower()
    if not sub or subdomain_error(sub):
        abort_problem(404, "No such site", "Unknown site.")

    def _load():
        with db_session() as s:
            site = s.query(GroupSite).filter(GroupSite.subdomain == sub).first()
            if site is None:
                abort_problem(404, "No such site", "Unknown site.")
            gate = _render_gate(site)
            if gate != "ok":
                return {"status": gate, "subdomain": site.subdomain}, 30
            group = s.query(Group).filter(Group.group_id == site.group_id).first()
            return {
                "status": "ok",
                "subdomain": site.subdomain,
                # Redirect modes short-circuit rendering in the tenant layout.
                "mode": site.mode or "builder",
                "redirect_target": _resolved_redirect_target(site),
                "group_id": site.group_id,
                "group_name": (group.group_name if group else "") or "",
                "icon_url": getattr(group, "icon_url", None),
                "theme_key": site.theme_key,
                "palette": json.loads(site.palette) if site.palette else {},
                "nav": json.loads(site.nav) if site.nav else [],
                "custom_css": site.custom_css or "",
                "needs_review": bool(site.needs_review),
                "pages": [
                    {"slug": p.slug, "title": p.title, "position": p.position}
                    for p in sorted(site.pages, key=lambda x: x.position)
                    if p.published
                ],
            }, 60

    payload, max_age = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(payload), max_age=max_age)


@sites_bp.get("/sites/<sub>/pages/<slug>")
async def site_page(sub: str, slug: str):
    sub = sub.strip().lower()
    slug = slug.strip().lower()
    token = request.args.get("preview")

    def _load():
        with db_session() as s:
            site = s.query(GroupSite).filter(GroupSite.subdomain == sub).first()
            if site is None:
                abort_problem(404, "No such site", "Unknown site.")
            preview = bool(token) and preview_token_valid(site.site_id, token)
            if token and not preview:
                abort_problem(403, "Bad preview token", "The preview link has expired.")
            gate = _render_gate(site)
            if gate != "ok" and not preview:
                abort_problem(404, "Unavailable", "This site is not available.")
            page = next((p for p in site.pages if p.slug == slug), None)
            if page is None or (not preview and not page.published):
                abort_problem(404, "No such page", "Unknown page.")
            raw = page.draft_blocks if preview else (page.published_blocks or "[]")
            return {
                "slug": page.slug,
                "title": page.title,
                "group_id": site.group_id,
                "blocks": json.loads(raw or "[]"),
                "custom_css": page.custom_css or "",
                "schema_version": page.schema_version,
                "preview": preview,
                "published_at": page.published_at.isoformat() if page.published_at else None,
            }, (0 if preview else 60)

    payload, max_age = await asyncio.to_thread(_load)
    resp = jsonify(payload)
    if max_age == 0:
        return private_no_store(resp)
    return with_cache_headers(resp, max_age=max_age)
