"""YT Publishing Dream Team — FastAPI entrypoint (free tier v1)."""
import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.admin import router as admin_router
from app.auth import router as auth_router
from app.config import FEATURE_FLAGS, flag, get_settings

BASE_DIR = Path(__file__).resolve().parent

# Uvicorn configures its own loggers but leaves the root logger at WARNING, so
# our logging.info() calls were silently dropped in production — the whole
# 'gemini usage' token line was invisible in `railway logs` while warnings and
# errors came through fine. Configure our own logger explicitly.
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
_root = logging.getLogger("dreamteam")
_root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
_root.addHandler(_handler)
_root.propagate = False  # avoid double-printing through uvicorn's root handler

log = logging.getLogger("dreamteam")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Load feature flags from the DB before serving, then keep them warm.

    flags.start() never raises: if Neon is unreachable at boot the app still
    comes up on env/default values rather than refusing to start.
    """
    from app import flags, jobs

    await flags.start()
    # Any job still 'running' in the DB belongs to a process that no longer
    # exists — this restart is precisely what orphaned it. Mark those stale so
    # their clients get "interrupted, please retry" instead of polling for ever.
    await jobs.sweep_stale()
    # The in-memory dict is GC'd after 15 min but the rows are not, so trim the
    # table on the way up. Artifacts are untouched.
    await jobs.purge_old()
    yield
    await flags.stop()
    from app.db import close_pool

    await close_pool()


app = FastAPI(
    title="YT Publishing Dream Team", docs_url=None, redoc_url=None, lifespan=lifespan
)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "dev-only-change-me"),
    https_only=os.getenv("ENVIRONMENT", "development") != "development",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
app.include_router(auth_router)
app.include_router(admin_router)


@app.get("/health")
async def health():
    # FEATURE_FLAGS is the live effective snapshot — app/flags.py rewrites it in
    # place from the feature_flags table, so this reflects DB overrides too.
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
    """Kick off a build and return immediately with a job id.

    The pack takes ~30-90s (Gemini 3.x thinks before it answers), which is far
    too long to hold a request open with no feedback. The browser now gets a
    job id straight away and subscribes to /job/{id}/stream for live progress.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "not_signed_in"}, status_code=401)

    from app import engine, jobs, ratelimit
    from app.yt import extract_video_id

    video_id = extract_video_id(video_url)
    if not video_id:
        return JSONResponse({"error": "That is not a valid YouTube URL."}, status_code=400)

    # Cache hit: no LLM call, no quota, no job — hand back the artifact at once.
    cached = await ratelimit.cached_artifact(video_id)
    if cached:
        pack = json.loads(cached["payload"])
        artifact_id = await engine.store_pack(user_id, video_id, pack)
        return JSONResponse({"artifact_id": artifact_id, "cached": True})

    ip = request.client.host if request.client else "unknown"
    email = request.session.get("email")
    status = await ratelimit.check(user_id, ip, email)
    if not status.allowed:
        return JSONResponse(
            {"error": f"You have reached your free-tier limit ({status.reason})."},
            status_code=429,
        )

    eta_ms = await engine.median_duration_ms()
    job = await jobs.create(user_id, video_id, eta_ms)
    asyncio.create_task(_run_job(job, user_id, ip))
    return JSONResponse({"job_id": job.id, "eta_ms": eta_ms})


async def _run_job(job, user_id: str, ip: str) -> None:
    """Background worker: builds the pack, streaming progress into the job."""
    from app import engine, jobs, ratelimit
    from app.llm import LLMError

    reporter = jobs.Reporter(job)
    try:
        pack = await engine.build_pack(job.video_id, report=reporter)
        reporter.start("store")
        await ratelimit.record(user_id, ip)
        artifact_id = await engine.store_pack(user_id, job.video_id, pack)
        reporter.done("store")
        job.artifact_id = artifact_id
        job.percent = 100
        job.status = "done"
        job.emit(
            "done",
            percent=100,
            artifact_id=artifact_id,
            message="Publishing pack ready",
            usage=pack.get("usage", {}),
        )
    except LLMError as e:
        log.error("LLMError on job %s (video %s): %s", job.id, job.video_id, e)
        job.status = "error"
        job.error = str(e)[:200]
        # Be explicit about whose fault it is. These are Google-side conditions,
        # not a bug in this app, and the user's quota is untouched because
        # ratelimit.record() only runs after a successful build.
        text = str(e)
        if "429" in text or "quota" in text.lower() or "RESOURCE_EXHAUSTED" in text:
            msg = ("Google's Gemini API has hit its rate limit on our side — this is a "
                   "limit on Google's end, not a problem with your link or your account. "
                   "We retried automatically 4 times. Please try again in a few minutes. "
                   "Your quota was not used.")
        elif "503" in text or "UNAVAILABLE" in text or "500" in text or "502" in text:
            msg = ("Google's Gemini API is overloaded right now — the problem is on "
                   "Google's side, not with your video or your account. We already "
                   "retried 4 times and tried a backup model. Please try again in a "
                   "minute. Your quota was not used.")
        else:
            msg = ("We could not reach the AI engine. Please try again in a few "
                   "minutes — your quota was not used.")
        job.emit("error", message=msg, detail=text[:160])
    except Exception as e:  # noqa: BLE001 — must never leave the job hanging
        log.exception("Job %s failed (video %s)", job.id, job.video_id)
        job.status = "error"
        job.error = str(e)[:200]
        job.emit("error", message="Something went wrong building your pack.", detail=str(e)[:160])
    finally:
        # Terminal state to Postgres, heartbeat stopped. In `finally` so an
        # unexpected failure path can never leave a row stuck on 'running'.
        await jobs.finish(job)


@app.get("/job/{job_id}")
async def job_status(request: Request, job_id: str):
    """Polling fallback for browsers/proxies where SSE does not survive."""
    from app import jobs

    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(404, "Job not found or expired")
    job = jobs.get(job_id, user_id)
    if not job:
        # Not in this process's memory: either a redeploy happened or the 15min
        # in-memory TTL expired. The DB still knows the outcome.
        stored = await jobs.load(job_id, user_id)
        if stored:
            return stored
        raise HTTPException(404, "Job not found or expired")
    return {
        "status": job.status,
        "percent": job.percent,
        "step": job.step,
        "message": job.message,
        "eta_ms": job.remaining_ms(),
        "elapsed_ms": job.elapsed_ms(),
        "artifact_id": job.artifact_id,
        "error": job.error,
    }


@app.get("/job/{job_id}/stream")
async def job_stream(request: Request, job_id: str):
    """Server-Sent Events feed of progress + fields for one build."""
    from app import jobs

    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(404, "Job not found or expired")
    job = jobs.get(job_id, user_id)
    if not job:
        # Survivor path: the live event log died with the old process, but the
        # outcome is in Postgres. Emit that single terminal event and close, so
        # a reconnecting browser resolves instead of falling into a poll loop.
        stored = await jobs.load(job_id, user_id)
        if not stored:
            raise HTTPException(404, "Job not found or expired")
        return StreamingResponse(
            jobs.sse_replay(stored),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return StreamingResponse(
        jobs.sse(job),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Railway's edge buffers by default; this forces immediate flush.
            "X-Accel-Buffering": "no",
        },
    )


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
