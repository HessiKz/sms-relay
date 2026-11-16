"""In-process revision counter for the Mini App long poll.

Every genuine change to inbox data increments ``app.state.rev`` and wakes
the requests waiting on ``app.state.rev_event``. This is deliberately
ephemeral: it only has to be good enough for one process serving one admin,
not a durable event log.

A change may also name the device and the conversation it belongs to. A
thread belongs to a device, so the scope of a change is the pair
``(device_id, thread_key)``. The scope history lets the Mini App skip a
thread reload when the change cannot affect the conversation that is
open; it is a hint, never a source of truth.
"""

import asyncio
from collections import deque

# How many changes keep their scope. Older entries are dropped, and a
# client that is further behind than this gets "unknown" instead of a
# wrong answer.
MAX_SCOPE_ENTRIES = 200


def init_rev(app) -> None:
    """Create the revision state; called once from the lifespan startup."""
    app.state.rev = 0
    app.state.rev_event = asyncio.Event()
    app.state.rev_scopes = deque(maxlen=MAX_SCOPE_ENTRIES)


def bump_rev(app, device_id: str | None = None,
             thread_key: str | None = None) -> int:
    """Increment the revision and wake waiters.

    ``device_id`` names the device the change belongs to and
    ``thread_key`` the conversation inside that device. A change with a
    device but no thread is device-wide (every conversation of that
    device may have changed). A change without a device is global and
    its scope is unknown.

    Safe to call before ``init_rev`` (for example in an isolated unit
    test): the counter still advances and there is simply no waiter.
    """
    current = getattr(app.state, "rev", 0)
    current = int(current) + 1 if isinstance(current, int) else 1
    app.state.rev = current
    scopes = getattr(app.state, "rev_scopes", None)
    if scopes is not None:
        scopes.append((current, device_id, thread_key))
    event = getattr(app.state, "rev_event", None)
    if event is not None:
        event.set()
        event.clear()
    return current


def changed_threads(app, since_rev: int) -> list[dict] | None:
    """The conversations touched after ``since_rev``.

    Every entry names the device and the thread: ``{"device_id": ...,
    "thread_key": ...}``. A device-wide change has ``thread_key`` set to
    ``None``.

    ``None`` means "cannot tell": the scope history is missing or too old,
    the counter was reset, the change was global, or a change named a
    thread without its device (a thread only exists inside a device, so
    such a scope cannot be resolved). Callers must then refresh
    everything.
    """
    scopes = getattr(app.state, "rev_scopes", None)
    if scopes is None or not scopes:
        return None
    current = int(getattr(app.state, "rev", 0) or 0)
    if since_rev > current:
        # The counter was reset by a restart; the client is ahead of us.
        return None
    if scopes[0][0] > since_rev + 1:
        # The scope window no longer covers every change since since_rev.
        return None
    changed: list[dict] = []
    seen: set[tuple] = set()
    for entry_rev, device_id, thread_key in scopes:
        if entry_rev <= since_rev:
            continue
        if device_id is None:
            # A global change, or a thread without its device: the scope
            # cannot be resolved, so the client has to reload everything.
            return None
        item = (device_id, thread_key)
        if item in seen:
            continue
        seen.add(item)
        changed.append({"device_id": device_id, "thread_key": thread_key})
    return changed
