"""Minimal stand-ins for aiogram objects, shared by the gateway tests."""

from __future__ import annotations

from types import SimpleNamespace


class FakeMessage:
    _ids = 1000

    def __init__(
        self,
        text: str | None,
        user_id: int = 7,
        chat_id: int = 99,
        chat_type: str = "private",
        *,
        caption: str | None = None,
        reply_to: FakeMessage | None = None,
        thread_id: int | None = None,
        entities: list | None = None,
    ) -> None:
        FakeMessage._ids += 1
        self.message_id = FakeMessage._ids
        self.text = text
        self.caption = caption
        self.entities = entities
        self.caption_entities = None
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=chat_id, type=chat_type, title="Test group")
        self.reply_to_message = reply_to
        self.message_thread_id = thread_id
        self.is_topic_message = thread_id is not None
        self.migrate_to_chat_id = None
        self.answers: list[str] = []
        self.replies: list[str] = []
        self.markups: list = []

    async def answer(self, text: str, **_) -> SimpleNamespace:
        self.answers.append(text)
        return SimpleNamespace(message_id=555, edit_text=self._edit)

    async def reply(self, text: str, **_) -> SimpleNamespace:
        self.replies.append(text)
        return SimpleNamespace(message_id=555, edit_text=self._edit)

    async def _edit(self, text: str, reply_markup=None, **_) -> None:
        self.replies.append(text)
        self.markups.append(reply_markup)


class FakeQuery:
    def __init__(self, data: str, user_id: int = 7) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = None
        self.answers: list[tuple] = []

    async def answer(self, *args, **kwargs) -> None:
        self.answers.append((args, kwargs))


class FakeBot:
    id = 6408937369
    username = "Mamad_AlertsBot"
    can_read_all = False

    def __init__(self, admin_in: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.admin_in = admin_in or set()  # chats where the bot itself is an admin

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=self.id, username=self.username, can_read_all_group_messages=self.can_read_all
        )

    async def send_message(self, chat_id: int, text: str, **_) -> None:
        self.sent.append((chat_id, text))

    async def get_chat_member(self, chat_id: int, user_id: int) -> SimpleNamespace:
        status = "administrator" if chat_id in self.admin_in else "member"
        return SimpleNamespace(status=status, user=SimpleNamespace(id=user_id))


def member_update(
    chat_id: int, old: str, new: str, chat_type: str = "supergroup", by: int = 7
) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type, title="Test group"),
        old_chat_member=SimpleNamespace(status=old),
        new_chat_member=SimpleNamespace(status=new),
        from_user=SimpleNamespace(id=by),
    )
