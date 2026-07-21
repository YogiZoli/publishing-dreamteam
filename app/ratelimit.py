"""Postgres-based rate limiting (no Redis).

Free tier: 2 artifacts/day and 10/month per user, secondary per-IP limit.
Cached results (existing artifact for the same video_id) do NOT consume quota.
"""
from dataclasses import dataclass

from app.config import get_settings
from app.db import get_pool

IP_PER_DAY_MULTIPLIER = 3  # generous secondary guard against account cycling


@dataclass
class RateStatus:
    allowed: bool
    reason: str = ""


async def check(user_id: str, ip: str) -> RateStatus:
    s = get_settings()
    pool = await get_pool()
    async with pool.acquire() as conn:
        day_user = await conn.fetchval(
            "SELECT count(*) FROM rate_limits WHERE subject=$1 AND created_at > now() - interval '1 day'",
            f"user:{user_id}",
        )
        if day_user >= s.rate_limit_per_day:
            return RateStatus(False, "daily_limit")
        month_user = await conn.fetchval(
            "SELECT count(*) FROM rate_limits WHERE subject=$1 AND created_at > now() - interval '30 days'",
            f"user:{user_id}",
        )
        if month_user >= s.rate_limit_per_month:
            return RateStatus(False, "monthly_limit")
        day_ip = await conn.fetchval(
            "SELECT count(*) FROM rate_limits WHERE subject=$1 AND created_at > now() - interval '1 day'",
            f"ip:{ip}",
        )
        if day_ip >= s.rate_limit_per_day * IP_PER_DAY_MULTIPLIER:
            return RateStatus(False, "ip_limit")
    return RateStatus(True)


async def record(user_id: str, ip: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO rate_limits (subject) VALUES ($1), ($2)",
            f"user:{user_id}",
            f"ip:{ip}",
        )


async def cached_artifact(video_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT payload FROM artifacts WHERE video_id=$1 ORDER BY created_at DESC LIMIT 1",
            video_id,
        )
