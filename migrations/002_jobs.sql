-- Job durability. Until now a job was a dict in process memory: a redeploy
-- mid-build killed the process, the client got 404 from /job/{id} and showed
-- "Lost connection" with no idea whether to retry.
--
-- This table does NOT make a build survive a restart - the worker really is
-- gone. It makes the OUTCOME survive, so the client gets a truthful answer:
-- a finished job still redirects to its artifact, and an interrupted one is
-- reported as interrupted with a retry prompt instead of a silent 404.
--
-- Deliberately no `result` column: the finished pack already lives in
-- artifacts.payload, so we reference it and never store a second copy that
-- could drift out of sync.
CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL,
    -- running | done | error | stale
    status TEXT NOT NULL DEFAULT 'running',
    percent INT NOT NULL DEFAULT 0,
    step TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    eta_ms INT NOT NULL DEFAULT 0,
    artifact_id UUID REFERENCES artifacts(id) ON DELETE SET NULL,
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Refreshed every JOB_HEARTBEAT_S while running. On boot, any 'running'
    -- row whose heartbeat is older than the stale threshold is an orphan from
    -- a process that died, and is marked 'stale'.
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_jobs_user_created ON jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_running ON jobs(status, heartbeat_at) WHERE status = 'running';
