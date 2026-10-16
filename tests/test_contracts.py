from __future__ import annotations

import json

from core.contracts import QUEUE_NAMES, ErrorCode, Job, Platform, Quality, Result


def test_job_roundtrip() -> None:
    job = Job(chat_id=1, user_id=2, message_id=3, url="https://youtu.be/x", platform="youtube")
    data = json.loads(job.model_dump_json())
    back = Job.model_validate(data)
    assert back == job
    assert back.platform is Platform.YOUTUBE
    assert back.quality is Quality.BEST
    assert len(back.job_id) == 32
    assert back.requested_at.tzinfo is not None


def test_result_roundtrip() -> None:
    ok = Result.ok("j", ["a", "b"], 120, cached=True)
    err = Result.error("j", ErrorCode.TOO_LARGE, 5)
    for r in (ok, err):
        assert Result.model_validate_json(r.model_dump_json()) == r
    assert err.error_code is ErrorCode.TOO_LARGE
    assert ok.status == "ok" and err.status == "error"


def test_every_platform_has_a_queue() -> None:
    assert set(QUEUE_NAMES) == set(Platform)
    assert len(set(QUEUE_NAMES.values())) == len(Platform)
