import hashlib
import hmac
import time
from collections import deque

from fastapi import HTTPException, Request

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


def _match(presented, expected_tokens, limiter):
    if presented is None or not any(token_equal(presented, token) for token in expected_tokens):
        key = "fail:" + (_digest(presented) or "?")
        if not limiter.allow(key):
            raise RATE_LIMITED
        raise UNAUTHORIZED


def _dependency(expected_tokens):
    limiter = RateLimiter()

    async def dep(request: Request):
        _match(_bearer_token(request), expected_tokens, limiter)

    return dep


def require_device(settings):
    return _dependency((settings.phone_a_token,))


def require_admin(settings):
    return _dependency((settings.admin_token,))


def require_any(settings):
    return _dependency((settings.phone_a_token, settings.admin_token))
