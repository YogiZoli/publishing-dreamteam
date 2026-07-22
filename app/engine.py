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
    # Timestamped, full-fidelity. The [m:ss] prefixes are what make the chapter
    # times real instead of guessed — the model can only copy a time that is
    # physically present in the prompt, and snap_chapters_to_segments then
    # enforces that regardless of what it returns.
    transcript_text = yt.format_transcript_for_prompt(segments)
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

    # Free tier ships ONE thumbnail prompt; the paid tier gets 3 for A/B
    # testing. One variable, flag-driven, so the count is instantly reversible.
    n_thumbs = 3 if flag("paid_tier") else 1
    pack, usage = await llm.generate_json_with_usage(
        llm.PACK_PROMPT.format(
            title=meta.get("title", "(unknown)"),
            channel=meta.get("channel", "(unknown)"),
            transcript=transcript_text or "(no transcript available)",
            n_thumbs=n_thumbs,
        ),
        on_retry=_on_retry,
    )
    calls.append({"step": "pack", **usage})

    # Chapter times come from the caption data, never from the model. See
    # yt.snap_chapters_to_segments — LLMs invent plausible timestamps even when
    # the real ones are in front of them, and an off-by-8s chapter is visible
    # to every viewer.
    raw_chapters = pack.get("chapters") or []
    pack["chapters"] = yt.snap_chapters_to_segments(raw_chapters, segments)
    pack["chapters_estimated"] = not segments

    # Defensive normalisation: hold the model to the shapes the artifact expects.
    # Thumbnails are capped to the tier's count; cards/end_screen are always
    # lists so the template can iterate them without a type guard.
    thumbs = pack.get("thumbnail_prompts") or []
    pack["thumbnail_prompts"] = thumbs[:n_thumbs] if isinstance(thumbs, list) else []
    if not isinstance(pack.get("cards"), list):
        pack["cards"] = []
    if not isinstance(pack.get("end_screen"), list):
        pack["end_screen"] = []
    report.done("llm")

    # Push the finished pieces one by one so the preview fills in live rather
    # than appearing all at once at the very end.
    for name in ("title", "description_hook", "hashtags", "pinned_comment",
                 "chapters", "thumbnail_prompts", "cards", "end_screen"):
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
    # The timed segments are ALWAYS stored: chapters are built from them, and
    # they are the raw material for paid-tier translation — text and timing
    # kept separate so a translation only has to touch the text and can reuse
    # the timing verbatim, 25 times over.
    pack["transcript_segments"] = segments
    pack["transcript_source"] = "youtube_captions" if segments else "none"

    # Raw ASR SRT is deliberately NOT shipped on the free tier. The user's video
    # already carries these same auto-captions, so handing back a copy adds
    # nothing — and if they upload it, YouTube stops labelling it as automatic,
    # so every misheard brand name becomes *their* published caption. Cleaned-up
    # SRT is a paid feature (it needs a full-text LLM rewrite, billed at the
    # output rate, which roughly doubles the cost of a pack).
    pack["srt_en"] = yt.transcript_to_srt(segments) if (segments and flag("srt_output")) else ""
    # Only announce the field when it actually ships. Otherwise the progress
    # stream would advertise a "paid feature" that does not exist yet, and
    # artifact.html already hides the card via {% if pack.srt_en %}.
    if pack["srt_en"]:
        report.field("srt_en", f"{len(pack['srt_en'])} chars")
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
