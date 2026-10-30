"""Instagram embed-page fallback: parsing fixtures, downloading with the cap, worker order."""

from __future__ import annotations

import html
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from core.contracts import ErrorCode, Job, Platform
from core.instagram_embed import (
    EmbedItem,
    download_items,
    embed_download,
    embed_url,
    fetch_embed_page,
    og_download,
    og_tags,
    parse_embed,
    parse_post_page,
    post_page_url,
)
from core.worker import ExtractError
from workers.instagram import worker as ig

FIX = Path(__file__).parent / "fixtures" / "instagram"
CDN = "https://scontent-fra3-1.cdninstagram.com/v/t51.2885-15"


def _page(name: str) -> str:
    return (FIX / name).read_text()


# ---- parsing --------------------------------------------------------------------------


def test_embed_url() -> None:
    assert embed_url("https://www.instagram.com/p/DfEciETonwc/") == (
        "https://www.instagram.com/p/DfEciETonwc/embed/captioned/"
    )
    assert embed_url("https://www.instagram.com/reel/C1a2B3c4D5e/").endswith(
        "C1a2B3c4D5e/embed/captioned/"
    )
    assert embed_url("https://www.instagram.com/someuser/p/C1a2B3c4D5e/").endswith(
        "C1a2B3c4D5e/embed/captioned/"
    )
    assert embed_url("https://www.instagram.com/someuser/") is None


def test_single_photo_picks_highest_resolution() -> None:
    items = parse_embed(_page("single_photo.html"))
    assert items == [EmbedItem(f"{CDN}/1_s1080x1080.jpg?x=1&amp;y=2", "image")]


def test_carousel_keeps_order_and_video() -> None:
    items = parse_embed(_page("carousel.html"))
    assert [i.kind for i in items] == ["image", "video", "image"]
    assert items[0].url.startswith(f"{CDN}/1_s1080x1080")
    assert items[1].url.startswith(f"{CDN}/2_video.mp4")
    assert items[2].url.startswith(f"{CDN}/3_s1080x1080")


def test_video_from_additional_data() -> None:
    items = parse_embed(_page("video_additional_data.html"))
    assert items == [EmbedItem(f"{CDN}/7_video.mp4?efg=abc&_nc_ht=x", "video")]


def test_img_only_page_unescapes_entities() -> None:
    assert parse_embed(_page("img_only.html")) == [
        EmbedItem(f"{CDN}/only_s1080x1080.jpg?a=1&b=2", "image")
    ]


def test_login_wall_has_no_media() -> None:
    assert parse_embed(_page("login_wall.html")) == []
    assert parse_embed("") == []
    assert parse_embed('<script>var x = {"shortcode_media": "nope"}</script>') == []


def test_bare_shortcode_media_blob() -> None:
    page = (
        '<script>foo({"data":{"shortcode_media":{"is_video":false,'
        '"display_url":"https://cdn/x.jpg"}}});</script>'
    )
    assert parse_embed(page) == [EmbedItem("https://cdn/x.jpg", "image")]


# ---- the post page's og tags (the path that works from the VPN exit) ---------------------


def test_post_page_photo_og_image_is_the_full_size_photo() -> None:
    items, carousel = parse_post_page(_page("post_page_photo.html"))
    assert len(items) == 1 and items[0].kind == "image" and not carousel
    url = items[0].url
    assert url.startswith("https://scontent-fra5-1.cdninstagram.com/v/t51.82787-15/878353926_")
    assert "&amp;" not in url and "&_nc_ht=scontent-fra5-1.cdninstagram.com" in url  # unescaped
    tags = og_tags(_page("post_page_photo.html"))
    assert tags["og:type"] == "article" and tags["og:title"] == 'Kasra on Instagram: "Meow"'


def test_post_page_reel_prefers_og_video() -> None:
    items, _ = parse_post_page(_page("post_page_reel.html"))
    assert len(items) == 1 and items[0].kind == "video"
    assert items[0].url.startswith("https://scontent-fra5-2.cdninstagram.com/o1/v/t16/")
    assert "&amp;" not in items[0].url


def test_post_page_carousel_is_flagged_first_item_only() -> None:
    items, carousel = parse_post_page(_page("post_page_carousel.html"))
    assert len(items) == 1 and carousel  # attribute order reversed in the fixture on purpose


def test_post_page_login_wall_yields_nothing() -> None:
    # og:image on the login page is a static rsrc.php logo, not media
    assert parse_post_page(_page("post_page_login.html")) == ([], False)
    assert parse_post_page("") == ([], False)
    assert post_page_url("https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=x") == (
        "https://www.instagram.com/p/C1a2B3c4D5e/"
    )


# ---- fetching against a local server ----------------------------------------------------


@pytest.fixture
async def cdn_server(aiohttp_server_factory):
    return aiohttp_server_factory


@pytest.fixture
async def aiohttp_server_factory():
    """Tiny aiohttp app standing in for instagram.com and its CDN."""
    servers = []

    async def make(routes: dict[str, web.Response | bytes]) -> str:
        app = web.Application()
        for path, body in routes.items():

            async def handler(_: web.Request, body=body) -> web.Response:
                if isinstance(body, web.Response):
                    return body
                return web.Response(body=body, content_type="image/jpeg")

            app.router.add_get(path, handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        servers.append(runner)
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        return f"http://127.0.0.1:{port}"

    yield make
    for r in servers:
        await r.cleanup()


async def test_download_items_writes_files_and_enforces_cap(
    aiohttp_server_factory, settings, tmp_path
):
    settings.max_file_mb = 1
    base = await aiohttp_server_factory(
        {
            "/a.jpg": b"\xff\xd8" + b"x" * 100,
            "/b.mp4": web.Response(body=b"v" * 50, content_type="video/mp4"),
            "/big.jpg": b"x" * (2 * 1024 * 1024),
        }
    )
    files = await download_items(
        [EmbedItem(f"{base}/a.jpg?x=1", "image"), EmbedItem(f"{base}/b.mp4", "video")],
        tmp_path,
        settings,
    )
    assert [f.name for f in files] == ["01.jpg", "02.mp4"]
    assert files[0].read_bytes().startswith(b"\xff\xd8") and files[1].stat().st_size == 50
    with pytest.raises(ExtractError) as e:
        await download_items([EmbedItem(f"{base}/big.jpg", "image")], tmp_path, settings)
    assert e.value.code is ErrorCode.TOO_LARGE


async def test_fetch_embed_page_status_mapping(aiohttp_server_factory, settings, monkeypatch):
    base = await aiohttp_server_factory(
        {
            "/ok/": web.Response(text=_page("single_photo.html"), content_type="text/html"),
            "/gone/": web.Response(status=404),
            "/wall/": web.Response(status=403),
            "/busy/": web.Response(status=429),
        }
    )
    import core.instagram_embed as mod

    monkeypatch.setattr(mod, "embed_url", lambda u: base + u)
    assert "EmbeddedMediaImage" in await fetch_embed_page("/ok/", settings)
    for path, code in (
        ("/gone/", ErrorCode.PRIVATE),
        ("/wall/", ErrorCode.LOGIN_REQUIRED),
        ("/busy/", ErrorCode.RATE_LIMITED),
    ):
        with pytest.raises(ExtractError) as e:
            await fetch_embed_page(path, settings)
        assert e.value.code is code


async def test_embed_download_end_to_end(aiohttp_server_factory, settings, tmp_path, monkeypatch):
    base = await aiohttp_server_factory({"/img.jpg": b"img-bytes"})
    page = _page("img_only.html").replace(
        f"{CDN}/only_s1080x1080.jpg?a=1&amp;b=2", f"{base}/img.jpg"
    )
    base2 = await aiohttp_server_factory(
        {
            "/post/": web.Response(text=page, content_type="text/html"),
            "/empty/": web.Response(text=_page("login_wall.html"), content_type="text/html"),
        }
    )
    import core.instagram_embed as mod

    monkeypatch.setattr(mod, "embed_url", lambda u: base2 + u)
    files = await embed_download("/post/", tmp_path / "j", settings)
    assert [f.name for f in files] == ["01.jpg"] and files[0].read_bytes() == b"img-bytes"
    with pytest.raises(ExtractError) as e:
        await embed_download("/empty/", tmp_path / "k", settings)
    assert e.value.code is ErrorCode.LOGIN_REQUIRED


# ---- worker chain -------------------------------------------------------------------------


def _job() -> Job:
    return Job(
        chat_id=1,
        user_id=2,
        message_id=3,
        url="https://www.instagram.com/p/DfEciETonwc/",
        platform=Platform.INSTAGRAM,
    )


async def test_og_download_end_to_end(aiohttp_server_factory, settings, tmp_path, monkeypatch):
    base = await aiohttp_server_factory({"/photo.jpg": b"\xff\xd8photo"})
    page = _page("post_page_photo.html")
    real = og_tags(page)["og:image"]
    page = page.replace(html.escape(real, quote=True), f"{base}/photo.jpg")
    assert f"{base}/photo.jpg" in page
    site = await aiohttp_server_factory(
        {
            "/p/ok/": web.Response(text=page, content_type="text/html"),
            "/p/login/": web.Response(text=_page("post_page_login.html"), content_type="text/html"),
        }
    )
    import re

    import core.instagram_embed as mod

    monkeypatch.setattr(mod, "post_page_url", lambda u: site + u)
    # the local stand-in for the CDN must pass the media-host check
    monkeypatch.setattr(mod, "_MEDIA_HOST_RE", re.compile(r"^http://127\.0\.0\.1:"))
    files = await og_download("/p/ok/", tmp_path / "j", settings)
    assert [f.name for f in files] == ["01.jpg"] and files[0].read_bytes() == b"\xff\xd8photo"
    with pytest.raises(ExtractError) as e:
        await og_download("/p/login/", tmp_path / "k", settings)
    assert e.value.code is ErrorCode.LOGIN_REQUIRED


async def test_worker_falls_through_to_og_page_without_cookies(monkeypatch, settings, tmp_path):
    """The live case: yt-dlp 'no video in this post', gallery-dl login wall, og:image works."""
    calls: list[str] = []

    async def ytdlp(url, dest, quality, settings, cookies=None):
        calls.append("ytdlp")
        raise ExtractError(ErrorCode.UNKNOWN, "There is no video in this post")

    async def gallerydl(url, dest, settings, cookies=None, max_items=10):
        calls.append("gallerydl")
        raise ExtractError(ErrorCode.LOGIN_REQUIRED, "HTTP redirect to login page")

    async def ogpage(url, dest, settings):
        calls.append("ogpage")
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / "01.jpg"
        f.write_bytes(b"x")
        return [f]

    async def embed(url, dest, settings):
        calls.append("embed")
        raise AssertionError("embed must not run when og:image succeeded")

    monkeypatch.setattr(ig, "ytdlp_download", ytdlp)
    monkeypatch.setattr(ig, "gallerydl_download", gallerydl)
    monkeypatch.setattr(ig, "og_download", ogpage)
    monkeypatch.setattr(ig, "embed_download", embed)
    settings.ig_cookies_file = None
    files = await ig.extract(_job(), tmp_path / "j", settings)
    assert [f.name for f in files] == ["01.jpg"]
    assert calls == ["ytdlp", "gallerydl", "ogpage"]


async def test_worker_embed_is_the_last_try(monkeypatch, settings, tmp_path):
    calls: list[str] = []

    async def failing(name, code):
        async def step(*a, **k):
            calls.append(name)
            raise ExtractError(code, name)

        return step

    monkeypatch.setattr(ig, "ytdlp_download", await failing("ytdlp", ErrorCode.UNKNOWN))
    monkeypatch.setattr(
        ig, "gallerydl_download", await failing("gallerydl", ErrorCode.LOGIN_REQUIRED)
    )
    monkeypatch.setattr(ig, "og_download", await failing("ogpage", ErrorCode.LOGIN_REQUIRED))

    async def embed(url, dest, settings):
        calls.append("embed")
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / "01.jpg"
        f.write_bytes(b"x")
        return [f]

    monkeypatch.setattr(ig, "embed_download", embed)
    settings.ig_cookies_file = None
    assert await ig.extract(_job(), tmp_path / "j", settings)
    assert calls == ["ytdlp", "gallerydl", "ogpage", "embed"]


async def test_worker_order_with_cookies_and_error_precedence(monkeypatch, settings, tmp_path):
    calls: list[str] = []

    async def gallerydl(url, dest, settings, cookies=None, max_items=10):
        calls.append("gallerydl")
        raise ExtractError(ErrorCode.UNKNOWN, "weird")

    async def ytdlp(url, dest, quality, settings, cookies=None):
        calls.append("ytdlp")
        raise ExtractError(ErrorCode.LOGIN_REQUIRED, "login")

    async def embed(url, dest, settings):
        calls.append("embed")
        raise ExtractError(ErrorCode.UNKNOWN, "no media")

    async def ogpage(url, dest, settings):
        calls.append("ogpage")
        raise ExtractError(ErrorCode.UNKNOWN, "no og")

    monkeypatch.setattr(ig, "ytdlp_download", ytdlp)
    monkeypatch.setattr(ig, "gallerydl_download", gallerydl)
    monkeypatch.setattr(ig, "og_download", ogpage)
    monkeypatch.setattr(ig, "embed_download", embed)
    settings.ig_cookies_file = "/secrets/ig.txt"
    with pytest.raises(ExtractError) as e:
        await ig.extract(_job(), tmp_path / "j", settings)
    assert calls == ["gallerydl", "ytdlp", "ogpage", "embed"]
    assert e.value.code is ErrorCode.LOGIN_REQUIRED  # most specific code wins


async def test_worker_stops_on_private(monkeypatch, settings, tmp_path):
    async def ytdlp(url, dest, quality, settings, cookies=None):
        raise ExtractError(ErrorCode.PRIVATE, "Private")

    embed_called = SimpleNamespace(v=False)

    async def embed(url, dest, settings):
        embed_called.v = True
        return []

    monkeypatch.setattr(ig, "ytdlp_download", ytdlp)
    monkeypatch.setattr(ig, "embed_download", embed)
    settings.ig_cookies_file = None
    with pytest.raises(ExtractError) as e:
        await ig.extract(_job(), tmp_path / "j", settings)
    assert e.value.code is ErrorCode.PRIVATE and not embed_called.v
