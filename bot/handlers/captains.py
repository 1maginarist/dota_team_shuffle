"""/draft — Immortal Draft: капитаны по очереди выбирают игроков."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .. import captain_draft as cd
from .. import texts
from ..config import Config
from ..db import CaptainDraft, Database, PlayerRow

log = logging.getLogger(__name__)
router = Router(name="captains")

MAX_SHOWN = 90
OWNER_ACTIONS = {"toggle", "all", "clear", "view", "refresh", "next", "cap", "top2", "back", "start",
                 "undo", "ok", "cancel"}
# Нажатия по одному драфту обрабатываем строго по очереди, иначе два быстрых
# пика могут выбрать двух игроков за один ход.
_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def _is_organizer(config: Config, user_id: int, draft: CaptainDraft) -> bool:
    return user_id == draft.owner_id or user_id in config.admin_ids


async def _shown_players(db: Database, draft: CaptainDraft) -> list[PlayerRow]:
    """Для шага выбора участников: онлайн + уже отмеченные (или все, если включено)."""
    if draft.show_all:
        return (await db.list_players())[:MAX_SHOWN]
    online = await db.list_players(only_online=True)
    ids = {p.user_id for p in online}
    extra = await db.get_players([uid for uid in draft.selected if uid not in ids])
    return (online + [extra[uid] for uid in draft.selected if uid in extra])[:MAX_SHOWN]


async def _edit(bot: Bot, chat_id: int, message_id: int, text: str,
                markup: InlineKeyboardMarkup | None) -> None:
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except TelegramBadRequest:
        pass  # не изменилось или сообщение удалено


async def _sync(bot: Bot, db: Database, config: Config, draft: CaptainDraft) -> None:
    """Обновить общее сообщение драфта и сообщения капитанам в личке."""
    players = await db.get_players(draft.selected)
    shown = await _shown_players(db, draft) if draft.stage == "select" else []
    if draft.message_id:
        await _edit(bot, draft.chat_id, draft.message_id,
                    cd.board_text(draft, players, config.weights), cd.board_kb(draft, players, shown))

    # «Утверждён» — финальные сообщения уже разосланы в _confirm.
    if draft.stage in ("select", "captains", "confirmed"):
        return
    changed = False
    for uid in draft.selected:
        if uid == draft.chat_id:
            continue  # общее сообщение и так у него в личке
        captain = uid in draft.captains
        is_turn = cd.current_captain(draft) == uid
        if captain:
            text = cd.captain_dm_text(draft, players, config.weights, uid)
            markup = cd.captain_dm_kb(draft, players, uid)
        else:
            text, markup = cd.participant_dm_text(draft, players, config.weights), None
        message_id, had_turn = draft.dm_messages.get(uid, [0, 0])
        if message_id == -1:
            continue  # не открывал бота — пусть смотрит общее сообщение
        if message_id and not (is_turn and not had_turn):
            await _edit(bot, uid, message_id, text, markup)
            draft.dm_messages[uid] = [message_id, int(is_turn)]
            continue
        if draft.stage == "cancelled":
            continue  # тем, кому ещё ничего не слали, об отмене писать незачем
        # Первое сообщение или ход перешёл к капитану — новым сообщением, чтобы пришло уведомление.
        if message_id:
            try:
                await bot.delete_message(uid, message_id)
            except TelegramAPIError:
                pass
        try:
            sent = await bot.send_message(uid, text, reply_markup=markup)
            draft.dm_messages[uid] = [sent.message_id, int(is_turn)]
        except TelegramAPIError as exc:
            log.warning("Не могу написать участнику %s: %s", uid, exc)
            draft.dm_messages[uid] = [-1, 0]
        changed = True
    if changed:
        await db.save_captain_draft(draft)


@router.message(Command("draft", "immortal"))
async def cmd_draft(message: Message, db: Database, config: Config) -> None:
    user = message.from_user
    if not config.can_organize(user.id):
        await message.reply("Запускать драфт могут только организаторы.")
        return
    me = await db.get_player(user.id)
    preselect = [user.id] if me is not None and me.is_complete and me.online else []
    draft = await db.create_captain_draft(message.chat.id, user.id, preselect)
    players = await db.get_players(draft.selected)
    shown = await _shown_players(db, draft)
    sent = await message.answer(
        cd.board_text(draft, players, config.weights), reply_markup=cd.board_kb(draft, players, shown)
    )
    draft.message_id = sent.message_id
    await db.save_captain_draft(draft)


@router.callback_query(cd.CaptainDraftCb.filter())
async def on_draft(call: CallbackQuery, callback_data: cd.CaptainDraftCb, db: Database, config: Config,
                   bot: Bot) -> None:
    async with _locks[callback_data.draft_id]:
        draft = await db.get_captain_draft(callback_data.draft_id)
        if draft is None:
            await call.answer("Драфт не найден", show_alert=True)
            return
        alert = await _apply(call, callback_data, db, config, bot, draft)
        if alert:
            await call.answer(alert, show_alert=True)
            return
        await db.save_captain_draft(draft)
        await _sync(bot, db, config, draft)
        await call.answer()


async def _apply(call: CallbackQuery, data: cd.CaptainDraftCb, db: Database, config: Config, bot: Bot,
                 draft: CaptainDraft) -> str | None:
    """Применить нажатие к драфту. Возвращает текст ошибки или None."""
    user_id = call.from_user.id
    action = data.action
    organizer = _is_organizer(config, user_id, draft)
    if draft.stage in ("confirmed", "cancelled"):
        return "Этот драфт уже закончен. Новый — командой /draft"
    if action in OWNER_ACTIONS and not organizer:
        return "Этим управляет тот, кто запустил драфт."

    if action == "cancel":
        draft.stage = "cancelled"
        return None

    if draft.stage == "select":
        if action == "toggle":
            if data.user_id in draft.selected:
                draft.selected.remove(data.user_id)
            elif len(draft.selected) >= cd.DRAFT_SIZE:
                return f"Уже отмечено {cd.DRAFT_SIZE}. Сними кого-нибудь, чтобы добавить другого."
            else:
                draft.selected.append(data.user_id)
        elif action == "all":
            for player in await db.list_players(only_online=True):
                if len(draft.selected) >= cd.DRAFT_SIZE:
                    break
                if player.user_id not in draft.selected:
                    draft.selected.append(player.user_id)
        elif action == "clear":
            draft.selected = []
        elif action == "view":
            draft.show_all = not draft.show_all
        elif action == "next":
            players = await db.get_players(draft.selected)
            draft.selected = [uid for uid in draft.selected if uid in players and players[uid].is_complete]
            if len(draft.selected) != cd.DRAFT_SIZE:
                return f"Нужно ровно {cd.DRAFT_SIZE} участников, сейчас {len(draft.selected)}."
            draft.stage = "captains"
            draft.captains = []
        elif action != "refresh":
            return "Этот шаг уже пройден"
        return None

    if draft.stage == "captains":
        if action == "cap":
            if data.user_id in draft.captains:
                draft.captains.remove(data.user_id)
            elif len(draft.captains) >= 2:
                return "Капитанов уже двое — сними одного, чтобы выбрать другого."
            elif data.user_id in draft.selected:
                draft.captains.append(data.user_id)
        elif action == "top2":
            draft.captains = cd.top_two_by_mmr(draft, await db.get_players(draft.selected))
        elif action == "back":
            draft.stage = "select"
            draft.captains = []
        elif action == "start":
            if len(draft.captains) != 2:
                return "Выбери двух капитанов."
            draft.captains = cd.flip_coin(draft.captains)
            draft.picks = []
            draft.stage = "picking"
        else:
            return "Этот шаг уже пройден"
        return None

    if action == "undo":
        if not draft.picks:
            return "Ещё никто не выбран."
        draft.picks.pop()
        draft.stage = "picking"
        return None

    if draft.stage == "picking" and action == "pick":
        captain = cd.current_captain(draft)
        if user_id != captain and not organizer:
            players = await db.get_players([captain])
            who = players[captain].name if captain in players else "другой капитан"
            return f"Сейчас выбирает {who}."
        if data.user_id not in cd.pool(draft):
            return "Этого игрока уже забрали."
        draft.picks.append(data.user_id)
        if len(draft.picks) == len(cd.PICK_ORDER):
            draft.stage = "done"
        return None

    if draft.stage == "done" and action == "ok":
        await _confirm(call, db, config, bot, draft)
        return None
    return "Этот шаг уже пройден"


async def _confirm(call: CallbackQuery, db: Database, config: Config, bot: Bot, draft: CaptainDraft) -> None:
    draft.stage = "confirmed"
    everyone = cd.team(draft, 0) + cd.team(draft, 1)
    await db.commit_bench(everyone, [])  # сыграли — сбрасываем очередь скамейки
    players = await db.get_players(everyone)
    lineup = cd.final_lineup(draft, players, config.weights)
    full = cd.board_text(draft, players, config.weights)
    # Живые доски заменяем финальным сообщением — новым, чтобы пришло уведомление.
    for uid, (message_id, _) in draft.dm_messages.items():
        if message_id > 0:
            try:
                await bot.delete_message(uid, message_id)
            except TelegramAPIError:
                pass
    draft.dm_messages = {}
    delivered = 0
    for uid in everyone:
        if uid == draft.chat_id:
            continue
        intro = texts.personal(lineup, uid) or ""
        try:
            await bot.send_message(uid, f"{intro}\n\n{full}")
            delivered += 1
        except TelegramAPIError as exc:
            log.warning("Не удалось написать игроку %s: %s", uid, exc)
    log.info("Драфт %s утверждён, разослано %s", draft.id, delivered)
