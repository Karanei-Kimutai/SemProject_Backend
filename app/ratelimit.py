"""Rate limiting for the sign-in endpoints (fixed windows, in memory).

In memory is enough for one API process (the Docker Compose setup). With several API processes, each
keeps its own counters, so the effective limit is multiplied; move the counters to Postgres or Redis then.
"""

from __future__ import annotations

import threading
import time

from fastapi import Request

from app.config import get_settings
from app.errors import ApiError

_lock = threading.Lock()
_hits: dict[str, tuple[float, int]] = {}

# name: (max requests, window seconds)
LIMITS = {
    "login_ip": (20, 60), "login_email": (5, 60),
    "register_ip": (5, 3600),
    "refresh_ip": (60, 60),
    "reset_request_ip": (5, 3600), "reset_request_email": (3, 3600),
    "reset_confirm_ip": (10, 600),
}


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def hit(limit: str, key: str) -> None:
    if not get_settings().rate_limit_enabled:
        return
    maximum, window = LIMITS[limit]
    now = time.monotonic()
    bucket = f"{limit}:{key}"
    with _lock:
        if len(_hits) > 50_000:  # drop expired windows
            for k in [k for k, (start, _) in _hits.items() if now - start > 3600]:
                del _hits[k]
        start, count = _hits.get(bucket, (now, 0))
        if now - start >= window:
            start, count = now, 0
        count += 1
        _hits[bucket] = (start, count)
    if count > maximum:
        retry_after = int(window - (now - start)) + 1
        raise ApiError("RATE_LIMITED", "Too many attempts. Wait a little and try again.", 429,
                       details={"retryAfterSeconds": retry_after})


def reset() -> None:
    with _lock:
        _hits.clear()
