"""Job/Result schemas, enums and queue names shared by the gateway and every worker.

This is the contract every worker is built against. Change it only with a spec.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Platform(StrEnum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    PINTEREST = "pinterest"


class Quality(StrEnum):
    """Requested quality. Workers for single-quality platforms ignore it."""

    BEST = "best"  # best that fits under the size cap
    P1080 = "1080p"
    P720 = "720p"
    P480 = "480p"
    P360 = "360p"
    AUDIO = "audio"


class ErrorCode(StrEnum):
    UNSUPPORTED_URL = "UNSUPPORTED_URL"
    PRIVATE = "PRIVATE"
    TOO_LARGE = "TOO_LARGE"
    RATE_LIMITED = "RATE_LIMITED"
    SOURCE_DOWN = "SOURCE_DOWN"
    UNKNOWN = "UNKNOWN"


# One-line, user-facing reason per error code. Workers put these in the placeholder message.
ERROR_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.UNSUPPORTED_URL: (
        "I can't handle that link. Send a YouTube, Instagram or Pinterest URL."
    ),
    ErrorCode.PRIVATE: "That content is private or needs a login, so I can't fetch it.",
    ErrorCode.TOO_LARGE: "That file is too large for me to send over Telegram.",
    ErrorCode.RATE_LIMITED: "The source is rate-limiting us right now. Try again in a few minutes.",
    ErrorCode.SOURCE_DOWN: "The source didn't respond. Try again in a few minutes.",
    ErrorCode.UNKNOWN: "Something went wrong while fetching that. Try again later.",
}

# arq queue name per platform. One worker type consumes exactly one queue.
QUEUE_NAMES: dict[Platform, str] = {
    Platform.YOUTUBE: "queue:youtube",
    Platform.INSTAGRAM: "queue:instagram",
    Platform.PINTEREST: "queue:pinterest",
}

# Name of the arq function every worker registers.
JOB_FUNCTION = "process_job"
# Cheap format listing, registered only by workers whose platform offers a quality choice.
PROBE_FUNCTION = "probe"

# Transient errors: the worker retries these with backoff, and they count toward the breaker.
TRANSIENT_ERRORS = frozenset({ErrorCode.SOURCE_DOWN})


def new_job_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Job(BaseModel):
    """A download request. The gateway creates it, exactly one worker consumes it."""

    job_id: str = Field(default_factory=new_job_id)
    chat_id: int
    user_id: int
    message_id: int  # the "Working on it…" placeholder the worker edits when done
    url: str  # normalised URL from the detector
    platform: Platform
    quality: Quality = Quality.BEST
    requested_at: datetime = Field(default_factory=utcnow)


class Result(BaseModel):
    """What a worker reports back after a job. Kept in Redis for /stats and debugging."""

    job_id: str
    status: Literal["ok", "error"]
    file_ids: list[str] = Field(default_factory=list)
    error_code: ErrorCode | None = None
    took_ms: int = 0
    cached: bool = False

    @classmethod
    def ok(cls, job_id: str, file_ids: list[str], took_ms: int, cached: bool = False) -> Result:
        return cls(job_id=job_id, status="ok", file_ids=file_ids, took_ms=took_ms, cached=cached)

    @classmethod
    def error(cls, job_id: str, code: ErrorCode, took_ms: int) -> Result:
        return cls(job_id=job_id, status="error", error_code=code, took_ms=took_ms)


class FormatOption(BaseModel):
    """One entry of the quality menu, as reported by a probe."""

    quality: Quality
    size_bytes: int | None = None  # None when the source doesn't say
    height: int | None = None

    @property
    def label(self) -> str:
        name = "Audio only" if self.quality == Quality.AUDIO else self.quality.value
        if self.size_bytes is None:
            return name
        mb = self.size_bytes / (1024 * 1024)
        return f"{name} · {mb:.0f} MB" if mb >= 10 else f"{name} · {mb:.1f} MB"


class ProbeResult(BaseModel):
    """Answer to a probe. `formats` is empty when the probe failed; see `error_code`."""

    url: str
    formats: list[FormatOption] = Field(default_factory=list)
    title: str | None = None
    error_code: ErrorCode | None = None
    took_ms: int = 0
