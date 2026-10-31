"""Settings loaded from the environment (and a local `.env` in development)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(default="", alias="BOT_TOKEN")
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    telegram_api_url: str | None = Field(default=None, alias="TELEGRAM_API_URL")
    # Proxies: TELEGRAM_PROXY for the Bot API (http://, socks5://), DOWNLOAD_PROXY for
    # yt-dlp / gallery-dl / short-link resolution. Both optional and independent.
    telegram_proxy: str | None = Field(default=None, alias="TELEGRAM_PROXY")
    download_proxy: str | None = Field(default=None, alias="DOWNLOAD_PROXY")
    # bgutil PO-token sidecar for YouTube; empty disables the plugin's remote mode.
    pot_provider_url: str | None = Field(default=None, alias="POT_PROVIDER_URL")

    # Gateway
    mode: Literal["polling", "webhook"] = Field(default="polling", alias="MODE")
    webhook_url: str = Field(default="", alias="WEBHOOK_URL")
    webhook_secret: str = Field(default="", alias="WEBHOOK_SECRET")
    webhook_host: str = Field(default="0.0.0.0", alias="WEBHOOK_HOST")
    webhook_port: int = Field(default=8080, alias="WEBHOOK_PORT")
    max_active_jobs: int = Field(default=2, alias="MAX_ACTIVE_JOBS")
    hourly_limit: int = Field(default=20, alias="HOURLY_LIMIT")
    # Per group chat, on top of the per-user limits: jobs in flight and requests per hour.
    max_active_per_chat: int = Field(default=2, alias="MAX_ACTIVE_PER_CHAT")
    group_hourly_limit: int = Field(default=60, alias="GROUP_HOURLY_LIMIT")
    admin_ids: str = Field(default="", alias="ADMIN_IDS")  # comma-separated user ids
    quality_choice_timeout: int = Field(default=60, alias="QUALITY_CHOICE_TIMEOUT")
    # Auto-download in groups: the same link posted again within this window is skipped.
    auto_dedupe_seconds: int = Field(default=120, alias="AUTO_DEDUPE_SECONDS")
    # Caption under delivered media: full (title, stats, description), short (title + link),
    # off. Groups can switch captions off in /settings.
    captions: Literal["full", "short", "off"] = Field(default="full", alias="CAPTIONS")
    # Show progress as a reaction on the user's message (👀 -> 👍 / 👎) instead of a
    # "Working on it…" message; falls back to the message where reactions aren't allowed.
    use_reactions: bool = Field(default=True, alias="USE_REACTIONS")
    probe_cache_ttl: int = Field(default=600, alias="PROBE_CACHE_TTL")
    alert_after_open_s: int = Field(default=1800, alias="ALERT_AFTER_OPEN_S")

    # Resilience
    breaker_threshold: int = Field(default=5, alias="BREAKER_THRESHOLD")
    breaker_window: int = Field(default=600, alias="BREAKER_WINDOW")
    breaker_open_for: int = Field(default=900, alias="BREAKER_OPEN_FOR")
    transient_retries: int = Field(default=1, alias="TRANSIENT_RETRIES")
    retry_backoff: float = Field(default=2.0, alias="RETRY_BACKOFF")
    health_port: int = Field(default=8081, alias="HEALTH_PORT")  # 0 disables

    # Workers
    max_file_mb: int = Field(default=48, alias="MAX_FILE_MB")
    job_dir: str = Field(default="/tmp/jobs", alias="JOB_DIR")
    worker_concurrency: int = Field(default=3, alias="WORKER_CONCURRENCY")
    extract_timeout: int = Field(default=30, alias="EXTRACT_TIMEOUT")
    download_timeout: int = Field(default=120, alias="DOWNLOAD_TIMEOUT")
    job_ttl: int = Field(default=600, alias="JOB_TTL")  # seconds a queued job stays valid
    rate_limit: str | None = Field(default=None, alias="DOWNLOAD_RATE_LIMIT")  # e.g. "5M"
    # Telegram upload request timeout: max(60, size_mb * UPLOAD_SECONDS_PER_MB), capped.
    upload_timeout: int = Field(default=600, alias="UPLOAD_TIMEOUT")
    upload_seconds_per_mb: float = Field(default=8.0, alias="UPLOAD_SECONDS_PER_MB")
    min_free_gb: float = Field(default=1.0, alias="MIN_FREE_GB")
    cache_ttl_days: int = Field(default=30, alias="CACHE_TTL_DAYS")
    heartbeat_ttl: int = Field(default=60, alias="HEARTBEAT_TTL")
    yt_cookies_file: str | None = Field(default=None, alias="YT_COOKIES_FILE")
    ig_cookies_file: str | None = Field(default=None, alias="IG_COOKIES_FILE")
    pin_cookies_file: str | None = Field(default=None, alias="PIN_COOKIES_FILE")
    # Writable copy of each cookies file that keeps the session rotations sites send back
    # (see extractors.cookie_copy). Unset: every job starts from the export in ./secrets.
    cookie_state_dir: str | None = Field(default=None, alias="COOKIE_STATE_DIR")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_json: bool = Field(default=False, alias="LOG_JSON")

    @field_validator(
        "telegram_api_url",
        "telegram_proxy",
        "download_proxy",
        "pot_provider_url",
        "rate_limit",
        "yt_cookies_file",
        "ig_cookies_file",
        "pin_cookies_file",
        "cookie_state_dir",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        """Docker's env_file hands us `KEY=` as "" and keeps inline `# comments` as part
        of the value, so treat blanks and comment-only values as unset."""
        if isinstance(v, str):
            v = v.split(" #", 1)[0].strip() if not v.lstrip().startswith("#") else ""
            return v or None
        return v

    @field_validator(
        "telegram_api_url", "telegram_proxy", "download_proxy", "pot_provider_url", "webhook_url"
    )
    @classmethod
    def _must_be_url(cls, v: str | None) -> str | None:
        if v and not v.startswith(("http://", "https://", "socks5://", "socks4://")):
            raise ValueError(f"expected a URL, got {v!r}")
        return v

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def admin_id_set(self) -> set[int]:
        return {int(x) for x in self.admin_ids.split(",") if x.strip().isdigit()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
