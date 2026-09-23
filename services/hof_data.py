"""The numbers behind a Hall of Fame boss message.

Builds the ``HofEntry`` that ``services/hof_layout.py`` renders: personal bests
by team size, highest kill counts, most loot this month and of all time, and
the boss's name, link, picture and icon. Shared by the Hall of Fame extension
(``services/hall_of_fame.py``, which posts the messages) and the website's
layout preview (``web_api/routes/hof_layouts.py``), so the preview shows a
group's real standings exactly as the bot would draw them.

Sources:

* Personal bests — ``personal_best_entries`` for the group's members, bucketed
  by team size (the five smallest sizes, fastest first).
* Kill count — ``player_npc_kc``, the per-(player, NPC) watermark the plugin
  reports on every drop and timed kill (``data/submissions/kc_milestones.py``).
  It fills in as members play; a member who has not killed a boss since it
  started recording is not on that boss's board yet.
* Loot — the per-NPC Redis boards ``services/redis_updates.py`` maintains
  (``leaderboard:group:{gid}:npc:{npc}[:{partition}]``; the global group reads
  the site-wide ``leaderboard:npc:...`` boards).

Nothing here talks to Discord. It only reads.
"""
from __future__ import annotations

import datetime
import logging
import os
import re
import uuid
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func

from db.models import NpcList, Player, PersonalBestEntry, PlayerNpcKc, get_current_partition
from services.hof_layout import HofEntry, Row, Scope
from utils.format import NPC_IMG_DIR, convert_from_ms, format_number
from utils.hof import RAID_GROUPS, SEPULCHRE_CANONICAL, canonical_display_name, npc_name_candidates
from utils.site_urls import WEBSITE_URL, npc_url

log = logging.getLogger(__name__)

#: The global/template group ranks everyone, so it reads the site-wide boards
#: and skips the member filter (it has tens of thousands of members).
GLOBAL_GROUP_ID = 2

_MAX_PB_BRACKETS = 5

_MODE_ORDER = {
    "Entry": 0,
    "Normal": 1,
    "Hard Mode": 2,
    "Challenge Mode": 2,
    "Expert": 2,
    "Nightmare": 0,
    "Phosani's Nightmare": 1,
    "Crystalline": 0,
    "Corrupted": 1,
}


# ── Naming helpers (moved from services/hall_of_fame.py) ────────────────────

def team_size_sort_key(team_size) -> Tuple[int, str]:
    value = str(team_size).strip()
    if value.casefold() in ("solo", "1"):
        return (1, "")
    if value.casefold() == "duo":
        return (2, "")
    if value.casefold() == "trio":
        return (3, "")
    # A bracket sorts by its lowest member, so Chambers' "11-15" sits
    # between "10" and "16-23" rather than after every exact size.
    head = value.split("-", 1)[0].rstrip("+").strip()
    try:
        return (int(head), "")
    except ValueError:
        return (99, value.casefold())


def team_size_label(team_size) -> str:
    match team_size:
        case 1 | "1" | "Solo":
            return "Solo"
        case 2 | "2" | "Duo":
            return "Duo"
        case 3 | "3" | "Trio":
            return "Trio"
        case _:
            return f"{team_size} players"


def variant_mode_name(canonical_name: str, npc_name: str) -> str:
    if canonical_name == "Chambers of Xeric":
        return "Challenge Mode" if ("Challenge" in npc_name or "CM" in npc_name) else "Normal"
    if canonical_name == "Theatre of Blood":
        if "Entry Mode" in npc_name:
            return "Entry"
        return "Hard Mode" if "Hard Mode" in npc_name else "Normal"
    if canonical_name == "Tombs of Amascut":
        if "Entry Mode" in npc_name:
            return "Entry"
        return "Expert" if "Expert" in npc_name else "Normal"
    if canonical_name == "Nightmare of Ashihama":
        return "Phosani's Nightmare" if "Phosani" in npc_name else "Nightmare"
    if canonical_name == "The Gauntlet":
        return "Corrupted" if "Corrupted" in npc_name else "Crystalline"
    if canonical_name == SEPULCHRE_CANONICAL:
        floor_match = re.search(r"Floor\s+(\d+)", npc_name)
        if floor_match:
            return f"Floor {floor_match.group(1)}"
    return npc_name


def short_name(npc_name: str) -> str:
    """The abbreviated label raid modes have always had on their boss link."""
    if "Theatre" in npc_name:
        if "Hard Mode" in npc_name:
            return "HM ToB"
        if "Entry Mode" in npc_name:
            return "EM ToB"
        return "ToB"
    if "Chambers" in npc_name:
        return "CM CoX" if ("Challenge" in npc_name or "CM" in npc_name) else "CoX"
    if "Tombs" in npc_name:
        if "Expert" in npc_name:
            return "Expert ToA"
        if "Entry Mode" in npc_name:
            return "Entry ToA"
        return "ToA"
    if "Nightmare" in npc_name:
        return "Phosani's" if "Phosani" in npc_name else "NM"
    return npc_name


def group_thumbnail_npc(canonical_name: str, npcs: List[NpcList]) -> NpcList:
    """Prefer the base/normal mode's artwork for a raid group's thumbnail."""
    for variant_name in RAID_GROUPS.get(canonical_name, []):
        for npc in npcs:
            if npc.npc_name == variant_name:
                return npc
    return npcs[0]


def _image_exists(npc_id) -> bool:
    return os.path.exists(f"{NPC_IMG_DIR}/{npc_id}.png")


def npc_image_url(session, npc: NpcList) -> str:
    """The boss's picture, falling back to another mode of the same raid when
    this mode's art is missing, so a thumbnail is never a broken image."""
    if _image_exists(npc.npc_id):
        return f"https://www.droptracker.io/img/npcdb/{npc.npc_id}.png"
    canonical = canonical_display_name(npc.npc_name)
    for variant_name in RAID_GROUPS.get(canonical) or []:
        for candidate in npc_name_candidates(variant_name):
            other = session.query(NpcList).filter(NpcList.npc_name == candidate).first()
            if other and other.npc_id != npc.npc_id and _image_exists(other.npc_id):
                return f"https://www.droptracker.io/img/npcdb/{other.npc_id}.png"
    return f"https://www.droptracker.io/img/npcdb/{npc.npc_id}.png"


def mode_npcs(canonical_name: str, npcs: List[NpcList]) -> List[Tuple[str, NpcList]]:
    """A grouped entry's modes, in play order, one NPC per mode."""
    pairs = [(variant_mode_name(canonical_name, npc.npc_name), npc) for npc in npcs]
    # Alias NPC rows ("Nightmare" and "The Nightmare") map to the same mode —
    # keep only the first so a mode never renders twice.
    seen: set = set()
    pairs = [(mode, npc) for mode, npc in pairs if not (mode in seen or seen.add(mode))]
    pairs.sort(key=lambda item: (_MODE_ORDER.get(item[0], 50), item[0].casefold()))
    return pairs


# ── Emoji ────────────────────────────────────────────────────────────────────

def _npc_emoji(names: Iterable[str], npc_ids: Iterable[int]) -> str:
    try:
        from utils.game_emojis import emoji_for_npc, emoji_for_npc_id

        for name in names:
            glyph = emoji_for_npc(name)
            if glyph:
                return glyph
        for npc_id in npc_ids:
            glyph = emoji_for_npc_id(npc_id)
            if glyph:
                return glyph
    except Exception:
        pass
    return ""


def coins_emoji() -> str:
    try:
        from utils.game_emojis import emoji_for_item

        return emoji_for_item("Coins") or ""
    except Exception:
        return ""


def resolve_emoji_refs(keys: Sequence[str]) -> Dict[str, str]:
    """``{emoji:key}`` glyphs for the running application ("" when unowned)."""
    out: Dict[str, str] = {}
    try:
        from utils.game_emojis import emoji_by_name

        for key in keys:
            out[key] = emoji_by_name(key) or ""
    except Exception:
        pass
    return out


def common_tokens(directory_url: Optional[str]) -> Dict[str, str]:
    """Tokens that are the same for every boss in a pass."""
    return {
        "{site_url}": WEBSITE_URL,
        "{pbs_url}": f"{WEBSITE_URL}/personal-bests",
        "{directory_url}": directory_url or "",
        "{coins_emoji}": coins_emoji(),
        "{month_name}": datetime.datetime.now().strftime("%B"),
    }


# ── Collection ───────────────────────────────────────────────────────────────

class HofDataCollector:
    """Reads a group's standings for one boss message at a time.

    ``player_ids`` are the group's members (the PB filter); ``needs`` is
    ``hof_layout.needed_boards`` — only the rankings a layout shows are read.
    """

    def __init__(self, session, group_id: int, player_ids: List[int],
                 display_name: Callable[[Optional[Player]], str],
                 redis=None):
        self._db = session
        self.group_id = group_id
        self.player_ids = player_ids
        self._display = display_name
        self._redis = redis
        self._npc_ids_by_name: Dict[str, List[int]] = {}
        self._players: Dict[int, Optional[Player]] = {}

    # -- plumbing ---------------------------------------------------------

    def _redis_client(self):
        if self._redis is None:
            from utils.redis import redis_client

            self._redis = redis_client
        return self._redis

    def _ids_for(self, npc_name: str) -> List[int]:
        """Every npc_list id carrying this name (the game reuses names)."""
        if npc_name not in self._npc_ids_by_name:
            self._npc_ids_by_name[npc_name] = [
                row[0] for row in self._db.query(NpcList.npc_id).filter(NpcList.npc_name == npc_name).all()
            ]
        return self._npc_ids_by_name[npc_name]

    def _player(self, player_id: int) -> Optional[Player]:
        if player_id not in self._players:
            self._players[player_id] = self._db.query(Player).filter(Player.player_id == player_id).first()
        return self._players[player_id]

    def _row(self, player: Optional[Player], value: str) -> Row:
        plain = (player.player_name if player is not None else None) or "Unknown"
        return Row(player=self._display(player), player_plain=plain, value=value)

    # -- personal bests ---------------------------------------------------

    def pb_buckets(self, npc_name: str) -> Dict[object, List[PersonalBestEntry]]:
        """Members' PBs at one NPC name, by team size, fastest first."""
        npc_ids = self._ids_for(npc_name)
        if not npc_ids or not self.player_ids:
            return {}
        pbs = self._db.query(PersonalBestEntry).filter(
            PersonalBestEntry.player_id.in_(self.player_ids),
            PersonalBestEntry.npc_id.in_(npc_ids),
        ).all()
        buckets: Dict[object, List[PersonalBestEntry]] = {}
        for pb in pbs:
            buckets.setdefault(pb.team_size, []).append(pb)
        # Cap at the smallest team sizes so one boss can't flood the message.
        if len(buckets) > _MAX_PB_BRACKETS:
            keep = sorted(buckets.keys(), key=team_size_sort_key)[:_MAX_PB_BRACKETS]
            buckets = {k: buckets[k] for k in keep}
        for entries in buckets.values():
            entries.sort(key=lambda pb: pb.personal_best)
        return buckets

    def _pb_scope_parts(self, npc_name: str, rows: int):
        buckets = self.pb_buckets(npc_name)
        brackets: List[Tuple[str, List[Row]]] = []
        fastest = None
        fastest_size = None
        total = 0
        for team_size in sorted(buckets.keys(), key=team_size_sort_key):
            entries = buckets[team_size]
            total += len(entries)
            for pb in entries[:1]:
                if fastest is None or pb.personal_best < fastest.personal_best:
                    fastest, fastest_size = pb, team_size
            brackets.append((
                team_size_label(team_size),
                [self._row(getattr(pb, "player", None), convert_from_ms(pb.personal_best))
                 for pb in entries[:rows]],
            ))
        tokens = {
            "{total_pbs}": str(total) if total else "",
            "{fastest_time}": convert_from_ms(fastest.personal_best) if fastest else "",
            "{fastest_team_size}": team_size_label(fastest_size) if fastest else "",
            "{fastest_player}": self._display(getattr(fastest, "player", None)) if fastest else "",
        }
        return brackets, tokens, total

    # -- kill count -------------------------------------------------------

    def kc_rows(self, npc_ids: List[int], rows: int) -> List[Row]:
        if not npc_ids or rows <= 0:
            return []
        query = self._db.query(
            PlayerNpcKc.player_id, func.sum(PlayerNpcKc.kill_count).label("kc"),
        ).filter(PlayerNpcKc.npc_id.in_(npc_ids))
        if self.group_id != GLOBAL_GROUP_ID:
            if not self.player_ids:
                return []
            query = query.filter(PlayerNpcKc.player_id.in_(self.player_ids))
        top = (
            query.group_by(PlayerNpcKc.player_id)
            .order_by(func.sum(PlayerNpcKc.kill_count).desc())
            .limit(rows)
            .all()
        )
        return [self._row(self._player(int(pid)), f"{int(kc):,}") for pid, kc in top if kc]

    # -- loot -------------------------------------------------------------

    def _loot_key(self, npc_id: int, partition: Optional[int]) -> str:
        suffix = f":{partition}" if partition is not None else ""
        if self.group_id == GLOBAL_GROUP_ID:
            return f"leaderboard:npc:{npc_id}{suffix}"
        return f"leaderboard:group:{self.group_id}:npc:{npc_id}{suffix}"

    def loot_rows(self, npc_ids: List[int], rows: int, month: bool) -> List[Row]:
        if not npc_ids or rows <= 0:
            return []
        partition = get_current_partition() if month else None
        keys = [self._loot_key(npc_id, partition) for npc_id in npc_ids]
        client = self._redis_client().client
        try:
            if len(keys) == 1:
                top = client.zrevrange(keys[0], 0, rows - 1, withscores=True)
            else:
                # Several NPC rows (raid modes, reused names): sum per player
                # server-side rather than guessing from each board's top few.
                tmp = f"hof:tmp:{uuid.uuid4().hex}"
                pipe = client.pipeline(transaction=False)
                pipe.zunionstore(tmp, keys)
                pipe.expire(tmp, 30)
                pipe.zrevrange(tmp, 0, rows - 1, withscores=True)
                pipe.delete(tmp)
                top = pipe.execute()[2]
        except Exception as e:
            log.warning("HOF: loot lookup failed for group %d npcs %s: %s", self.group_id, npc_ids, e)
            return []
        out: List[Row] = []
        for player_id, score in top:
            if not score:
                continue
            try:
                pid = int(player_id)
            except (TypeError, ValueError):
                continue
            out.append(self._row(self._player(pid), format_number(score)))
        return out

    def total_loot(self, npc_ids: List[int]) -> str:
        total = 0
        client = self._redis_client()
        for npc_id in npc_ids:
            try:
                total += int(client.zsum(self._loot_key(npc_id, None)) or 0)
            except Exception:
                continue
        return format_number(total) if total else ""

    # -- scopes -----------------------------------------------------------

    def _rankings(self, scope: Scope, npc_ids: List[int], needs: Dict[str, int]) -> None:
        if "kc" in needs:
            scope.boards["kc"] = self.kc_rows(npc_ids, needs["kc"])
        if "loot_month" in needs:
            scope.boards["loot_month"] = self.loot_rows(npc_ids, needs["loot_month"], month=True)
        if "loot_all" in needs:
            scope.boards["loot_all"] = self.loot_rows(npc_ids, needs["loot_all"], month=False)
        for board, (player_token, value_token) in {
            "kc": ("{top_kc_player}", "{top_kc}"),
            "loot_month": ("{top_looter_month}", "{top_loot_month}"),
            "loot_all": ("{top_looter_all}", "{top_loot_all}"),
        }.items():
            top = (scope.boards.get(board) or [None])[0]
            scope.tokens[player_token] = top.player if top else ""
            scope.tokens[value_token] = top.value if top else ""
        scope.tokens["{total_loot}"] = self.total_loot(npc_ids)

    def _npc_scope(self, npc: NpcList, needs: Dict[str, int], mode_name: str,
                   fallback_emoji: str = "") -> Scope:
        pb_rows = needs.get("pb", 5)
        brackets, pb_tokens, _ = self._pb_scope_parts(npc.npc_name, pb_rows)
        url = npc_url(npc.npc_id)
        scope = Scope(
            tokens={
                "{boss_name}": npc.npc_name,
                "{boss_link}": f"[{short_name(npc.npc_name)}]({url})",
                "{boss_url}": url,
                "{boss_image_url}": npc_image_url(self._db, npc),
                "{boss_emoji}": _npc_emoji([npc.npc_name], [npc.npc_id]) or fallback_emoji,
                "{mode_name}": mode_name,
                **pb_tokens,
            },
            pb_brackets=brackets,
        )
        self._rankings(scope, self._ids_for(npc.npc_name), needs)
        return scope

    def build_entry(self, display_name: str, grouped: bool, npcs: List[NpcList],
                    needs: Dict[str, int]) -> HofEntry:
        """Everything one boss message can show."""
        if not grouped:
            scope = self._npc_scope(npcs[0], needs, mode_name="")
            return HofEntry(scope=scope, modes=[scope])

        thumb = group_thumbnail_npc(display_name, npcs)
        entry_emoji = _npc_emoji([display_name, thumb.npc_name], [thumb.npc_id])
        modes = [
            self._npc_scope(npc, needs, mode_name=mode, fallback_emoji=entry_emoji)
            for mode, npc in mode_npcs(display_name, npcs)
        ]
        url = npc_url(thumb.npc_id)
        combined = Scope(
            tokens={
                "{boss_name}": display_name,
                "{boss_link}": f"[{display_name}]({url})",
                "{boss_url}": url,
                "{boss_image_url}": npc_image_url(self._db, thumb),
                "{boss_emoji}": entry_emoji,
                "{mode_name}": "",
                # Times from different modes are not comparable, so the boss as
                # a whole has no single fastest kill; the per-mode blocks do.
                "{fastest_time}": "",
                "{fastest_team_size}": "",
                "{fastest_player}": "",
            },
            # A pb leaderboard not set to repeat per mode still shows every
            # mode's lists, each labelled with its mode.
            pb_brackets=[
                (f"{mode.tokens['{mode_name}']} · {label}", rows)
                for mode in modes for label, rows in mode.pb_brackets
            ],
        )
        total_pbs = sum(int(m.tokens.get("{total_pbs}") or 0) for m in modes)
        combined.tokens["{total_pbs}"] = str(total_pbs) if total_pbs else ""
        all_ids: List[int] = []
        for npc in npcs:
            for npc_id in self._ids_for(npc.npc_name):
                if npc_id not in all_ids:
                    all_ids.append(npc_id)
        self._rankings(combined, all_ids, needs)
        return HofEntry(scope=combined, modes=modes)
