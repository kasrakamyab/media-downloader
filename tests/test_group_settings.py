"""G2: per-group settings, auto mode, quality cap, platform filter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from arq.connections import ArqRedis
from arq.jobs import deserialize_job

from core.breaker import Breaker
from core.cache import FileCache
from core.contracts import QUEUE_NAMES, FormatOption, Job, Platform, Quality
from core.groups import GroupRegistry, GroupSettings, cap_quality, within_cap
from core.health import beat
from core.limits import UserLimits
from core.telegram import FakeSender
from core.worker import ExtractError, run_job
from gateway.handlers import WORKING, extract_ask, on_text, probe_key
from gateway.settings_handlers import apply, keyboard, on_settings, on_settings_tap, render
from tests.fakes import FakeMessage, FakeQuery

YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
IG = "https://www.instagram.com/reel/C1a2B3c4D5e/"
GROUP = -100123
BOT = "Mamad_AlertsBot"
MENU = [
    FormatOption(quality=Quality.P1080, size_bytes=40_000_000),
    FormatOption(quality=Quality.P720, size_bytes=20_000_000),
    FormatOption(quality=Quality.P360, size_bytes=5_000_000),
    FormatOption(quality=Quality.AUDIO, size_bytes=1_000_000),
]


# ---- pure pieces ----------------------------------------------------------------------


def test_cap_quality() -> None:
    assert cap_quality(Quality.BEST, Quality.BEST) is Quality.BEST
    assert cap_quality(Quality.BEST, Quality.P480) is Quality.P480
    assert cap_quality(Quality.P720, Quality.P480) is Quality.P480
    assert cap_quality(Quality.P360, Quality.P480) is Quality.P360
    assert cap_quality(Quality.AUDIO, Quality.P360) is Quality.AUDIO
    assert within_cap(Quality.P1080, Quality.BEST) and not within_cap(Quality.P1080, Quality.P720)
    assert within_cap(Quality.AUDIO, Quality.P360)


def test_settings_roundtrip_and_defaults() -> None:
    s = GroupSettings(auto=True, max_quality=Quality.P480, platforms=[Platform.YOUTUBE])
    assert GroupSettings.from_hash(s.to_hash()) == s
    assert GroupSettings.from_hash({}) == GroupSettings()
    assert GroupSettings.from_hash({"max_quality": "nope", "platforms": "youtube,bogus"}) == (
        GroupSettings(platforms=[Platform.YOUTUBE])
    )
    assert GroupSettings().allows(Platform.PINTEREST)


def test_apply_transitions() -> None:
    s = GroupSettings()
    assert s.auto  # on by default
    s = apply(s, "auto", "")
    assert not s.auto
    s = apply(s, "maxq", "")
    assert s.max_quality is Quality.P1080
    for _ in range(5):
        s = apply(s, "maxq", "")
    assert s.max_quality is Quality.BEST  # cycled back
    s = apply(s, "plat", "instagram")
    assert s.platforms == [Platform.YOUTUBE, Platform.PINTEREST]
    s = apply(s, "plat", "instagram")
    assert s.platforms == list(Platform)  # order restored
    assert apply(s, "plat", "tiktok") == s and apply(s, "bogus", "") == s


def test_render_and_keyboard() -> None:
    s = GroupSettings(auto=True, max_quality=Quality.P720, platforms=[Platform.YOUTUBE])
    text = render("Cats & <dogs>", s)
    assert "Cats &amp; &lt;dogs&gt;" in text and "<b>on</b>" in text and "720p" in text
    kb = keyboard(GROUP, s)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert labels[0] == "Auto-download: ON" and labels[1] == "Max quality: 720p ▸"
    assert labels[2:5] == ["✅ YouTube", "⬜ Instagram", "⬜ Pinterest"]
    assert kb.inline_keyboard[2][1].callback_data == f"gs:{GROUP}:plat:instagram"


# ---- registry ---------------------------------------------------------------------------


async def test_registry_settings(redis) -> None:
    groups = GroupRegistry(redis)
    assert await groups.get_settings(GROUP) == GroupSettings()
    await groups.save_settings(GROUP, GroupSettings(auto=False, max_quality=Quality.P360))
    s = await groups.get_settings(GROUP)
    assert not s.auto and s.max_quality is Quality.P360
    assert await groups.auto_off_count() == 1
    await groups.add(GROUP, "g", "supergroup", 1)
    await groups.remove(GROUP)
    assert await groups.get_settings(GROUP) == GroupSettings()  # removed with the group


# ---- /settings handlers -----------------------------------------------------------------


class AdminBot:
    def __init__(self, admins: set[int]) -> None:
        self.admins = admins

    async def get_chat_member(self, chat_id: int, user_id: int):
        return SimpleNamespace(status="administrator" if user_id in self.admins else "member")


class SettingsMessage(FakeMessage):
    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.sender_chat = None
        self.edits: list[tuple[str, object]] = []

    async def edit_text(self, text: str, reply_markup=None, **_) -> None:
        self.edits.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup=None, **_) -> None:
        self.edits.append(("<markup>", reply_markup))


async def test_settings_command_admin_only(redis) -> None:
    groups = GroupRegistry(redis)
    bot = AdminBot({1})
    m = SettingsMessage("/settings", user_id=2, chat_id=GROUP, chat_type="supergroup")
    await on_settings(m, bot, groups)
    assert "Only group admins" in m.replies[0]
    m = SettingsMessage("/settings", user_id=1, chat_id=GROUP, chat_type="supergroup")
    await on_settings(m, bot, groups)
    assert "Settings for" in m.replies[0]
    private = SettingsMessage("/settings", user_id=1)
    await on_settings(private, bot, groups)
    assert "inside a group" in private.answers[0]


async def test_settings_tap_changes_and_guards(redis) -> None:
    groups = GroupRegistry(redis)
    bot = AdminBot({1})
    msg = SettingsMessage("x", chat_id=GROUP, chat_type="supergroup")

    q = FakeQuery(f"gs:{GROUP}:auto:", user_id=1)
    q.message = msg
    await on_settings_tap(q, bot, groups)
    assert (await groups.get_settings(GROUP)).auto is False  # toggled off from the default
    assert msg.edits and "Auto-download: <b>off</b>" in msg.edits[-1][0]

    q = FakeQuery(f"gs:{GROUP}:plat:youtube", user_id=2)  # not an admin
    q.message = msg
    await on_settings_tap(q, bot, groups)
    assert (await groups.get_settings(GROUP)).allows(Platform.YOUTUBE)
    assert "Only group admins" in q.answers[0][0][0]

    q = FakeQuery(f"gs:{GROUP + 1}:auto:", user_id=1)  # keyboard from another chat
    q.message = msg
    await on_settings_tap(q, bot, groups)
    assert "another chat" in q.answers[0][0][0]

    q = FakeQuery(f"gs:{GROUP}:close:", user_id=1)
    q.message = msg
    await on_settings_tap(q, bot, groups)
    assert msg.edits[-1] == ("<markup>", None)


# ---- behaviour in on_text ---------------------------------------------------------------


@pytest.fixture
async def arq(redis) -> ArqRedis:
    pool = ArqRedis(connection_pool=redis.connection_pool)
    yield pool
    await pool.aclose()


@pytest.fixture
async def deps(arq, settings):
    for p in Platform:
        await beat(arq, p, 60)
    settings.quality_choice_timeout = 1
    import json

    await arq.set(probe_key(YT), json.dumps([f.model_dump(mode="json") for f in MENU]))
    return {
        "settings": settings,
        "arq": arq,
        "limits": UserLimits(arq, settings.max_active_jobs, settings.hourly_limit),
        "cache": FileCache(arq),
        "breaker": Breaker(arq),
        "bot_username": BOT,
        "groups": GroupRegistry(arq),
    }


async def _queued(arq: ArqRedis, platform: Platform) -> list[Job]:
    ids = await arq.zrange(QUEUE_NAMES[platform], 0, -1)
    out = []
    for jid in ids:
        raw = await arq.get(f"arq:job:{jid.decode()}")
        out.append(Job.model_validate(deserialize_job(raw).args[0]))
    return sorted(out, key=lambda j: j.requested_at)  # the queue zset has no stable order


def test_extract_ask_auto_mode() -> None:
    m = FakeMessage(f"look {YT}", chat_type="supergroup")
    assert extract_ask(m, BOT, auto=False) is None
    ask = extract_ask(m, BOT, auto=True)
    assert ask and not ask.explicit and ask.source is m
    assert extract_ask(FakeMessage("no link", chat_type="supergroup"), BOT, auto=True) is None
    explicit = extract_ask(FakeMessage(f"/dl {YT}", chat_type="supergroup"), BOT, auto=True)
    assert explicit and explicit.explicit


async def test_auto_mode_enqueues_quietly_without_menu(arq, deps) -> None:
    await deps["groups"].save_settings(GROUP, GroupSettings(auto=True, max_quality=Quality.P720))
    m = FakeMessage(f"watch {YT}", chat_id=GROUP, chat_type="supergroup")
    await on_text(m, **deps)
    assert m.replies == [] and m.answers == []  # no placeholder, no menu
    (job,) = await _queued(arq, Platform.YOUTUBE)
    assert job.quiet and job.message_id == 0
    assert job.quality is Quality.P720  # cap used directly
    assert job.reply_to_message_id == m.message_id


async def test_auto_mode_refusals_are_silent(arq, deps) -> None:
    await deps["groups"].save_settings(
        GROUP, GroupSettings(auto=True, platforms=[Platform.YOUTUBE])
    )
    m = FakeMessage(f"look {IG}", chat_id=GROUP, chat_type="supergroup")
    await on_text(m, **deps)
    assert m.replies == [] and m.answers == []
    assert await _queued(arq, Platform.INSTAGRAM) == []
    m2 = FakeMessage("https://tiktok.com/@a/video/1", chat_id=GROUP, chat_type="supergroup")
    await on_text(m2, **deps)
    assert m2.replies == []


async def test_explicit_ask_platform_off_is_told(arq, deps) -> None:
    await deps["groups"].save_settings(GROUP, GroupSettings(platforms=[Platform.YOUTUBE]))
    m = FakeMessage(f"/dl {IG}", chat_id=GROUP, chat_type="supergroup")
    await on_text(m, **deps)
    assert "Instagram links are turned off" in m.replies[0]
    assert await _queued(arq, Platform.INSTAGRAM) == []


async def test_explicit_ask_menu_is_capped(arq, deps) -> None:
    await deps["groups"].save_settings(GROUP, GroupSettings(max_quality=Quality.P720))
    m = FakeMessage(f"/dl {YT}", chat_id=GROUP, chat_type="supergroup")
    await on_text(m, **deps)
    assert m.replies[0] == WORKING and m.replies[1].startswith("Pick a quality")
    labels = [b.text for row in m.markups[0].inline_keyboard for b in row]
    assert labels == ["720p · 19 MB", "360p · 4.8 MB", "Audio only · 1.0 MB"]  # no 1080p


async def test_cap_without_menu_choice(arq, deps) -> None:
    await deps["groups"].save_settings(GROUP, GroupSettings(max_quality=Quality.P360))
    m = FakeMessage(f"/dl {YT}", chat_id=GROUP, chat_type="supergroup")
    await on_text(m, **deps)
    # only 360p and audio fit under the cap: still a choice of two -> menu
    labels = [b.text for row in m.markups[0].inline_keyboard for b in row]
    assert labels == ["360p · 4.8 MB", "Audio only · 1.0 MB"]

    await deps["groups"].save_settings(GROUP, GroupSettings(max_quality=Quality.AUDIO))
    m2 = FakeMessage(f"/dl {YT}", chat_id=GROUP, chat_type="supergroup")
    await on_text(m2, **deps)
    assert m2.markups == []  # one option: straight to the queue
    jobs = await _queued(arq, Platform.YOUTUBE)
    assert jobs[-1].quality is Quality.AUDIO


async def test_private_chats_ignore_group_settings(arq, deps) -> None:
    m = FakeMessage(f"{YT}", chat_id=7)
    await on_text(m, **deps)
    assert m.replies[1].startswith("Pick a quality")  # full menu, nothing capped


# ---- worker: quiet jobs -----------------------------------------------------------------


async def test_quiet_job_never_edits_or_reports(redis, settings) -> None:
    job = Job(
        chat_id=GROUP,
        user_id=2,
        message_id=0,
        quiet=True,
        url="https://x/q",
        platform=Platform.YOUTUBE,
        chat_type="supergroup",
        reply_to_message_id=12,
    )
    limits = UserLimits(redis)

    async def ok(job, dest, settings):
        f = dest / "v.mp4"
        f.write_bytes(b"x")
        return [f]

    async def fails(job, dest, settings):
        raise ExtractError(ErrorCode.PRIVATE, "x")

    from core.contracts import ErrorCode  # noqa: F811

    sender = FakeSender()
    r = await run_job(
        job,
        extractor=ok,
        sender=sender,
        cache=FileCache(redis),
        limits=limits,
        redis=redis,
        settings=settings,
    )
    assert r.status == "ok" and sender.finished == [] and sender.targets == [(12, None)]

    sender = FakeSender()
    r = await run_job(
        job.model_copy(update={"url": "https://x/q2"}),
        extractor=fails,
        sender=sender,
        cache=FileCache(redis),
        limits=limits,
        redis=redis,
        settings=settings,
    )
    assert r.status == "error" and sender.finished == []
