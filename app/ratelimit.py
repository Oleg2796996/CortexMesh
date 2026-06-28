"""Redis-backed sliding-window rate limiter.

Replaces the per-process in-memory bucket so multiple workers (and a future
horizontal scale-out) share a single, consistent view of "requests per IP
per minute".

Algorithm
---------
For each key (here: IP), maintain a Redis sorted set whose members are unique
request tokens and whose scores are epoch-millisecond timestamps. On every
request we:

    1. Drop entries older than the window (ZREMRANGEBYSCORE).
    2. Count remaining entries (ZCARD).
    3. If under the cap, add the new request (ZADD + EXPIRE).

Steps 1+2+3 are wrapped in a MULTI/EXEC pipeline for atomicity. The whole
thing is one round-trip.

Failure mode
------------
If Redis is unreachable, we **fail open** (log + allow). This is a deliberate
trade-off: the API is for collaborative agents, not for protecting a paid
resource. A hard fail-closed would make Redis a single point of failure that
takes the whole API down with it. Set ``CORTEXMESH_RL_FAIL_CLOSED=1`` to flip
the default if you need stricter behaviour.

Configuration (env)
-------------------
CORTEXMESH_REDIS_URL     default redis://127.0.0.1:6379/0
CORTEXMESH_RL_PER_MIN    default 60
CORTEXMESH_RL_WINDOW_S   default 60
CORTEXMESH_RL_FAIL_CLOSED default 0
CORTEXMESH_DISABLE_RL    default 0  (set=1 to skip limiter entirely, for tests)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import redis

log = logging.getLogger("cortexmesh.ratelimit")

REDIS_URL = os.environ.get("CORTEXMESH_REDIS_URL", "redis://127.0.0.1:6379/0")
PER_MIN = int(os.environ.get("CORTEXMESH_RL_PER_MIN", "60"))
WINDOW_S = int(os.environ.get("CORTEXMESH_RL_WINDOW_S", "60"))
FAIL_CLOSED = os.environ.get("CORTEXMESH_RL_FAIL_CLOSED", "0") == "1"
DISABLED = os.environ.get("CORTEXMESH_DISABLE_RL", "0") == "1"

# Module-level client, lazily initialised. Connection pool is thread-safe.
_client: Optional[redis.Redis] = None


def get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            REDIS_URL,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
            retry_on_timeout=True,
            health_check_interval=30,
        )
    return _client


def _key(scope: str, ident: str) -> str:
    return f"rl:{scope}:{ident}"


def check(scope: str, ident: str) -> tuple[bool, int, int]:
    """Return (allowed, current_count, limit) for a given scope/identifier.

    `scope` is e.g. ``"ip"`` so we can later add per-api-key buckets.
    """
    if DISABLED:
        return True, 0, PER_MIN

    key = _key(scope, ident)
    now_ms = int(time.time() * 1000)
    window_ms = WINDOW_S * 1000
    cutoff = now_ms - window_ms

    try:
        r = get_client()
        pipe = r.pipeline(transaction=True)
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.zcard(key)
        pipe.zadd(key, {f"{now_ms}-{os.urandom(4).hex()}": now_ms})
        pipe.expire(key, WINDOW_S + 5)  # safety TTL > window
        _, count_before, _, _ = pipe.execute()
        # `count_before` is the number of entries *before* our new one was added.
        # So the effective count after this request = count_before + 1.
        effective = count_before + 1
        if effective > PER_MIN:
            # We added ourselves optimistically; roll back so the bucket doesn't
            # carry rejected requests forward (otherwise a hammering client
            # could push the visible count arbitrarily high and make recovery
            # slower than necessary).
            try:
                # Best effort — if this fails we still fail closed/open per policy.
                r.zremrangebyscore(key, now_ms, now_ms)
            except Exception:
                pass
            return False, count_before, PER_MIN
        return True, effective, PER_MIN
    except redis.RedisError as exc:
        log.warning("rate-limit redis error: %s (fail_closed=%s)", exc, FAIL_CLOSED)
        if FAIL_CLOSED:
            return False, 0, PER_MIN
        return True, 0, PER_MIN


def ping() -> bool:
    """Health probe — used by /health to surface limiter status."""
    try:
        return bool(get_client().ping())
    except Exception:
        return False
