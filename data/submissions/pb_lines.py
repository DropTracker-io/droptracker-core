"""Kill times from the plugin's raw chat lines.

The RuneLite plugin used to read personal-best and kill-time messages itself
(PbHandler) and submit the result as ``boss_name``/``kill_time``/... fields.
That parser was ~3k of the plugin's 200k-token plugin-hub budget, so newer
plugins forward the candidate chat lines of a kill instead, and this module
does what PbHandler did: same patterns, same line selection, same merging of
a kill's lines across game ticks.

The wire format, on an ordinary ``npc_kill`` embed:

* ``chat_line_1`` .. ``chat_line_N``: one JSON object per game message, in
  arrival order: ``m`` the sanitized message, ``t`` the game tick it arrived
  on, ``p`` the raid party sizes the client could see at that moment, as
  ``[tob, toa, tfa, royal_titans]`` (roster or orb count, whichever is larger;
  Royal Titans is players in view).
* ``precise_timing``, ``equipment``, ``inventory``, ``model_fingerprint``: as
  before, captured when the kill completed.

:func:`expand` turns that into the field dicts PbHandler used to send (zero,
one, or rarely several kills), each then processed as an ordinary kill time.
Pure and stdlib-only (Redis is reached lazily for the cross-batch duplicate
guard) so the unit tests can load it directly.
"""
from __future__ import annotations

import json
import re

_I = re.IGNORECASE | re.ASCII

# The key admits parentheses for the awakened DT2 bosses, whose count line
# reads "Your Vardorvis (awakened) kill count is: 3." (suggestion #199).
BOSS_COUNT_PATTERN = re.compile(
    r"Your (?P<key>[\w\s:'()-]+) (?P<type>kill|chest|completion|success) count is:? (?P<value>[\d,]+)", _I)
AWAKENED_SUFFIX = re.compile(r"\s*\(awakened\)$", _I)
SECONDARY_BOSS_PATTERN = re.compile(
    r"Your (?P<type>completed|subdued) (?P<key>[\w\s:]+) count is:?[ \t]*(?P<value>[\d,]+)", _I)
TIME_WITH_PB_PATTERN = re.compile(
    r"(?P<prefix>.*?)(?P<duration>\d*:?\d+:\d+(?:\.\d+)?)\.?\s*"
    r"(?:Personal best: (?P<pbtime>\d*:?\d+:\d+(?:\.\d+)?)\.?\s*)?(?P<pbIndicator>\(new personal best\))?", _I)
# "16-23 players" and "24+ players" are whole bracket labels (suggestion #153).
TEAM_SIZE_PATTERN = re.compile(
    r"Team size:\s*(?P<size>\d+\s*-\s*\d+|\d+\s*\+|\d+|Solo)\s*(?:players?)?", _I)
# The wall-clock total is not the tracked metric, except at the Fractured
# Archive, whose "Total completion time" IS the raid time.
TOTAL_COMPLETION_PATTERN = re.compile(r"(?<!Fractured Archive: )total completion time", _I)
COMPLETION_TIME_PATTERN = re.compile(r"(?<!total )completion time:", _I)
DELVE_LEVEL_PATTERN = re.compile(r"Delve level:\s*(?P<level>\d+)(?P<deep>\+)?(?:\s*\((?P<actual>\d+)\))?", re.ASCII)
DELVE_TOTAL_PATTERN = re.compile(r"Delve level\s+\d+\s*-\s*\d+\s+duration", re.ASCII)

DOOM = "Doom of Mokhaiotl"
TFA = "Fractured Archive"
GAUNTLET_NAME, GAUNTLET_BOSS = "Gauntlet", "Crystalline Hunllef"
CG_NAME, CG_BOSS = "Corrupted Gauntlet", "Corrupted Hunllef"

TICK_MS = 600
PARTIAL_EXPIRY_MS = 10_000
DUPLICATE_THRESHOLD_MS = 5_000
MAX_LINES = 25


class Kill:
    __slots__ = ("boss", "count", "time", "best", "pb", "team", "message", "delve")

    def __init__(self, boss=None, count=None, time=0, best=0, pb=False, team=None, message=None, delve=None):
        self.boss, self.count, self.time, self.best = boss, count, time, best
        self.pb, self.team, self.message, self.delve = pb, team, message, delve

    def valid(self) -> bool:
        if self.boss is not None and "Doom" in self.boss:
            return bool(self.time)
        return self.boss is not None and self.count is not None and bool(self.time)


# --------------------------------------------------------------------------- #
# Parsing (PbHandler.parseMessage and friends)
# --------------------------------------------------------------------------- #
def parse_time(text) -> int:
    """PbHandler.parseTime: "1:23.4" / "1:02:03" → milliseconds; 0 if unreadable."""
    if text is None or not text.strip():
        return 0
    try:
        part = text[: text.index(".")] if "." in text else text
        parts = part.split(":")
        hours = minutes = seconds = millis = 0
        if len(parts) == 3:
            hours, minutes, seconds = (int(p) for p in parts)
        elif len(parts) == 2:
            minutes, seconds = (int(p) for p in parts)
        if "." in text:
            millis = int(text[text.index(".") + 1:].ljust(3, "0"))
        return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis
    except ValueError:
        return 0


def format_time(ms, precise: bool) -> str:
    """PbHandler.formatTime: "[HH:]MM:SS[.cc]" on a 24h clock."""
    ms = int(ms or 0) % 86_400_000
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    out = f"{hours:02d}:" if hours > 0 else ""
    out += f"{minutes:02d}:{seconds:02d}"
    return out + f".{millis // 10:02d}" if precise else out


def _primary_boss(boss, kind):
    kind = kind.lower()
    if kind == "chest":
        if boss.lower() == "barrows":
            return boss
        return f"{boss} {kind}" if boss == "Lunar" else None
    if kind == "completion":
        if boss.lower() == GAUNTLET_NAME.lower():
            return GAUNTLET_BOSS
        if boss.lower() == CG_NAME.lower():
            return CG_BOSS
        return None
    if kind not in ("kill", "success"):
        return None
    # The game prints "(awakened)"; npc_list spells it "(Awakened)".
    return AWAKENED_SUFFIX.sub(" (Awakened)", boss)


def _secondary_boss(boss):
    if boss is None or boss.lower() == "wintertodt":
        return boss
    sep = boss.rfind(":")
    raid = (boss[:sep] if sep > 0 else boss).lower()
    if raid in ("theatre of blood", "tombs of amascut", "chambers of xeric",
                "chambers of xeric challenge mode", TFA.lower()):
        return boss
    return None


def _boss_count(boss, match):
    if boss is None:
        return None
    try:
        return boss, int(match.group("value").replace(",", ""))
    except ValueError:
        return None


def _parse_boss_count(message):
    m = BOSS_COUNT_PATTERN.search(message)
    if m:
        found = _boss_count(_primary_boss(m.group("key"), m.group("type")), m)
        if found:
            return found
    m = SECONDARY_BOSS_PATTERN.search(message)
    if m:
        return _boss_count(_secondary_boss(m.group("key")), m)
    return None


def _select_time_line(message, completion_only):
    first = None
    for line in message.split("\n"):
        if TOTAL_COMPLETION_PATTERN.search(line):
            continue
        if completion_only and not COMPLETION_TIME_PATTERN.search(line):
            continue
        m = TIME_WITH_PB_PATTERN.search(line)
        if not m:
            continue
        if m.group("pbIndicator") is not None or m.group("pbtime") is not None:
            return line
        if first is None:
            first = line
    return first


def select_time_line(message):
    """Raid completion lines first; else a line with PB info; else the first
    timed line (see PbHandler.selectTimeLine for why)."""
    if message is None:
        return None
    line = _select_time_line(message, True)
    return line if line is not None else _select_time_line(message, False)


def _boss_from_context(message):
    if "Tombs of Amascut" in message:
        return "Tombs of Amascut: Expert Mode" if "Expert Mode" in message else "Tombs of Amascut"
    if "Theatre of Blood" in message:
        return "Theatre of Blood"
    if "Chambers of Xeric" in message:
        return "Chambers of Xeric"
    if TFA in message:
        return TFA
    if "Olm Duration" in message:
        return "Chambers of Xeric"
    if "Corrupted challenge" in message:
        return "Corrupted Hunllef"
    if "Challenge duration" in message:
        return "Crystalline Hunllef"
    if "Colosseum duration" in message:
        return "Sol Heredit"
    if "Delve level" in message:
        return delve_boss(message)
    return None


def delve_boss(message):
    if DELVE_TOTAL_PATTERN.search(message):
        return DOOM
    m = DELVE_LEVEL_PATTERN.search(message)
    if m:
        return f"{DOOM} (Level:{m.group('level')}{'+' if m.group('deep') else ''})"
    return DOOM


def delve_level(message):
    if message is None or DELVE_TOTAL_PATTERN.search(message):
        return None
    m = DELVE_LEVEL_PATTERN.search(message)
    if not m:
        return None
    level = m.group("actual") if m.group("deep") else m.group("level")
    return int(level) if level is not None else None


def raid_team_size(size) -> str:
    """PbHandler.formatRaidTeamSize on the client's max(roster, orbs)."""
    return "Solo" if size == 1 else str(size)


def _team_size(message, boss, party):
    m = TEAM_SIZE_PATTERN.search(message)
    if m:
        raw = m.group("size")
        if raw is None or raw.lower() == "solo":
            return "Solo"
        size = re.sub(r"\s+", "", raw)
        return "Solo" if size == "1" else size

    def mentions(name):
        return name in message or (boss is not None and name in boss)

    tob, toa, tfa, titans = (list(party or []) + [0, 0, 0, 0])[:4]
    if mentions("Tombs of Amascut"):
        return raid_team_size(toa)
    if mentions(TFA):
        return raid_team_size(tfa)
    if mentions("Theatre of Blood"):
        return raid_team_size(tob)
    if mentions("Royal Titans"):
        return "Solo" if titans == 1 else str(titans)
    return "Solo"


def parse_message(message, party=None):
    """One game message → a partial Kill, or None (PbHandler.parseMessage)."""
    if message.startswith("Preparation") or "target time" in message:
        return None
    count = _parse_boss_count(message)
    if count:
        return Kill(count[0], count[1], 0, 0, False, None, message)
    line = select_time_line(message)
    if line is None:
        return None
    m = TIME_WITH_PB_PATTERN.search(line)
    if not m:
        return None
    try:
        boss = _boss_from_context(message)
        return Kill(boss, None, parse_time(m.group("duration")),
                    parse_time(m.group("pbtime")) if m.group("pbtime") is not None else 0,
                    m.group("pbIndicator") is not None, _team_size(message, boss, party),
                    message, delve_level(message))
    except Exception:
        return None


def _merge(old, new):
    if old is None:
        return new
    return Kill(new.boss if new.boss is not None else old.boss,
                new.count if new.count is not None else old.count,
                new.time or old.time, new.best or old.best, new.pb or old.pb,
                new.team if new.team is not None else old.team,
                new.message if new.message is not None else old.message)


# --------------------------------------------------------------------------- #
# Replay (PbHandler.onGameMessage / onTick / processKill)
# --------------------------------------------------------------------------- #
def replay(lines, seen_recently=None):
    """The kills PbHandler would have sent for these lines, in order.

    ``lines`` are ``{"m", "t", "p"}`` dicts in arrival order. Lines are
    applied tick by tick; after each tick a complete kill is taken (as
    PbHandler.onTick did) and a partial one older than ten seconds dropped.
    ``seen_recently(boss, count)`` is the duplicate guard: True means the same
    kill was already sent in the last few seconds."""
    kills = []
    last_sent = {}
    state = {"data": None, "updated": 0}

    def process(data, now):
        if data is None or not data.valid():
            return
        key = f"{data.boss}-{data.count}"
        previous = last_sent.get("key")
        if previous == key and now - last_sent["at"] < DUPLICATE_THRESHOLD_MS and "1-8" not in data.boss:
            return
        last_sent.update(key=key, at=now)
        if seen_recently is not None and "1-8" not in data.boss and seen_recently(data.boss, data.count):
            return
        kills.append(data)

    by_tick = []
    for line in lines[:MAX_LINES]:
        if not isinstance(line, dict) or not isinstance(line.get("m"), str):
            continue
        tick = line.get("t") if isinstance(line.get("t"), int) else 0
        if by_tick and by_tick[-1][0] == tick:
            by_tick[-1][1].append(line)
        else:
            by_tick.append((tick, [line]))

    first_tick = by_tick[0][0] if by_tick else 0
    for tick, group in by_tick:
        now = (tick - first_tick) * TICK_MS
        data = state["data"]
        if data is not None and not data.valid() and now - state["updated"] > PARTIAL_EXPIRY_MS:
            state["data"] = None
        for line in group:
            parsed = parse_message(line["m"], line.get("p"))
            if parsed is None:
                continue
            if parsed.boss is not None and parsed.boss.startswith(DOOM) and parsed.time:
                # A delve line is a whole kill on its own.
                process(parsed, now)
            else:
                state["data"] = _merge(state["data"], parsed)
                state["updated"] = now
        data = state["data"]
        if data is not None and data.valid():
            if data.count is not None or "Doom" in data.boss:
                process(data, now)
                state["data"] = None
    return kills


# --------------------------------------------------------------------------- #
# Submission expansion
# --------------------------------------------------------------------------- #
def has_lines(pb_data) -> bool:
    return isinstance(pb_data, dict) and "chat_line_1" in pb_data


def read_lines(pb_data) -> list:
    out = []
    for i in range(1, MAX_LINES + 1):
        raw = pb_data.get(f"chat_line_{i}")
        if raw is None:
            break
        try:
            line = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        if isinstance(line, dict):
            out.append(line)
    return out


def _field(value) -> str:
    """BaseEventHandler.addFields: null or blank is "N/A", the rest String.valueOf."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "N/A"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def kill_fields(kill, precise) -> dict:
    """The fields PbHandler.sendKillNotification put on the embed, as the
    strings it sent them as."""
    fields = {
        "boss_name": kill.boss,
        "kill_time": format_time(kill.time, precise),
        "best_time": format_time(kill.best, precise),
        "precise_timing": precise,
        "is_pb": kill.pb,
        "team_size": kill.team if kill.team is not None else "Solo",
        "killcount": kill.count,
    }
    if kill.delve is not None:
        fields["delve_level"] = kill.delve
    return {k: _field(v) for k, v in fields.items()}


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def _redis_seen(player_key):
    """Cross-batch duplicate guard: a kill with the same boss and count as the
    player's last one, inside the threshold, is the same kill (PbHandler kept
    its last kill in memory; a plugin batch only holds one kill's lines)."""
    def seen(boss, count):
        try:
            from utils.redis import redis_client

            key = f"pb_lines:last:{player_key}"
            kill = f"{boss}-{count}"
            previous = redis_client.client.get(key)
            if isinstance(previous, bytes):
                previous = previous.decode()
            if previous == kill:
                return True
            redis_client.client.set(key, kill, px=DUPLICATE_THRESHOLD_MS)
            return False
        except Exception:
            return False
    return seen


def expand(pb_data, seen=None) -> list:
    """The submission as one ordinary kill-time dict per kill in its lines.
    ``seen`` overrides the Redis duplicate guard (tests)."""
    lines = read_lines(pb_data)
    # "source" is the client's own hint (its count line, for the raid roster
    # and screenshot timing); the kill's boss comes from the parse.
    base = {k: v for k, v in pb_data.items()
            if not str(k).startswith("chat_line_") and k != "source"}
    player_key = base.get("acc_hash") or base.get("player_name")
    if seen is None and player_key:
        seen = _redis_seen(player_key)
    kills = replay(lines, seen)
    precise = _truthy(base.get("precise_timing"))
    out = []
    for index, kill in enumerate(kills):
        entry = dict(base)
        entry.update(kill_fields(kill, precise))
        if index and entry.get("guid"):
            # One submission id per kill: the status check and the
            # duplicate-submission guard both key on it.
            entry["guid"] = f"{entry['guid']}-{index}"
        out.append(entry)
    return out
