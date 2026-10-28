"""In-process revision counter for the Mini App long poll.

Every genuine change to inbox data increments ``app.state.rev`` and wakes
the requests waiting on ``app.state.rev_event``. This is deliberately
ephemeral: it only has to be good enough for one process serving one admin,
not a durable event log.
"""

import asyncio


def init_rev(app) -> None:
    """Create the revision state; called once from the lifespan startup."""
    app.state.rev = 0
    app.state.rev_event = asyncio.Event()


def bump_rev(app) -> int:
    """Increment the revision and wake waiters.

    Safe to call before ``init_rev`` (for example in an isolated unit
    test): the counter still advances and there is simply no waiter.
    """
    current = getattr(app.state, "rev", 0)
    current = int(current) + 1 if isinstance(current, int) else 1
    app.state.rev = current
    event = getattr(app.state, "rev_event", None)
    if event is not None:
        event.set()
        event.clear()
    return current
