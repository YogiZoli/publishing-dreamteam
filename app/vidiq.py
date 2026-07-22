"""BYO vidIQ keyword-research adapter.

"Bring your own": the operator supplies their OWN vidIQ token in the
VIDIQ_API_KEY env var — the app never carries a key. The engine calls
``keyword_research()`` before building the pack; when the byo_vidiq flag is on
and a key is set, it returns live keyword rows (term + search volume +
competition + related terms) that get injected into the pack prompt so tags and
the title come from real SEO data rather than the model's priors.

Design rule (from the handover): vidIQ is an ENHANCEMENT, never a dependency.
Every failure path — flag off, no key, timeout, non-200, unexpected JSON shape —
returns None, and the caller falls straight back to the pure-LLM path. A pack is
never blocked or broken because vidIQ was unreachable. The endpoint, auth style
and path are all env-configurable, so if vidIQ changes their API it is a Railway
variable edit, not a redeploy.
"""
import logging
from urllib.parse import quote

import httpx

from app.config import flag, get_settings

log = logging.getLogger("dreamteam.vidiq")


def _auth_headers(style: str, key: str) -> dict:
    if style.lower() == "header":
        return {"x-api-key": key}
    return {"Authorization": f"Bearer {key}"}


def _coerce_rows(data) -> list[dict]:
    """Normalise assorted vidIQ response shapes into a flat list of dicts.

    vidIQ's payloads vary by endpoint/plan, so accept the common containers
    ({"keywords": [...]}, {"results": [...]}, {"data": [...]} or a bare list)
    and keep only the fields we use. Unknown shape -> empty list, which the
    caller treats as "no data" and falls back to the LLM.
    """
    if isinstance(data, dict):
        for k in ("keywords", "results", "data", "items"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
    if not isinstance(data, list):
        return []
    rows: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            # A bare list of strings is still useful as raw terms.
            if isinstance(item, str) and item.strip():
                rows.append({"keyword": item.strip()})
            continue
        term = (
            item.get("keyword")
            or item.get("term")
            or item.get("query")
            or item.get("text")
            or ""
        )
        if not str(term).strip():
            continue
        rows.append(
            {
                "keyword": str(term).strip(),
                "volume": item.get("volume") or item.get("search_volume"),
                "competition": item.get("competition") or item.get("difficulty"),
                "score": item.get("score") or item.get("overall"),
            }
        )
    return rows


async def keyword_research(query: str) -> list[dict] | None:
    """Return live vidIQ keyword rows for ``query`` or None to fall back.

    None means "no vidIQ data — use the pure-LLM path". Never raises.
    """
    if not flag("byo_vidiq"):
        return None
    s = get_settings()
    if not (s.vidiq_api_key and query.strip()):
        return None
    path = s.vidiq_keywords_path.replace("{q}", quote(query.strip()))
    url = s.vidiq_base_url.rstrip("/") + "/" + path.lstrip("/")
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(
                url, headers=_auth_headers(s.vidiq_auth_style, s.vidiq_api_key)
            )
        if r.status_code != 200:
            log.warning("vidiq non-200 status=%s body=%s", r.status_code, r.text[:160])
            return None
        rows = _coerce_rows(r.json())
        if not rows:
            log.warning("vidiq returned no usable rows for query=%r", query[:60])
            return None
        log.info("vidiq keywords query=%r rows=%d", query[:60], len(rows))
        return rows[:40]
    except Exception as e:  # noqa: BLE001 — enhancement must never break a pack
        log.warning("vidiq lookup failed (%s: %s) — falling back to LLM", type(e).__name__, str(e)[:160])
        return None


def format_for_prompt(rows: list[dict]) -> str:
    """Render vidIQ rows as a compact block the pack prompt can lean on."""
    lines = []
    for r in rows:
        parts = [r["keyword"]]
        if r.get("volume") is not None:
            parts.append(f"vol={r['volume']}")
        if r.get("competition") is not None:
            parts.append(f"comp={r['competition']}")
        if r.get("score") is not None:
            parts.append(f"score={r['score']}")
        lines.append(" · ".join(str(p) for p in parts))
    return "\n".join(lines)
