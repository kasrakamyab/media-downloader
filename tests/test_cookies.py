"""Cookies live in a read-only mount; extractors must never write back to them."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from core.contracts import ErrorCode, Quality
from core.extractors import cookie_copy, ytdlp_download, ytdlp_probe
from core.worker import ExtractError

ORIGINAL = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"


@pytest.fixture
def readonly_cookies(tmp_path: Path) -> Path:
    """A cookies file the way docker-compose mounts ./secrets: not writable."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    f = secrets / "youtube.txt"
    f.write_text(ORIGINAL)
    f.chmod(0o444)
    secrets.chmod(0o555)
    yield f
    secrets.chmod(0o755)
    f.chmod(0o644)


def test_cookie_copy_is_private_and_cleaned_up(readonly_cookies: Path, tmp_path: Path) -> None:
    work = tmp_path / "job"
    with cookie_copy(str(readonly_cookies), work) as jar:
        assert jar and Path(jar).parent == work
        assert Path(jar).read_text() == ORIGINAL
        assert oct(Path(jar).stat().st_mode & 0o777) == "0o600"
        Path(jar).write_text(ORIGINAL + "refreshed\n")  # what yt-dlp does on close
    assert not Path(jar).exists()
    assert readonly_cookies.read_text() == ORIGINAL


def test_cookie_copy_none_and_missing(tmp_path: Path) -> None:
    with cookie_copy(None, tmp_path) as jar:
        assert jar is None
    with pytest.raises(ExtractError) as e, cookie_copy(str(tmp_path / "nope.txt"), tmp_path):
        pass
    assert e.value.code is ErrorCode.UNKNOWN and "missing" in e.value.detail


@pytest.fixture
def fake_ytdlp(monkeypatch, tmp_path: Path):
    """A stand-in yt_dlp that behaves like the real one on close: it writes the cookie jar
    back to `cookiefile`. The real module can't be exercised without the network."""
    calls: dict = {"cookiefile": [], "written": []}

    class DownloadError(Exception):
        pass

    class YoutubeDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def download(self, urls):
            out = Path(self.opts["outtmpl"]).parent / "vid.mp4"
            out.write_bytes(b"video")

        def extract_info(self, url, download=False):
            if download:  # the real one downloads and returns the info dict
                self.download([url])
            return {"title": "t", "formats": [], "ext": "mp4"}

        def sanitize_info(self, info):
            return dict(info)

        def __exit__(self, *exc):
            jar = self.opts.get("cookiefile")
            calls["cookiefile"].append(jar)
            if jar:  # save_cookies(): raises OSError on a read-only path, like the real one
                with open(jar, "a") as fh:
                    fh.write("# refreshed\n")
                calls["written"].append(jar)

    mod = types.ModuleType("yt_dlp")
    mod.YoutubeDL = YoutubeDL
    mod.utils = types.SimpleNamespace(DownloadError=DownloadError, parse_bytes=lambda s: 1)
    monkeypatch.setitem(sys.modules, "yt_dlp", mod)
    return calls


async def test_download_with_readonly_cookies(
    fake_ytdlp, readonly_cookies: Path, settings, tmp_path: Path
) -> None:
    dest = tmp_path / "job1"
    got = await ytdlp_download(
        "https://youtu.be/x", dest, Quality.P360, settings, str(readonly_cookies)
    )
    files = got.files
    assert [f.name for f in files] == ["vid.mp4"]  # the cookie copy is not a "download"
    assert got.meta is not None and got.meta.title == "t"  # info dict feeds the caption
    jar = fake_ytdlp["cookiefile"][0]
    assert jar != str(readonly_cookies) and jar.startswith(str(dest))
    assert fake_ytdlp["written"] == [jar]
    assert not Path(jar).exists()
    assert readonly_cookies.read_text() == ORIGINAL


async def test_probe_with_readonly_cookies(fake_ytdlp, readonly_cookies: Path, settings) -> None:
    formats, title = await ytdlp_probe("https://youtu.be/x", settings, str(readonly_cookies))
    assert title == "t" and formats == []
    jar = fake_ytdlp["cookiefile"][0]
    assert jar.startswith(settings.job_dir) and not Path(jar).exists()
    assert readonly_cookies.read_text() == ORIGINAL


async def test_filesystem_error_is_a_clear_failure(fake_ytdlp, settings, tmp_path: Path) -> None:
    # Simulate the old failure mode: yt-dlp handed a path it cannot write.
    import yt_dlp

    orig_exit = yt_dlp.YoutubeDL.__exit__

    def exploding_exit(self, *exc):
        raise OSError(30, "Read-only file system", "/secrets/youtube.txt")

    yt_dlp.YoutubeDL.__exit__ = exploding_exit
    try:
        with pytest.raises(ExtractError) as e:
            await ytdlp_download("https://youtu.be/x", tmp_path / "j", Quality.P360, settings)
    finally:
        yt_dlp.YoutubeDL.__exit__ = orig_exit
    assert e.value.code is ErrorCode.UNKNOWN
    assert "Read-only file system" in e.value.detail


def test_ro_fixture_is_really_readonly_for_non_root(readonly_cookies: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores permission bits")
    with pytest.raises(PermissionError):
        readonly_cookies.write_text("x")


# --- COOKIE_STATE_DIR: keep the rotations a site sends back -------------------------------

HEADER = "# Netscape HTTP Cookie File\n"


def _line(name: str, value: str, expires: int = 4102444800) -> str:
    return f".youtube.com\tTRUE\t/\tTRUE\t{expires}\t{name}\t{value}\n"


def _values(path: Path) -> dict[str, str]:
    out = {}
    for raw in path.read_text().splitlines():
        parts = raw.split("\t")
        if len(parts) == 7 and not raw.startswith("# "):
            out[parts[5]] = parts[6]
    return out


@pytest.fixture
def export(tmp_path: Path) -> Path:
    f = tmp_path / "secrets" / "youtube.txt"
    f.parent.mkdir()
    f.write_text(HEADER + _line("SID", "s1") + _line("PSIDTS", "t1"))
    return f


def _rotate(jar: str, **values: str) -> None:
    """What yt-dlp does on close: rewrite the whole jar with the cookies it now holds."""
    current = _values(Path(jar)) | values
    Path(jar).write_text(HEADER + "".join(_line(k, v) for k, v in current.items()))


def test_rotation_is_kept_and_export_untouched(export: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    with cookie_copy(str(export), tmp_path / "j1", str(state)) as jar:
        assert _values(Path(jar)) == {"SID": "s1", "PSIDTS": "t1"}
        _rotate(jar, PSIDTS="t2")
    assert _values(state / "youtube.txt") == {"SID": "s1", "PSIDTS": "t2"}
    assert _values(export) == {"SID": "s1", "PSIDTS": "t1"}
    # the next job starts from the rotated jar, not from the export
    with cookie_copy(str(export), tmp_path / "j2", str(state)) as jar:
        assert _values(Path(jar))["PSIDTS"] == "t2"


def test_older_job_does_not_undo_a_newer_rotation(export: Path, tmp_path: Path) -> None:
    state = str(tmp_path / "state")
    with cookie_copy(str(export), tmp_path / "a", state) as jar_a:
        with cookie_copy(str(export), tmp_path / "b", state) as jar_b:
            _rotate(jar_b, PSIDTS="t2")
        _rotate(jar_a)  # A saves its whole jar, still holding t1: nothing it changed
    assert _values(tmp_path / "state" / "youtube.txt")["PSIDTS"] == "t2"


def test_new_export_reseeds_the_state(export: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    with cookie_copy(str(export), tmp_path / "j1", str(state)) as jar:
        _rotate(jar, PSIDTS="t2")
    export.write_text(HEADER + _line("SID", "fresh") + _line("PSIDTS", "f1"))
    os.utime(export, ns=(export.stat().st_atime_ns, export.stat().st_mtime_ns + 10**9))
    with cookie_copy(str(export), tmp_path / "j2", str(state)) as jar:
        assert _values(Path(jar)) == {"SID": "fresh", "PSIDTS": "f1"}


def test_failed_call_and_expired_cookies_are_not_merged(export: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    with pytest.raises(RuntimeError), cookie_copy(str(export), tmp_path / "j1", str(state)) as jar:
        _rotate(jar, PSIDTS="bad")
        raise RuntimeError("download failed")
    assert _values(state / "youtube.txt")["PSIDTS"] == "t1"
    with cookie_copy(str(export), tmp_path / "j2", str(state)) as jar:
        Path(jar).write_text(HEADER + _line("SID", "s1") + _line("PSIDTS", "gone", expires=1))
    assert _values(state / "youtube.txt")["PSIDTS"] == "t1"


async def test_download_uses_the_state_dir_from_settings(
    fake_ytdlp, readonly_cookies: Path, settings, tmp_path: Path
) -> None:
    settings.cookie_state_dir = str(tmp_path / "state")
    await ytdlp_download(
        "https://youtu.be/x", tmp_path / "j", Quality.P360, settings, str(readonly_cookies)
    )
    assert (tmp_path / "state" / "youtube.txt").is_file()
    assert readonly_cookies.read_text() == ORIGINAL
