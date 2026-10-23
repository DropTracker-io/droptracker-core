"""Rules for the /tools directory listings (web146a): validation and who may edit what.

Pure functions with no database or Quart imports, so tests can pin them
directly. The routes in web_api/routes/community_listings.py call these and
turn ListingError into a 422 (or 403 for a field the editor may not touch).

Who may edit:
  * superadmins: everything, including which section a listing sits in, its
    order, whether it is shown, and the "how it fits" note (our voice).
  * maintainers of a listing: its content (name, summary, details, tags,
    links, logo, accent). Nothing that changes where or whether it appears.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable
from urllib.parse import urlsplit

# Goal-based sections, in page order. Keep in step with the web's
# lib/tools-directory.ts TOOL_SECTIONS.
SECTIONS = ("events", "progress", "clans", "foundations")

MAINTAINER_FIELDS = frozenset(
    {"name", "summary", "details", "tags", "links", "logo_url", "accent"}
)
ADMIN_ONLY_FIELDS = frozenset(
    {"slug", "section", "sort_order", "visible", "fit_label", "fit_note"}
)
ALL_FIELDS = MAINTAINER_FIELDS | ADMIN_ONLY_FIELDS

MAX_TAGS = 8
MAX_LINKS = 6

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class ListingError(ValueError):
    """A value staff or a maintainer sent that we will not store."""

    def __init__(self, title: str, detail: str, *, status: int = 422):
        super().__init__(detail)
        self.title = title
        self.detail = detail
        self.status = status


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:64].strip("-")


def _text(body: dict, key: str, label: str, max_len: int, *, required: bool) -> str | None:
    raw = body.get(key)
    if raw is None:
        if required:
            raise ListingError(f"Missing {label.lower()}", f"{label} is required.")
        return None
    if not isinstance(raw, str):
        raise ListingError(f"Invalid {label.lower()}", f"{label} must be text.")
    value = raw.strip()
    if not value:
        if required:
            raise ListingError(f"Missing {label.lower()}", f"{label} is required.")
        return None
    if len(value) > max_len:
        raise ListingError(
            f"{label} too long", f"{label} must be {max_len} characters or fewer."
        )
    return value


def _url(raw: Any, label: str, *, https_only: bool = False) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ListingError(f"Invalid {label.lower()}", f"{label} needs a web address.")
    value = raw.strip()
    if len(value) > 512:
        raise ListingError(f"Invalid {label.lower()}", f"{label} is too long.")
    parts = urlsplit(value)
    allowed = ("https",) if https_only else ("http", "https")
    if parts.scheme.lower() not in allowed or not parts.netloc or any(c.isspace() for c in value):
        scheme = "https://" if https_only else "http:// or https://"
        raise ListingError(
            f"Invalid {label.lower()}", f"{label} must be a full address starting with {scheme}."
        )
    return value


def normalize_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ListingError("Invalid tags", "Tags must be a list.")
    out: list[str] = []
    seen: set[str] = set()
    for tag in raw:
        if not isinstance(tag, str):
            raise ListingError("Invalid tags", "Each tag must be text.")
        value = tag.strip()
        if not value:
            continue
        if len(value) > 32:
            raise ListingError("Invalid tags", "Each tag must be 32 characters or fewer.")
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    if len(out) > MAX_TAGS:
        raise ListingError("Too many tags", f"A listing can have up to {MAX_TAGS} tags.")
    return out


def normalize_links(raw: Any) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise ListingError("Missing link", "A listing needs at least one link.")
    if len(raw) > MAX_LINKS:
        raise ListingError("Too many links", f"A listing can have up to {MAX_LINKS} links.")
    out: list[dict] = []
    for link in raw:
        if not isinstance(link, dict):
            raise ListingError("Invalid link", "Each link needs a label and an address.")
        label = link.get("label")
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > 40:
            raise ListingError("Invalid link", "Each link label must be 1 to 40 characters.")
        out.append({"label": label.strip(), "href": _url(link.get("href"), "Link")})
    return out


def validate_listing(body: dict, *, partial: bool) -> dict:
    """Return the column values to write for a create (partial=False) or a PATCH.

    Only keys present in ``body`` are returned for a PATCH. Raises ListingError.
    """
    if not isinstance(body, dict):
        raise ListingError("Invalid body", "Send the listing as a JSON object.")
    unknown = set(body) - ALL_FIELDS
    if unknown:
        raise ListingError("Unknown fields", "Unknown fields: " + ", ".join(sorted(unknown)))

    def wants(key: str) -> bool:
        return not partial or key in body

    fields: dict = {}
    if wants("name"):
        fields["name"] = _text(body, "name", "Name", 80, required=True)
    if wants("summary"):
        fields["summary"] = _text(body, "summary", "Summary", 300, required=True)
    if wants("details"):
        fields["details"] = _text(body, "details", "Details", 2000, required=False)
    if wants("fit_label"):
        fields["fit_label"] = _text(body, "fit_label", "Fit badge", 40, required=False)
    if wants("fit_note"):
        fields["fit_note"] = _text(body, "fit_note", "Fit note", 400, required=False)

    if wants("section"):
        section = body.get("section")
        if section not in SECTIONS:
            raise ListingError(
                "Invalid section", "Section must be one of: " + ", ".join(SECTIONS) + "."
            )
        fields["section"] = section

    if not partial and not body.get("slug"):
        # New listing with no slug: derive it from the name.
        derived = slugify(fields["name"])
        if not derived:
            raise ListingError("Invalid slug", "Add a slug: the name has no letters or digits.")
        fields["slug"] = derived
    elif "slug" in body:
        slug = body.get("slug")
        if not isinstance(slug, str) or not _SLUG_RE.match(slug) or len(slug) > 64:
            raise ListingError(
                "Invalid slug", "Slug must be lowercase letters, digits and single dashes."
            )
        fields["slug"] = slug

    if wants("tags"):
        fields["tags_json"] = json.dumps(normalize_tags(body.get("tags")))
    if wants("links"):
        fields["links_json"] = json.dumps(normalize_links(body.get("links")))

    if wants("logo_url"):
        raw = body.get("logo_url")
        fields["logo_url"] = (
            None if raw is None or (isinstance(raw, str) and not raw.strip())
            else _url(raw, "Logo", https_only=True)
        )
    if wants("accent"):
        raw = body.get("accent")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            fields["accent"] = None
        elif isinstance(raw, str) and _ACCENT_RE.match(raw.strip()):
            fields["accent"] = raw.strip().lower()
        else:
            raise ListingError("Invalid colour", "Colour must be a hex value like #3b82f6.")

    if wants("sort_order"):
        raw = body.get("sort_order", 100)
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 9999:
            raise ListingError("Invalid order", "Order must be a whole number from 0 to 9999.")
        fields["sort_order"] = raw
    if wants("visible"):
        raw = body.get("visible", True)
        if not isinstance(raw, bool):
            raise ListingError("Invalid visibility", "Visible must be true or false.")
        fields["visible"] = raw

    if partial and not fields:
        raise ListingError("Nothing to change", "Send at least one field to update.")
    return fields


def check_edit_allowed(body_keys: Iterable[str], *, is_admin: bool, is_maintainer: bool) -> None:
    """Raise a 403 ListingError unless this editor may change every key sent."""
    if is_admin:
        return
    if not is_maintainer:
        raise ListingError(
            "Forbidden", "Only staff or this listing's maintainers can edit it.", status=403
        )
    blocked = sorted(set(body_keys) & ADMIN_ONLY_FIELDS)
    if blocked:
        raise ListingError(
            "Forbidden",
            "Only staff can change: " + ", ".join(blocked) + ".",
            status=403,
        )


def can_view_directory(*, is_admin: bool, maintains_any: bool) -> bool:
    """While /tools is a staff preview, staff and maintainers can open it."""
    return is_admin or maintains_any


def visible_to(listing_visible: bool, *, is_admin: bool, is_maintainer: bool) -> bool:
    """Hidden listings show only to staff and that listing's own maintainers."""
    return listing_visible or is_admin or is_maintainer


def parse_json_list(raw: str | None) -> list:
    """Read a stored JSON list column; anything unreadable is an empty list."""
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []
