"""Keeping clan leaders in the loop about changes to the DropTracker.

Two pieces share this module:

* **The Follows Updates role by default.** Every clan leader (a ``group_admins``
  row, owner or admin) who is in the HQ server gets the role that unlocks the
  plugin / website / discord update channels (``services/news_optin.py``). The
  opt-in button there now doubles as the opt-out, and pressing it off stores
  ``follows_updates = 0`` in ``user_configurations`` so the sweep never hands
  the role back. Granting a role sends no message; it only changes what the
  leader can see.
* **Site-wide announcements on Discord.** A global announcement published with
  "Also post to Discord" goes to the public news channel (an Announcement
  channel) and is published there, so servers that follow the channel get a
  copy in their own staff channel. Before this, global announcements never
  left the website.
* **Owner review.** Only an approver (the owner, user_id 0, by default)
  publishes a site-wide post. Anyone else's, and anything an agent or the
  weekly roundup drafts, is saved as a draft and DMed to the approver, who
  edits, approves or cancels it on /admin/announcements (web129a).

Pilot first. Until ``LEADER_UPDATES_LIVE`` is switched on, both pieces act on
the pilot accounts only (``LEADER_UPDATES_PILOT_DISCORD_IDS``, defaulting to
the owner): the role sweep touches no one else, and a global announcement is
DMed to the pilot accounts as a preview instead of being posted to the news
channel. Going live is an env change plus a restart of ``droptracker-webapi``
(announcements) and ``droptracker-core`` (role sweep).
"""
from __future__ import annotations

import os
from typing import Iterable, Optional

ENV_LIVE = "LEADER_UPDATES_LIVE"
ENV_PILOT_IDS = "LEADER_UPDATES_PILOT_DISCORD_IDS"
DEFAULT_PILOT_DISCORD_IDS = ("528746710042804247",)

# HQ server, the public news channel (an Announcement channel), and the
# per-user preference key. news_optin imports these so there is one copy.
HQ_GUILD_ID = 1172737525069135962
NEWS_CHANNEL_ID = 1527845346582073426
FOLLOW_PREF_KEY = "follows_updates"

LEADER_ROLES = ("owner", "admin")

_EMBED_COLOR = 0xC8AA6E
_EMBED_DESCRIPTION_MAX = 4000


def is_live() -> bool:
    return os.getenv(ENV_LIVE, "").strip().lower() in ("1", "true", "yes", "on")


def pilot_discord_ids() -> set[str]:
    raw = os.getenv(ENV_PILOT_IDS)
    if raw is None or not raw.strip():
        return set(DEFAULT_PILOT_DISCORD_IDS)
    return {p.strip() for p in raw.split(",") if p.strip().isdigit()}


def in_audience(discord_id) -> bool:
    """Whether this Discord account may be acted on right now."""
    if discord_id is None:
        return False
    return is_live() or str(discord_id) in pilot_discord_ids()


# --------------------------------------------------------------------------- #
# Leaders and their preference
# --------------------------------------------------------------------------- #
def leader_discord_ids(session) -> set[str]:
    """Discord ids of every clan owner/admin with a grant row.

    Discord MANAGE_GUILD admins without a grant row can't be listed server-side
    (no guild member lists), so they are not included; they can still follow
    with the button."""
    from db.models import GroupAdmin, User

    rows = (
        session.query(User.discord_id)
        .join(GroupAdmin, GroupAdmin.user_id == User.user_id)
        .filter(GroupAdmin.role.in_(LEADER_ROLES))
        .distinct()
        .all()
    )
    return {str(d) for (d,) in rows if d is not None and str(d).strip().isdigit()}


def opted_out_ids(session, discord_ids: Iterable[str]) -> set[str]:
    """The subset of ``discord_ids`` that turned the role off."""
    ids = [str(d) for d in discord_ids]
    if not ids:
        return set()
    from db.models import User, UserConfiguration

    rows = (
        session.query(User.discord_id)
        .join(UserConfiguration, UserConfiguration.user_id == User.user_id)
        .filter(
            UserConfiguration.config_key == FOLLOW_PREF_KEY,
            UserConfiguration.config_value == "0",
            User.discord_id.in_(ids),
        )
        .all()
    )
    return {str(d) for (d,) in rows}


def set_follow_pref(session, discord_id, following: bool) -> bool:
    """Record the button choice. Returns False when the presser has no users
    row (never signed in or linked) — their role still toggles, there is just
    nothing to remember it on, and a non-leader is never swept anyway."""
    from db.models import User, UserConfiguration

    user = session.query(User).filter(User.discord_id == str(discord_id)).first()
    if user is None:
        return False
    value = "1" if following else "0"
    row = (
        session.query(UserConfiguration)
        .filter(UserConfiguration.user_id == user.user_id,
                UserConfiguration.config_key == FOLLOW_PREF_KEY)
        .first()
    )
    if row is None:
        session.add(UserConfiguration(user_id=user.user_id,
                                      config_key=FOLLOW_PREF_KEY,
                                      config_value=value))
    else:
        row.config_value = value
    session.commit()
    return True


def role_grant_candidates(session) -> list[str]:
    """Leaders who should hold the role and haven't opted out, limited to the
    pilot accounts until the feature is live. Sorted for a stable sweep order."""
    leaders = leader_discord_ids(session)
    if not is_live():
        leaders &= pilot_discord_ids()
    return sorted(leaders - opted_out_ids(session, leaders))


# --------------------------------------------------------------------------- #
# Role sweep bookkeeping (Redis)
# --------------------------------------------------------------------------- #
# Each leader is handled once: after the role is given (or found already
# held) they join GRANTED_KEY and are never looked up again, so an admin who
# removes the role by hand is not overridden either. Leaders who aren't in HQ
# are re-checked a day later, which is how someone who joins later is caught
# without the members intent.
GRANTED_KEY = "news:follow:granted"
ABSENT_KEY_PREFIX = "news:follow:absent:"
ABSENT_RECHECK_SECONDS = 86400
MAX_LOOKUPS_PER_SWEEP = 40


def _absent_key(discord_id) -> str:
    return f"{ABSENT_KEY_PREFIX}{discord_id}"


def _decode(value) -> str:
    return value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else str(value)


def pending_role_grants(session, conn, cap: int = MAX_LOOKUPS_PER_SWEEP) -> list[str]:
    """The next leaders to look up in HQ: candidates not yet handled and not
    recently found absent, at most ``cap`` of them."""
    candidates = role_grant_candidates(session)
    if not candidates or conn is None:
        return []
    granted = {_decode(v) for v in (conn.smembers(GRANTED_KEY) or ())}
    todo = [d for d in candidates if d not in granted]
    if not todo:
        return []
    pipe = conn.pipeline()
    for d in todo:
        pipe.exists(_absent_key(d))
    absent_flags = pipe.execute()
    return [d for d, absent in zip(todo, absent_flags) if not absent][:cap]


def mark_handled(conn, discord_id) -> None:
    conn.sadd(GRANTED_KEY, str(discord_id))


def mark_absent(conn, discord_id) -> None:
    conn.set(_absent_key(discord_id), "1", ex=ABSENT_RECHECK_SECONDS)


def forget(session, conn, discord_id) -> None:
    """Treat this account as never seen: clear its stored choice and the sweep
    markers, so the next sweep hands it the role again (pilot testing)."""
    from db.models import User, UserConfiguration

    user = session.query(User).filter(User.discord_id == str(discord_id)).first()
    if user is not None:
        (session.query(UserConfiguration)
         .filter(UserConfiguration.user_id == user.user_id,
                 UserConfiguration.config_key == FOLLOW_PREF_KEY)
         .delete(synchronize_session=False))
        session.commit()
    if conn is not None:
        conn.srem(GRANTED_KEY, str(discord_id))
        conn.delete(_absent_key(discord_id))


# --------------------------------------------------------------------------- #
# Site-wide announcements on Discord
# --------------------------------------------------------------------------- #
def announcement_url(ann_id: int) -> str:
    from utils.site_urls import WEBSITE_URL

    return f"{WEBSITE_URL}/announcements/{int(ann_id)}"


def announcement_embed(title: str, body_md: str) -> dict:
    return {
        "title": title,
        "description": body_md[:_EMBED_DESCRIPTION_MAX],
        "color": _EMBED_COLOR,
    }


def enqueue_global_announcement(session, *, ann_id: int, title: str, body_md: str,
                                actor_user_id: Optional[int] = None) -> list:
    """Queue a published global announcement for Discord.

    Live: one ``news_post`` row for the news channel; the drain posts and then
    publishes it so following servers receive it. Pilot: one ``dm`` per pilot
    account carrying the same embed, so the post can be checked before anyone
    else sees it. Returns the queued rows."""
    from services.discord_outbox import enqueue

    embed = announcement_embed(title, body_md)
    components = [{"label": "Read on the website", "url": announcement_url(ann_id)}]

    if is_live():
        return [enqueue(
            session,
            channel_id=str(NEWS_CHANNEL_ID),
            embed=embed,
            components=components,
            kind="news_post",
            ref_type="announcement",
            ref_id=ann_id,
            actor_user_id=actor_user_id,
        )]

    note = (
        "-# Pilot preview. When this goes live, the post below goes to "
        f"<#{NEWS_CHANNEL_ID}> and is published to every server that follows it."
    )
    return [
        enqueue(
            session,
            channel_id=discord_id,
            content=note,
            embed=embed,
            components=components,
            kind="dm",
            ref_type="announcement_preview",
            ref_id=ann_id,
            actor_user_id=actor_user_id,
        )
        for discord_id in sorted(pilot_discord_ids())
    ]


# --------------------------------------------------------------------------- #
# Owner review (web129a)
# --------------------------------------------------------------------------- #
# Site-wide posts are public on the site and, once live, in every following
# server. So only an approver (the owner by default) publishes one: anything
# written by anyone else, and everything an agent or the roundup job drafts,
# lands as a draft in the review queue on /admin/announcements, where the
# approver can edit it, approve it, or cancel it. Approvers are site user ids,
# because approving happens on the website under their own session.
ENV_APPROVER_USER_IDS = "LEADER_UPDATES_APPROVER_USER_IDS"
DEFAULT_APPROVER_USER_IDS = (0,)
REVIEW_PATH = "/admin/announcements"
_REVIEW_PREVIEW_MAX = 1500


def approver_user_ids() -> set[int]:
    raw = os.getenv(ENV_APPROVER_USER_IDS)
    if raw is None or not raw.strip():
        return set(DEFAULT_APPROVER_USER_IDS)
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def is_approver(user_id) -> bool:
    # user_id 0 is the owner: never truthiness-test it.
    if user_id is None:
        return False
    try:
        return int(user_id) in approver_user_ids()
    except (TypeError, ValueError):
        return False


def _approver_discord_ids(session) -> list[str]:
    from db.models import User

    ids = list(approver_user_ids())
    if not ids:
        return []
    rows = session.query(User.discord_id).filter(User.user_id.in_(ids)).all()
    return sorted({str(d) for (d,) in rows if d is not None and str(d).strip().isdigit()})


def _queue_review_dm(session, ann) -> None:
    """Tell the approvers a post is waiting. The DM shows the text so it can be
    read on a phone, but approving only happens on the website."""
    from services.discord_outbox import enqueue
    from utils.site_urls import WEBSITE_URL

    body = ann.body_md
    if len(body) > _REVIEW_PREVIEW_MAX:
        body = body[:_REVIEW_PREVIEW_MAX].rstrip() + "\n..."
    where = "the website and Discord" if ann.post_to_discord else "the website"
    content = (
        f"**A post is waiting for your review.** Drafted by {ann.source_label or 'staff'}. "
        f"Nothing goes out until you approve it; it would be published on {where}."
    )
    for discord_id in _approver_discord_ids(session):
        enqueue(
            session,
            channel_id=discord_id,
            content=content,
            embed=announcement_embed(ann.title, body),
            components=[{"label": "Review, edit or cancel", "url": f"{WEBSITE_URL}{REVIEW_PATH}"}],
            kind="dm",
            ref_type="announcement_review",
            ref_id=ann.id,
            commit=False,
        )


def create_review_draft(session, *, title: str, body_md: str, post_to_discord: bool,
                        source_label: Optional[str], author_user_id: Optional[int] = None,
                        pinned: bool = False, cover_image_url: Optional[str] = None):
    """Save a global post as a draft for review and DM the approvers.
    Returns the Announcement row. Nothing about it is public."""
    from db.models import Announcement

    ann = Announcement(
        scope_type="global",
        group_id=None,
        author_user_id=author_user_id,
        title=title,
        body_md=body_md,
        cover_image_url=cover_image_url,
        pinned=bool(pinned),
        status="draft",
        published_at=None,
        post_to_discord=bool(post_to_discord),
        source_label=(source_label or None) and str(source_label)[:64],
    )
    session.add(ann)
    session.flush()
    try:
        _queue_review_dm(session, ann)
    except Exception as e:  # the draft is what matters; the DM is a nudge
        print(f"[leader_updates] review DM not queued for {ann.id}: {e}")
    session.commit()
    return ann


def publish_reviewed(session, ann, reviewer_user_id) -> None:
    """Publish an approved global post: it goes public on the site and, if the
    author asked for Discord, out through ``enqueue_global_announcement``
    (still pilot-gated). Caller has checked ``is_approver``."""
    from datetime import datetime

    ann.status = "published"
    ann.published_at = datetime.now()
    ann.reviewed_by = int(reviewer_user_id)
    ann.reviewed_at = ann.published_at
    session.commit()
    if ann.post_to_discord:
        enqueue_global_announcement(
            session, ann_id=ann.id, title=ann.title, body_md=ann.body_md,
            actor_user_id=reviewer_user_id,
        )
