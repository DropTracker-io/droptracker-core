"""Whether the current task is processing mirrored production traffic.

When the admin panel switches on mirroring (see edge/intake-capture and
services/edge_config.py), the Cloudflare Worker sends a second copy of every
live submission to the dev instance, marked ``X-DT-Mirror: 1``. The consumer
enters :func:`mirror_sink` for the life of such a submission, and everything
downstream can ask whether it is looking at mirrored traffic.

This lives in ``utils`` — with no imports beyond the standard library — because
the things that need to ask are spread across ``data/submissions``,
``services`` and ``osrs_api``, and any of those importing another to reach a
ContextVar would risk a cycle. A leaf module is importable from all of them.

A ContextVar rather than a module global because the consumer runs submissions
as concurrent asyncio tasks: a global would leak the sink between them, which
in the bad direction means a real submission announcing into the dev sink, and
in the worse direction a mirrored one escaping into a real clan's Discord.
"""

import contextlib
import contextvars
import os

_mirror_sink_group: contextvars.ContextVar = contextvars.ContextVar(
    "mirror_sink_group", default=None
)


@contextlib.contextmanager
def mirror_sink(group_id):
    """Mark this block as mirrored traffic, rerouted to ``group_id``.

    The caller is responsible for having confirmed ``group_id`` names a group in
    a dev-allowlisted guild — see
    ``workers.webhook_consumer._resolve_mirror_sink``.
    """
    token = _mirror_sink_group.set(group_id)
    try:
        yield
    finally:
        _mirror_sink_group.reset(token)


def sink_group_id():
    """The group mirrored notifications reroute to, or None if not mirrored."""
    return _mirror_sink_group.get()


def is_mirrored_submission() -> bool:
    """True when the current task is processing a mirrored copy of production.

    The general "should I skip this?" test for side effects that reach outside
    this instance: external APIs on shared quotas, Discord sends, anything that
    writes to production.
    """
    return _mirror_sink_group.get() is not None


def mirrored_extras_enabled() -> bool:
    """Whether mirrored traffic should also drive events and the points ledger.

    Off by default. Dev's database is a production dump, so mirrored traffic
    would apply progress to real clans' live events and write point rows for
    real players — all contained in dev, but noisy enough to drown out whatever
    is actually being tested. Set MIRROR_PROCESS_EXTRAS=true to soak the event
    engine deliberately.
    """
    return (os.getenv("MIRROR_PROCESS_EXTRAS") or "").strip().strip('"').strip(
        "'"
    ).lower() in ("1", "true", "yes")


def skip_mirrored_extras() -> bool:
    """True when this is mirrored traffic and extras have not been opted into."""
    return is_mirrored_submission() and not mirrored_extras_enabled()
