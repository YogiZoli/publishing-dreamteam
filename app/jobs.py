"""Job registry for long-running pack builds: memory for the live stream,
Postgres for durability.

One Railway container running one uvicorn worker, so a process-local dict is
still the right place for the live SSE event log — no Redis, consistent with
the rest of the stack. What the dict cannot do is survive a redeploy, and that
used to mean the client got a bare 404 and showed "Lost connection" with no way
to tell a finished build from a dead one.

So the compact state (status, percent, step, message, artifact_id, error) is
mirrored into the `jobs` table. To be clear about what this does and does not
buy: **a build does not resume after a restart** — the worker process really is
gone. What survives is the OUTCOME. A job that finished before the redeploy
still redirects to its artifact; one that was interrupted is reported as
interrupted, with a retry prompt, instead of vanishing.

Write volume is deliberately low: one row on create, one every
JOB_HEARTBEAT_S while running, one on the terminal state. Live progress comes
from the in-memory event log over SSE, so the DB never sits in the hot path.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger("dreamteam.jobs")

JOB_TTL_S = 900  # keep finished jobs for 15 min so a reload can still read them
JOB_HEARTBEAT_S = 5  # how often a running job refreshes heartbeat_at
JOB_STALE_S = 120  # heartbeat older than this on boot ⇒ orphaned by a restart

# Weighted pipeline steps. `share` values sum to 100 and drive the progress bar
# server-side, so the browser never has to guess where we are.
STEPS: list[tuple[str, str, int]] = [
    ("validate", "Checking the YouTube link", 3),
    ("metadata", "Fetching video metadata", 7),
    ("transcript", "Reading the transcript", 10),
    ("llm", "Writing your publishing pack", 55),
    ("tags", "Trimming tags to YouTube's budget", 8),
    ("srt", "Building the English SRT", 7),
    ("store", "Saving your artifact", 10),
]

_SHARE = {key: share for key, _label, share in STEPS}
_LABEL = {key: label for key, label, _share in STEPS}
_ORDER = [key for key, _l, _s in STEPS]


def percent_at(step_key: str, done: bool = True) -> int:
    """Cumulative percent once `step_key` finishes (or starts, if done=False)."""
    total = 0
    for key in _ORDER:
        if key == step_key:
            return min(100, total + (_SHARE[key] if done else 0))
        total += _SHARE[key]
    return 100


@dataclass
class Job:
    id: str
    user_id: str
    video_id: str
    status: str = "running"  # running | done | error
    percent: int = 0
    step: str = ""
    message: str = "Starting…"
    eta_ms: int = 0
    artifact_id: str = ""
    error: str = ""
    started_at: float = field(default_factory=time.monotonic)
    created_at: float = field(default_factory=time.time)
    # Append-only event log. SSE subscribers replay from index 0, so a client
    # that connects late (or reconnects) still sees the whole history.
    events: list[dict] = field(default_factory=list)
    _bell: asyncio.Event = field(default_factory=asyncio.Event)
    _hb: asyncio.Task | None = None

    def emit(self, kind: str, **payload) -> None:
        self.events.append({"kind": kind, **payload})
        self._bell.set()

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def remaining_ms(self) -> int:
        return max(0, self.eta_ms - self.elapsed_ms())


_JOBS: dict[str, Job] = {}


async def create(user_id: str, video_id: str, eta_ms: int) -> Job:
    _gc()
    job = Job(id=str(uuid.uuid4()), user_id=user_id, video_id=video_id, eta_ms=eta_ms)
    _JOBS[job.id] = job
    job.emit("progress", percent=0, step="queued", message="Starting…", eta_ms=eta_ms)
    await persist(job)
    job._hb = asyncio.create_task(_heartbeat(job))
    return job


def get(job_id: str, user_id: str) -> Job | None:
    job = _JOBS.get(job_id)
    if job and job.user_id == user_id:
        return job
    return None


# ---------------------------------------------------------------- durability


async def persist(job: Job) -> None:
    """Upsert the job's compact state. Never raises: losing a status write must
    not break a build that is otherwise working."""
    from app.db import get_pool

    try:
        pool = await get_pool()
        await pool.execute(
            """
            INSERT INTO jobs (id, user_id, video_id, status, percent, step, message,
                              eta_ms, artifact_id, error, updated_at, heartbeat_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                    NULLIF($9, '')::uuid, $10, now(), now())
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status, percent = EXCLUDED.percent,
                step = EXCLUDED.step, message = EXCLUDED.message,
                eta_ms = EXCLUDED.eta_ms, artifact_id = EXCLUDED.artifact_id,
                error = EXCLUDED.error, updated_at = now(), heartbeat_at = now()
            """,
            job.id, job.user_id, job.video_id, job.status, job.percent, job.step,
            job.message, job.remaining_ms(), job.artifact_id, job.error,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("job persist failed id=%s: %s", job.id, e)


async def _heartbeat(job: Job) -> None:
    try:
        while job.status == "running":
            await asyncio.sleep(JOB_HEARTBEAT_S)
            if job.status != "running":
                break
            await persist(job)
    except asyncio.CancelledError:
        pass


async def finish(job: Job) -> None:
    """Stop the heartbeat and write the terminal state exactly once."""
    if job._hb is not None:
        job._hb.cancel()
        job._hb = None
    await persist(job)


async def load(job_id: str, user_id: str) -> dict | None:
    """Read a job's state from the DB — used when it is not in memory, i.e.
    after a redeploy. A 'stale' row is presented to the client as an error so
    the existing retry UI handles it with no template change."""
    from app.db import get_pool

    try:
        uuid.UUID(job_id)  # reject anything that is not a uuid before hitting PG
    except ValueError:
        return None
    try:
        pool = await get_pool()
        row = await pool.fetchrow(
            "SELECT status, percent, step, message, eta_ms, artifact_id, error, "
            "heartbeat_at FROM jobs WHERE id = $1::uuid AND user_id = $2::uuid",
            job_id, user_id,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("job load failed id=%s: %s", job_id, e)
        return None
    if row is None:
        return None

    status = row["status"]
    error = row["error"] or ""
    if status == "running":
        # In the DB as running but absent from this process's memory: the worker
        # that owned it is gone. Do not lie to the client and let it poll for
        # ever — report it as interrupted.
        status, error = "error", _STALE_MSG
    elif status == "stale":
        status, error = "error", (error or _STALE_MSG)
    return {
        "status": status,
        "percent": row["percent"],
        "step": row["step"],
        "message": row["message"],
        "eta_ms": row["eta_ms"],
        "elapsed_ms": 0,
        "artifact_id": str(row["artifact_id"]) if row["artifact_id"] else "",
        "error": error,
    }


_STALE_MSG = (
    "This build was interrupted by a server restart and did not finish. "
    "Nothing was charged against your quota — please start it again."
)


async def sweep_stale() -> int:
    """Mark orphaned 'running' rows as stale. Runs once at startup: anything
    still 'running' with an old heartbeat belongs to a process that is gone."""
    from app.db import get_pool

    try:
        pool = await get_pool()
        n = await pool.fetchval(
            "WITH upd AS (UPDATE jobs SET status = 'stale', error = $2, updated_at = now() "
            "WHERE status = 'running' AND heartbeat_at < now() - ($1 || ' seconds')::interval "
            "RETURNING 1) SELECT count(*) FROM upd",
            str(JOB_STALE_S), _STALE_MSG,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("stale job sweep failed: %s", e)
        return 0
    if n:
        log.info("marked %d orphaned job(s) stale on startup", n)
    return int(n or 0)


def _gc() -> None:
    cutoff = time.time() - JOB_TTL_S
    for jid in [j for j, job in _JOBS.items() if job.created_at < cutoff]:
        _JOBS.pop(jid, None)


class Reporter:
    """Handed to engine.build_pack so the engine stays HTTP-agnostic.

    `step()` marks a pipeline stage finished and records how long it took;
    `field()` pushes one finished piece of the pack so the artifact preview can
    fill in live instead of appearing all at once at the end.
    """

    def __init__(self, job: Job):
        self.job = job
        self.timings: dict[str, int] = {}
        self._last = time.monotonic()

    def start(self, key: str) -> None:
        self.job.step = key
        self.job.message = _LABEL.get(key, key)
        self.job.percent = percent_at(key, done=False)
        self.job.emit(
            "progress",
            percent=self.job.percent,
            step=key,
            message=self.job.message,
            eta_ms=self.job.remaining_ms(),
        )

    def done(self, key: str) -> None:
        now = time.monotonic()
        self.timings[key] = int((now - self._last) * 1000)
        self._last = now
        self.job.percent = percent_at(key, done=True)
        self.job.emit(
            "progress",
            percent=self.job.percent,
            step=key,
            message=_LABEL.get(key, key),
            eta_ms=self.job.remaining_ms(),
            took_ms=self.timings[key],
        )

    def field(self, name: str, preview) -> None:
        self.job.emit("field", name=name, preview=preview)

    def note(self, message: str, extra_ms: int = 0) -> None:
        """Status text change without advancing the bar — used for retries, so
        the user sees *why* it is taking longer instead of a frozen bar."""
        self.job.message = message
        if extra_ms:
            self.job.eta_ms += extra_ms
        self.job.emit(
            "progress",
            percent=self.job.percent,
            step=self.job.step,
            message=message,
            eta_ms=self.job.remaining_ms(),
        )


async def sse_replay(stored: dict):
    """One-shot SSE for a job that outlived the process that built it.

    There is no event log to replay — that died with the old worker — so we
    emit the single terminal event the client needs to resolve, in exactly the
    shape the live stream uses, and close. No template change required.
    """
    import json

    if stored["status"] == "done" and stored["artifact_id"]:
        ev = {"kind": "done", "percent": 100, "artifact_id": stored["artifact_id"],
              "message": "Publishing pack ready"}
    else:
        ev = {"kind": "error",
              "message": stored["error"] or "This build did not finish.",
              "detail": ""}
    yield f"data: {json.dumps(ev)}\n\n"


async def sse(job: Job):
    """Server-Sent Events generator: replays the event log, then follows live.

    A heartbeat comment every 15s keeps proxies (Railway edge) from closing an
    idle connection during the long LLM step.
    """
    import json

    sent = 0
    while True:
        while sent < len(job.events):
            yield f"data: {json.dumps(job.events[sent])}\n\n"
            sent += 1
        if job.status != "running":
            return
        job._bell.clear()
        try:
            await asyncio.wait_for(job._bell.wait(), timeout=15)
        except asyncio.TimeoutError:
            # Heartbeat + a refreshed countdown so the UI keeps ticking down
            # even while the single long LLM call is in flight.
            yield f"data: {json.dumps({'kind': 'tick', 'eta_ms': job.remaining_ms(), 'elapsed_ms': job.elapsed_ms()})}\n\n"
