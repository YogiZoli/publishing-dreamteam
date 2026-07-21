"""YT Publishing Dream Team — FastAPI entrypoint (free tier v1)."""
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.auth import router as auth_router
from app.config import FEATURE_FLAGS, flag, get_settings

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="YT Publishing Dream Team", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "dev-only-change-me"),
    https_only=os.getenv("ENVIRONMENT", "development") != "development",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
app.include_router(auth_router)


@app.get("/health")
async def health():
    return {"status": "ok", "flags": FEATURE_FLAGS}


@app.get("/")
async def landing(request: Request):
    settings = get_settings()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "app_name": settings.app_name,
            "free_tier": flag("free_tier"),
            "paid_tier": flag("paid_tier"),
        },
    )


@app.get("/app")
async def app_home(request: Request):
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse(
        request=request,
        name="app.html",
        context={
            "email": request.session.get("email", ""),
            "name": request.session.get("name", ""),
        },
    )


@app.post("/artifact")
async def create_artifact(request: Request, video_url: str = Form(...)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=303)

    from app import engine, ratelimit
    from app.yt import extract_video_id

    video_id = extract_video_id(video_url)
    if not video_id:
        raise HTTPException(400, "Not a valid YouTube URL")

    # Cache hit does not consume quota
    cached = await ratelimit.cached_artifact(video_id)
    if cached:
        import json as _json

        pack = _json.loads(cached["payload"])
        artifact_id = await engine.store_pack(user_id, video_id, pack)
        return RedirectResponse(f"/artifact/{artifact_id}", status_code=303)

    ip = request.client.host if request.client else "unknown"
    status = await ratelimit.check(user_id, ip)
    if not status.allowed:
        raise HTTPException(429, f"Rate limit reached ({status.reason})")

    from app.llm import LLMError

    try:
        pack = await engine.build_pack(video_id)
    except LLMError as e:
        # Log the real cause — otherwise a 503 is invisible in the Railway logs.
        logging.getLogger("dreamteam").error("LLMError on /artifact: %s", e)
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": "Our AI engine is temporarily unavailable. Please try again in a few minutes.", "detail": str(e)[:120]},
            status_code=503,
        )
    await ratelimit.record(user_id, ip)
    artifact_id = await engine.store_pack(user_id, video_id, pack)
    return RedirectResponse(f"/artifact/{artifact_id}", status_code=303)


@app.get("/artifact/{artifact_id}")
async def view_artifact(request: Request, artifact_id: str):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    from app import engine

    pack = await engine.get_pack(artifact_id, user_id)
    if not pack:
        raise HTTPException(404, "Artifact not found")
    return templates.TemplateResponse(
        request=request,
        name="artifact.html",
        context={"pack": pack, "paid_tier": flag("paid_tier")},
    )
