"""Paid-tier YouTube write path (yt_write_path flag).

Incremental authorization, done the Google-recommended way: the FREE tier keeps
the plain non-sensitive Google Sign-In (openid/email/profile) from app.auth and
never sees a YouTube consent screen. Only when a PAID user clicks "Connect
YouTube channel" do we request the SENSITIVE youtube scopes — so the scary
"manage your YouTube account" consent, and the pre-verification unverified-app
warning, land only on the users who actually opted into the write feature, not
on every free signup.

What the write path does (Approval Mode — the user always confirms first):
  * videos.update  → title, description, tags, localizations (25 languages)
  * captions.insert → the cleaned SRT as a real caption track
All of it only ever touches the AUTHENTICATED user's OWN video — the API
enforces this (the token is theirs), and we additionally verify channel
ownership before writing, so the app can never write to someone else's video.

Only the long-lived refresh token is stored, Fernet-encrypted in Postgres.
Short-lived access tokens are minted on demand and never persisted.
"""
import json
import logging
import secrets

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import flag, get_settings
from app.db import get_pool

log = logging.getLogger("dreamteam.ytwrite")

router = APIRouter(tags=["yt_write"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_API = "https://www.googleapis.com/youtube/v3"
YT_UPLOAD = "https://www.googleapis.com/upload/youtube/v3"

# The two SENSITIVE scopes justified in the GCP verification submission.
# force-ssl carries the write capability (videos.update, captions.insert);
# readonly is used for the ownership/read step so BOTH sensitive scopes have a
# genuine, filmable usage in the verification demo video.
YT_SCOPES = (
    "openid email "
    "https://www.googleapis.com/auth/youtube.readonly "
    "https://www.googleapis.com/auth/youtube.force-ssl"
)


def _redirect_uri() -> str:
    return f"{get_settings().base_url}/yt/connect/callback"


# ---------------------------------------------------------------------------
# Fernet token store
# ---------------------------------------------------------------------------
def _fernet() -> Fernet:
    key = get_settings().fernet_key
    if not key:
        raise HTTPException(503, "Token encryption not configured (FERNET_KEY unset)")
    return Fernet(key.encode() if isinstance(key, str) else key)


async def _save_credentials(
    user_id: str, refresh_token: str, scopes: str, channel_id: str, channel_title: str
) -> None:
    enc = _fernet().encrypt(refresh_token.encode())
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO yt_credentials
                (user_id, encrypted_refresh_token, scopes, channel_id, channel_title, updated_at)
            VALUES ($1, $2, $3, $4, $5, now())
            ON CONFLICT (user_id) DO UPDATE SET
                encrypted_refresh_token = EXCLUDED.encrypted_refresh_token,
                scopes = EXCLUDED.scopes,
                channel_id = EXCLUDED.channel_id,
                channel_title = EXCLUDED.channel_title,
                updated_at = now()
            """,
            user_id, enc, scopes, channel_id, channel_title,
        )


async def _load_credentials(user_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT encrypted_refresh_token, scopes, channel_id, channel_title "
            "FROM yt_credentials WHERE user_id=$1",
            user_id,
        )
    if not row:
        return None
    try:
        refresh = _fernet().decrypt(bytes(row["encrypted_refresh_token"])).decode()
    except InvalidToken:
        log.error("yt refresh token failed to decrypt for user=%s", user_id)
        return None
    return {
        "refresh_token": refresh,
        "scopes": row["scopes"],
        "channel_id": row["channel_id"],
        "channel_title": row["channel_title"],
    }


async def disconnect(user_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM yt_credentials WHERE user_id=$1", user_id)


async def _access_token(user_id: str) -> tuple[str, dict]:
    """Mint a short-lived access token from the stored refresh token.

    Returns (access_token, credentials). Raises 409 if the channel is not
    connected and 401 if the refresh token has been revoked (user must
    reconnect). Access tokens are never stored — this runs per write.
    """
    creds = await _load_credentials(user_id)
    if not creds:
        raise HTTPException(409, "No YouTube channel connected")
    s = get_settings()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret,
                "refresh_token": creds["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
    if r.status_code != 200:
        log.warning("refresh_token exchange failed %s: %s", r.status_code, r.text[:200])
        # invalid_grant = user revoked access → force a reconnect.
        raise HTTPException(401, "YouTube authorization expired — please reconnect your channel")
    return r.json()["access_token"], creds


# ---------------------------------------------------------------------------
# OAuth connect flow — sensitive scopes, paid users only
# ---------------------------------------------------------------------------
def _require_paid_write(request: Request) -> str:
    """Gate: signed in + paid tier + write path on. Returns the user_id."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(401, "Not signed in")
    if not (flag("paid_tier") and flag("yt_write_path")):
        raise HTTPException(403, "YouTube publishing is a paid-tier feature")
    return user_id


@router.get("/yt/connect")
async def connect(request: Request):
    """Start the sensitive-scope consent. Only reachable by a paid, signed-in
    user — the free tier never lands here, so it never sees a YouTube consent."""
    user_id = _require_paid_write(request)
    s = get_settings()
    if not s.google_client_id:
        raise HTTPException(503, "Google OAuth not configured")
    state = secrets.token_urlsafe(24)
    request.session["yt_oauth_state"] = state
    # Where to send the user back after connecting (the artifact they came from).
    request.session["yt_return_to"] = request.query_params.get("return_to", "/app")
    params = httpx.QueryParams(
        client_id=s.google_client_id,
        redirect_uri=_redirect_uri(),
        response_type="code",
        scope=YT_SCOPES,
        state=state,
        # offline + consent = we get a refresh token (and re-get one on
        # reconnect). include_granted_scopes keeps the earlier openid/email
        # grant — this is textbook incremental authorization.
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{params}")


@router.get("/yt/connect/callback")
async def connect_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(401, "Not signed in")
    if error:
        raise HTTPException(400, f"YouTube auth error: {error}")
    if not code or state != request.session.pop("yt_oauth_state", None):
        raise HTTPException(400, "Invalid OAuth state")
    s = get_settings()

    async with httpx.AsyncClient(timeout=20) as client:
        tok = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret,
                "redirect_uri": _redirect_uri(),
                "grant_type": "authorization_code",
            },
        )
        if tok.status_code != 200:
            log.warning("yt code exchange failed %s: %s", tok.status_code, tok.text[:200])
            raise HTTPException(400, "Token exchange failed")
        payload = tok.json()
        refresh_token = payload.get("refresh_token")
        access_token = payload.get("access_token")
        granted_scopes = payload.get("scope", "")
        if not refresh_token:
            # No refresh token means Google remembered a prior consent and did
            # not re-issue one. prompt=consent should prevent this; if it still
            # happens, tell the user to remove the app at myaccount and retry.
            raise HTTPException(
                400,
                "Google did not return a refresh token. Remove this app under "
                "your Google Account → Security → Third-party access, then reconnect.",
            )
        # Read the connected channel (uses the readonly sensitive scope).
        ch = await client.get(
            f"{YT_API}/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    channel_id = channel_title = ""
    if ch.status_code == 200:
        items = ch.json().get("items", [])
        if items:
            channel_id = items[0]["id"]
            channel_title = items[0]["snippet"].get("title", "")

    await _save_credentials(user_id, refresh_token, granted_scopes, channel_id, channel_title)
    log.info("yt channel connected user=%s channel=%s", user_id, channel_id)
    return RedirectResponse(request.session.pop("yt_return_to", "/app"))


@router.get("/yt/status")
async def status(request: Request):
    """Tells the artifact page whether to show 'Connect' or the publish UI.

    Returns available:false unless both flags are on, so the free tier and any
    non-write build get a plain 'not available' and render nothing extra.
    """
    user_id = request.session.get("user_id")
    available = bool(user_id and flag("paid_tier") and flag("yt_write_path"))
    if not available:
        return {"available": False, "connected": False}
    creds = await _load_credentials(user_id)
    return {
        "available": True,
        "connected": bool(creds),
        "channel_title": creds["channel_title"] if creds else "",
    }


@router.post("/yt/disconnect")
async def disconnect_route(request: Request):
    user_id = _require_paid_write(request)
    await disconnect(user_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Field assembly — must match exactly what artifact.html shows in each box, so
# "Publish" writes precisely what the user reviewed and would have copy-pasted.
# ---------------------------------------------------------------------------
def build_youtube_description(pack: dict) -> str:
    lines = [pack.get("description_hook", "").strip(), "", "CHAPTERS"]
    for c in pack.get("chapters", []) or []:
        lines.append(f"{c.get('time', '')} {c.get('title', '')}".strip())
    about = pack.get("description_about", "").strip()
    if about:
        lines += ["", about]
    hashtags = pack.get("hashtags", []) or []
    if hashtags:
        lines += ["", " ".join(hashtags)]
    return "\n".join(lines).strip()


def _localizations_payload(pack: dict) -> dict:
    """The 25-language block in the {lang: {title, description}} shape the
    videos.update `localizations` part expects."""
    out = {}
    for code, loc in (pack.get("localizations") or {}).items():
        title = (loc.get("title") or "").strip()
        desc = (loc.get("description") or "").strip()
        if title or desc:
            out[code] = {"title": title, "description": desc}
    return out


# ---------------------------------------------------------------------------
# The writes
# ---------------------------------------------------------------------------
async def _fetch_video_snippet(client: httpx.AsyncClient, access_token: str, video_id: str) -> dict:
    r = await client.get(
        f"{YT_API}/videos",
        params={"part": "snippet,localizations", "id": video_id},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if r.status_code != 200:
        raise HTTPException(502, f"Could not read the video from YouTube ({r.status_code})")
    items = r.json().get("items", [])
    if not items:
        raise HTTPException(404, "Video not found on the connected channel")
    return items[0]


async def _publish_fields(user_id: str, pack: dict, fields: list[str]) -> dict:
    """Apply the requested fields to the user's own video. Approval Mode: the
    caller (UI) has already shown the user exactly what will be written."""
    access_token, creds = await _access_token(user_id)
    video_id = pack.get("video_id")
    if not video_id:
        raise HTTPException(400, "Artifact has no video id")
    results: dict[str, dict] = {}

    async with httpx.AsyncClient(timeout=30) as client:
        existing = await _fetch_video_snippet(client, access_token, video_id)
        snippet = dict(existing.get("snippet", {}))

        # Ownership guard (belt and braces — the token already scopes to the
        # user's channel, but never write to a video the connected channel does
        # not own).
        if creds.get("channel_id") and snippet.get("channelId") \
                and creds["channel_id"] != snippet["channelId"]:
            raise HTTPException(403, "That video does not belong to the connected channel")

        meta_fields = [f for f in fields if f in ("title", "description", "tags")]
        wants_loc = "localizations" in fields
        wants_caps = "captions" in fields

        if meta_fields or wants_loc:
            # videos.update replaces the snippet, so start from the existing one
            # (categoryId is required and must survive) and overlay only what
            # the user chose to publish.
            if "title" in meta_fields:
                snippet["title"] = pack.get("title", snippet.get("title", ""))[:100]
            if "description" in meta_fields:
                snippet["description"] = build_youtube_description(pack)
            if "tags" in meta_fields:
                snippet["tags"] = pack.get("tags", []) or []
            body: dict = {"id": video_id, "snippet": snippet}
            parts = "snippet"
            if wants_loc:
                loc = _localizations_payload(pack)
                if loc:
                    snippet.setdefault("defaultLanguage", "en")
                    body["localizations"] = loc
                    parts = "snippet,localizations"
            r = await client.put(
                f"{YT_API}/videos",
                params={"part": parts},
                headers={"Authorization": f"Bearer {access_token}"},
                json=body,
            )
            ok = r.status_code == 200
            msg = "Updated" if ok else f"Failed ({r.status_code}): {r.text[:160]}"
            for f in meta_fields:
                results[f] = {"ok": ok, "message": msg}
            if wants_loc:
                results["localizations"] = {"ok": ok, "message": msg}
            if not ok:
                log.warning("videos.update failed %s: %s", r.status_code, r.text[:200])

        if wants_caps:
            results["captions"] = await _insert_caption(client, access_token, video_id, pack)

        if "translated_captions" in fields:
            results["translated_captions"] = await _insert_translated_captions(
                client, access_token, video_id, pack
            )

    return results


# Human-readable names for the caption translation prompt (codes match engine.LOCALES).
LANG_NAMES = {
    "hu": "Hungarian", "de": "German", "fr": "French", "es": "Spanish", "pt": "Portuguese",
    "it": "Italian", "nl": "Dutch", "pl": "Polish", "ro": "Romanian", "cs": "Czech",
    "sk": "Slovak", "hr": "Croatian", "sr": "Serbian", "bg": "Bulgarian", "el": "Greek",
    "tr": "Turkish", "ru": "Russian", "uk": "Ukrainian", "ar": "Arabic", "hi": "Hindi",
    "id": "Indonesian", "vi": "Vietnamese", "th": "Thai", "ja": "Japanese", "ko": "Korean",
}


async def _upload_caption(
    client: httpx.AsyncClient, access_token: str, video_id: str,
    srt: str, language: str, name: str,
) -> dict:
    """Low-level captions.insert for ONE language via a hand-built
    multipart/related body (metadata JSON + SRT bytes). 409 = a track for that
    language already exists — reported, not fatal."""
    boundary = "----dreamteam" + secrets.token_hex(12)
    meta = {"snippet": {"videoId": video_id, "language": language, "name": name, "isDraft": False}}
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(meta)}\r\n"
        f"--{boundary}\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + srt.encode("utf-8") + f"\r\n--{boundary}--\r\n".encode()
    r = await client.post(
        f"{YT_UPLOAD}/captions",
        params={"part": "snippet", "uploadType": "multipart"},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        content=body,
    )
    if r.status_code in (200, 201):
        return {"ok": True, "message": "uploaded"}
    if r.status_code == 409:
        return {"ok": False, "message": f"a {language} caption track already exists"}
    log.warning("captions.insert %s failed %s: %s", language, r.status_code, r.text[:200])
    return {"ok": False, "message": f"failed ({r.status_code})"}


async def _insert_caption(
    client: httpx.AsyncClient, access_token: str, video_id: str, pack: dict
) -> dict:
    """The English cleaned-SRT track (the 'captions' publish field)."""
    srt = (pack.get("srt_en") or "").strip()
    if not srt:
        return {"ok": False, "message": "No caption file in this pack (paid SRT feature off?)"}
    res = await _upload_caption(client, access_token, video_id, srt, "en", "English (cleaned)")
    if res["ok"]:
        return {"ok": True, "message": "Caption track uploaded"}
    if "already exists" in res["message"]:
        return {"ok": False, "message": "An English caption track already exists on this video"}
    return {"ok": False, "message": res["message"]}


async def _insert_translated_captions(
    client: httpx.AsyncClient, access_token: str, video_id: str, pack: dict
) -> dict:
    """Translate the transcript into every localization language and upload one
    caption track per language. Each language is independent — a translation or
    upload failure skips that language and never blocks the rest. SLOW by nature
    (one LLM translation call per language), so it is its own opt-in field."""
    from app import llm, yt

    segments = pack.get("transcript_segments") or []
    if not segments:
        return {"ok": False, "message": "No transcript available to translate into captions"}
    codes = list((pack.get("localizations") or {}).keys()) or list(LANG_NAMES.keys())
    texts = [s["text"] for s in segments]
    ok = 0
    fails: list[str] = []
    for code in codes:
        name = LANG_NAMES.get(code, code)
        try:
            translated = await llm.generate_translated_captions(texts, name)
            tsegs = [{**s, "text": (t or "").strip()} for s, t in zip(segments, translated)]
            tsegs = [s for s in tsegs if s["text"]]
            if not tsegs:
                fails.append(code)
                continue
            srt = yt.transcript_to_srt(tsegs)
            res = await _upload_caption(client, access_token, video_id, srt, code, f"{name} (auto)")
            if res["ok"]:
                ok += 1
            else:
                fails.append(code)
        except Exception as e:  # noqa: BLE001 — one language must never break the rest
            log.warning("translated caption failed lang=%s: %s", code, str(e)[:160])
            fails.append(code)
    msg = f"{ok}/{len(codes)} language tracks uploaded"
    if fails:
        msg += f" (skipped: {', '.join(fails)})"
    return {"ok": ok > 0, "message": msg}


ALLOWED_FIELDS = {"title", "description", "tags", "localizations", "captions", "translated_captions"}


@router.post("/yt/publish")
async def publish(request: Request):
    """Approval-Mode write. Body: {artifact_id, fields:[...]}.

    Powers BOTH UI paths: the big 'Publish to YouTube' review screen sends every
    checked field at once; a single per-field 'Upload' button sends one field.
    Same endpoint, same ownership + flag guards, so there is one write path to
    reason about for the GCP review.
    """
    user_id = _require_paid_write(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Invalid request body")
    artifact_id = payload.get("artifact_id")
    fields = [f for f in (payload.get("fields") or []) if f in ALLOWED_FIELDS]
    if not artifact_id or not fields:
        raise HTTPException(400, "artifact_id and at least one valid field are required")

    from app import engine

    pack = await engine.get_pack(artifact_id, user_id)
    if not pack:
        raise HTTPException(404, "Artifact not found")

    results = await _publish_fields(user_id, pack, fields)
    any_ok = any(v.get("ok") for v in results.values())
    return JSONResponse({"ok": any_ok, "results": results})
