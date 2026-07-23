"""LLM adapter — Gemini by default (GEMINI_API_KEY). Pure-LLM path also serves as
the fallback when no keyword-research provider is available."""
import asyncio
import json
import logging
import os
import random
import time

import httpx

log = logging.getLogger("dreamteam.llm")

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

# Transient conditions worth retrying. 503 UNAVAILABLE ("model is currently
# experiencing high demand") hit production on 2026-07-21 and killed the whole
# build on the first try — Gemini capacity spikes are normal and short-lived.
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4

# Fallback CHAIN, tried in order after the pinned model exhausts its retries.
#
# Hard lesson (2026-07-21): "gemini-flash-latest" was useless as a fallback
# because it is an ALIAS that resolves to the same model as gemini-3.5-flash —
# it returned the identical 503 from the identical overloaded capacity pool.
# Never put a *-latest alias in this chain. Every entry must be a distinct,
# explicitly-versioned model, verified with a live probe.
#
# Probed 2026-07-21 while 3.5-flash was 503: all three below returned 200 and
# produced correctly-shaped nested JSON. The lite models emit zero thinking
# tokens, so they are also markedly cheaper if we ever need a cost lever.
DEFAULT_FALLBACKS = "gemini-3-flash-preview,gemini-3.5-flash-lite,gemini-3.1-flash-lite"
FALLBACK_MODELS = [
    m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", DEFAULT_FALLBACKS).split(",")
    if m.strip()
]


class LLMError(Exception):
    pass


async def generate_json(prompt: str, model: str | None = None) -> dict:
    """Backwards-compatible wrapper: returns only the parsed JSON."""
    data, _usage = await generate_json_with_usage(prompt, model)
    return data


async def _post_with_retry(api_key: str, model: str, prompt: str, on_retry=None):
    """POST to Gemini, retrying transient failures, then falling back to a
    second model. Returns (response_body, model_actually_used).

    `on_retry(attempt, delay_s, status, model)` lets the caller surface "retrying
    in Ns" in the UI instead of leaving the progress bar looking frozen.
    """
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7},
    }
    candidates = [model] + [m for m in FALLBACK_MODELS if m != model]
    last_err = "no attempt made"

    async with httpx.AsyncClient(timeout=120) as client:
        for m_idx, m in enumerate(candidates):
            # Full retry budget for the pinned model; one shot each for the
            # fallbacks. A fallback that 503s once is genuinely busy too, and
            # trying the NEXT distinct model beats hammering this one — that is
            # the whole point of a chain. Keeps worst case bounded.
            attempts = MAX_ATTEMPTS if m_idx == 0 else 1
            for attempt in range(1, attempts + 1):
                status = 0
                try:
                    r = await client.post(
                        GEMINI_URL.format(model=m), params={"key": api_key}, json=payload
                    )
                    status = r.status_code
                    if status == 200:
                        if m != model:
                            log.warning("gemini fell back to %s after %s failed", m, model)
                        return r.json(), m
                    last_err = f"Gemini error {status}: {r.text[:200]}"
                except httpx.RequestError as e:
                    last_err = f"Gemini network error: {e}"

                # Non-retryable (400 bad request, 404 retired model, 403 bad key)
                # — no point burning the budget, move to the next model.
                if status and status not in RETRY_STATUSES:
                    log.error("gemini non-retryable on %s: %s", m, last_err)
                    break
                if attempt == attempts:
                    break
                delay = min(2 ** (attempt - 1) * 2, 16) + random.uniform(0, 1.5)
                log.warning(
                    "gemini %s attempt %d/%d failed (status=%s), retrying in %.1fs: %s",
                    m, attempt, attempts, status, delay, last_err[:120],
                )
                if on_retry:
                    on_retry(attempt, delay, status, m)
                await asyncio.sleep(delay)

    raise LLMError(f"{last_err} (all retries and fallbacks exhausted)")


async def generate_json_with_usage(
    prompt: str, model: str | None = None, on_retry=None
) -> tuple[dict, dict]:
    """Returns (parsed_json, usage). Usage carries Gemini's own token counts so
    per-artifact cost can be measured; empty dict if the API omits them."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise LLMError("GEMINI_API_KEY not configured")
    # NOTE: gemini-2.0-flash has a free-tier quota of 0, and gemini-2.5-flash
    # was retired for new users (404). gemini-3.5-flash is the verified default.
    model = model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    t0 = time.monotonic()
    body, model = await _post_with_retry(api_key, model, prompt, on_retry)
    um = body.get("usageMetadata") or {}
    # thoughtsTokenCount is the reasoning Gemini 3.x does before it answers. It
    # is NOT included in candidatesTokenCount but IS included in totalTokenCount
    # and IS billed at the output rate — which is why prompt + output never adds
    # up to total on a thinking model. Measured 2026-07-21: 497 + 1126 = 1623,
    # total 3514, the missing 1891 being thoughts. Always account for it.
    usage = {
        "model": model,
        "prompt_tokens": um.get("promptTokenCount", 0),
        "output_tokens": um.get("candidatesTokenCount", 0),
        "thoughts_tokens": um.get("thoughtsTokenCount", 0),
        "cached_tokens": um.get("cachedContentTokenCount", 0),
        "total_tokens": um.get("totalTokenCount", 0),
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }
    # Structured, greppable cost line — this is what shows up in `railway logs`.
    log.info(
        "gemini usage model=%s prompt=%d thoughts=%d output=%d cached=%d total=%d billable_out=%d duration_ms=%d",
        model,
        usage["prompt_tokens"],
        usage["thoughts_tokens"],
        usage["output_tokens"],
        usage["cached_tokens"],
        usage["total_tokens"],
        usage["thoughts_tokens"] + usage["output_tokens"],
        usage["duration_ms"],
    )
    try:
        # Gemini 3.x thinking models can emit non-text parts (thoughtSignature
        # only) before the answer, so pick the first part that actually has text.
        parts = body["candidates"][0]["content"]["parts"]
        text = next(p["text"] for p in parts if p.get("text"))
        return json.loads(text), usage
    except (KeyError, IndexError, StopIteration, json.JSONDecodeError) as e:
        raise LLMError(f"Bad LLM response: {e}")


PACK_PROMPT = """You are an expert YouTube publishing strategist. Using the video data below,
produce a complete publishing pack as strict JSON.

VIDEO TITLE (original): {title}
CHANNEL: {channel}
TRANSCRIPT (may be empty): {transcript}
{keyword_data}
Rules:
- title: under 60 chars, primary keyword in the first half, compelling promise, no clickbait words like hack/trick/easy.
- description_hook: 1 paragraph, first 150 chars must compel the click, no "in this video", no emojis.
- description_about: 2-3 sentences about the creator + subscribe CTA (generic, second person).
- tags: 25-35 comma-free tag strings ordered by SEO value (mix of exact keyword, variations, broader topics).
  If a "KEYWORD RESEARCH" block is present above, treat it as authoritative live SEO data: prioritise its
  high-volume / low-competition terms in the tags AND weave the strongest one into the title verbatim.
- hashtags: exactly 3, with # prefix.
- pinned_comment: a specific engagement question about the video's core promise.
- chapters: list of {{"time": "m:ss", "title": "..."}}. First MUST be 0:00 Introduction. 5-10 chapters,
  min 10s apart, benefit-driven titles.
  TIMESTAMP RULE: the transcript above is prefixed with real timestamps in [m:ss] form. You MUST copy
  a timestamp VERBATIM from those brackets — pick the line where the topic actually changes. Never
  compute, round, average or invent a time. If (and only if) the transcript is empty, estimate at 130 wpm.
- thumbnail_prompts: exactly {n_thumbs} distinct prompt(s) for an image AI. EACH must include verbatim:
  "Use the attached profile photo as the main subject's face - preserve his exact likeness, do not
  alter facial features. The right side of the face faces forward and the right hand points to the left."
  Plus: 1280x720 16:9, bold 3-5 word overlay matching the title promise, high contrast, readable at
  120x68 px, one focal point, and a concrete scene derived from this video's topic.
  A/B RULE: when more than one prompt is requested, they are competing A/B variants — make each a
  GENUINELY different testable bet, not a cosmetic tweak. Vary the lever deliberately: variant 1 =
  face/emotion-led close-up, variant 2 = object/result-led scene, variant 3 = text/curiosity-gap led.
  Each must still preserve the exact likeness sentence above and target the same video promise, so the
  only thing under test is the visual angle. Prefix each prompt's overlay idea so the operator can tell
  the variants apart at a glance.
- cards: exactly 2 objects, each {{"time": "m:ss", "element": "Video|Playlist|Channel", "note": "..."}}.
  Decide each card individually from THIS video's actual content — do NOT use a fixed template. Read the
  transcript and pick the two specific moments where it is most natural to point the viewer to more of the
  creator's own content, and explain in "note" why that moment fits. "note" MUST instruct the user to link
  ONLY their own video / playlist / channel — this app cannot know their URLs, so never invent or suggest
  any external or third-party link. "time" must be a real moment taken from the transcript timestamps.
- end_screen: exactly 2 objects, each {{"time": "m:ss", "element": "Video|Playlist|Channel|Subscribe", "note": "..."}}.
  Placed in the final 5-20 seconds of the video. Choose the two elements that best fit THIS video's ending
  and call-to-action (again decided from the content, not a template). Any Video/Playlist/Channel element
  refers to the user's OWN content only; never an external link.

Return JSON with keys: title, description_hook, description_about, tags (array), hashtags (array),
pinned_comment, chapters (array), thumbnail_prompts (array of {n_thumbs}), cards (array of 2),
end_screen (array of 2)."""


CLEAN_SRT_PROMPT = """You are cleaning up a raw YouTube auto-caption transcript.
Below is a JSON array of caption lines, in order. Each is raw ASR text: missing
punctuation, wrong casing, filler words ("um", "uh", "you know"), false starts
and occasional mis-heard words.

Return STRICT JSON: an object {{"lines": [...]}} whose "lines" array has EXACTLY
{n} strings, one per input line, in the SAME ORDER. For each line:
- fix punctuation, capitalisation and obvious grammar,
- remove filler words and stutters,
- correct clear mis-hearings only when unambiguous from context,
- DO NOT merge, split, reorder, add or drop lines — the count MUST stay {n},
- DO NOT translate; keep the original language,
- if a line is only filler, return it as an empty string "" (keep the slot).

Raw lines:
{lines}"""


# Retry model for cleaned-SRT count mismatches (2026-07-23 live test finding).
# The pinned pack model (gemini-3.5-flash-lite, no thinking) failed to hold an
# exact 1:1 line count on 2/2 live tests (72-line transcript -> 40, then 69
# lines back) -- it is fast for the pack but not reliable at this rigid a
# task. Rather than re-pin GEMINI_MODEL globally (which would undo the
# Session 6 latency win for every pack, not just this one paid-tier field),
# retry ONCE with a distinct, explicitly-versioned "thinking" model that holds
# instructions more reliably. Never a *-latest alias -- see the fallback-chain
# note above for why that already bit this project twice. Overridable via env
# so a future model retirement is a Railway var edit, not a redeploy.
CLEAN_SRT_RETRY_MODEL = os.getenv("CLEAN_SRT_RETRY_MODEL", "gemini-3.6-flash")


async def generate_cleaned_captions(texts: list[str]) -> list[str]:
    """LLM-clean a list of caption texts, preserving order and count.

    The timing (start/dur) is handled by the caller and never touched here — we
    only rewrite text. Returns a list of the SAME length as ``texts``. Tries the
    default pinned model first; on a count mismatch (not a network/HTTP error --
    the call succeeded, the shape is just wrong) retries once against
    CLEAN_SRT_RETRY_MODEL before giving up. Raises LLMError only if BOTH
    attempts mismatch, so the caller can fall back to the raw SRT rather than
    shipping misaligned captions.
    """
    if not texts:
        return []
    payload = json.dumps(texts, ensure_ascii=False)
    prompt = CLEAN_SRT_PROMPT.format(n=len(texts), lines=payload)

    def _extract(data: dict) -> list[str] | None:
        lines = data.get("lines")
        if isinstance(lines, list) and len(lines) == len(texts):
            return [str(x) for x in lines]
        return None

    data, _usage = await generate_json_with_usage(prompt)
    lines = _extract(data)
    if lines is not None:
        return lines

    got = data.get("lines")
    log.warning(
        "cleaned caption count mismatch on primary model: got %s, expected %d "
        "— retrying once with %s",
        len(got) if isinstance(got, list) else "n/a", len(texts), CLEAN_SRT_RETRY_MODEL,
    )
    data2, _usage2 = await generate_json_with_usage(prompt, model=CLEAN_SRT_RETRY_MODEL)
    lines = _extract(data2)
    if lines is not None:
        return lines

    got2 = data2.get("lines")
    raise LLMError(
        f"cleaned caption count mismatch on both models (primary + {CLEAN_SRT_RETRY_MODEL}): "
        f"got {len(got2) if isinstance(got2, list) else 'n/a'}, expected {len(texts)}"
    )


TRANSLATE_SRT_PROMPT = """Translate this JSON array of YouTube caption lines into {lang}.
Return STRICT JSON: an object {{"lines": [...]}} whose "lines" array has EXACTLY {n} strings,
one per input line, in the SAME ORDER. Translate naturally and idiomatically (not word-for-word).
DO NOT merge, split, reorder, add or drop lines — the count MUST stay {n}. If an input line is
only filler, return it as an empty string "" (keep the slot).

Lines:
{lines}"""


# Caption TRANSLATION runs on a stronger "thinking" model by default, not the
# fast lite pack model: holding an EXACT 1:1 line count across a translation is
# the same rigid-count task that the lite model slipped on for cleaned SRT, and
# it slips more often per-language on translation. gemini-3.6-flash holds it far
# better. Env-overridable; never a *-latest alias (see the fallback-chain note).
TRANSLATE_CAPTIONS_MODEL = os.getenv("TRANSLATE_CAPTIONS_MODEL", "gemini-3.6-flash")


async def generate_translated_captions(
    texts: list[str], lang_name: str, model: str | None = None
) -> list[str]:
    """Translate caption texts into one language, preserving order and count.

    Timing is the caller's job — this only rewrites text, 1:1 with the input, so
    the existing start/dur are reused verbatim for the translated SRT. Runs on
    the stronger TRANSLATE_CAPTIONS_MODEL and re-rolls it TWICE on a count
    mismatch (the call succeeded, the shape is just wrong — a fresh roll usually
    lands), then raises so the caller can skip that one language rather than
    upload a misaligned track."""
    if not texts:
        return []
    model = model or TRANSLATE_CAPTIONS_MODEL
    payload = json.dumps(texts, ensure_ascii=False)
    prompt = TRANSLATE_SRT_PROMPT.format(lang=lang_name, n=len(texts), lines=payload)

    def _extract(data: dict) -> list[str] | None:
        lines = data.get("lines")
        if isinstance(lines, list) and len(lines) == len(texts):
            return [str(x) for x in lines]
        return None

    for _attempt in range(2):
        data, _u = await generate_json_with_usage(prompt, model=model)
        lines = _extract(data)
        if lines is not None:
            return lines
    raise LLMError(f"translated caption count mismatch for {lang_name}")


LOCALIZE_PROMPT = """Translate this YouTube title and description into ALL {n} languages listed — no omissions.
Title max 100 chars per language; keep the primary keyword. Translate the description in full (all
paragraphs), natural and idiomatic, not word-for-word. Return STRICT JSON: an object keyed by the
EXACT language codes given below (one key per code, all {n} present), each value
{{"title": "...", "description": "..."}}.

Languages: {langs}
TITLE: {title}
DESCRIPTION: {description}"""
