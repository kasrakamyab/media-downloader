from __future__ import annotations

from core.extractors import ytdlp_common_opts


def test_pot_provider_and_proxy_are_passed_to_ytdlp(settings) -> None:
    settings.pot_provider_url = "http://pot-provider:4416/"
    settings.download_proxy = "socks5://proxy:1080"
    opts = ytdlp_common_opts(settings, "/secrets/yt.txt")
    assert opts["extractor_args"] == {
        "youtubepot-bgutilhttp": {"base_url": ["http://pot-provider:4416"]}
    }
    assert opts["proxy"] == "socks5://proxy:1080"
    assert "cookiefile" not in opts  # callers pass a writable copy, never the original
    assert opts["noplaylist"] is True


def test_defaults_are_minimal(settings) -> None:
    opts = ytdlp_common_opts(settings, None)
    assert "extractor_args" not in opts
    assert "proxy" not in opts
    assert "cookiefile" not in opts
