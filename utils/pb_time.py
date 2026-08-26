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
0.99 across 34k rows, holding per boss and independently for ``kill_time``. A
control confirmed the premise: the four residues that only a precise client can
produce (600/1200/1800/2400) are uniform to within 0.7 points, and the whole
excess sits on residue 0, exactly where the non-precise rows land.

So the game rounds to the nearest second, and inverting it means snapping back
to the nearest tick. Two properties make that safe to apply to every time,
without needing to know which client sent it:

* **Every tick-aligned value is a fixed point.** The snap therefore cannot
  disturb a time a precise client could have produced — only whole seconds that
  are not multiples of 3000 ever move, and those are provably non-precise.
* **Nothing moves by more than 200 ms**, because a whole second sits 200 ms
  from a tick boundary on one side or the other.

The residual ambiguity is irreducible and worth being honest about: a displayed
whole second has two tick preimages 600 ms apart, and they are equally likely.
Snapping to the nearest picks one, which is exact about half the time and 600 ms
out otherwise — erring slow for half the residues and fast for the other half,
so it does not systematically advantage or penalise players who leave precise
timing off.
"""

#: One OSRS game tick, in milliseconds. Every real duration is a multiple.
TICK_MS = 600


def snap_to_tick(ms) -> int:
    """Round ``ms`` to the nearest whole game tick.

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
    # Integer half-up: Python's round() is banker's rounding, which would send
    # exact midpoints to the even tick and break the round-nearest rule.
    snapped = ((value + TICK_MS // 2) // TICK_MS) * TICK_MS
    return snapped or TICK_MS


def is_tick_aligned(ms) -> bool:
    """Whether ``ms`` is already a legal game duration."""
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return False
    return value > 0 and value % TICK_MS == 0
