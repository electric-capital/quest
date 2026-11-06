"""In-memory sliding-window rate limiter shared by the unauthenticated
routes (password sign-in, first-run setup). Per process; resets on
restart."""

import time
from collections import defaultdict, deque


class SlidingWindow:
    """Allow at most ``limit`` events per key within ``window`` seconds."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._events: dict[str, deque] = defaultdict(deque)

    def _trim(self, key: str, now: float) -> deque:
        events = self._events[key]
        while events and events[0] <= now - self.window:
            events.popleft()
        if not events:
            self._events.pop(key, None)
            return deque()
        return events

    def blocked(self, key: str) -> bool:
        return len(self._trim(key, time.monotonic())) >= self.limit

    def hit(self, key: str) -> None:
        now = time.monotonic()
        self._trim(key, now)
        self._events[key].append(now)

    def reset(self, key: str) -> None:
        self._events.pop(key, None)
