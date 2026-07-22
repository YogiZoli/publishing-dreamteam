"""YouTube helpers: video-id parsing, metadata (oEmbed), timed transcript."""
import asyncio
import logging
import re

import httpx

log = logging.getLogger("dreamteam.yt")

_ID_PATTERNS = [
    r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})",
]


def extract_video_id(url: str) -> str | None:
    for pat in _ID_PATTERNS:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


async def fetch_metadata(video_id: str) -> dict:
    """Title/author via oEmbed (works for unlisted too)."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
        )
        if r.status_code != 200:
            return {}
        d = r.json()
        return {"title": d.get("title", ""), "channel": d.get("author_name", "")}


EN_LANGS = ("en", "en-US", "en-GB")


def _build_api():
    """Construct a YouTubeTranscriptApi honouring the TRANSCRIPT_EGRESS switch.

    YouTube blanket-blocks Railway's datacenter egress IP (RequestBlocked on
    the first request), so on production the caption fetch must go through a
    residential proxy. Locked against the installed youtube-transcript-api
    (1.2.x): proxies are configured with a GenericProxyConfig passed to the
    constructor — read from source, not memory.

    Only routes through a proxy when the transcript_proxy flag is on AND
    egress=proxy AND a URL is set; otherwise a plain (direct) client, which is
    exactly today's behaviour and works from any residential IP (dev, 'local').
    """
    from app.config import flag, get_settings
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api.proxies import GenericProxyConfig

    settings = get_settings()
    egress = (settings.transcript_egress or "none").lower()
    if flag("transcript_proxy") and egress == "proxy" and settings.transcript_proxy_url:
        url = settings.transcript_proxy_url
        return YouTubeTranscriptApi(
            proxy_config=GenericProxyConfig(http_url=url, https_url=url)
        )
    return YouTubeTranscriptApi()


def _fetch_transcript_sync(video_id: str) -> list[dict]:
    """Blocking fetch of YouTube's timed caption track (English only).

    IMPORTANT — the timestamps here are MEASURED against the audio, not
    estimated. Word accuracy of auto-generated captions is ~85-95% and it does
    fumble brand names, but the timing is real, which is all the chapter
    boundaries depend on. This is the whole reason chapters stopped being a
    "130 wpm" guess.

    Prefers YouTube's own auto-generated (ASR) English track — the "en-orig"
    true machine transcription the C0 brief calls for — and only falls back to
    a manually-created English track if no ASR one exists. English only,
    deliberately: translation is a paid-tier feature and this app never
    machine-translates on the free tier.
    """
    from youtube_transcript_api._errors import IpBlocked, RequestBlocked
    from app.config import flag, get_settings

    # A rotating residential proxy hands out a fresh exit IP per connection, and
    # a fraction of those IPs are already flagged by YouTube → IpBlocked on that
    # single attempt. A new api instance = a new tunnel = a new IP, so on the
    # proxied path we retry a few times rather than falling straight back to
    # estimated chapters. Direct (non-proxy) egress gets one shot as before.
    proxied = (
        flag("transcript_proxy")
        and (get_settings().transcript_egress or "").lower() == "proxy"
    )
    attempts = 3 if proxied else 1
    last_block = None
    for _ in range(attempts):
        api = _build_api()
        try:
            tlist = api.list(video_id)
        except (IpBlocked, RequestBlocked) as e:
            last_block = e  # flagged exit IP — rotate and retry
            continue
        try:
            transcript = tlist.find_generated_transcript(list(EN_LANGS))
        except Exception:
            # No ASR track — accept any English track rather than failing.
            transcript = tlist.find_transcript(list(EN_LANGS))
        try:
            fetched = transcript.fetch()
        except (IpBlocked, RequestBlocked) as e:
            last_block = e
            continue
        return [
            {
                "start": float(s.start),
                "dur": float(s.duration),
                "text": (s.text or "").replace("\n", " ").strip(),
            }
            for s in fetched
            if (s.text or "").strip()
        ]
    # Every attempt hit a blocked exit IP. Raise so fetch_transcript logs it and
    # degrades to estimated chapters; the async backfill will try again later.
    raise last_block if last_block else RuntimeError("transcript fetch failed")


async def fetch_transcript(video_id: str) -> list[dict]:
    """Returns [{start, dur, text}] or [] when captions are unavailable.

    Never raises: a missing transcript must degrade to estimated chapters
    rather than failing the whole pack. youtube-transcript-api is synchronous
    and does network I/O, so it runs in a thread to keep the event loop free
    for the SSE progress stream.
    """
    try:
        segments = await asyncio.to_thread(_fetch_transcript_sync, video_id)
        log.info("transcript video=%s segments=%d", video_id, len(segments))
        return segments
    except Exception as e:
        # RequestBlocked / IpBlocked here means YouTube flagged the egress IP
        # (a known risk on datacenter hosts like Railway). Logged explicitly so
        # the cause is obvious in `railway logs` rather than looking like a
        # video that simply has no captions.
        log.warning(
            "transcript unavailable video=%s: %s: %s",
            video_id, type(e).__name__, str(e)[:200],
        )
        return []


def format_transcript_for_prompt(segments: list[dict], limit: int = 60000) -> str:
    """Full-fidelity timestamped transcript for the LLM.

    Deliberately NOT downsampled or summarised: input tokens are cheap
    ($1.50/1M) and a 5-minute video is only ~4k characters, so there is no
    reason to trade away accuracy. Every line is prefixed with its real
    timestamp so the model can only ever copy a timestamp that exists.
    """
    lines = [f"[{seconds_to_stamp(s['start'])}] {s['text']}" for s in segments]
    return "\n".join(lines)[:limit]


def seconds_to_stamp(sec: float) -> str:
    total = int(sec)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def stamp_to_seconds(stamp: str) -> float | None:
    parts = str(stamp).strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def snap_chapters_to_segments(
    chapters: list[dict], segments: list[dict], min_gap: float = 10.0
) -> list[dict]:
    """Force every chapter time onto a timestamp that actually exists.

    An LLM will happily invent a plausible-looking timestamp even when the real
    ones are right there in the prompt, and a chapter that is 8 seconds off is
    visible to every viewer. So the model's job is reduced to CHOOSING a
    boundary and TITLING it; the actual second comes from the caption data.

    Also enforces YouTube's own rules, which silently disable chapters if
    broken: first chapter must be 0:00, ascending order, >=10s apart.
    """
    if not segments:
        return chapters
    starts = [s["start"] for s in segments]
    out: list[dict] = []
    for ch in chapters:
        secs = stamp_to_seconds(ch.get("time", ""))
        if secs is None:
            continue
        nearest = min(starts, key=lambda x: abs(x - secs))
        out.append({"time": seconds_to_stamp(nearest), "title": str(ch.get("title", "")).strip(),
                    "_sec": nearest})
    out.sort(key=lambda c: c["_sec"])

    deduped: list[dict] = []
    for ch in out:
        if deduped and ch["_sec"] - deduped[-1]["_sec"] < min_gap:
            continue  # too close together — YouTube would reject the whole list
        deduped.append(ch)

    if deduped:
        deduped[0]["_sec"] = 0.0
        deduped[0]["time"] = "0:00"
    return [{"time": c["time"], "title": c["title"]} for c in deduped]


def transcript_to_srt(segments: list[dict]) -> str:
    def ts(sec: float) -> str:
        h, rem = divmod(int(sec), 3600)
        m, s = divmod(rem, 60)
        ms = int((sec - int(sec)) * 1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"

    lines = []
    for i, seg in enumerate(segments, 1):
        end = seg["start"] + max(seg["dur"], 0.5)
        lines.append(f"{i}\n{ts(seg['start'])} --> {ts(end)}\n{seg['text']}\n")
    return "\n".join(lines)


def effective_tag_length(tags: list[str]) -> int:
    """YouTube API counts multi-word tags as quoted; separators count too."""
    total = sum(len(t) + (2 if " " in t else 0) for t in tags)
    total += max(len(tags) - 1, 0)  # separators
    return total


def trim_tags_to_budget(tags: list[str], budget: int = 470) -> list[str]:
    out: list[str] = []
    for t in tags:
        candidate = out + [t]
        if effective_tag_length(candidate) > budget:
            break
        out.append(t)
    return out
