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
    LOGIN_REQUIRED = "LOGIN_REQUIRED"  # the platform wants a session; the bot has no cookies
    TOO_LARGE = "TOO_LARGE"
    UPLOAD_FAILED = "UPLOAD_FAILED"  # downloaded fine, Telegram upload timed out / dropped
    FORBIDDEN = "FORBIDDEN"  # the bot may not send media in this chat (group rights)
    RATE_LIMITED = "RATE_LIMITED"
    SOURCE_DOWN = "SOURCE_DOWN"
    UNKNOWN = "UNKNOWN"


# One-line, user-facing reason per error code. Workers put these in the placeholder message.
ERROR_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.UNSUPPORTED_URL: (
        "I can't handle that link. Send a YouTube, Instagram or Pinterest URL."
    ),
    ErrorCode.PRIVATE: "That content is private, so I can't fetch it.",
    ErrorCode.LOGIN_REQUIRED: (
        "That platform wants a login for this content and I'm not set up with one yet. "
        "The admin has been notified."
    ),
    ErrorCode.TOO_LARGE: "That file is too large for me to send over Telegram.",
    ErrorCode.UPLOAD_FAILED: (
        "I downloaded it, but the upload to Telegram timed out (slow connection). "
        "It may still show up in the next minute or two; if not, try again or pick a "
        "lower quality."
    ),
    ErrorCode.FORBIDDEN: (
        "I'm not allowed to send media in this chat. An admin needs to give the bot "
        "permission to send photos and videos."
    ),
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
    message_id: int  # the "Working on it…" placeholder the worker edits when done; 0 = none
    quiet: bool = False  # auto mode in groups: no placeholder, failures are only logged
    captions: str = "full"  # full | short | off — decided by the gateway per chat
    # The user's own message when progress is shown as a reaction on it (👀 -> 👍 / 👎)
    # instead of a placeholder message; None when reactions aren't available there.
    react_message_id: int | None = None
    url: str  # normalised URL from the detector
    platform: Platform
    quality: Quality = Quality.BEST
    requested_at: datetime = Field(default_factory=utcnow)
    # Where to deliver. In groups the result replies to the message that carried the link
    # and stays inside its forum topic; in private chats both are None.
    chat_type: str = "private"  # private | group | supergroup
    reply_to_message_id: int | None = None
    thread_id: int | None = None

    @property
    def is_group(self) -> bool:
        return self.chat_type in ("group", "supergroup")


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
