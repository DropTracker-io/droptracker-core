"""A player logged out (plugin 6.1.29+).

Not something a player did in the game, and nothing is stored for it. The
official hiscores update when a player logs out, so this is the moment a
reading of their skills and kill counts is worth taking:
``services.hiscores_poller.note_logout`` books one a minute and a half from
now.

It arrives like any other submission, on whichever transport the player
uses, so players who keep "Use API" off are covered through the Discord
webhooks exactly as their drops are.
"""
from db.models import Player

from .common import SubmissionResponse, select_session_and_flag


async def logout_processor(data, external_session=None):
    session, _use_external_session = select_session_and_flag(external_session)

    account_hash = data.get("acc_hash")
    if not account_hash:
        return SubmissionResponse(False, "Logout has no account")
    player_id = (
        session.query(Player.player_id)
        .filter(Player.account_hash == str(account_hash))
        .limit(1)
        .scalar()
    )
    if player_id is None:
        # Identity is made by a real submission, never by this.
        return SubmissionResponse(True, "No player for this account yet; logout ignored")

    from services.hiscores_poller import note_logout
    from utils.redis import redis_client

    booked = note_logout(redis_client.client, int(player_id))
    return SubmissionResponse(True, "Logout noted" if booked else "Logout already noted")
