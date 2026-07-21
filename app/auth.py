"""Google Sign-In (OpenID Connect) — non-sensitive scopes only: openid email profile."""
import secrets

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.db import get_pool

router = APIRouter(prefix="/auth/google", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SCOPES = "openid email profile"


@router.get("/login")
async def login(request: Request):
    settings = get_settings()
    if not settings.google_client_id:
        raise HTTPException(503, "Google Sign-In not configured")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    params = httpx.QueryParams(
        client_id=settings.google_client_id,
        redirect_uri=f"{settings.base_url}/auth/google/callback",
        response_type="code",
        scope=SCOPES,
        state=state,
        prompt="select_account",
    )
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{params}")


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    settings = get_settings()
    if error:
        raise HTTPException(400, f"Google auth error: {error}")
    if not code or state != request.session.pop("oauth_state", None):
        raise HTTPException(400, "Invalid OAuth state")

    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": f"{settings.base_url}/auth/google/callback",
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            raise HTTPException(400, "Token exchange failed")
        access_token = token_resp.json()["access_token"]
        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if userinfo_resp.status_code != 200:
            raise HTTPException(400, "Userinfo fetch failed")
        info = userinfo_resp.json()

    if not info.get("email_verified", False):
        raise HTTPException(403, "Email not verified")

    pool = await get_pool()
    async with pool.acquire() as conn:
        user_id = await conn.fetchval(
            """
            INSERT INTO users (google_sub, email, name, last_login_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (google_sub)
            DO UPDATE SET email = EXCLUDED.email, name = EXCLUDED.name, last_login_at = now()
            RETURNING id
            """,
            info["sub"],
            info["email"],
            info.get("name"),
        )

    request.session["user_id"] = str(user_id)
    request.session["email"] = info["email"]
    request.session["name"] = info.get("name", "")
    return RedirectResponse("/app")


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")
