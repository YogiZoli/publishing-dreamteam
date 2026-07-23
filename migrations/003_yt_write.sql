-- publishing-dreamteam · paid-tier YouTube write path (yt_write_path flag)
--
-- Stores ONE connected YouTube channel per user. Only the long-lived OAuth
-- refresh token is persisted, Fernet-encrypted (never plaintext) — access
-- tokens are short-lived and minted on demand from the refresh token, so they
-- are never written to the DB. Reuses the same Fernet key (FERNET_KEY, Railway
-- env only) the schema already reserved for encrypted_token on auth_connections.
--
-- Sensitive scopes live here so a support query can see WHAT a user granted
-- without decrypting anything. Owner-only writes are guaranteed by the API
-- itself: the token is the user's own, and videos.update / captions.insert
-- only affect videos the authenticated channel owns.
CREATE TABLE IF NOT EXISTS yt_credentials (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    encrypted_refresh_token BYTEA NOT NULL,   -- Fernet-encrypted, never plaintext
    scopes TEXT NOT NULL DEFAULT '',
    channel_id TEXT,
    channel_title TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Make sure the flag row exists (idempotent; 001 already seeds it, but a fresh
-- DB that never ran 001's later edits still gets it here).
INSERT INTO feature_flags (name, enabled) VALUES ('yt_write_path', false)
ON CONFLICT (name) DO NOTHING;
