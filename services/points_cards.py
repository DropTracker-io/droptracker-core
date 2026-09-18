"""Pure half of the Discord points commands (``commands/points.py``).

Everything here is text and plain dicts: what a board page says, what a player
card says, and the ids the paging buttons carry. No ``interactions`` import and
no DB access, the same split as ``activity_launch_core`` / ``channel_name_render``
-- so the wording, the paging arithmetic and the custom-id round-trip are
unit-tested directly, and the command module only turns these specs into
embeds and buttons.

**Buttons carry their whole subject.** ``gpts:{dir}:{group}:{page}:{period}``
names the board and the page it leads to, so a press needs no memory of the
message it came from and still works after a bot restart. The period travels
as the *resolved* token (``202609``, not ``month``): a board opened at 23:59 on
the 30th must keep paging September after midnight, not silently become
October's.

Standings come from ``db/point_standings.py`` -- who stands where (current
members only, RSNs optionally combined per Discord user) is decided there, and
nothing in this module re-derives it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from utils.site_urls import WEBSITE_URL, player_url

#: Rows per board page. Ten keeps a page inside one screen on a phone.
PAGE_SIZE = 10

NAV_PREFIX = "gpts:"
ME_PREFIX = "gpts_me:"
PERIOD_SELECT_PREFIX = "gpts_period:"

#: Slash-option choices and the first four rows of the period picker.
PRESETS = (
    ("all", "All-time"),
    ("month", "This month"),
    ("week", "This week"),
    ("day", "Today"),
)

#: A Discord select holds 25 options; the presets take four.
MAX_SEASON_OPTIONS = 21

_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)


# --------------------------------------------------------------------------- #
# custom_id codec
# --------------------------------------------------------------------------- #
def nav_custom_id(direction: str, group_id: int, page: int, period: str) -> str:
    """``direction`` ("prev"/"next") is part of the id only to keep the two
    buttons distinct when they name the same page (a one-page board, both
    disabled) -- Discord rejects a message with duplicate custom ids."""
    return f"{NAV_PREFIX}{direction}:{int(group_id)}:{int(page)}:{period}"


def parse_nav_custom_id(custom_id: str) -> Optional[tuple]:
    """-> ``(group_id, page, period)`` or None. The period comes last and is
    split off whole because season tokens contain a colon (``season:12``)."""
    if not (custom_id or "").startswith(NAV_PREFIX):
        return None
    parts = custom_id[len(NAV_PREFIX):].split(":", 3)
    if len(parts) != 4:
        return None
    try:
        return int(parts[1]), max(1, int(parts[2])), parts[3]
    except ValueError:
        return None


def me_custom_id(group_id: int, period: str) -> str:
    return f"{ME_PREFIX}{int(group_id)}:{period}"


def parse_me_custom_id(custom_id: str) -> Optional[tuple]:
    if not (custom_id or "").startswith(ME_PREFIX):
        return None
    parts = custom_id[len(ME_PREFIX):].split(":", 1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), parts[1]
    except ValueError:
        return None


def period_select_id(group_id: int) -> str:
    return f"{PERIOD_SELECT_PREFIX}{int(group_id)}"


def parse_period_select_id(custom_id: str) -> Optional[int]:
    if not (custom_id or "").startswith(PERIOD_SELECT_PREFIX):
        return None
    try:
        return int(custom_id[len(PERIOD_SELECT_PREFIX):])
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Periods
# --------------------------------------------------------------------------- #
def season_id_of(period: str) -> Optional[int]:
    if not (period or "").startswith("season:"):
        return None
    try:
        return int(period.split(":", 1)[1])
    except ValueError:
        return None


def period_label(token: str, seasons: Sequence[dict] = ()) -> str:
    """A resolved period token, the way a person would say it."""
    token = (token or "").strip()
    sid = season_id_of(token)
    if sid is not None:
        for season in seasons:
            if int(season.get("id", -1)) == sid:
                return str(season.get("name") or f"Season {sid}")
        return f"Season {sid}"
    if token.lower() == "all":
        return "All-time"
    if len(token) == 6 and token.isdigit():
        month = int(token[4:])
        if 1 <= month <= 12:
            return f"{_MONTHS[month - 1]} {token[:4]}"
    if len(token) == 8 and token.isdigit():
        month = int(token[4:6])
        if 1 <= month <= 12:
            return f"{int(token[6:])} {_MONTHS[month - 1]} {token[:4]}"
    if len(token) == 7 and token[4] in "Ww":
        return f"Week {int(token[5:])}, {token[:4]}"
    return token or "All-time"


def period_options(seasons: Sequence[dict], current: str, *,
                   now: Optional[datetime] = None) -> list:
    """Rows for the period picker: the four presets, then the clan's seasons
    (newest first, as loaded). ``current`` is the resolved token being shown;
    a preset is marked selected when it resolves to that same token today."""
    from db.point_standings import resolve_period

    options = []
    for value, label in PRESETS:
        try:
            resolved = resolve_period(value, now)[0]
        except ValueError:
            resolved = value
        options.append({"label": label, "value": value, "default": resolved == current})
    for season in list(seasons)[:MAX_SEASON_OPTIONS]:
        value = f"season:{int(season['id'])}"
        options.append({
            "label": str(season.get("name") or value)[:100],
            "value": value,
            "default": value == current,
        })
    return options


def leaderboard_url(group_id: int, period: str) -> str:
    return f"{WEBSITE_URL}/groups/{int(group_id)}/points/leaderboard?period={period}"


# --------------------------------------------------------------------------- #
# Board
# --------------------------------------------------------------------------- #
def _names(standing, limit: int = 3) -> str:
    """The row's account names: `Main`, or `Alt` + `Main` on a combined row.
    Hidden accounts are never named (they still count towards the total)."""
    visible = list(standing.visible_accounts)
    shown = " + ".join(f"`{a.name}`" for a in visible[:limit])
    extra = len(visible) - limit
    return f"{shown} +{extra} more" if extra > 0 else shown


def standing_line(standing, *, is_viewer: bool = False) -> str:
    line = f"**{standing.rank}.** {_names(standing)} — **{standing.points:,}**"
    return f"{line} ◀ you" if is_viewer else line


def board_view(
    *,
    group_id: int,
    group_name: str,
    standings: Sequence,
    period: str,
    seasons: Sequence[dict] = (),
    page: int = 1,
    combined: bool = False,
    viewer_player_ids: Sequence[int] = (),
    page_size: int = PAGE_SIZE,
) -> dict:
    """One page of a clan's board as a render spec.

    ``standings`` is the FULL ranked board. Hidden rows are left out of the
    pages but keep their rank, so the viewer is looked up on the full list --
    somebody who hid themselves can still see where they stand, in a reply
    only they can read.
    """
    from db.point_standings import find_standing, page_of, paginate, visible_standings

    shown = visible_standings(standings)
    # On a per-RSN board a viewer with several accounts holds several places:
    # point at the best one.
    ranked = [
        s for s in (find_standing(standings, player_id=pid) for pid in viewer_player_ids)
        if s is not None
    ]
    viewer = min(ranked, key=lambda s: s.rank) if ranked else None

    rows, page, pages = paginate(shown, page, page_size)
    label = period_label(period, seasons)
    total_points = sum(s.points for s in standings)

    header = f"**{label}** · {len(standings):,} ranked · {total_points:,} points"
    if combined:
        header += "\n-# RSNs claimed by the same Discord user are counted together."
    if rows:
        own_ranks = {s.rank for s in ranked}
        body = "\n".join(standing_line(s, is_viewer=s.rank in own_ranks) for s in rows)
    else:
        body = "No points have been earned in this period yet."

    if viewer is not None:
        you = f"You: #{viewer.rank} · {viewer.points:,} pts"
    elif viewer_player_ids:
        you = "You: not ranked in this period"
    else:
        you = "Claim your account with /claim-rsn to see your rank"

    return {
        "title": f"{group_name} — Points",
        "description": f"{header}\n\n{body}",
        "footer": f"Page {page} of {pages} • {you}",
        "page": page,
        "pages": pages,
        "prev_id": nav_custom_id("prev", group_id, max(1, page - 1), period),
        "next_id": nav_custom_id("next", group_id, min(pages, page + 1), period),
        "can_prev": page > 1,
        "can_next": page < pages,
        "me_id": me_custom_id(group_id, period),
        # Only worth pressing when it would move: ranked, visible, elsewhere.
        "can_me": (
            viewer is not None and not viewer.hidden
            and page_of(shown, viewer, page_size) != page
        ),
        "viewer_page": page_of(shown, viewer, page_size) if viewer is not None else None,
        "url": leaderboard_url(group_id, period),
        "period_select_id": period_select_id(group_id),
        "period_options": period_options(seasons, period),
    }


# --------------------------------------------------------------------------- #
# Player card
# --------------------------------------------------------------------------- #
def _short_gp(value: int) -> str:
    value = int(value or 0)
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(value) >= size:
            return f"{value / size:.1f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def _unix(when) -> Optional[int]:
    """Naive server-local ``date_added`` -> unix seconds for a ``<t:…:R>``."""
    if isinstance(when, datetime):
        try:
            return int(when.timestamp())
        except Exception:
            return None
    return None


def _award_line(award: dict, names: dict, *, show_account: bool) -> str:
    amount = int(award.get("amount") or 0)
    reason = " ".join(str(award.get("reason") or "").split()) or "award"
    if len(reason) > 60:
        reason = reason[:57].rstrip() + "…"
    line = f"**{amount:+,}** · {reason}"
    if show_account:
        name = names.get(int(award.get("player_id") or 0))
        if name:
            line += f" · `{name}`"
    stamp = _unix(award.get("date_added"))
    return f"{line} · <t:{stamp}:R>" if stamp else line


def _standing_sentence(label: str, standing, board_size: int) -> str:
    if standing is None:
        return f"{label}: not ranked"
    return f"{label}: **#{standing.rank}** of {board_size:,} — **{standing.points:,}** pts"


def player_card(
    *,
    accounts: Sequence[dict],
    is_self: bool,
    group_id: Optional[int] = None,
    group_name: Optional[str] = None,
    card: Optional[dict] = None,
    other_groups: Sequence[tuple] = (),
    points_active: bool = True,
) -> dict:
    """The /my-points and /lookup card as a render spec.

    ``accounts`` are the RSNs being described -- dicts with ``player_id``,
    ``name`` and optionally ``total_level``, ``log_slots``, ``month_loot`` --
    already filtered to what this viewer may see. ``card`` is
    ``db.point_standings.load_points_card`` for the server's clan, or None
    outside one (a DM, an unlinked server). ``other_groups`` is
    ``[(group_name, points)]`` and is only ever passed for a self view.
    """
    from db.point_standings import find_standing

    names = {int(a["player_id"]): a["name"] for a in accounts}
    ordered = sorted(accounts, key=lambda a: str(a["name"]).lower())
    who = "Your" if is_self else (f"{ordered[0]['name']}'s" if len(ordered) == 1 else "Their")
    lines: list = []
    fields: list = []

    if card is not None and group_name:
        members = set(card.get("member_ids") or ())
        all_time, month = card.get("all_time") or [], card.get("month") or []
        in_clan = [a for a in ordered if int(a["player_id"]) in members]
        outside = [a for a in ordered if int(a["player_id"]) not in members]

        if not points_active and not all_time:
            lines.append(f"**{group_name}** doesn't use DropTracker clan points.")
        elif not in_clan:
            lines.append(
                f"None of these accounts are currently in **{group_name}**, so they "
                "hold no place on its points board."
            )
        elif card.get("combined"):
            # Every in-clan account shares one standing on a combined board.
            standing = next(
                (s for s in (find_standing(all_time, player_id=a["player_id"]) for a in in_clan) if s),
                None,
            )
            month_standing = next(
                (s for s in (find_standing(month, player_id=a["player_id"]) for a in in_clan) if s),
                None,
            )
            lines.append(f"**{group_name}**")
            lines.append(_standing_sentence("All-time", standing, len(all_time)))
            lines.append(_standing_sentence("This month", month_standing, len(month)))
            if len(in_clan) > 1:
                lines.append(f"-# Combined across {len(in_clan)} accounts on the same Discord user.")
            if standing is not None and len(in_clan) > 1:
                by_id = {acc.player_id: acc.points for acc in standing.accounts}
                fields.append((
                    "Accounts",
                    "\n".join(
                        f"`{a['name']}` — {by_id.get(int(a['player_id']), 0):,} pts"
                        for a in sorted(in_clan, key=lambda a: -by_id.get(int(a["player_id"]), 0))
                    ),
                    False,
                ))
        else:
            lines.append(f"**{group_name}**")
            for account in in_clan:
                pid = int(account["player_id"])
                prefix = f"`{account['name']}` — " if len(in_clan) > 1 else ""
                lines.append(prefix + _standing_sentence(
                    "All-time", find_standing(all_time, player_id=pid), len(all_time)))
                lines.append(prefix + _standing_sentence(
                    "This month", find_standing(month, player_id=pid), len(month)))

        if outside and in_clan:
            fields.append((
                "Not in this clan",
                ", ".join(f"`{a['name']}`" for a in outside)
                + "\n-# Points only count while an account is a member.",
                False,
            ))

        recent = card.get("recent") or []
        if recent:
            fields.append((
                "Recent awards",
                "\n".join(_award_line(r, names, show_account=len(in_clan) > 1) for r in recent),
                False,
            ))
    elif group_id is None:
        lines.append("-# Use this inside your clan's Discord server to see clan standings.")

    if other_groups:
        fields.append((
            "Your other clans" if is_self else "Other clans",
            "\n".join(f"`{name}` — **{int(points):,}** pts" for name, points in list(other_groups)[:8]),
            False,
        ))

    about = []
    for account in ordered:
        bits = []
        if account.get("month_loot"):
            bits.append(f"{_short_gp(account['month_loot'])} loot this month")
        if account.get("total_level"):
            bits.append(f"total level {int(account['total_level']):,}")
        if account.get("log_slots"):
            bits.append(f"{int(account['log_slots']):,} log slots")
        link = f"[{account['name']}]({player_url(account['player_id'])})"
        about.append(f"{link} — {' · '.join(bits)}" if bits else link)
    if about:
        fields.append(("Accounts on DropTracker" if len(about) > 1 else "On DropTracker",
                       "\n".join(about[:10]), False))

    return {
        "title": f"{who} points",
        "description": "\n".join(lines),
        "fields": fields,
        "url": player_url(ordered[0]["player_id"]) if len(ordered) == 1 else None,
        "footer": "Only current clan members' points count • droptracker.io",
    }
