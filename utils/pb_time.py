"""Snapping personal-best times onto the game's tick grid.

Old School RuneScape measures every duration in game ticks of 600 ms, so a real
kill time is always a multiple of 600. What a player's client *prints* is
another matter: with the "precise timing" option off (varbit 11866) the chat
line carries whole seconds instead, and the plugin faithfully passes that on.
The result is one board holding two different quantizations, where a rounded
time can out-rank a true one it never actually beat.

**The rounding rule was measured, not assumed.** Whole-second times that are not
multiples of 3000 can only have come from a non-precise client, because a
precise client's whole-second times are always multiples of 3000 (five ticks).
Splitting those rows by their residue mod 3000 discriminates the two candidate
rules: truncation predicts a 2:1 split between the 1000 and 2000 residues, while
round-to-nearest predicts 1:1. Production showed 17,054 vs 17,291 — a ratio of
0.99 across 34k rows, holding per boss and independently for ``kill_time``.

A second, independent confirmation came out of ticket #182 (below): among
same-raid pairs that disagree, 290 of 291 land on exactly the two residue
signatures round-to-nearest allows, and 120 of those are a signature truncation
cannot produce at all. So the game rounds a duration to the *nearest* second.

**Inverting it is the part that needs care, and getting it wrong is what
ticket #182 reported.** A display of ``S`` seconds means the true duration lay
in ``[1000S - 500, 1000S + 500)``, and for two seconds out of every three that
window holds *two* ticks, 600 ms apart. Picking the nearer one is the best
guess for a single row, but it guesses low half the time, and a time recorded
below the truth is a record the player did not earn. Two clan-mates on the same
raid made that visible: identical raid, but the one with precise timing off
came out 600 ms ahead. Across 2,339 same-raid pairs recorded in the month after
tick snapping shipped, 291 disagreed when they had to be identical, and in 170
of them the non-precise player held the unearned advantage.

So the inverse here is deliberately **not** the nearest tick but the *slowest*
tick the display is consistent with — a plain ceiling onto the grid. Three
properties make that both safe and fair:

* **Every tick-aligned value is a fixed point.** The snap therefore cannot
  disturb a time a precise client could have produced — only whole seconds that
  are not multiples of 3000 ever move, and those are provably non-precise.
* **It never moves a time downwards**, so a non-precise client cannot be
  credited with a duration shorter than the one it actually achieved. A player
  can no longer beat a raid-mate they did not beat.
* **Nothing moves by more than 400 ms**, the widest gap between a whole second
  and the next tick above it.

The residual ambiguity is irreducible and worth being honest about: the true
time is the upper tick of the pair about half the time, and the lower tick the
other half. Rounding up is exact in three tick-residues out of five and one
tick pessimistic in the other two. That is a real cost, paid only by players
who leave precise timing off, and it is the right way round for a leaderboard:
a record is never credited to someone who did not set it, and turning precise
timing on removes the penalty entirely.
"""

import re

#: One OSRS game tick, in milliseconds. Every real duration is a multiple.
TICK_MS = 600


def snap_to_tick(ms) -> int:
    """Round ``ms`` up to the slowest game tick its display is consistent with.

    Non-positive values pass through unchanged: zero is the "no time recorded"
    sentinel throughout the PB pipeline and must not become a real duration.
    A positive time never rounds below a single tick, so a sub-tick value —
    which the game cannot produce — cannot be flattened into that sentinel.
    """
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return value
    # Ceiling division. Rounding to the *nearest* tick here is what ticket #182
    # reported: it resolves the two-tick ambiguity downwards half the time, and
    # a PB below the true duration is a record nobody earned.
    return -(-value // TICK_MS) * TICK_MS


def is_tick_aligned(ms) -> bool:
    """Whether ``ms`` is already a legal game duration."""
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return False
    return value > 0 and value % TICK_MS == 0


def time_is_precise(value, flag=None):
    """Whether a submitted time came from a client with precise timing on.

    ``flag`` is the plugin's own ``precise_timing`` field (6.1.0+), which wins
    when present. Without it the formatted time still says so: a precise client
    always prints hundredths (``"28:14.40"``, even ``"28:15.00"``), a
    non-precise one never does (``"28:14"``). Returns None when neither tells
    us — a raw millisecond value from a form, an untimed kill, or junk.
    """
    if flag is not None and str(flag).strip() != "":
        text = str(flag).strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
    if not isinstance(value, str):
        return None
    text = value.strip()
    if ":" not in text:
        return None
    # Fractional digits, not merely a dot: a broadcast sentence can end the
    # time with a full stop ("personal best: 28:14.").
    return re.search(r":\d{2}\.\d", text) is not None


def board_sort_key(time_ms, precise, date_ts=0):
    """Ordering for a PB board: fastest first, then a measured time ahead of an
    approximate or unknown one, then the earlier date.

    A whole-second time is already credited with the slowest tick it could
    stand for, so when it lands on exactly the same value as a precise time the
    two genuinely cannot be told apart. The tie goes to the one that was
    measured.
    """
    return (int(time_ms or 0), 0 if precise is True else 1, int(date_ts or 0))
