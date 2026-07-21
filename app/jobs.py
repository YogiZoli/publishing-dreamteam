"""In-memory job registry for long-running pack builds.

One Railway container running one uvicorn worker, so a process-local dict is
enough — no Redis, consistent with the rest of the stack. A job is lost on
redeploy; the client then gets 404 from /job/{id} and shows a retry prompt.

Jobs are garbage-collected after JOB_TTL_S so the dict cannot grow unbounded.
"""
import asyncio
import time
import uuid
from dataclasses import dataclass, field

JOB_TTL_S = 900  # keep finished jobs for 15 min so a reload can still read them

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

    def emit(self, kind: str, **payload) -> None:
        self.events.append({"kind": kind, **payload})
        self._bell.set()

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def remaining_ms(self) -> int:
        return max(0, self.eta_ms - self.elapsed_ms())


_JOBS: dict[str, Job] = {}


def create(user_id: str, video_id: str, eta_ms: int) -> Job:
    _gc()
    job = Job(id=uuid.uuid4().hex, user_id=user_id, video_id=video_id, eta_ms=eta_ms)
    _JOBS[job.id] = job
    job.emit("progress", percent=0, step="queued", message="Starting…", eta_ms=eta_ms)
    return job


def get(job_id: str, user_id: str) -> Job | None:
    job = _JOBS.get(job_id)
    if job and job.user_id == user_id:
        return job
    return None


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
