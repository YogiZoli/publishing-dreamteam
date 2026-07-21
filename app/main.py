"""YT Publishing Dream Team — FastAPI entrypoint (free tier v1)."""
import os
from pathlib import Path

from fastapi import FastAPI, Request
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
