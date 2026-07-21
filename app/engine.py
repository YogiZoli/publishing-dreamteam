"""Artifact engine — orchestrates fetch → generate → localize → store."""
import json

from app import llm, yt
from app.config import flag
from app.db import get_pool

LOCALES = [
    "hu", "de", "fr", "es", "pt", "it", "nl", "pl", "ro", "cs", "sk", "hr",
    "sr", "bg", "el", "tr", "ru", "uk", "ar", "hi", "id", "vi", "th", "ja", "ko",
]


async def build_pack(video_id: str) -> dict:
    meta = await yt.fetch_metadata(video_id)
    segments = await yt.fetch_transcript(video_id)
    transcript_text = " ".join(s["text"] for s in segments)[:24000]

    calls: list[dict] = []

    pack, usage = await llm.generate_json_with_usage(
        llm.PACK_PROMPT.format(
            title=meta.get("title", "(unknown)"),
            channel=meta.get("channel", "(unknown)"),
            transcript=transcript_text or "(no transcript available)",
        )
    )
    calls.append({"step": "pack", **usage})

    # Enforce tag budget (440-470 effective chars, API quote-counting rule)
    tags = yt.trim_tags_to_budget([t.strip() for t in pack.get("tags", []) if t.strip()])
    pack["tags"] = tags
    pack["tags_effective_chars"] = yt.effective_tag_length(tags)

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

    pack["usage"] = {
        "calls": calls,
        "prompt_tokens": sum(c.get("prompt_tokens", 0) for c in calls),
        "output_tokens": sum(c.get("output_tokens", 0) for c in calls),
        "total_tokens": sum(c.get("total_tokens", 0) for c in calls),
    }

    pack["srt_en"] = yt.transcript_to_srt(segments) if segments else ""
    pack["chapters_estimated"] = not segments
    pack["video_id"] = video_id
    pack["source_title"] = meta.get("title", "")
    pack["source_channel"] = meta.get("channel", "")
    return pack


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
