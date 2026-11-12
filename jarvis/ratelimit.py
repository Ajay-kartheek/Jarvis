"""Pacing and retry helpers for the Google mirrors."""

from __future__ import annotations

import http.client
import json
import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

_TRANSPORT_ERROR_NAMES = {"HttpLib2Error", "TransportError"}

_RATE_LIMIT_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "RATE_LIMIT_EXCEEDED",
    "RESOURCE_EXHAUSTED",
}


class RateLimiter:
    """Paces calls so the average stays at or under ``units_per_second``.

    There is no burst allowance: each call waits until the cost of the
    previous one has been "paid off".
    """

    def __init__(
        self,
        units_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if units_per_second <= 0:
            raise ValueError("units_per_second must be positive")
        self.rate = float(units_per_second)
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0

    def acquire(self, units: float = 1.0) -> None:
        now = self._clock()
        if self._next > now:
            self._sleep(self._next - now)
            now = self._next
        self._next = now + units / self.rate


def error_status(exc: BaseException) -> int | None:
    """HTTP status of a googleapiclient ``HttpError``, or None."""
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def error_reason(exc: BaseException) -> str:
    """Google's machine-readable reason for an ``HttpError``, or ''."""
    content = getattr(exc, "content", None)
    if not content:
        return ""
    try:
        error = json.loads(content).get("error", {})
    except (ValueError, AttributeError):
        return ""
    if not isinstance(error, dict):
        return ""
    for item in error.get("errors", []) or []:
        if item.get("reason"):
            return str(item["reason"])
    for item in error.get("details", []) or []:
        if item.get("reason"):
            return str(item["reason"])
    return str(error.get("status", ""))


def is_transient(exc: BaseException) -> bool:
    """True for errors worth retrying: rate limits, server errors, network."""
    status = error_status(exc)
    if status is not None:
        if status == 429 or status >= 500:
            return True
        return status == 403 and error_reason(exc) in _RATE_LIMIT_REASONS
    if isinstance(exc, (TimeoutError, ConnectionError, OSError, http.client.HTTPException)):
        return True
    # httplib2 and google-auth transport failures, matched by name so this
    # module does not have to import either library.
    return any(cls.__name__ in _TRANSPORT_ERROR_NAMES for cls in type(exc).__mro__)


def backoff_seconds(attempt: int, base: float = 1.0, ceiling: float = 64.0) -> float:
    """Exponential backoff with jitter; ``attempt`` starts at 1."""
    return min(ceiling, base * 2 ** (attempt - 1)) * (0.5 + random.random() / 2)


def call_with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 6,
    base: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn``, retrying transient failures with exponential backoff."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == attempts or not is_transient(exc):
                raise
            sleep(backoff_seconds(attempt, base))
    raise AssertionError("unreachable")
