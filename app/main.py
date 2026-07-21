"""YT Publishing Dream Team — FastAPI entrypoint (free tier v1 skeleton)."""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import FEATURE_FLAGS, flag, get_settings

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="YT Publishing Dream Team", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


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


@app.get("/auth/google/login")
async def google_login():
    """Google Sign-In entry — implemented in build step 2 (dedicated GCP project)."""
    return JSONResponse({"detail": "Google Sign-In coming in step 2"}, status_code=501)


@app.get("/auth/google/callback")
async def google_callback():
    return JSONResponse({"detail": "Google Sign-In coming in step 2"}, status_code=501)
