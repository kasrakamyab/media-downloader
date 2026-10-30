"""/settings for group admins: auto-download, max quality, allowed platforms.

An inline keyboard edits the settings in place; every tap is re-checked against the group's
admin list so a forwarded or stale keyboard can't change another group's settings.
"""

from __future__ import annotations

import contextlib
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.contracts import Platform, Quality
from core.groups import MAX_QUALITY_CYCLE, GroupRegistry, GroupSettings
from core.logging import get_logger

log = get_logger("gateway.settings")
settings_router = Router()

GROUP_TYPES = {"group", "supergroup"}
ADMIN_STATUSES = {"creator", "administrator"}
LABEL = {
    Platform.YOUTUBE: "YouTube",
    Platform.INSTAGRAM: "Instagram",
    Platform.PINTEREST: "Pinterest",
}

# Telegram posts on behalf of anonymous admins with this user id and sender_chat = the group.
ANONYMOUS_ADMIN_ID = 1087968824


async def is_group_admin(bot: Bot, chat_id: int, user_id: int, sender_chat_id: int | None) -> bool:
    if sender_chat_id == chat_id or user_id == ANONYMOUS_ADMIN_ID and sender_chat_id:
        return True  # anonymous admin posting as the group
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.info("admin_check_failed", chat_id=chat_id, user_id=user_id, error=str(e)[:100])
        return False
    return member.status in ADMIN_STATUSES


def render(title: str, s: GroupSettings) -> str:
    platforms = ", ".join(LABEL[p] for p in Platform if s.allows(p)) or "none"
    quality = "best under the size limit" if s.max_quality == Quality.BEST else s.max_quality.value
    auto = "on" if s.auto else "off"
    return (
        f"⚙️ <b>Settings for {escape(title or 'this group')}</b>\n\n"
        f"Auto-download: <b>{auto}</b> (on by default) — when on, I fetch every supported "
        f"link posted here without /dl, quietly and without a quality menu. Telegram only "
        f"shows me those messages if the bot is an admin or privacy mode is off "
        f"(BotFather /setprivacy).\n"
        f"Max quality: <b>{quality}</b>\n"
        f"Platforms: <b>{platforms}</b>\n\n"
        f"Tap to change. Admins only."
    )


def keyboard(chat_id: int, s: GroupSettings) -> InlineKeyboardMarkup:
    def cb(action: str, value: str = "") -> str:
        return f"gs:{chat_id}:{action}:{value}"

    quality = "best" if s.max_quality == Quality.BEST else s.max_quality.value
    rows = [
        [
            InlineKeyboardButton(
                text=f"Auto-download: {'ON' if s.auto else 'OFF'}", callback_data=cb("auto")
            )
        ],
        [InlineKeyboardButton(text=f"Max quality: {quality} ▸", callback_data=cb("maxq"))],
        [
            InlineKeyboardButton(
                text=f"{'✅' if s.allows(p) else '⬜'} {LABEL[p]}",
                callback_data=cb("plat", p.value),
            )
            for p in Platform
        ],
        [InlineKeyboardButton(text="Done", callback_data=cb("close"))],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def apply(s: GroupSettings, action: str, value: str) -> GroupSettings:
    """Pure state transition for one tap; unknown actions leave the settings alone."""
    if action == "auto":
        return s.model_copy(update={"auto": not s.auto})
    if action == "maxq":
        i = MAX_QUALITY_CYCLE.index(s.max_quality) if s.max_quality in MAX_QUALITY_CYCLE else 0
        return s.model_copy(
            update={"max_quality": MAX_QUALITY_CYCLE[(i + 1) % len(MAX_QUALITY_CYCLE)]}
        )
    if action == "plat":
        try:
            p = Platform(value)
        except ValueError:
            return s
        platforms = [x for x in s.platforms if x != p] if s.allows(p) else [*s.platforms, p]
        return s.model_copy(update={"platforms": [x for x in Platform if x in platforms]})
    return s


@settings_router.message(Command("settings"))
async def on_settings(message: Message, bot: Bot, groups: GroupRegistry) -> None:
    if message.chat.type not in GROUP_TYPES:
        await message.answer("Settings are per group: run /settings inside a group.")
        return
    user_id = message.from_user.id if message.from_user else 0
    sender_chat_id = message.sender_chat.id if message.sender_chat else None
    if not await is_group_admin(bot, message.chat.id, user_id, sender_chat_id):
        await message.reply("Only group admins can change my settings here.")
        return
    s = await groups.get_settings(message.chat.id)
    await message.reply(
        render(message.chat.title or "", s), reply_markup=keyboard(message.chat.id, s)
    )


@settings_router.callback_query(F.data.startswith("gs:"))
async def on_settings_tap(query: CallbackQuery, bot: Bot, groups: GroupRegistry) -> None:
    try:
        _, raw_chat, action, value = (query.data or "").split(":", 3)
        chat_id = int(raw_chat)
    except ValueError:
        await query.answer("Bad button")
        return
    msg = query.message  # Message, or InaccessibleMessage (has .chat but can't be edited)
    if msg is None or getattr(msg, "chat", None) is None or msg.chat.id != chat_id:
        await query.answer("This keyboard belongs to another chat.", show_alert=True)
        return
    if not await is_group_admin(bot, chat_id, query.from_user.id, None):
        await query.answer("Only group admins can change settings.", show_alert=True)
        return
    if action == "close":
        await query.answer("Saved")
        with contextlib.suppress(TelegramBadRequest):
            await msg.edit_reply_markup(reply_markup=None)
        return
    before = await groups.get_settings(chat_id)
    after = apply(before, action, value)
    if after != before:
        await groups.save_settings(chat_id, after)
        log.info(
            "group_settings_changed",
            chat_id=chat_id,
            by=query.from_user.id,
            action=action,
            value=value,
            now=after.to_hash(),
        )
    await query.answer()
    with contextlib.suppress(TelegramBadRequest):  # "message is not modified" on a no-op tap
        await msg.edit_text(
            render(msg.chat.title or "", after), reply_markup=keyboard(chat_id, after)
        )
