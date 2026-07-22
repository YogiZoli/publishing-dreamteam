"""Async chapter backfill — upgrade estimated chapters to real caption timing.

The pack returns instantly with ESTIMATED chapters because YouTube's auto-
captions are not ready the instant a video is uploaded (measured live: ~3 min
for a 54s clip, ~9m41s for a 12.5-min one). A background task retries the
caption fetch on a minute-scale schedule and, when captions land, snaps the
model's chosen boundaries onto real timestamps and rewrites every artifact for
that video.

Best-effort and in-memory by design: a redeploy drops any in-flight backfill,
which simply leaves the artifact on its estimated chapters — no worse than the
free tier already was. The safety net is the retry window, not persistence.

Gated by the caller behind the transcript_proxy flag: without a working egress
(dev residential IP, or a residential proxy on prod) the fetch cannot succeed,
so there is nothing to schedule.
"""
import asyncio
import json
import logging

from app import yt
from app.db import get_pool

log = logging.getLogger("dreamteam.backfill")

# Minute-scale retries (NOT the seconds-scale Gemini API backoff). This window
# comfortably covers full-length videos per the Session 7 live measurements;
# after the last attempt the chapters simply stay estimated.
RETRY_MINUTES = (2, 5, 10, 15, 20)

# One backfill per video at a time — a cache hit can store several artifacts for
# the same video_id, but one fetch upgrades them all.
_active: set[str] = set()


def schedule(video_id: str) -> None:
    """Fire-and-forget a backfill for one video. Deduped per video_id."""
    if video_id in _active:
        return
    _active.add(video_id)
    asyncio.create_task(_run(video_id))


async def _run(video_id: str) -> None:
    try:
        prev = 0
        for minute in RETRY_MINUTES:
            await asyncio.sleep((minute - prev) * 60)
            prev = minute
            segments = await yt.fetch_transcript(video_id)
            if segments:
                n = await _upgrade(video_id, segments)
                log.info(
                    "backfill upgraded video=%s artifacts=%d after ~%dm",
                    video_id, n, minute,
                )
                return
            log.info("backfill: no captions yet video=%s (~%dm)", video_id, minute)
        log.info(
            "backfill gave up video=%s after %dm — chapters stay estimated",
            video_id, RETRY_MINUTES[-1],
        )
    except Exception:  # noqa: BLE001 — a backfill must never crash the app
        log.exception("backfill failed video=%s", video_id)
    finally:
        _active.discard(video_id)


async def _upgrade(video_id: str, segments: list[dict]) -> int:
    """Re-snap chapters onto real timing for every ESTIMATED artifact of this
    video, and store the timed segments for paid-tier translation reuse.

    Uses the stored chapters as the model input: on an estimated pack
    snap_chapters_to_segments returned them untouched, so they still carry the
    model's original boundaries — exactly what needs snapping onto real caption
    starts now.
    """
    pool = await get_pool()
    upgraded = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, payload FROM artifacts WHERE video_id = $1", video_id
        )
        for row in rows:
            pack = json.loads(row["payload"])
            if not pack.get("chapters_estimated"):
                continue  # already upgraded (or was real from the start)
            pack["chapters"] = yt.snap_chapters_to_segments(
                pack.get("chapters") or [], segments
            )
            pack["chapters_estimated"] = False
            pack["transcript_segments"] = segments
            pack["transcript_source"] = "youtube_captions_backfill"
            await conn.execute(
                "UPDATE artifacts SET payload = $2 WHERE id = $1",
                row["id"], json.dumps(pack),
            )
            upgraded += 1
    return upgraded
