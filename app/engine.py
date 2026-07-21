"""Artifact engine — orchestrates fetch → generate → localize → store."""
import json
import logging
import statistics
import time

from app import llm, yt

from app.config import flag
from app.db import get_pool

log = logging.getLogger("dreamteam.engine")

# Used as the progress ETA until the DB has enough real runs to median over.
DEFAULT_ETA_MS = 75_000

LOCALES = [
    "hu", "de", "fr", "es", "pt", "it", "nl", "pl", "ro", "cs", "sk", "hr",
    "sr", "bg", "el", "tr", "ru", "uk", "ar", "hi", "id", "vi", "th", "ja", "ko",
]


class _NullReporter:
    """No-op progress sink so build_pack still works without a job attached."""

    timings: dict = {}

    def start(self, key): pass
    def done(self, key): pass
    def field(self, name, preview): pass
    def note(self, message, extra_ms=0): pass


async def build_pack(video_id: str, report=None) -> dict:
    report = report or _NullReporter()
    t_pack0 = time.monotonic()

    report.start("metadata")
    meta = await yt.fetch_metadata(video_id)
    report.field("source_title", meta.get("title", ""))
    report.done("metadata")

    report.start("transcript")
    segments = await yt.fetch_transcript(video_id)
    transcript_text = " ".join(s["text"] for s in segments)[:24000]
    report.field(
        "transcript",
        f"{len(segments)} caption segments" if segments else "no transcript — chapters will be estimated",
    )
    report.done("transcript")

    calls: list[dict] = []

    report.start("llm")

    def _on_retry(attempt, delay, status, model_name):
        # Gemini capacity spikes (503) are routine. Tell the user we are still
        # working and extend the ETA, rather than letting the bar look stuck.
        report.note(
            f"AI engine busy — retrying in {int(delay)}s (attempt {attempt + 1})",
            extra_ms=int(delay * 1000) + 5000,
        )

    pack, usage = await llm.generate_json_with_usage(
        llm.PACK_PROMPT.format(
            title=meta.get("title", "(unknown)"),
            channel=meta.get("channel", "(unknown)"),
            transcript=transcript_text or "(no transcript available)",
        ),
        on_retry=_on_retry,
    )
    calls.append({"step": "pack", **usage})
    report.done("llm")

    # Push the finished pieces one by one so the preview fills in live rather
    # than appearing all at once at the very end.
    for name in ("title", "description_hook", "hashtags", "pinned_comment",
                 "chapters", "thumbnail_prompts"):
        if pack.get(name):
            report.field(name, pack[name])

    report.start("tags")
    # Enforce tag budget (440-470 effective chars, API quote-counting rule)
    tags = yt.trim_tags_to_budget([t.strip() for t in pack.get("tags", []) if t.strip()])
    pack["tags"] = tags
    pack["tags_effective_chars"] = yt.effective_tag_length(tags)
    report.field("tags", tags)
    report.done("tags")

    # Localizations (25 locales) — one LLM call. Paid-tier only: gated behind
    # the "localization" flag so the free tier does not spend tokens on it.
    pack["localizations"] = {}
    if flag("localization"):
        try:
            pack["localizations"], loc_usage = await llm.generate_json_with_usage(
                llm.LOCALIZE_PROMPT.format(
                    n=len(LOCALES),
                    langs=", ".join(LOCALES),
                    title=pack.get("title", meta.get("title", "")),
                    description=pack.get("description_hook", ""),
                )
            )
            calls.append({"step": "localization", **loc_usage})
        except llm.LLMError:
            pack["localizations"] = {}

    report.start("srt")
    pack["srt_en"] = yt.transcript_to_srt(segments) if segments else ""
    pack["chapters_estimated"] = not segments
    report.field("srt_en", f"{len(pack['srt_en'])} chars" if pack["srt_en"] else "not available")
    report.done("srt")

    prompt_t = sum(c.get("prompt_tokens", 0) for c in calls)
    output_t = sum(c.get("output_tokens", 0) for c in calls)
    thoughts_t = sum(c.get("thoughts_tokens", 0) for c in calls)
    total_t = sum(c.get("total_tokens", 0) for c in calls)
    duration_ms = int((time.monotonic() - t_pack0) * 1000)

    pack["usage"] = {
        "calls": calls,
        "prompt_tokens": prompt_t,
        "output_tokens": output_t,
        # Billed at the output rate but reported separately by Gemini; without
        # this, prompt + output does not reconcile with total on a 3.x model.
        "thoughts_tokens": thoughts_t,
        "billable_output_tokens": output_t + thoughts_t,
        "total_tokens": total_t,
        "unaccounted_tokens": total_t - (prompt_t + output_t + thoughts_t),
        # Wall-clock timings feed the data-driven ETA on the next run.
        "duration_ms": duration_ms,
        "step_ms": dict(getattr(report, "timings", {})),
    }
    log.info(
        "pack built video=%s duration_ms=%d prompt=%d thoughts=%d output=%d total=%d steps=%s",
        video_id, duration_ms, prompt_t, thoughts_t, output_t, total_t,
        pack["usage"]["step_ms"],
    )

    pack["video_id"] = video_id
    pack["source_title"] = meta.get("title", "")
    pack["source_channel"] = meta.get("channel", "")
    return pack


async def median_duration_ms(sample: int = 20) -> int:
    """Data-driven ETA: median wall-clock of the last N real builds.

    Better than a hardcoded guess because it adapts as Gemini's latency,
    transcript sizes and prompt length change. Median (not mean) so one
    pathological 120s timeout does not skew every future estimate.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT (payload->'usage'->>'duration_ms')::int AS d FROM artifacts "
                "WHERE payload->'usage'->>'duration_ms' IS NOT NULL "
                "ORDER BY created_at DESC LIMIT $1",
                sample,
            )
        vals = [r["d"] for r in rows if r["d"]]
        if len(vals) >= 3:
            # +15% headroom so the bar rarely stalls at 99%.
            return int(statistics.median(vals) * 1.15)
    except Exception as e:  # never let telemetry break a build
        log.warning("median_duration_ms failed, using default: %s", e)
    return DEFAULT_ETA_MS


async def store_pack(user_id: str | None, video_id: str, pack: dict) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row_id = await conn.fetchval(
            "INSERT INTO artifacts (user_id, video_id, payload) VALUES ($1, $2, $3) RETURNING id",
            user_id,
            video_id,
            json.dumps(pack),
        )
    return str(row_id)


async def get_pack(artifact_id: str, user_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payload FROM artifacts WHERE id=$1 AND user_id=$2", artifact_id, user_id
        )
    return json.loads(row["payload"]) if row else None
