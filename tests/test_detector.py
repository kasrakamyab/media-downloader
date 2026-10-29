from __future__ import annotations

import pytest

from core.contracts import Platform
from core.detector import UnsupportedURL, detect, find_urls, strip_tracking

YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
IG_P = "https://www.instagram.com/p/C1a2B3c4D5e/"
IG_R = "https://www.instagram.com/reel/C1a2B3c4D5e/"

CASES: list[tuple[str, Platform, str]] = [
    # YouTube
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", Platform.YOUTUBE, YT),
    ("https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s", Platform.YOUTUBE, YT),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&feature=share", Platform.YOUTUBE, YT),
    ("https://www.youtube.com/watch?feature=youtu.be&v=dQw4w9WgXcQ", Platform.YOUTUBE, YT),
    ("https://youtu.be/dQw4w9WgXcQ", Platform.YOUTUBE, YT),
    ("https://youtu.be/dQw4w9WgXcQ?si=abcDEF123", Platform.YOUTUBE, YT),
    ("http://youtu.be/dQw4w9WgXcQ", Platform.YOUTUBE, YT),
    ("youtu.be/dQw4w9WgXcQ", Platform.YOUTUBE, YT),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", Platform.YOUTUBE, YT),
    ("https://youtube.com/shorts/dQw4w9WgXcQ?feature=share", Platform.YOUTUBE, YT),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", Platform.YOUTUBE, YT),
    ("https://www.youtube.com/live/dQw4w9WgXcQ", Platform.YOUTUBE, YT),
    ("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RDAMVM", Platform.YOUTUBE, YT),
    ("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", Platform.YOUTUBE, YT),
    ("WWW.YOUTUBE.COM/watch?v=dQw4w9WgXcQ", Platform.YOUTUBE, YT),
    # Instagram
    ("https://www.instagram.com/p/C1a2B3c4D5e/", Platform.INSTAGRAM, IG_P),
    ("https://instagram.com/p/C1a2B3c4D5e", Platform.INSTAGRAM, IG_P),
    ("https://www.instagram.com/p/C1a2B3c4D5e/?igsh=MzRlODBiNWFlZA==", Platform.INSTAGRAM, IG_P),
    ("https://www.instagram.com/p/C1a2B3c4D5e/?utm_source=ig_web_copy", Platform.INSTAGRAM, IG_P),
    ("https://www.instagram.com/reel/C1a2B3c4D5e/", Platform.INSTAGRAM, IG_R),
    ("https://www.instagram.com/reels/C1a2B3c4D5e/", Platform.INSTAGRAM, IG_R),
    ("https://www.instagram.com/someuser/reel/C1a2B3c4D5e/", Platform.INSTAGRAM, IG_R),
    ("https://www.instagram.com/some.user_1/p/C1a2B3c4D5e/", Platform.INSTAGRAM, IG_P),
    ("https://instagr.am/p/C1a2B3c4D5e/", Platform.INSTAGRAM, IG_P),
    (
        "https://m.instagram.com/tv/C1a2B3c4D5e/",
        Platform.INSTAGRAM,
        "https://www.instagram.com/tv/C1a2B3c4D5e/",
    ),
    # Pinterest
    (
        "https://www.pinterest.com/pin/123456789012345678/",
        Platform.PINTEREST,
        "https://www.pinterest.com/pin/123456789012345678/",
    ),
    (
        "https://pinterest.com/pin/123456789012345678",
        Platform.PINTEREST,
        "https://www.pinterest.com/pin/123456789012345678/",
    ),
    (
        "https://www.pinterest.co.uk/pin/123456789012345678/",
        Platform.PINTEREST,
        "https://www.pinterest.com/pin/123456789012345678/",
    ),
    (
        "https://de.pinterest.com/pin/123456789012345678/?utm_campaign=x",
        Platform.PINTEREST,
        "https://www.pinterest.com/pin/123456789012345678/",
    ),
    # what pin.it share links actually redirect to
    (
        "https://www.pinterest.com/pin/636907572345689110/sent/?invite_code=abc123&sender=42&sfo=1",
        Platform.PINTEREST,
        "https://www.pinterest.com/pin/636907572345689110/",
    ),
    (
        "https://www.pinterest.com/pin/636907572345689110/feedback/",
        Platform.PINTEREST,
        "https://www.pinterest.com/pin/636907572345689110/",
    ),
    (
        "https://www.pinterest.com/someuser/my-board/",
        Platform.PINTEREST,
        "https://www.pinterest.com/someuser/my-board/",
    ),
    (
        "https://www.pinterest.ca/someuser/my-board",
        Platform.PINTEREST,
        "https://www.pinterest.com/someuser/my-board/",
    ),
    ("https://pin.it/7abcDEF", Platform.PINTEREST, "https://pin.it/7abcDEF"),
    ("pin.it/7abcDEF", Platform.PINTEREST, "https://pin.it/7abcDEF"),
]


@pytest.mark.parametrize(("raw", "platform", "norm"), CASES)
def test_detect(raw: str, platform: Platform, norm: str) -> None:
    d = detect(raw)
    assert d.platform == platform
    assert d.url == norm


def test_same_video_same_cache_key() -> None:
    forms = [c[0] for c in CASES if c[1] == Platform.YOUTUBE]
    assert {detect(f).url for f in forms} == {YT}


UNSUPPORTED = [
    "",
    "hello",
    "https://example.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/",
    "https://www.youtube.com/watch?v=short",
    "https://www.youtube.com/@channel",
    "https://www.youtube.com/playlist?list=PL123",
    "https://youtu.be/",
    "https://www.instagram.com/someuser/",
    "https://www.instagram.com/explore/tags/cats/",
    "https://www.instagram.com/stories/user/123/",
    "https://www.pinterest.com/",
    "https://www.pinterest.com/search/pins/?q=cats",
    "https://www.pinterest.com/pin/not-a-number/",
    "https://www.pinterest.com/ideas/cats/",
    "https://pin.it/",
    "https://tiktok.com/@x/video/123",
    "ftp://youtube.com/watch?v=dQw4w9WgXcQ",
]


@pytest.mark.parametrize("raw", UNSUPPORTED)
def test_unsupported(raw: str) -> None:
    with pytest.raises(UnsupportedURL):
        detect(raw)


def test_find_urls() -> None:
    text = "look at this https://youtu.be/dQw4w9WgXcQ, and www.instagram.com/p/abc/ too!"
    assert find_urls(text) == ["https://youtu.be/dQw4w9WgXcQ", "www.instagram.com/p/abc/"]
    assert find_urls("") == []
    assert find_urls("no links here") == []


def test_strip_tracking() -> None:
    assert (
        strip_tracking("https://x.com/a?utm_source=1&id=5&fbclid=2#frag") == "https://x.com/a?id=5"
    )
    assert strip_tracking("https://x.com/a") == "https://x.com/a"
