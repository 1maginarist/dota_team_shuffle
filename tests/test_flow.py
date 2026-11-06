"""Сквозной тест: апдейты Telegram → диспетчер → что бот отправил.

Сеть подменяется фейковой сессией, которая записывает все вызовы API.
"""

import asyncio
import itertools
from datetime import datetime

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.__main__ import build_dispatcher
from bot.config import Config
from bot.balancer import Weights
from bot.db import Database
from bot.keyboards import DraftCb, PickCb, RolesCb

GROUP_ID = -100500
BOT_USER = User(id=999, is_bot=True, first_name="ShuffleBot", username="shuffle_bot")


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self._ids = itertools.count(1000)

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetMe):
            return BOT_USER
        if isinstance(method, (SendMessage, EditMessageText)):
            chat_id = method.chat_id
            chat_type = "private" if chat_id and chat_id > 0 else "group"
            # Как настоящий aiogram: ответ привязан к боту, у него работают .edit_text() и т.п.
            return Message(
                message_id=getattr(method, "message_id", None) or next(self._ids),
                date=datetime.now(),
                chat=Chat(id=chat_id or 0, type=chat_type),
                text=method.text,
            ).as_(bot)
        return True

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    def of(self, kind):
        return [c for c in self.calls if isinstance(c, kind)]


class Harness:
    def __init__(self, admin_ids=frozenset()):
        self.session = FakeSession()
        self.bot = Bot("42:TEST", session=self.session)
        self.db = Database(":memory:")
        self.config = Config(
            bot_token="42:TEST", db_path=":memory:", admin_ids=admin_ids,
            weights=Weights(), reshuffle_tolerance=200,
        )
        self.dp = None
        self._update_ids = itertools.count(1)

    async def start(self):
        await self.db.connect()
        self.dp = build_dispatcher(self.db, self.config)

    @staticmethod
    def user(uid):
        return User(id=uid, is_bot=False, first_name=f"Игрок{uid}")

    async def text(self, uid, text, chat_id=None):
        chat_id = chat_id or uid
        chat = Chat(id=chat_id, type="private" if chat_id > 0 else "group")
        msg = Message(message_id=next(self._update_ids), date=datetime.now(), chat=chat,
                      from_user=self.user(uid), text=text)
        await self.dp.feed_update(self.bot, Update(update_id=next(self._update_ids), message=msg))

    async def press(self, uid, data, chat_id=None, message_id=1):
        chat_id = chat_id or uid
        chat = Chat(id=chat_id, type="private" if chat_id > 0 else "group")
        msg = Message(message_id=message_id, date=datetime.now(), chat=chat, from_user=BOT_USER, text="…")
        call = CallbackQuery(id=str(next(self._update_ids)), from_user=self.user(uid), chat_instance="x",
                             message=msg, data=data.pack() if hasattr(data, "pack") else data)
        await self.dp.feed_update(self.bot, Update(update_id=next(self._update_ids), callback_query=call))

    def last_text(self, chat_id):
        for call in reversed(self.session.calls):
            if isinstance(call, (SendMessage, EditMessageText)) and call.chat_id == chat_id:
                return call.text
        return None

    def last_alert(self):
        return self.session.of(AnswerCallbackQuery)[-1].text

    async def register(self, uid, mmr, roles):
        await self.text(uid, "/start")
        await self.text(uid, str(mmr))
        await self.press(uid, RolesCb(action="save", order=roles))


ROLES = ["1", "2", "3", "4", "5", "12", "23", "34", "45", "51", "2", "5"]


def test_full_flow():
    async def scenario():
        h = Harness()
        await h.start()
        try:
            await full_flow(h)
        finally:
            await h.db.close()

    asyncio.run(scenario())


async def full_flow(h):
    # Регистрация
    await h.text(1, "/start")
    assert "Пришли свой ММР" in h.last_text(1)
    await h.text(1, "сто")
    assert "Не понял" in h.last_text(1)
    await h.text(1, "99999")
    assert "от 1 до 20 000" in h.last_text(1)
    await h.text(1, "4 500")
    assert "Выбери роли" in h.last_text(1)
    await h.press(1, RolesCb(action="save", order="-"))
    assert h.last_alert() == "Выбери хотя бы одну роль"
    await h.press(1, RolesCb(action="set", order="23"))
    assert "2 Мид → 3 Оффлейн" in h.last_text(1)
    await h.press(1, RolesCb(action="save", order="23"))
    assert "ты зарегистрирован" in h.last_text(1)
    me = await h.db.get_player(1)
    assert me.mmr == 4500 and me.roles == (2, 3) and me.online

    for uid in range(2, 13):
        await h.register(uid, 2000 + uid * 300, ROLES[uid - 1])
    await h.text(12, "/offline")
    assert not (await h.db.get_player(12)).online
    await h.text(2, "/mmr 7000")
    assert (await h.db.get_player(2)).mmr == 7000

    # Организатор в группе: /game → организатор уже отмечен
    await h.text(1, "/game", chat_id=GROUP_ID)
    assert "Отмечено: <b>1</b>" in h.last_text(GROUP_ID)
    picker_id = 1

    # Чужой человек не может трогать меню
    await h.press(5, PickCb(picker_id=picker_id, action="all"), chat_id=GROUP_ID)
    assert "собирает другой человек" in h.last_alert()

    # Отмечаем всех онлайн (11 человек, 12-й оффлайн)
    await h.press(1, PickCb(picker_id=picker_id, action="all"), chat_id=GROUP_ID)
    picker = await h.db.get_picker(picker_id)
    assert len(picker.selected) == 11 and 12 not in picker.selected
    assert "5 на 5, ещё 1 на скамейку" in h.last_text(GROUP_ID)

    # Распределяем
    await h.press(1, PickCb(picker_id=picker_id, action="go"), chat_id=GROUP_ID)
    draft = await h.db.get_draft(1)
    assert len(draft.players) == 10 and len(draft.bench) == 1
    text = h.last_text(GROUP_ID)
    assert "Свет" in text and "Тьма" in text and "Скамейка" in text

    # Другой вариант
    await h.press(1, DraftCb(draft_id=1, action="again"), chat_id=GROUP_ID)
    draft2 = await h.db.get_draft(1)
    assert draft2.lineup.key != draft.lineup.key
    assert "вариант 2" in h.last_text(GROUP_ID)

    # Утверждаем → всем в личку
    before = len(h.session.of(SendMessage))
    await h.press(1, DraftCb(draft_id=1, action="ok"), chat_id=GROUP_ID)
    assert "Состав утверждён" in h.last_text(GROUP_ID)
    dms = [m for m in h.session.of(SendMessage)[before:] if m.chat_id > 0]
    assert len(dms) == 11
    benched = draft2.bench[0]
    assert any(m.chat_id == benched and "скамейке" in m.text for m in dms)
    player_dm = next(m for m in dms if m.chat_id == draft2.players[0])
    assert "Ты за" in player_dm.text and "позиция" in player_dm.text

    # Повторно утвердить нельзя
    await h.press(1, DraftCb(draft_id=1, action="ok"), chat_id=GROUP_ID)
    assert "уже утверждён" in h.last_alert()

    # Ротация скамейки: тот, кто сидел, в следующей игре играет
    assert (await h.db.get_player(benched)).bench_debt == 1
    await h.text(1, "/game", chat_id=GROUP_ID)
    await h.press(1, PickCb(picker_id=2, action="all"), chat_id=GROUP_ID)
    await h.press(1, PickCb(picker_id=2, action="go"), chat_id=GROUP_ID)
    next_draft = await h.db.get_draft(2)
    assert benched in next_draft.players


def test_admin_only():
    async def scenario():
        h = Harness(admin_ids=frozenset({1}))
        await h.start()
        try:
            await h.register(2, 3000, "1")
            await h.text(2, "/game")
            assert "только организаторы" in h.last_text(2)
            await h.text(1, "/game")
            assert "Сбор игры" in h.last_text(1)
        finally:
            await h.db.close()

    asyncio.run(scenario())


def test_group_redirects_profile_to_private():
    async def scenario():
        h = Harness()
        await h.start()
        try:
            await h.text(3, "/start", chat_id=GROUP_ID)
            assert "t.me/shuffle_bot" in h.last_text(GROUP_ID)
        finally:
            await h.db.close()

    asyncio.run(scenario())


# --- /mvp -------------------------------------------------------------------

import copy  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

from bot.handlers import mvp as mvp_handler  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "opendota"


class FakeOpenDota:
    """OpenDota без сети: один матч сначала не разобран и «разбирается» после запроса."""

    def __init__(self, unparsed_id=None):
        self.matches = {int(f.stem): json.loads(f.read_text()) for f in FIXTURES.glob("*.json")}
        self.unparsed_id = unparsed_id
        self.parse_requests = []

    async def match(self, match_id):
        data = self.matches.get(match_id)
        if data is None:
            return None
        if match_id == self.unparsed_id:
            data = copy.deepcopy(data)
            data["version"] = None
        return data

    async def request_parse(self, match_id):
        self.parse_requests.append(match_id)
        return 777

    async def job_finished(self, job_id):
        self.unparsed_id = None  # реплей разобран
        return True

    async def heroes(self):
        return {}

    async def close(self):
        pass


def test_mvp_command(monkeypatch):
    monkeypatch.setattr(mvp_handler, "POLL_INTERVAL", 0.01)

    async def scenario():
        h = Harness(admin_ids=frozenset({1}))
        await h.db.connect()
        fake = FakeOpenDota(unparsed_id=9077267013)
        h.dp = build_dispatcher(h.db, h.config, opendota=fake)
        try:
            await h.text(2, "/mvp 9077341933")
            assert "только организаторы" in h.last_text(2)

            await h.text(1, "/mvp")
            assert "Пришли номера матчей" in h.last_text(1)

            await h.text(1, "/mvp https://www.dotabuff.com/matches/9077341933 9077497263, 9077267013 123456789")
            assert fake.parse_requests == [9077267013, 123456789]
            texts = [c.text for c in h.session.calls if isinstance(c, (SendMessage, EditMessageText)) and c.chat_id == 1]
            assert any("разбираю реплей" in t for t in texts)
            summary, details = texts[-2], texts[-1]
            assert "MVP вечера" in summary
            assert "Матч 123456789 не найден" in summary
            assert "без разбора" not in summary  # третий матч дождались
            assert "Разбор лидеров" in details
        finally:
            await h.db.close()

    asyncio.run(scenario())
