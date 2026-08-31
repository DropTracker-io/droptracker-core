"""Whether a death cost the player anything — the server's fallback classifier.

The plugin already answers this and sends the answer as ``is_safe_death``
(shipped in 6.0). This module exists for the deaths that arrive *without* it:
a pre-6.0 client, a manual submission, or a death the client could not locate.
Without it those deaths would sail past a group's "ignore safe deaths" setting
purely because of which client version happened to send them.

The region sets are a transcription of the plugin's ``DeathRegions.java``,
which is itself a port of Dink's ``WorldUtils#getDangerLevel``. Keep the three
in step.

**This is strictly weaker than the plugin's answer, and is only ever a
fallback.** Two inputs exist only on the client:

* *account type* — every death is dangerous for a hardcore ironman, because the
  status is what is lost. The server does not know an account's type at
  submission time, so an HCIM's Gauntlet death from an old client classifies as
  safe here and would be muted by a group that mutes safe deaths.
* *Pest Control* — the islands are ordinary regions; only the live status
  overlay distinguishes a game in progress. Only the lander region is covered
  here.

Both errors point the same way (calling a dangerous death safe), which is why
``is_safe_death`` from the payload always wins when it is present.
"""
from __future__ import annotations

#: Items cannot be carried in or out, so a death costs the run, not the bank.
#: Raid deaths inside a still-running raid are recoverable by the team.
_GAUNTLET = frozenset({7512, 7768, 12127})
_CHAMBERS_OF_XERIC = frozenset({
    12889, 13136, 13137, 13138, 13139, 13140, 13141, 13145,
    13393, 13394, 13395, 13396, 13397, 13401,
})
_THEATRE_OF_BLOOD = frozenset({
    12611, 12612, 12613, 12867, 12869, 13122, 13123, 13125, 13379,
})
_TOMBS_OF_AMASCUT = frozenset({
    14160, 14162, 14164, 14674, 14676, 15184, 15186, 15188, 15696, 15698, 15700,
})

_BARBARIAN_ASSAULT = frozenset({7508, 7509, 10322})
_CASTLE_WARS = frozenset({9520, 9620})
_CLAN_WARS = frozenset({
    12621, 12622, 12623, 13130, 13131, 13133, 13134, 13135, 13386, 13387,
    13390, 13641, 13642, 13643, 13644, 13645, 13646, 13647, 13899, 13900,
    14155, 14156,
})
_LAST_MAN_STANDING = frozenset({
    13658, 13659, 13660, 13914, 13915, 13916, 13918, 13919, 13920,
    14174, 14175, 14176, 14430, 14431, 14432,
})
_PLAYER_OWNED_HOUSE = frozenset({
    7257, 7534, 7535, 7790, 7791, 8046, 8047, 8302, 8303,
})
_SOUL_WARS = frozenset({8493, 8748, 8749, 9005})

_CLAN_HALL = 6997
_CREATURE_GRAVEYARD = 13462
_NIGHTMARE_ZONE = 9033
_PEST_CONTROL_LANDER = 10536
_TZHAAR_FIGHT_PIT = 9552

#: Deliberately absent, matching the plugin: the Inferno (9043) and the TzHaar
#: Fight Caves (9551). Both are mechanically safe, but losing a run there is
#: the whole point of announcing it — this is Dink's default
#: ``deathSafeExceptions`` baked in. A group that disagrees blacklists the
#: region instead.
SAFE_REGIONS: frozenset[int] = frozenset().union(
    _GAUNTLET,
    _CHAMBERS_OF_XERIC,
    _THEATRE_OF_BLOOD,
    _TOMBS_OF_AMASCUT,
    _BARBARIAN_ASSAULT,
    _CASTLE_WARS,
    _CLAN_WARS,
    _LAST_MAN_STANDING,
    _PLAYER_OWNED_HOUSE,
    _SOUL_WARS,
    {
        _CLAN_HALL,
        _CREATURE_GRAVEYARD,
        _NIGHTMARE_ZONE,
        _PEST_CONTROL_LANDER,
        _TZHAAR_FIGHT_PIT,
    },
)


def is_safe_region(region_id) -> bool:
    """Whether a non-hardcore death in this region costs no items.

    An unparseable or unknown region is **not** safe: the filter this feeds
    mutes safe deaths, so guessing "safe" would silently swallow a real one.
    """
    try:
        return int(region_id) in SAFE_REGIONS
    except (TypeError, ValueError):
        return False
