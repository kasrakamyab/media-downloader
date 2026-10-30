"""Telegram handlers. The gateway never downloads: detect -> availability -> limits ->
placeholder -> (quality keyboard for YouTube) -> enqueue.

Private chats: any message with a link is a request. Groups: only explicit asks are —
`/dl <link>`, `/dl` as a reply to a message with a link, or `@bot <link>` — and the result
replies to the message that carried the link, inside its forum topic if any.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)
from arq.connections import ArqRedis
from pydantic import BaseModel

from core.breaker import Breaker
from core.cache import FileCache, url_hash
from core.contracts import (
    JOB_FUNCTION,
    PROBE_FUNCTION,
    QUEUE_NAMES,
    FormatOption,
    Job,
    Platform,
    ProbeResult,
    Quality,
)
from core.detector import UnsupportedURL, detect, find_urls
from core.groups import GroupRegistry, GroupSettings, cap_quality, within_cap
from core.health import is_alive, stats
from core.limits import UserLimits
from core.logging import get_logger
from core.settings import Settings
from gateway.identity import BotIdentity
from core.telegram import REACT_WORKING

log = get_logger("gateway")
router = Router()

PLATFORM_LABEL = {
    Platform.YOUTUBE: "YouTube",
    Platform.INSTAGRAM: "Instagram",
    Platform.PINTEREST: "Pinterest",
}
# Platforms whose worker answers probes; the others have one quality and skip the keyboard.
PROBE_PLATFORMS = {Platform.YOUTUBE}
GROUP_TYPES = {"group", "supergroup"}
MEMBER_STATUSES = {"member", "administrator", "restricted", "creator"}
GONE_STATUSES = {"left", "kicked"}

WORKING = "⏳ Working on it…"
HELP = (
    "Send me a <b>YouTube</b>, <b>Instagram</b> or <b>Pinterest</b> link and I'll reply with "
    "the video or pictures.\n\n"
    "• YouTube: videos and Shorts; pick a quality from the menu, or I choose the best one "
    "under the size limit after {choice}s\n"
    "• Instagram: public posts, reels and carousels (up to 10 items)\n"
    "• Pinterest: pins and short boards (up to 10 items)\n\n"
    "Add <code>audio</code> after a YouTube link to skip the menu and get just the sound.\n\n"
    "In groups I only act when asked: <code>/dl &lt;link&gt;</code>, reply to a message that "
    "has a link with <code>/dl</code>, or mention me with the link.\n\n"
    "Limits: {active} downloads at a time, {hourly} per hour.\n"
    "For personal use only. Private content is refused."
)


def group_help(bot_username: str, can_see: bool, joined: bool = False, auto: bool = True) -> str:
    """What works in this group right now. With privacy mode on and no admin rights Telegram
    only delivers /commands and replies to the bot; as an admin the bot sees every message
    and, unless admins turned it off, grabs every link on its own."""
    bot = bot_username or "bot"
    head = "👋 Thanks for adding me. " if joined else ""
    ways = [
        "• <code>/dl &lt;link&gt;</code>",
        "• reply to a message that has a link with <code>/dl</code>",
    ]
    if can_see:
        ways.append(f"• <code>@{bot} &lt;link&gt;</code>")
        if auto:
            lead = (
                "I'm an admin here, so <b>just post a link and I'll grab it</b> — no command "
                "needed. You can also ask explicitly:\n"
            )
            tail = "Admins: /settings turns auto-download off or caps the quality."
        else:
            lead = "Auto-download is off in this group (admins: /settings). Ask me with:\n"
            tail = ""
    else:
        lead = "Ask me with:\n"
        tail = (
            "<b>Make me an admin</b> and I'll grab every link posted here on my own. Until "
            "then Telegram only shows me /dl commands and replies to my messages."
        )
    body = (
        "\n".join(ways)
        + "\nI reply to the message that had the link; add <code>audio</code> for sound only."
    )
    return f"{head}{lead}{body}" + (f"\n\n{tail}" if tail else "")


PROMOTED_TEXT = (
    "Thanks for making me an admin! From now on just post a link and I'll grab it; "
    "<code>@{bot} &lt;link&gt;</code> and /dl work too. Admins: /settings to turn "
    "auto-download off or cap the quality."
)
DEMOTED_TEXT = (
    "I've lost admin rights, so Telegram will only show me <code>/dl</code> commands and "
    "replies to my messages again."
)


class PendingChoice(BaseModel):
    """A job waiting for the user to pick a quality (or for the timeout to pick for them)."""

    job: Job
    default: Quality
    offered: list[Quality]


@dataclass(frozen=True, slots=True)
class Ask:
    """A message that asks us for a download: the text to search and the message to reply to.
    `explicit` is False in auto mode, where nobody asked and the bot must stay quiet on
    anything but success."""

    text: str
    source: Message
    explicit: bool = True


def pending_key(job_id: str) -> str:
    return f"pending:{job_id}"


def probe_key(url: str) -> str:
    return f"probe:{url_hash(url)}"


def is_group(message: Message) -> bool:
    return message.chat.type in GROUP_TYPES


def message_text(message: Message) -> str:
    """Text or caption, plus the targets of any text_link entities (hidden links)."""
    text = message.text or message.caption or ""
    for ent in (message.entities or []) + (message.caption_entities or []):
        if ent.type == "text_link" and ent.url:
            text += " " + ent.url
    return text


def extract_ask(message: Message, bot_username: str, auto: bool = False) -> Ask | None:
    """Decide whether a message asks for a download.

    Private chat: anything with a link. Group: only `/dl`, a reply with `/dl`, or a mention —
    plus, with the group's auto-download on, any message that carries a link (quietly).
    Returns None when the bot should stay silent.
    """
    text = message_text(message)
    if not is_group(message):
        return Ask(text, message)
    lowered = text.lower()
    is_command = lowered.startswith("/dl")
    mentioned = bool(bot_username) and f"@{bot_username.lower()}" in lowered
    if not (is_command or mentioned):
        if auto and find_urls(text):
            return Ask(text, message, explicit=False)
        return None
    if find_urls(text):
        return Ask(text, message)
    reply = message.reply_to_message
    if reply:
        quoted = message_text(reply)
        if find_urls(quoted):
            return Ask(quoted + " " + text, reply)  # keep e.g. the `audio` keyword
    return Ask(text, message)  # asked, but no link: on_text answers with a hint


async def say(message: Message, text: str, *, reply: bool | None = None) -> Message | None:
    """Reply in groups (keeps context), plain answer in private chats. Never raises."""
    try:
        if reply if reply is not None else is_group(message):
            return await message.reply(text)
        return await message.answer(text)
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        log.warning("cannot_reply", chat_id=message.chat.id, error=str(e)[:120])
        return None


async def platform_available(arq: ArqRedis, breaker: Breaker, platform: Platform) -> bool:
    return await is_alive(arq, platform) and not await breaker.is_open(platform)


def unavailable_text(platform: Platform, others_up: list[Platform]) -> str:
    text = f"{PLATFORM_LABEL[platform]} is temporarily unavailable."
    if others_up:
        names = [PLATFORM_LABEL[p] for p in others_up]
        verb = "works" if len(names) == 1 else "work"
        text += f" {' and '.join(names)} still {verb}."
    return text


def _help(settings: Settings) -> str:
    return HELP.format(
        active=settings.max_active_jobs,
        hourly=settings.hourly_limit,
        choice=settings.quality_choice_timeout,
    )


async def bot_can_see(bot: Bot, chat_id: int, bot_can_read_all: bool) -> bool:
    """Does Telegram deliver plain group messages to us here? Yes when privacy mode is off
    globally, or when the bot is an admin of this chat."""
    if bot_can_read_all:
        return True
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        log.info("self_member_check_failed", chat_id=chat_id, error=str(e)[:100])
        return False
    return member.status == "administrator"


def _who(
    identity: BotIdentity | None, bot_username: str, bot_can_read_all: bool
) -> tuple[str, bool]:
    """Username and privacy-off flag: from the live identity holder when the dispatcher
    provides one (always, in production), else from the plain kwargs (tests)."""
    if identity is not None and identity.resolved:
        return identity.username, identity.can_read_all
    return bot_username, bot_can_read_all


@router.message(CommandStart())
async def on_start(
    message: Message,
    settings: Settings,
    bot: Bot,
    identity: BotIdentity | None = None,
    bot_username: str = "",
    bot_can_read_all: bool = False,
) -> None:
    if is_group(message):
        username, read_all = _who(identity, bot_username, bot_can_read_all)
        can_see = await bot_can_see(bot, message.chat.id, read_all)
        await say(message, group_help(username, can_see))
        return
    await message.answer("👋 Hi! " + _help(settings))


@router.message(Command("help"))
async def on_help(
    message: Message,
    settings: Settings,
    bot: Bot,
    identity: BotIdentity | None = None,
    bot_username: str = "",
    bot_can_read_all: bool = False,
    groups: GroupRegistry | None = None,
) -> None:
    if is_group(message):
        username, read_all = _who(identity, bot_username, bot_can_read_all)
        can_see = await bot_can_see(bot, message.chat.id, read_all)
        auto = (await groups.get_settings(message.chat.id)).auto if groups else True
        await say(message, group_help(username, can_see, auto=auto))
        return
    await message.answer(_help(settings))


@router.message(Command("stats"))
async def on_stats(
    message: Message,
    settings: Settings,
    arq: ArqRedis,
    breaker: Breaker,
    groups: GroupRegistry | None = None,
) -> None:
    if not message.from_user or message.from_user.id not in settings.admin_id_set:
        return
    lines = []
    total: dict[str, int] = {"ok": 0, "error": 0}
    for platform in Platform:
        s = await stats(arq, platform)
        g = await stats(arq, platform, "group")
        alive = await is_alive(arq, platform)
        open_ = await breaker.is_open(platform)
        depth = await arq.zcard(QUEUE_NAMES[platform])
        ok, err = s.get("ok", 0), s.get("error", 0)
        hits = s.get("cache_hit", 0)
        rate = f"{hits / ok:.0%}" if ok else "–"
        if open_:
            opened = await breaker.opened_at(platform) or time.time()
            state = f"🟠 breaker open {int((time.time() - opened) / 60)} min"
        elif alive:
            state = f"🟢 up, {await breaker.failures(platform)} recent failures"
        else:
            state = "🔴 no heartbeat"
        lines.append(
            f"<b>{PLATFORM_LABEL[platform]}</b> — {state}\n"
            f"   queue={depth} ok={ok} err={err} cache hits={rate}\n"
            f"   in groups: ok={g.get('ok', 0)} err={g.get('error', 0)}"
        )
        total["ok"] += ok
        total["error"] += err
    if groups is not None:
        lines.append(
            f"<b>Groups</b>: {await groups.count()} joined, "
            f"{await groups.auto_off_count()} with auto-download turned off"
        )
    lines.append(f"<b>Total</b>: ok={total['ok']} err={total['error']}")
    await message.answer("\n".join(lines))


# ---- membership -----------------------------------------------------------------------


async def _announce(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        log.info("group_announce_failed", chat_id=chat_id, error=str(e)[:120])


@router.my_chat_member()
async def on_my_chat_member(
    event: ChatMemberUpdated,
    bot: Bot,
    groups: GroupRegistry,
    identity: BotIdentity | None = None,
    bot_username: str = "",
    bot_can_read_all: bool = False,
) -> None:
    chat = event.chat
    if chat.type not in GROUP_TYPES:
        return  # private block/unblock, channels: nothing to track yet
    bot_username, bot_can_read_all = _who(identity, bot_username, bot_can_read_all)
    old, new = event.old_chat_member.status, event.new_chat_member.status
    # The update itself says whether we're an admin now; no extra API call needed.
    can_see = bot_can_read_all or new == "administrator"
    if new in MEMBER_STATUSES and old not in MEMBER_STATUSES:
        await groups.add(chat.id, chat.title or "", chat.type, event.from_user.id)
        await groups.set_visibility(chat.id, can_see)
        log.info(
            "group_joined", chat_id=chat.id, title=chat.title, by=event.from_user.id, sees=can_see
        )
        await _announce(bot, chat.id, group_help(bot_username, can_see, joined=True))
    elif new in GONE_STATUSES and old not in GONE_STATUSES:
        await groups.remove(chat.id)
        log.info("group_left", chat_id=chat.id, title=chat.title, status=new)
    elif new == "administrator" and old != "administrator":
        await groups.set_visibility(chat.id, True)
        log.info("group_promoted", chat_id=chat.id, title=chat.title)
        await _announce(bot, chat.id, PROMOTED_TEXT.format(bot=bot_username or "bot"))
    elif old == "administrator" and new in MEMBER_STATUSES:
        await groups.set_visibility(chat.id, can_see)
        log.info("group_demoted", chat_id=chat.id, title=chat.title)
        if not can_see:
            await _announce(bot, chat.id, DEMOTED_TEXT)


@router.message(F.migrate_to_chat_id)
async def on_migrate(message: Message, groups: GroupRegistry) -> None:
    """Group became a supergroup: Telegram gives it a new chat id."""
    new_id = message.migrate_to_chat_id
    if new_id and await groups.migrate(message.chat.id, new_id):
        log.info("group_migrated", old=message.chat.id, new=new_id)


# ---- download requests ----------------------------------------------------------------


@router.message(F.text | F.caption)
async def on_text(
    message: Message,
    settings: Settings,
    arq: ArqRedis,
    limits: UserLimits,
    cache: FileCache,
    breaker: Breaker,
    bot_username: str = "",
    groups: GroupRegistry | None = None,
    identity: BotIdentity | None = None,
    bot: Bot | None = None,
) -> None:
    if not message.from_user:
        return
    bot_username, read_all = _who(identity, bot_username, False)
    group = is_group(message)
    if group and getattr(message.from_user, "is_bot", False):
        return  # another bot (e.g. a second downloader in the same group): never a request
    gs = await groups.get_settings(message.chat.id) if (group and groups) else GroupSettings()
    ask = extract_ask(message, bot_username, auto=gs.auto)
    if ask is None:
        return  # a group message that isn't for us
    if not ask.explicit and groups is not None and not read_all:
        # Plain link in auto mode: only where Telegram is known to show us messages (admin
        # here, or privacy off). A stale "can_see=0" after a promotion is corrected by the
        # my_chat_member update that promotion sends.
        info = await groups.get(message.chat.id)
        if info is not None and info.get("can_see") == "0":
            return

    async def tell(text: str) -> None:
        """User-facing refusals: spoken when asked, swallowed in auto mode."""
        if ask.explicit:
            await say(message, text)

    urls = find_urls(ask.text)
    if not urls:
        await tell(
            "Send me a link to a YouTube video, Instagram post or Pinterest pin."
            if not group
            else "Give me a link: <code>/dl &lt;link&gt;</code>, or reply to one with /dl."
        )
        return

    try:
        found = detect(urls[0])
    except UnsupportedURL:
        await tell("I can't handle that link yet. I support YouTube, Instagram and Pinterest.")
        return

    if group and not gs.allows(found.platform):
        await tell(
            f"{PLATFORM_LABEL[found.platform]} links are turned off in this group "
            "(admins: /settings)."
        )
        return

    if not ask.explicit:
        # Two people sharing the same video within a couple of minutes get one download.
        fresh = await arq.set(
            f"recent:{message.chat.id}:{url_hash(found.url)}",
            "1",
            ex=settings.auto_dedupe_seconds,
            nx=True,
        )
        if not fresh:
            log.info("auto_duplicate_skipped", chat_id=message.chat.id, url=found.url)
            return

    quality = Quality.BEST
    if found.platform == Platform.YOUTUBE and "audio" in ask.text.lower().split():
        quality = Quality.AUDIO
    quality = cap_quality(quality, gs.max_quality)

    if await cache.is_blocked(found.url):
        await tell("That link has been blocked on this bot.")
        return

    if not await platform_available(arq, breaker, found.platform):
        others = [
            p for p in Platform if p != found.platform and await platform_available(arq, breaker, p)
        ]
        await tell(unavailable_text(found.platform, others))
        return

    chat_scope = message.chat.id if group else None
    reason = await limits.try_acquire(message.from_user.id, chat_scope)
    if reason:
        await tell(
            {
                "hourly": f"You've hit the limit of {settings.hourly_limit} requests per hour. "
                "Try again a bit later.",
                "active": f"You already have {settings.max_active_jobs} downloads running. "
                "Wait for them to finish first.",
                "group_hourly": f"This group has used its {settings.group_hourly_limit} "
                "downloads for this hour. Try again later.",
                "group_active": f"This group already has {settings.max_active_per_chat} "
                "downloads running. Wait for them to finish.",
            }[reason]
        )
        return

    # Progress: a 👀 reaction on the user's message (nothing to delete later), or, where
    # reactions aren't allowed, the classic "Working on it…" reply. Auto mode posts nothing
    # until the media itself, but may still react.
    placeholder = None
    react_id = None
    if settings.use_reactions and bot is not None:
        if await _react(bot, message.chat.id, ask.source.message_id, REACT_WORKING):
            react_id = ask.source.message_id
    if react_id is None and ask.explicit:
        placeholder = await say(ask.source, WORKING, reply=True)  # attached to the link
        if placeholder is None:  # can't even post text here: nothing more we can do
            await limits.release(message.from_user.id, chat_scope)
            return
    job = Job(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        message_id=placeholder.message_id if placeholder else 0,
        react_message_id=react_id,
        quiet=not ask.explicit,
        url=found.url,
        platform=found.platform,
        quality=quality,
        chat_type=message.chat.type,
        reply_to_message_id=ask.source.message_id if group else None,
        thread_id=message.message_thread_id if (group and message.is_topic_message) else None,
    )

    # The menu is for people who asked. A cached answer for the default quality skips it
    # too: the worker serves that instantly.
    if (
        ask.explicit
        and found.platform in PROBE_PLATFORMS
        and quality in (Quality.BEST, gs.max_quality)
        and not await cache.get(found.url, quality)
    ):
        offered = [
            f
            for f in await _quality_menu(arq, found.url, settings)
            if within_cap(f.quality, gs.max_quality)
        ]
        if len(offered) >= 2 and await _offer_choice(
            arq, ask.source, placeholder, job, offered, settings
        ):
            return

    await _enqueue(arq, limits, job, settings, placeholder)


async def _react(bot: Bot, chat_id: int, message_id: int, emoji: str) -> bool:
    """Set our reaction on a message; False where the chat doesn't allow it (reactions off,
    no rights, old group) so the caller can fall back to a message."""
    try:
        await bot.set_message_reaction(
            chat_id, message_id, reaction=[ReactionTypeEmoji(emoji=emoji)]
        )
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.info("reaction_unavailable", chat_id=chat_id, error=str(e)[:100])
        return False


async def _dismiss_menu(menu: Message | None, job: Job) -> None:
    """After a choice: with a reaction in play the menu is just noise, delete it; otherwise
    the menu message doubles as the placeholder and turns into "Working on it…"."""
    if menu is None:
        return
    if job.react_message_id:
        try:
            await menu.delete()
            job.message_id = 0
            return
        except (TelegramBadRequest, TelegramForbiddenError):
            pass  # can't delete: keep it as the placeholder instead
    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await menu.edit_text(WORKING)


async def _quality_menu(arq: ArqRedis, url: str, settings: Settings) -> list[FormatOption]:
    """Ask the platform worker for formats (cached for a few minutes); [] when unknown."""
    cached = await arq.get(probe_key(url))
    if cached:
        with contextlib.suppress(ValueError):
            return [FormatOption.model_validate(o) for o in json.loads(cached)]
    try:
        handle = await arq.enqueue_job(
            PROBE_FUNCTION,
            url,
            _queue_name=QUEUE_NAMES[Platform.YOUTUBE],
            _expires=settings.extract_timeout,
        )
        if handle is None:  # same probe already queued: wait for it instead
            return []
        raw = await handle.result(timeout=settings.extract_timeout + 10, poll_delay=0.25)
        result = ProbeResult.model_validate(raw)
    except Exception as e:  # timeout, worker gone, bad payload: fall back to automatic quality
        log.warning("probe_unavailable", url=url, error=repr(e))
        return []
    offered = [
        f for f in result.formats if f.size_bytes is None or f.size_bytes <= settings.max_file_bytes
    ]
    await arq.set(
        probe_key(url),
        json.dumps([f.model_dump(mode="json") for f in offered]),
        ex=settings.probe_cache_ttl,
    )
    return offered


def quality_keyboard(job_id: str, offered: list[FormatOption]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f.label, callback_data=f"q:{job_id}:{f.quality.value}")]
        for f in offered
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _offer_choice(
    arq: ArqRedis,
    source: Message,
    placeholder: Message | None,
    job: Job,
    offered: list[FormatOption],
    settings: Settings,
) -> bool:
    """Show the quality menu: by editing the placeholder, or as a new reply when progress
    is a reaction. Returns False when the menu couldn't be posted (caller enqueues)."""
    text = (
        f"Pick a quality (I'll go with {offered[0].label} in {settings.quality_choice_timeout}s):"
    )
    markup = quality_keyboard(job.job_id, offered)
    try:
        if placeholder is not None:
            await placeholder.edit_text(text, reply_markup=markup)
            menu = placeholder
        else:
            menu = await source.reply(text, reply_markup=markup)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.info("menu_failed", job_id=job.job_id, error=str(e)[:100])
        return False
    job.message_id = menu.message_id  # the worker edits/deletes it if nobody chooses
    default = offered[0].quality  # best rung under the cap comes first
    pending = PendingChoice(job=job, default=default, offered=[f.quality for f in offered])
    await arq.set(
        pending_key(job.job_id), pending.model_dump_json(), ex=settings.quality_choice_timeout + 30
    )
    asyncio.create_task(_choose_default_later(arq, menu, job.job_id, settings))
    return True


async def _choose_default_later(
    arq: ArqRedis, menu: Message, job_id: str, settings: Settings
) -> None:
    await asyncio.sleep(settings.quality_choice_timeout)
    pending = await _take_pending(arq, job_id)
    if pending is None:
        return  # the user already chose
    pending.job.quality = pending.default
    await _dismiss_menu(menu, pending.job)
    await _enqueue_pending(arq, pending.job, settings)


async def _take_pending(arq: ArqRedis, job_id: str) -> PendingChoice | None:
    raw = await arq.getdel(pending_key(job_id))
    return PendingChoice.model_validate_json(raw) if raw else None


@router.callback_query(F.data.startswith("q:"))
async def on_quality_chosen(
    query: CallbackQuery, settings: Settings, arq: ArqRedis, limits: UserLimits
) -> None:
    try:
        _, job_id, raw_quality = (query.data or "").split(":", 2)
        quality = Quality(raw_quality)
    except ValueError:
        await query.answer("Bad choice")
        return
    pending = await _take_pending(arq, job_id)
    if pending is None:
        await query.answer("That menu expired. Send the link again.", show_alert=True)
        return
    if query.from_user.id != pending.job.user_id:
        await arq.set(
            pending_key(job_id), pending.model_dump_json(), ex=settings.quality_choice_timeout
        )
        await query.answer("Only the person who asked can choose.")
        return
    pending.job.quality = quality if quality in pending.offered else pending.default
    await query.answer()
    # InaccessibleMessage (too old / no access) has no edit_text: nothing to dismiss then.
    menu = query.message if hasattr(query.message, "edit_text") else None
    await _dismiss_menu(menu, pending.job)
    await _enqueue_pending(arq, pending.job, settings)


async def _enqueue_pending(arq: ArqRedis, job: Job, settings: Settings) -> None:
    try:
        await arq.enqueue_job(
            JOB_FUNCTION,
            job.model_dump(mode="json"),
            _job_id=job.job_id,
            _queue_name=QUEUE_NAMES[job.platform],
            _expires=settings.job_ttl,
        )
        log.info("job_enqueued", job_id=job.job_id, platform=job.platform, quality=job.quality)
    except Exception:
        log.exception("enqueue_failed", job_id=job.job_id)


async def _enqueue(
    arq: ArqRedis,
    limits: UserLimits,
    job: Job,
    settings: Settings,
    placeholder: Message | None,
) -> None:
    try:
        await arq.enqueue_job(
            JOB_FUNCTION,
            job.model_dump(mode="json"),
            _job_id=job.job_id,
            _queue_name=QUEUE_NAMES[job.platform],
            _expires=settings.job_ttl,
        )
    except Exception:
        log.exception("enqueue_failed", job_id=job.job_id)
        await limits.release(job.user_id, job.chat_id if job.is_group else None)
        if placeholder is not None:
            with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
                await placeholder.edit_text(
                    "❌ Couldn't queue that right now. Try again in a minute."
                )
        return
    log.info(
        "job_enqueued",
        job_id=job.job_id,
        platform=job.platform,
        quality=job.quality,
        chat_type=job.chat_type,
    )
