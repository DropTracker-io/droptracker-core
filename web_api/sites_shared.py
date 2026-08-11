"""Validation constants and helpers for group mini-sites (sites-v1).

Single authority for subdomain/page-slug rules, reserved words, and size
limits. The web repo mirrors the client-visible parts in
``packages/api-types/src/sites.ts`` — keep the two in sync (the meta endpoint
serves these values, so the editor never hardcodes them).
"""
from __future__ import annotations

import re

# --- subdomain (DNS label) --------------------------------------------------
# 3-30 chars, lowercase a-z0-9 + interior hyphens. Rejecting `xn--` keeps
# punycode/homoglyph labels out entirely.
SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{1,28})[a-z0-9]$")

# Never claimable. Three families: infrastructure labels that must keep
# working (or might someday), credential/payment-shaped labels that read as
# official pages on a hosted domain, and brand/authority words.
RESERVED_SUBDOMAINS: frozenset[str] = frozenset(
    {
        # infrastructure
        "www", "mail", "smtp", "imap", "pop", "webmail", "mx", "ns1", "ns2",
        "ftp", "api", "cdn", "static", "assets", "img", "images", "files",
        "media", "admin", "dev", "staging", "test", "demo", "status", "docs",
        "blog", "help", "app", "activity", "xf", "autodiscover", "_dmarc",
        "site", "sites", "preview", "beta",
        # credential-shaped
        "login", "logon", "signin", "sign-in", "signup", "sign-up", "auth",
        "account", "accounts", "secure", "security", "verify", "verification",
        "confirm", "password", "reset", "session", "token", "oauth", "sso",
        "2fa", "mfa",
        # payment-shaped
        "billing", "payment", "payments", "pay", "checkout", "wallet", "bank",
        "paypal", "stripe", "refund", "prize", "prizes", "giveaway", "free",
        "rewards", "claim",
        # brand / authority
        "official", "staff", "mod", "mods", "moderator", "support", "team",
        "droptracker", "jagex", "runescape", "oldschool", "osrs", "jmod",
        "discord", "discordapp", "nitro", "steam", "google", "microsoft",
        "apple",
    }
)

# Substring denial on top of the exact list — catches "jagex-support",
# "secure-login-gp" and friends anywhere in the label.
RESERVED_SUBSTRINGS: tuple[str, ...] = ("jagex", "runescape", "login", "verify")


def subdomain_error(sub: str) -> str | None:
    """Return a human-readable rejection reason, or None if claimable."""
    if not isinstance(sub, str):
        return "Subdomain must be a string."
    sub = sub.strip().lower()
    if not SUBDOMAIN_RE.match(sub):
        return (
            "Subdomains are 3-30 characters of lowercase letters, digits and "
            "hyphens, and cannot start or end with a hyphen."
        )
    if sub.startswith("xn--"):
        return "Punycode labels are not allowed."
    if sub in RESERVED_SUBDOMAINS:
        return f"'{sub}' is reserved."
    for frag in RESERVED_SUBSTRINGS:
        if frag in sub:
            return f"Subdomains containing '{frag}' are reserved."
    return None


# --- page slugs -------------------------------------------------------------
# Charset (no dots/underscores) inherently excludes robots.txt, favicon.ico,
# _next and every other system path; these are blocked defensively anyway.
PAGE_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")
RESERVED_PAGE_SLUGS: frozenset[str] = frozenset({"api", "sites", "img", "preview"})
HOME_SLUG = "home"


def page_slug_error(slug: str) -> str | None:
    if not isinstance(slug, str):
        return "Page slug must be a string."
    slug = slug.strip().lower()
    if not PAGE_SLUG_RE.match(slug):
        return (
            "Page slugs are 1-40 characters of lowercase letters, digits and "
            "hyphens, and cannot start or end with a hyphen."
        )
    if slug in RESERVED_PAGE_SLUGS:
        return f"'{slug}' is a reserved page slug."
    return None


# --- limits (served by the meta endpoint; the editor never hardcodes them) --
MAX_PAGES_PER_SITE = 8
MAX_BLOCKS_PER_PAGE = 30
# Raw HTML is the highest-risk block type, but the controls that actually
# contain it are the save-time sanitizer, the tenant CSP, and the byte caps
# below — not a low count. The original 3 was arbitrary and made ordinary
# hand-built pages (a staff roster of sections, say) impossible to finish.
MAX_CUSTOM_HTML_BLOCKS_PER_PAGE = 12
MAX_CUSTOM_HTML_BYTES = 32 * 1024  # per block, source size — reject, never truncate
MAX_CUSTOM_CSS_BYTES = 64 * 1024  # site-wide
# Serialized draft_blocks. Custom HTML stores BOTH source and sanitized output,
# so each such block costs roughly twice its source — at 128 KB a page ran out
# of room after two full-size HTML blocks, well before the count cap, which
# surfaced as a second confusing rejection right after raising the first.
MAX_PAGE_JSON_BYTES = 512 * 1024
MAX_NAV_ITEMS = 12
MAX_PALETTE_KEYS = 24

SCHEMA_VERSION = 1

# Block types the v1 renderer knows. Unknown types are skipped by the
# renderer (forward compat), but the API refuses to *store* them.
BLOCK_TYPES: tuple[str, ...] = (
    "hero",
    "markdown",
    "stats_row",
    "top_players",
    "records",
    "boss_activity",
    "recent_drops",
    "lootboard",
    "pb_board",
    "leaderboard",
    "recap",
    "announcements",
    "live_ticker",
    "image",
    "buttons",
    "divider",
    "custom_html",
    "wom_achievements",
    "member_roster",
    "event_standings",
    "npc_board",
)

# Palette keys the theme wrapper will forward as CSS custom properties.
# Anything else in the stored palette JSON is dropped at save.
ALLOWED_PALETTE_KEYS: frozenset[str] = frozenset(
    {
        "--dt-text", "--dt-text-muted", "--dt-brown", "--dt-brown-dark",
        "--dt-bronze", "--dt-gold", "--dt-gold-bright", "--dt-red",
        "--dt-green", "--dt-stone", "--dt-surface-0", "--dt-surface-1",
        "--dt-surface-2", "--dt-surface-3", "--dt-ember", "--dt-glow",
        "--dt-shadow-card", "--dt-shadow-pop",
    }
)

_PALETTE_VALUE_RE = re.compile(r"^[#a-zA-Z0-9(),.%\s/-]{1,64}$")


def palette_value_ok(value: str) -> bool:
    """Colors/shadows only — no url(), quotes, braces, or semicolons, so a
    palette value can never escape its CSS custom-property declaration."""
    return bool(
        isinstance(value, str)
        and _PALETTE_VALUE_RE.match(value)
        and "url" not in value.lower()
    )
