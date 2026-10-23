from __future__ import annotations

import pytest
from fakeredis import aioredis as fakeredis

from core.settings import Settings


@pytest.fixture
async def redis():
    r = fakeredis.FakeRedis()
    yield r
    await r.aclose()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        BOT_TOKEN="123:test",
        JOB_DIR=str(tmp_path / "jobs"),
        MAX_FILE_MB=1,
        MIN_FREE_GB=0,
        EXTRACT_TIMEOUT=2,
        DOWNLOAD_TIMEOUT=2,
        _env_file=None,
    )
