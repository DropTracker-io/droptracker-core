"""Whether the current task is processing mirrored production traffic.

When the admin panel switches on mirroring (see edge/intake-capture and
services/edge_config.py), the Cloudflare Worker sends a second copy of live
submissions to the dev instance, marked with an ``X-DT-Mirror`` header. The
header says which kind of copy it is (:func:`mirror_kind`):

* ``1`` — the firehose ("Everyone" mode): a sample of all production traffic.
  The consumer enters :func:`mirror_sink` for the life of such a submission,
  and everything downstream can ask whether it is looking at mirrored traffic.
* ``tester`` — a Bug Tester's own submission. It is processed like one sent
  straight to dev (groups, events, points and all), so it never enters the
  sink; what keeps it inside dev is the instance's own guild guard.

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

#: A sampled copy of everyone's production traffic (header value ``1``).
KIND_ALL = "all"
#: A Bug Tester's own submission (header value ``tester``).
KIND_TESTER = "tester"


def mirror_kind(header_value):
    """Classify an ``X-DT-Mirror`` header value: None, KIND_TESTER or KIND_ALL.

    Anything present but unrecognised is treated as the firehose, because the
    firehose is the path with the sink: an unknown kind of mirrored traffic
    must never get the tester path's freedom by accident.
    """
    if header_value is None:
        return None
    value = str(header_value).strip().lower()
    if not value:
        return None
    if value == KIND_TESTER:
        return KIND_TESTER
    return KIND_ALL


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
