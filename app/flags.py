"""Runtime feature flags: feature_flags table wins, env is the fallback.

Resolution order for every flag, highest priority first:

    1. a row in the feature_flags table
    2. the FLAG_<NAME> environment variable
    3. the hard-coded default in config.FEATURE_FLAGS

Why a background refresher instead of reading the DB per call: config.flag() is
called inside request handlers and inside the build pipeline, and it must stay
synchronous and instant. So a task refreshes an in-memory snapshot every
settings.flag_refresh_s seconds and writes it into config.FEATURE_FLAGS; every
existing call site keeps working untouched.

Failure behaviour is deliberately boring. If the DB is unreachable we keep the
last known snapshot and log a warning — flags never "flap" to off because of a
transient Neon hiccup, and a flag read can never raise.
"""
import asyncio
import logging

from app import config
from app.db import get_pool

log = logging.getLogger("dreamteam.flags")

_task: asyncio.Task | None = None


async def refresh() -> dict[str, bool]:
    """Pull the table and rewrite the effective snapshot. Never raises."""
    try:
        pool = await get_pool()
        rows = await pool.fetch("SELECT name, enabled FROM feature_flags")
    except Exception as e:  # noqa: BLE001 — a flag read must never break a request
        log.warning("flag refresh failed, keeping last snapshot: %s", e)
        return dict(config.FEATURE_FLAGS)

    db = {r["name"]: bool(r["enabled"]) for r in rows}
    for name in config.FEATURE_FLAGS:
        if name in db:
            config.FEATURE_FLAGS[name] = db[name]
            config.FLAG_SOURCES[name] = "db"
        else:
            # No row: fall back to the ORIGINAL env/default value, not to
            # whatever the DB happened to say last time round.
            config.FEATURE_FLAGS[name] = config.ENV_FLAGS[name]
            config.FLAG_SOURCES[name] = (
                "env" if name in config.ENV_FLAGS and _from_env(name) else "default"
            )
    unknown = set(db) - set(config.FEATURE_FLAGS)
    if unknown:
        # A row for a flag the code does not know about. Harmless, but it is
        # almost always a typo in an /admin/flags call, so make it visible.
        log.warning("feature_flags has unknown rows, ignored: %s", sorted(unknown))
    return dict(config.FEATURE_FLAGS)


def _from_env(name: str) -> bool:
    import os

    return os.getenv(f"FLAG_{name.upper()}") is not None


async def set_flag(name: str, enabled: bool) -> dict[str, bool]:
    """Upsert one flag and refresh immediately, so a write is live at once
    rather than up to flag_refresh_s later."""
    if name not in config.FEATURE_FLAGS:
        raise KeyError(name)
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO feature_flags (name, enabled, updated_at) VALUES ($1, $2, now()) "
        "ON CONFLICT (name) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()",
        name,
        enabled,
    )
    log.info("flag set name=%s enabled=%s", name, enabled)
    return await refresh()


async def clear_flag(name: str) -> dict[str, bool]:
    """Delete the row so the flag reverts to its env/default value."""
    if name not in config.FEATURE_FLAGS:
        raise KeyError(name)
    pool = await get_pool()
    await pool.execute("DELETE FROM feature_flags WHERE name = $1", name)
    log.info("flag cleared name=%s (reverts to env/default)", name)
    return await refresh()


def snapshot() -> dict[str, dict]:
    """Effective value of every flag plus where it came from — the debug view."""
    return {
        name: {"enabled": value, "source": config.FLAG_SOURCES.get(name, "default")}
        for name, value in sorted(config.FEATURE_FLAGS.items())
    }


async def _loop(interval_s: int) -> None:
    while True:
        await asyncio.sleep(interval_s)
        await refresh()


async def start() -> None:
    """Initial load + background refresher. Called from the app lifespan."""
    global _task
    await refresh()
    log.info("flags loaded: %s", snapshot())
    if _task is None:
        _task = asyncio.create_task(_loop(config.get_settings().flag_refresh_s))


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
