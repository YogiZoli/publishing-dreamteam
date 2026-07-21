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

    # Rate limits (free tier). Raised 2026-07-21: measured cost is ~3.5k tokens
    # per pack, so 30/month/user is still fractions of a cent — the old 2/10
    # limit was throttling adoption for no real cost reason.
    rate_limit_per_day: int = int(os.getenv("RATE_LIMIT_PER_DAY", "3"))
    rate_limit_per_month: int = int(os.getenv("RATE_LIMIT_PER_MONTH", "30"))
    # Comma-separated emails exempt from all quota checks (owner/testing).
    unlimited_emails: str = os.getenv("UNLIMITED_EMAILS", "humorketing@gmail.com")

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
}


def flag(name: str) -> bool:
    return FEATURE_FLAGS.get(name, False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
