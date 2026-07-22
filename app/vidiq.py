"""BYO vidIQ keyword-research adapter — real MCP client (Session 11 rebuild).

"Bring your own": the operator supplies their OWN vidIQ token in the
VIDIQ_API_KEY env var — the app never carries a key. The engine calls
``keyword_research()`` before building the pack; when the byo_vidiq flag is on
and a key is set, it returns live keyword rows (term + volume + competition +
overall score + estimated monthly search) that get injected into the pack
prompt so tags and the title come from real SEO data rather than the model's
priors.

CORRECTED 2026-07-23 (Session 11): vidIQ has NO plain REST API for
developers — the original design here (a bare HTTP GET against
VIDIQ_BASE_URL + VIDIQ_KEYWORDS_PATH) was built on a wrong assumption and
always 403'd (confirmed live, and via https://vidiq.com/mcp/). vidIQ only
exposes an MCP server at VIDIQ_MCP_URL (default https://mcp.vidiq.com/mcp),
speaking the standard MCP protocol over Streamable HTTP. Live-tested
2026-07-23: `Authorization: Bearer <VIDIQ_API_KEY>` works for a plain API key
(no OAuth dance needed) — confirmed with a real tools/list + a real
vidiq_keyword_research call returning live data. This module is a thin async
MCP client scoped to exactly that one tool call.

Design rule (unchanged from the original adapter): vidIQ is an ENHANCEMENT,
never a dependency. Every failure path — flag off, no key, timeout, MCP
error, unexpected response shape — returns None, and the caller falls
straight back to the pure-LLM path. A pack is never blocked or broken because
vidIQ was unreachable.
"""
import asyncio
import json
import logging

from app.config import flag, get_settings

log = logging.getLogger("dreamteam.vidiq")

MAX_ROWS = 20
CALL_TIMEOUT_S = 20
TOOL_NAME = "vidiq_keyword_research"


def _row(item: dict) -> dict:
    return {
        "keyword": str(item.get("keyword", "")).strip(),
        "volume": item.get("volume"),
        "competition": item.get("competition"),
        "score": item.get("overall"),
        "monthly_search": item.get("estimatedMonthlySearch"),
    }


def _rows_from_result(data: dict) -> list[dict]:
    """vidIQ's keyword_research shape: {seedKeyword: {...}, relatedKeywords: [...]}.

    Not a flat list — the seed term and its related terms are separate keys.
    Flatten both into one list, seed first, for format_for_prompt().
    """
    rows: list[dict] = []
    seed = data.get("seedKeyword")
    if isinstance(seed, dict) and str(seed.get("keyword", "")).strip():
        rows.append(_row(seed))
    for item in data.get("relatedKeywords") or []:
        if isinstance(item, dict) and str(item.get("keyword", "")).strip():
            rows.append(_row(item))
    return rows


async def _call_mcp(query: str, api_key: str, mcp_url: str) -> dict | None:
    """One MCP round-trip: initialize -> call vidiq_keyword_research -> parse.

    A fresh session per call (no persistent connection) — pack builds are
    infrequent enough (free-tier rate limits) that connection reuse is not
    worth the complexity of keeping an MCP session alive across requests.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {api_key}"}
    async with streamablehttp_client(mcp_url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                TOOL_NAME, {"keyword": query[:100], "mode": "research"}
            )
    if getattr(result, "isError", False):
        raise RuntimeError(f"vidIQ tool error: {result.content!r}")
    text = next((c.text for c in result.content if hasattr(c, "text")), None)
    if not text:
        return None
    return json.loads(text)


async def keyword_research(query: str) -> list[dict] | None:
    """Return live vidIQ keyword rows for ``query`` or None to fall back.

    None means "no vidIQ data — use the pure-LLM path". Never raises.
    """
    if not flag("byo_vidiq"):
        return None
    s = get_settings()
    if not (s.vidiq_api_key and query.strip()):
        return None
    try:
        data = await asyncio.wait_for(
            _call_mcp(query.strip(), s.vidiq_api_key, s.vidiq_mcp_url),
            timeout=CALL_TIMEOUT_S,
        )
        if not data:
            log.warning("vidiq MCP call returned no parseable content")
            return None
        rows = _rows_from_result(data)
        if not rows:
            log.warning("vidiq returned no usable rows for query=%r", query[:60])
            return None
        log.info("vidiq keywords query=%r rows=%d", query[:60], len(rows))
        return rows[:MAX_ROWS]
    except Exception as e:  # noqa: BLE001 — enhancement must never break a pack
        log.warning(
            "vidiq MCP lookup failed (%s: %s) — falling back to LLM",
            type(e).__name__, str(e)[:160],
        )
        return None


def format_for_prompt(rows: list[dict]) -> str:
    """Render vidIQ rows as a compact block the pack prompt can lean on."""
    lines = []
    for r in rows:
        parts = [r["keyword"]]
        if r.get("monthly_search") is not None:
            try:
                parts.append(f"~{int(r['monthly_search']):,}/mo")
            except (TypeError, ValueError):
                pass
        if r.get("volume") is not None:
            parts.append(f"vol={_fmt_num(r['volume'])}")
        if r.get("competition") is not None:
            parts.append(f"comp={_fmt_num(r['competition'])}")
        if r.get("score") is not None:
            parts.append(f"score={_fmt_num(r['score'])}")
        lines.append(" · ".join(str(p) for p in parts))
    return "\n".join(lines)


def _fmt_num(v) -> str:
    try:
        return f"{float(v):.0f}"
    except (TypeError, ValueError):
        return str(v)
