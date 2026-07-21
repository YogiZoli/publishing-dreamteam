"""LLM adapter — Gemini by default (GEMINI_API_KEY). Pure-LLM path also serves as
the fallback when no keyword-research provider is available."""
import json
import os

import httpx

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


class LLMError(Exception):
    pass


async def generate_json(prompt: str, model: str | None = None) -> dict:
    """Backwards-compatible wrapper: returns only the parsed JSON."""
    data, _usage = await generate_json_with_usage(prompt, model)
    return data


async def generate_json_with_usage(
    prompt: str, model: str | None = None
) -> tuple[dict, dict]:
    """Returns (parsed_json, usage). Usage carries Gemini's own token counts so
    per-artifact cost can be measured; empty dict if the API omits them."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise LLMError("GEMINI_API_KEY not configured")
    # NOTE: gemini-2.0-flash has a free-tier quota of 0, and gemini-2.5-flash
    # was retired for new users (404). gemini-3.5-flash is the verified default.
    model = model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            GEMINI_URL.format(model=model),
            params={"key": api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.7,
                },
            },
        )
    if r.status_code != 200:
        raise LLMError(f"Gemini error {r.status_code}: {r.text[:200]}")
    body = r.json()
    um = body.get("usageMetadata") or {}
    usage = {
        "model": model,
        "prompt_tokens": um.get("promptTokenCount", 0),
        "output_tokens": um.get("candidatesTokenCount", 0),
        "total_tokens": um.get("totalTokenCount", 0),
    }
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

Rules:
- title: under 60 chars, primary keyword in the first half, compelling promise, no clickbait words like hack/trick/easy.
- description_hook: 1 paragraph, first 150 chars must compel the click, no "in this video", no emojis.
- description_about: 2-3 sentences about the creator + subscribe CTA (generic, second person).
- tags: 25-35 comma-free tag strings ordered by SEO value (mix of exact keyword, variations, broader topics).
- hashtags: exactly 3, with # prefix.
- pinned_comment: a specific engagement question about the video's core promise.
- chapters: list of {{"time": "m:ss", "title": "..."}}. First MUST be 0:00 Introduction. 5-10 chapters,
  min 10s apart, benefit-driven titles. If transcript has no timing info, estimate at 130 wpm.
- thumbnail_prompts: exactly 3 distinct prompts for an image AI. EACH must include verbatim:
  "Use the attached profile photo as the main subject's face - preserve his exact likeness, do not
  alter facial features. The right side of the face faces forward and the right hand points to the left."
  Plus: 1280x720 16:9, bold 3-5 word overlay matching the title promise, high contrast, readable at
  120x68 px, one focal point, and a concrete scene derived from this video's topic (different per prompt).
- cards_endscreen: short manual instruction text for cards (at ~20% playlist card, ~70% latest-video card)
  and end screens (subscribe + latest video).

Return JSON with keys: title, description_hook, description_about, tags (array), hashtags (array),
pinned_comment, chapters (array), thumbnail_prompts (array of 3), cards_endscreen."""


LOCALIZE_PROMPT = """Translate this YouTube title and description hook into the {n} languages listed.
Title max 100 chars per language. Return strict JSON: an object keyed by language code, each value
{{"title": "...", "description": "..."}}.

Languages: {langs}
TITLE: {title}
DESCRIPTION: {description}"""
