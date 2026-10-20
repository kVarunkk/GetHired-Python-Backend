"""Session configs, rate limits, active-session slots and undelivered results, in Upstash Redis."""
import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager

from upstash_redis.asyncio import Redis

from voice_server import settings
from voice_server.models import InterviewConfig

logger = logging.getLogger("voice_agent.store")

redis = Redis.from_env()


async def _over_rate_limit(key: str, limit: int) -> bool:
    """Counts a request in a one-minute window; True once the window is past `limit`."""
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 60)
    return count > limit


async def session_creates_over_limit(interview_id: str) -> bool:
    # Every request comes from the app's server, so limit per interview, with a
    # ceiling for the whole platform.
    return (
        await _over_rate_limit(f"ratelimit:interview:{interview_id}", settings.SESSION_CREATES_PER_INTERVIEW_PER_MIN)
        or await _over_rate_limit("ratelimit:sessions", settings.MAX_SESSION_CREATES_PER_MIN)
    )


async def create_session(cfg: InterviewConfig) -> str:
    """Stores the config under a new secure session id, for INTERVIEW_SESSION_TTL seconds."""
    sid = secrets.token_urlsafe(24)
    await redis.set(f"session:{sid}", json.dumps({"cfg": cfg.model_dump()}), ex=settings.INTERVIEW_SESSION_TTL)
    return sid


async def take_session(sid: str) -> InterviewConfig | None:
    """Fetches and consumes a session (single use)."""
    key = f"session:{sid}"
    raw = await redis.get(key)
    if not raw:
        return None
    await redis.delete(key)
    return InterviewConfig(**json.loads(raw)["cfg"])


async def delete_session(sid: str):
    await redis.delete(f"session:{sid}")


async def get_result(sid: str) -> dict | None:
    stored = await redis.get(f"result:{sid}")
    return json.loads(stored) if stored else None


async def save_result(sid: str, payload: dict, ttl: int):
    await redis.set(f"result:{sid}", json.dumps(payload), ex=ttl)


async def count_active_sessions() -> int:
    cursor, total = 0, 0
    while True:
        cursor, keys = await redis.scan(cursor, match="active:*", count=100)
        total += len(keys)
        if int(cursor) == 0:
            return total


async def _keep_active(key: str, ttl: int):
    while True:
        await asyncio.sleep(settings.ACTIVE_REFRESH_SECONDS)
        try:
            await redis.expire(key, ttl)
        except Exception as redis_err:
            logger.error(f"Failed to refresh {key}: {redis_err}")


@asynccontextmanager
async def active_slot(sid: str, mode: str, ttl: int):
    """Holds an `active:{sid}` key while the session runs, refreshed so it
    only expires if the server dies."""
    key = f"active:{sid}"
    await redis.set(key, mode, ex=ttl)
    keep_active = asyncio.create_task(_keep_active(key, ttl))
    try:
        yield
    finally:
        # Free the slot even if the session crashed; if this fails, the key expires.
        keep_active.cancel()
        try:
            await redis.delete(key)
        except Exception as redis_err:
            logger.error(f"Failed to remove {key}: {redis_err}")
