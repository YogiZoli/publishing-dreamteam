"""YouTube helpers: video-id parsing, metadata (oEmbed), transcript (timedtext)."""
import re
import xml.etree.ElementTree as ET

import httpx

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


async def fetch_transcript(video_id: str) -> list[dict]:
    """Best-effort captions via the public timedtext endpoint (EN).

    Returns [{start: float, dur: float, text: str}] or [] when unavailable.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        for lang in ("en", "en-US", "en-GB"):
            r = await client.get(
                "https://video.google.com/timedtext",
                params={"lang": lang, "v": video_id},
            )
            if r.status_code == 200 and r.text.strip():
                try:
                    root = ET.fromstring(r.text)
                except ET.ParseError:
                    continue
                out = []
                for el in root.iter("text"):
                    out.append(
                        {
                            "start": float(el.get("start", 0)),
                            "dur": float(el.get("dur", 0)),
                            "text": (el.text or "").replace("&#39;", "'").strip(),
                        }
                    )
                if out:
                    return out
    return []


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
