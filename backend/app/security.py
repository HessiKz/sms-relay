import hashlib
import hmac
import time
from collections import deque

from fastapi import HTTPException, Request

import app.db as db

UNAUTHORIZED = HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
RATE_LIMITED = HTTPException(429, "rate limited")
DEFAULT_MAX_EVENTS = 10
DEFAULT_WINDOW_S = 60.0
MAX_TRACKED_KEYS = 1024


def _digest(value):
    if not isinstance(value, str):
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def token_equal(a, b) -> bool:
    digest_a = _digest(a)
    digest_b = _digest(b)
    if digest_a is None or digest_b is None:
        return False
    return hmac.compare_digest(digest_a, digest_b)


class RateLimiter:
    """Sliding-window, per-key in-memory limiter."""

    def __init__(self, max_events: int = DEFAULT_MAX_EVENTS, window_s: float = DEFAULT_WINDOW_S):
        self.max_events = int(max_events)
        self.window_s = float(window_s)
        self._events: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        self._evict_if_needed()
        queue = self._events.setdefault(key, deque())
        cutoff = now - self.window_s
        while queue and queue[0] <= cutoff:
            queue.popleft()
        if not queue:
            del self._events[key]
        if len(queue) >= self.max_events:
            return False
        self._events[key] = queue
        queue.append(now)
        return True

    def _evict_if_needed(self) -> None:
        """Bound the number of tracked keys.

        The limiter is keyed by the digest of the *presented* credential, so
        an unauthenticated caller could otherwise grow the dict without
        bound by presenting thousands of distinct wrong tokens. Dropping
        the oldest keys is acceptable: it only weakens the limit for
        credentials that are not currently being attacked.
        """
        if len(self._events) < MAX_TRACKED_KEYS:
            return
        victims = sorted(self._events.items(),
                         key=lambda item: item[1][0] if item[1] else 0.0)
        for key, _ in victims[: max(1, MAX_TRACKED_KEYS // 4)]:
            self._events.pop(key, None)


def _bearer_token(request):
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    credential = credential.strip()
    return credential or None


def _db_conn(request):
    """The database of the running app, or None outside a request."""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    return getattr(state, "db_conn", None)


def _match(presented, expected_tokens, limiter):
    if presented is None or not any(token_equal(presented, token) for token in expected_tokens):
        _reject(presented, limiter)


def _reject(presented, limiter):
    """Count a failed attempt and reject it.

    The limiter is keyed by the digest of the presented credential, so an
    attacker cannot lock out a real device by guessing other tokens.
    """
    key = "fail:" + (_digest(presented) or "?")
    if not limiter.allow(key):
        raise RATE_LIMITED
    raise UNAUTHORIZED


def _dependency(expected_tokens):
    limiter = RateLimiter()

    async def dep(request: Request):
        _match(_bearer_token(request), expected_tokens, limiter)

    return dep


def _device_for_token(request, token):
    """The active device that owns a bearer token, or None."""
    conn = _db_conn(request)
    if conn is None or token is None:
        return None
    try:
        return db.device_by_token(conn, token)
    except Exception:
        return None


def _touch(conn, device_id: str) -> None:
    """Refresh the heartbeat of a device; a hiccup must not fail a request."""
    if conn is None:
        return
    try:
        db.touch_device(conn, device_id, int(time.time()))
    except Exception:
        pass


def require_device(settings):
    """FastAPI dependency for the phone routes.

    Only a per-device token is accepted: the shared phone token is gone,
    so a phone has to pair before it can talk to the backend. The device
    row is attached to ``request.state.device`` and its heartbeat is
    refreshed at most every 30 seconds.
    """
    limiter = RateLimiter()

    async def dep(request: Request):
        presented = _bearer_token(request)
        device = _device_for_token(request, presented)
        if device is None:
            _reject(presented, limiter)
        request.state.device = device
        _touch(_db_conn(request), device["id"])

    return dep


def require_admin(settings):
    return _dependency((settings.admin_token,))


def require_any(settings):
    """Accept the admin token or a device token.

    A device token is scoped to that device; the admin token may see
    everything.
    """
    limiter = RateLimiter()

    async def dep(request: Request):
        presented = _bearer_token(request)
        if presented is not None and token_equal(presented, settings.admin_token):
            request.state.device = None
            request.state.is_admin = True
            return
        device = _device_for_token(request, presented)
        if device is None:
            _reject(presented, limiter)
        request.state.device = device
        request.state.is_admin = False
        _touch(_db_conn(request), device["id"])

    return dep
