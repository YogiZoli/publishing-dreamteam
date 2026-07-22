"""App configuration and feature flags.

Feature flags default per SPEC v2 Section 2. They can be overridden via
environment variables (FLAG_<NAME>=true/false) and, later, via the
feature_flags table in Supabase.
"""
import os
from functools import lru_cache

from pydantic_settings import BaseSettings


def _env_flag(name: str, default: bool) -> bool:
    val = os.getenv(f"FLAG_{name.upper()}")
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class Settings(BaseSettings):
    app_name: str = "YT Publishing Dream Team"
    base_url: str = os.getenv("BASE_URL", "https://dreamteam.commentclient.com")
    environment: str = os.getenv("ENVIRONMENT", "development")

    # Database (Neon Postgres, pooled connection; statement_cache_size=0 — asyncpg + pgbouncer)
    database_url: str = os.getenv("DATABASE_URL", "")
    db_statement_cache_size: int = 0
    # No Redis: rate limiting + artifact cache live in Postgres (rate_limits, artifacts).

    # Google Sign-In (dedicated GCP project; non-sensitive scopes only)
    google_client_id: str = os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.getenv("GOOGLE_CLIENT_SECRET", "")

    # Fernet key for encrypting any stored tokens
    fernet_key: str = os.getenv("FERNET_KEY", "")

    # CRM adapter (server-side only; never exposed in public copy)
    crm_provider: str = os.getenv("CRM_PROVIDER", "ghl")
    crm_api_token: str = os.getenv("CRM_API_TOKEN", "")
    crm_location_id: str = os.getenv("CRM_LOCATION_ID", "")
    crm_free_user_tag: str = os.getenv("CRM_FREE_USER_TAG", "dreamteam-free-user")

    # Rate limits (free tier). Raised 2026-07-21 from the old 2/day + 10/month.
    #
    # COST REALITY (comment corrected 2026-07-22 — the previous version was
    # wrong and would have misled the next cost decision). A pack is ~3.5k
    # tokens, but ~60% of those are THINKING tokens, which bill at the OUTPUT
    # rate. Measured cost is ~$0.03/pack, so 30/month/user is ~$0.90/user/month
    # — roughly 100x the "fractions of a cent" this comment used to claim.
    #
    # The 3/30 limit still stands (confirmed by Zoltan 2026-07-22): free users
    # do not max it out, and the headroom matters for demos and seminars.
    # Billing is not connected, so actual spend today is $0. Revisit before any
    # growth push — see the cost-ceiling item in the handover doc.
    rate_limit_per_day: int = int(os.getenv("RATE_LIMIT_PER_DAY", "3"))
    rate_limit_per_month: int = int(os.getenv("RATE_LIMIT_PER_MONTH", "30"))
    # Comma-separated emails exempt from all quota checks (owner/testing).
    unlimited_emails: str = os.getenv("UNLIMITED_EMAILS", "humorketing@gmail.com")

    # Accurate-chapter transcript egress (Session 7 C0). YouTube blocks
    # Railway's datacenter IP, so the caption fetch needs a clean egress:
    #   none  = disabled (behaves like before — direct fetch, estimated on prod)
    #   local = direct fetch, NO proxy. Works only on a residential IP (dev). $0.
    #   proxy = route through a residential HTTP proxy (required on Railway).
    # Only consulted when the transcript_proxy flag is on.
    transcript_egress: str = os.getenv("TRANSCRIPT_EGRESS", "none")
    # Generic HTTP proxy URL (e.g. http://user:pass@host:port) used when
    # transcript_egress=proxy. Lives ONLY in Railway env vars, never in code.
    transcript_proxy_url: str = os.getenv("TRANSCRIPT_PROXY_URL", "")

    # Bearer token for /admin/flags. If empty, the admin endpoints refuse every
    # request — an unset token must never mean "open", it means "closed".
    admin_token: str = os.getenv("ADMIN_TOKEN", "")
    # How long a DB-sourced flag snapshot is trusted before the background
    # refresher replaces it. Writes through /admin/flags refresh immediately,
    # so this only bounds how long an out-of-band DB edit takes to appear.
    flag_refresh_s: int = int(os.getenv("FLAG_REFRESH_S", "30"))

    class Config:
        env_file = ".env"
        extra = "ignore"


FEATURE_FLAGS: dict[str, bool] = {
    "free_tier": _env_flag("free_tier", True),
    "paid_tier": _env_flag("paid_tier", False),
    "crm_connector": _env_flag("crm_connector", False),
    "byo_vidiq": _env_flag("byo_vidiq", False),
    # 25-locale title/description block. Paid-tier feature: nobody pastes 25
    # localizations into Studio by hand, so running it on free tier only burns
    # tokens and clutters the artifact. Flip on when the paid tier ships.
    "localization": _env_flag("localization", False),
    # Raw English SRT in the artifact. OFF: the video already has these exact
    # auto-captions, so a copy adds no value, and uploading it would strip
    # YouTube's "automatic" label off the ASR errors. Paid tier ships a
    # CLEANED SRT instead (full-text LLM rewrite = output-rate tokens).
    "srt_output": _env_flag("srt_output", False),
    "yt_write_path": _env_flag("yt_write_path", False),
    # Accurate chapters from YouTube's own auto-caption track via a residential
    # egress, with async backfill upgrading estimated chapters when captions
    # land. OFF: prod fetch is IP-blocked without a proxy, so leave estimated
    # until the egress (local dev IP or a residential proxy) is wired. See the
    # Session 7 C0 brief in the handover.
    "transcript_proxy": _env_flag("transcript_proxy", False),
}


# Where each flag's current value came from: "db", "env" or "default".
# app/flags.py rewrites FEATURE_FLAGS and this map when it refreshes from the
# feature_flags table. Kept here (not in flags.py) so that flag() stays a plain
# dict read and every existing call site works unchanged.
FLAG_SOURCES: dict[str, str] = {
    name: ("env" if os.getenv(f"FLAG_{name.upper()}") is not None else "default")
    for name in FEATURE_FLAGS
}

# Snapshot of the env/default resolution, captured before any DB refresh. When
# a row is deleted from feature_flags the flag must fall back to exactly this,
# not to whatever the DB last said.
ENV_FLAGS: dict[str, bool] = dict(FEATURE_FLAGS)


def flag(name: str) -> bool:
    """Effective value of a feature flag.

    Deliberately synchronous and free of I/O: app/flags.py keeps FEATURE_FLAGS
    warm in the background, so a request never waits on Postgres to find out
    whether a feature is on.
    """
    return FEATURE_FLAGS.get(name, False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
