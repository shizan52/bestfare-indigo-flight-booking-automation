"""A fake Telegram for tests: records everything the bot sends / edits / deletes, and lets a test type messages and
press the buttons it was shown - through the real handlers, with no network."""
from __future__ import annotations

import importlib
import itertools
from typing import Optional

from telegram import (Bot, CallbackQuery, Chat, InlineKeyboardMarkup, KeyboardButton, Message, MessageEntity,
                      ReplyKeyboardMarkup, Update, User)
from telegram.error import BadRequest, Forbidden


class Wire:
    def __init__(self) -> None:
        self.events: list[dict] = []                 # every send / edit / delete, in order
        self.messages: dict[tuple[int, int], dict] = {}   # (chat, message id) -> {"text", "markup"}
        self.answers: list[tuple] = []               # callback-query answers (text, alert)
        self.blocked: set[int] = set()               # chats the bot may not message (never pressed Start)
        self.documents: list[dict] = []              # files the bot sent (chat, filename, caption, bytes)
        self.profile: dict = {"name": "", "short": "", "description": "", "commands": {}}
        self._ids = itertools.count(100)

    # -- what the bot called --------------------------------------------------
    def sent_to(self, chat: int) -> list[dict]:
        return [e for e in self.events if e["chat"] == chat and e["kind"] in ("send", "edit")]

    def texts(self, chat: int) -> list[str]:
        return [e["text"] for e in self.sent_to(chat)]

    def last(self, chat: int) -> str:
        return self.texts(chat)[-1]

    def card(self, chat: int) -> Optional[dict]:
        """The most recently touched message of this chat that has an inline keyboard."""
        for e in reversed(self.sent_to(chat)):
            cur = self.messages.get((chat, e["mid"]))
            if cur and isinstance(cur["markup"], InlineKeyboardMarkup):
                return {"mid": e["mid"], **cur}
        return None


def buttons(markup) -> list[tuple[str, Optional[str]]]:
    """Flatten a keyboard into (label, callback data)."""
    out = []
    if isinstance(markup, InlineKeyboardMarkup):
        for row in markup.inline_keyboard:
            out += [(b.text, b.callback_data) for b in row]
    elif isinstance(markup, ReplyKeyboardMarkup):
        for row in markup.keyboard:
            out += [((b.text if isinstance(b, KeyboardButton) else b), None) for b in row]
    return out


def install(monkeypatch) -> Wire:
    """Replace every Bot network method with a recorder."""
    w = Wire()

    async def send_message(self, chat_id, text, *a, reply_markup=None, **k):
        if chat_id in w.blocked:
            raise Forbidden("bot can't initiate conversation with a user")
        mid = next(w._ids)
        w.messages[(chat_id, mid)] = {"text": text, "markup": reply_markup}
        w.events.append({"kind": "send", "chat": chat_id, "mid": mid, "text": text, "markup": reply_markup})
        return Message(message_id=mid, date=None, chat=Chat(chat_id, "private"), text=text)

    async def edit_message_text(self, text, chat_id=None, message_id=None, *a, reply_markup=None, **k):
        cur = w.messages.get((chat_id, message_id))
        if cur is None:
            raise BadRequest("Message to edit not found")
        if cur["text"] == text and repr(cur["markup"]) == repr(reply_markup):
            raise BadRequest("Message is not modified")
        w.messages[(chat_id, message_id)] = {"text": text, "markup": reply_markup}
        w.events.append({"kind": "edit", "chat": chat_id, "mid": message_id, "text": text, "markup": reply_markup})
        return True

    async def send_document(self, chat_id, document=None, *a, filename=None, caption=None, **k):
        if chat_id in w.blocked:
            raise Forbidden("bot can't initiate conversation with a user")
        mid = next(w._ids)
        w.documents.append({"chat": chat_id, "filename": filename, "caption": caption, "bytes": document})
        w.events.append({"kind": "document", "chat": chat_id, "mid": mid, "text": caption, "markup": None})
        return Message(message_id=mid, date=None, chat=Chat(chat_id, "private"))

    async def delete_message(self, chat_id, message_id, *a, **k):
        w.events.append({"kind": "delete", "chat": chat_id, "mid": message_id, "text": None, "markup": None})
        w.messages.pop((chat_id, message_id), None)
        return True

    async def answer_callback_query(self, callback_query_id, text=None, show_alert=None, *a, **k):
        w.answers.append((text, bool(show_alert)))
        return True

    async def set_my_commands(self, commands, scope=None, *a, **k):
        w.profile["commands"][getattr(scope, "chat_id", None)] = [c.command for c in commands]
        return True

    async def set_my_name(self, name=None, *a, **k):
        w.profile["name"] = name
        w.profile["name_calls"] = w.profile.get("name_calls", 0) + 1
        return True

    async def get_my_name(self, *a, **k):
        return type("N", (), {"name": w.profile["name"]})()

    async def set_my_short_description(self, short_description=None, *a, **k):
        w.profile["short"] = short_description
        return True

    async def get_my_short_description(self, *a, **k):
        return type("S", (), {"short_description": w.profile["short"]})()

    async def set_my_description(self, description=None, *a, **k):
        w.profile["description"] = description
        return True

    async def get_my_description(self, *a, **k):
        return type("D", (), {"description": w.profile["description"]})()

    async def initialize(self):                       # no network: skip get_me()
        self._bot_initialized = True
        self._requests_initialized = True

    async def shutdown(self):
        return None

    for name, fn in [("send_message", send_message), ("edit_message_text", edit_message_text),
                     ("send_document", send_document),
                     ("delete_message", delete_message), ("answer_callback_query", answer_callback_query),
                     ("set_my_commands", set_my_commands), ("set_my_name", set_my_name), ("get_my_name", get_my_name),
                     ("set_my_short_description", set_my_short_description),
                     ("get_my_short_description", get_my_short_description), ("set_my_description", set_my_description),
                     ("get_my_description", get_my_description), ("initialize", initialize), ("shutdown", shutdown)]:
        monkeypatch.setattr(Bot, name, fn, raising=True)
    monkeypatch.setattr(Bot, "bot", property(lambda self: User(1, "BestFare", True, username="bestfare_bot")), raising=False)
    return w


_update_ids = itertools.count(1)
_message_ids = itertools.count(5000)


class Person:
    """One Telegram user talking to the bot."""

    def __init__(self, app, wire: Wire, uid: int, name: str = "Test", username: Optional[str] = None) -> None:
        self.app, self.wire, self.uid = app, wire, uid
        self.user = User(uid, name, False, username=username)

    async def say(self, text: str) -> None:
        mid = next(_message_ids)
        ents = [MessageEntity("bot_command", 0, len(text.split()[0]))] if text.startswith("/") else None
        msg = Message(message_id=mid, date=None, chat=Chat(self.uid, "private"), from_user=self.user, text=text, entities=ents)
        msg.set_bot(self.app.bot)
        await self.app.process_update(Update(next(_update_ids), message=msg))

    async def tap(self, data: str, mid: Optional[int] = None) -> None:
        """Press the inline button with this callback data (on the latest card, or on message `mid`)."""
        card = self.wire.card(self.uid)
        mid = mid or (card["mid"] if card else next(_message_ids))
        msg = Message(message_id=mid, date=None, chat=Chat(self.uid, "private"), text="card")
        q = CallbackQuery("q", self.user, "ci", data=data, message=msg)
        q.set_bot(self.app.bot)
        await self.app.process_update(Update(next(_update_ids), callback_query=q))

    async def share(self, user_id: int, first_name: str = "Shared", username: Optional[str] = None) -> None:
        """Use the 'Pick a person' button: Telegram sends a users_shared message."""
        from telegram import SharedUser, UsersShared
        msg = Message(message_id=next(_message_ids), date=None, chat=Chat(self.uid, "private"), from_user=self.user,
                      users_shared=UsersShared(1, [SharedUser(user_id, first_name=first_name, username=username)]))
        msg.set_bot(self.app.bot)
        await self.app.process_update(Update(next(_update_ids), message=msg))

    def sid(self) -> str:
        """The wizard session id found in the latest card's buttons."""
        for _text, data in buttons((self.wire.card(self.uid) or {}).get("markup")):
            if data and data.startswith("w:"):
                return data.split(":")[1]
        raise AssertionError("no wizard card on screen")

    async def cmd(self, cmd: str, arg: str = "") -> None:
        """Press a wizard button by command + argument, e.g. cmd('n', '2') or cmd('d', '2026-12-09')."""
        await self.tap(f"w:{self.sid()}:{cmd}" + (f":{arg}" if arg else ""))

    def find(self, label: str) -> Optional[str]:
        """Callback data of the first button on the latest card whose text contains `label`."""
        card = self.wire.card(self.uid)
        for text, data in buttons(card["markup"] if card else None):
            if label.lower() in text.lower() and data:
                return data
        return None

    async def press(self, label: str) -> None:
        data = self.find(label)
        assert data, f"no button '{label}' on the card; buttons: {[b[0] for b in buttons((self.wire.card(self.uid) or {}).get('markup'))]}"
        await self.tap(data)

    def card_text(self) -> str:
        c = self.wire.card(self.uid)
        return c["text"] if c else ""

    def labels(self) -> list[str]:
        c = self.wire.card(self.uid)
        return [b[0] for b in buttons(c["markup"] if c else None)]

    def last(self) -> str:
        return self.wire.last(self.uid)


def reload_app_modules() -> None:
    """Fresh module state (queue, alert throttle ...) for every test, in dependency order (config/db come from fixtures)."""
    for name in ("ui", "common", "doctor", "automation", "worker", "wizard", "admin", "bot"):
        importlib.reload(__import__(name))
