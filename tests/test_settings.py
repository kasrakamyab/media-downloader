"""Settings must survive what Docker's env_file actually hands us."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.settings import Settings


def _s(**env) -> Settings:
    return Settings(BOT_TOKEN="1:x", _env_file=None, **env)


def test_blank_optional_values_are_none() -> None:
    s = _s(TELEGRAM_API_URL="", DOWNLOAD_RATE_LIMIT="   ", IG_COOKIES_FILE="", TELEGRAM_PROXY="")
    assert s.telegram_api_url is None
    assert s.rate_limit is None
    assert s.ig_cookies_file is None
    assert s.telegram_proxy is None


def test_inline_comment_leftovers_are_ignored() -> None:
    # What compose produced from `TELEGRAM_API_URL=     # http://telegram-bot-api:8081 ...`
    s = _s(
        TELEGRAM_API_URL="# http://telegram-bot-api:8081 when self-hosting the Bot API server",
        DOWNLOAD_RATE_LIMIT="# e.g. 5M to cap each download at 5 MB/s",
        YT_COOKIES_FILE="/secrets/yt.txt # Netscape format",
    )
    assert s.telegram_api_url is None
    assert s.rate_limit is None
    assert s.yt_cookies_file == "/secrets/yt.txt"


def test_urls_are_validated() -> None:
    assert _s(TELEGRAM_PROXY="socks5://host.docker.internal:10808").telegram_proxy
    assert _s(TELEGRAM_API_URL="http://telegram-bot-api:8081").telegram_api_url
    with pytest.raises(ValidationError):
        _s(TELEGRAM_API_URL="telegram-bot-api:8081")
    with pytest.raises(ValidationError):
        _s(DOWNLOAD_PROXY="10808")


def test_admin_ids_tolerate_junk() -> None:
    assert _s(ADMIN_IDS="12, 34,# comment").admin_id_set == {12, 34}
    assert _s(ADMIN_IDS="").admin_id_set == set()
