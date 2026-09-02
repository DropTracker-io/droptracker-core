"""The single User-Agent DropTracker presents to the OSRS Wiki's APIs.

One identity, defined once. Every caller of ``oldschool.runescape.wiki`` and
``prices.runescape.wiki`` must import :data:`USER_AGENT` from here rather than
spelling a string of its own — we have now been blocklisted twice, and both
times the second copy of the identity is what made it expensive:

* **2026-08-20** — the wiki blocklisted ``@joelhalen - www.droptracker.io``.
  Every ``api.php`` request got a 403 and the high-value drop check silently
  fail-opened for days. Fixed on 2026-08-25 in ``osrs_api/client.py``.
* **2026-08-28** — ``prices.runescape.wiki`` blocklisted
  ``DropTracker.io - GE Price API Integration - @joelhalen``, the *private
  copy* of the identity that ``utils/ge_value.py`` had kept when the first fix
  landed. Every GE price lookup 403'd. Override-priced items (Araxxor parts,
  the DT2 vestiges, bludgeon pieces) stored ``value = 0`` and sent no
  notification, and every other drop silently fell back to the spoofable
  client-reported value. 128 drops across 98 players before it was found.

Note that a blocklist is a *symptom*: what got us listed is request volume.
``utils/ge_value.py`` is where that is bounded (mapping cache, negative price
cache, circuit breaker) — read its module docstring before adding a caller.

Stdlib-only on purpose: this module must be importable from anywhere,
including tests that load a single module in isolation.
"""
from __future__ import annotations

# Descriptive, with a contact route, per the wiki's API etiquette policy.
USER_AGENT = "DropTracker/2.0 (https://www.droptracker.io; contact: @joelhalen on Discord)"

# Identities the wiki has actually blocklisted. Never reuse one of these — a
# revived string is refused at the edge and the failure is easy to mistake for
# "the item has no price". ``tests/unit/test_wiki_user_agent.py`` fails the
# build if one reappears in the tree.
BLOCKLISTED_USER_AGENTS = (
    "@joelhalen - www.droptracker.io",
    "DropTracker.io - GE Price API Integration - @joelhalen",
)
