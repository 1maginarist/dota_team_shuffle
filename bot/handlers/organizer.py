"""Организатор: выбор игроков из онлайна, распределение, утверждение."""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .. import texts
from ..balancer import balance, pick_players
from ..config import Config
from ..db import Database, Draft, Picker, PlayerRow
from ..keyboards import DraftCb, PickCb, draft_kb, picker_kb

log = logging.getLogger(__name__)
router = Router(name="organizer")

MAX_SHOWN = 90  # у Telegram лимит 100 кнопок на сообщение, 5 уходят на управление


def _is_owner(config: Config, user_id: int, owner_id: int) -> bool:
    return user_id == owner_id or user_id in config.admin_ids


async def _render_picker(db: Database, picker: Picker) -> tuple[str, InlineKeyboardMarkup]:
    online = await db.list_players(only_online=True)
    if picker.show_all:
        shown = await db.list_players()
    else:
        # Онлайн + уже отмеченные, даже если они успели уйти в оффлайн.
        shown_ids = {p.user_id for p in online}
        missing = [uid for uid in picker.selected if uid not in shown_ids]
        extra = await db.get_players(missing)
        shown = online + [extra[uid] for uid in missing if uid in extra]
    text = texts.picker(len(online), len(picker.selected), picker.show_all)
    return text, picker_kb(picker, shown[:MAX_SHOWN])


async def _draft_text(db: Database, draft: Draft, footer: str = "") -> str:
    bench = await db.get_players(draft.bench)
    bench_names = [bench[uid].name for uid in draft.bench if uid in bench]
    text = texts.lineup(draft.lineup, bench_names, variant=len(draft.seen))
    return text + (f"\n\n{footer}" if footer else "")


@router.message(Command("game", "sbor"))
async def cmd_game(message: Message, db: Database, config: Config) -> None:
    user = message.from_user
    if not config.can_organize(user.id):
        await message.reply("Собирать команды могут только организаторы.")
        return
    me = await db.get_player(user.id)
    preselect = [user.id] if me is not None and me.is_complete and me.online else []
    picker = await db.create_picker(message.chat.id, user.id, preselect)
    text, markup = await _render_picker(db, picker)
    await message.answer(text, reply_markup=markup)


@router.callback_query(PickCb.filter())
async def on_pick(call: CallbackQuery, callback_data: PickCb, db: Database, config: Config, bot: Bot) -> None:
    picker = await db.get_picker(callback_data.picker_id)
    if picker is None:
        await call.answer("Это меню устарело, открой новое: /game", show_alert=True)
        return
    if not _is_owner(config, call.from_user.id, picker.owner_id):
        await call.answer("Это меню собирает другой человек. Своё можно открыть командой /game.", show_alert=True)
        return

    action = callback_data.action
    selected = list(picker.selected)
    if action == "toggle":
        uid = callback_data.user_id
        if uid in selected:
            selected.remove(uid)
        else:
            selected.append(uid)
        await db.update_picker(picker.id, selected=selected)
    elif action == "all":
        for player in await db.list_players(only_online=True):
            if player.user_id not in selected:
                selected.append(player.user_id)
        await db.update_picker(picker.id, selected=selected)
    elif action == "clear":
        await db.update_picker(picker.id, selected=[])
    elif action == "view":
        await db.update_picker(picker.id, show_all=not picker.show_all)
    elif action == "go":
        await _build(call, db, config, bot, picker)
        return

    picker = await db.get_picker(picker.id)
    text, markup = await _render_picker(db, picker)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass  # ничего не поменялось
    await call.answer()


async def _build(call: CallbackQuery, db: Database, config: Config, bot: Bot, picker: Picker) -> None:
    rows = await db.get_players(picker.selected)
    selected: list[PlayerRow] = [rows[uid] for uid in picker.selected if uid in rows and rows[uid].is_complete]
    if len(selected) < 2:
        await call.answer("Отметь хотя бы двоих игроков", show_alert=True)
        return

    playing, bench = pick_players(
        [row.to_player() for row in selected],
        {row.user_id: row.bench_debt for row in selected},
    )
    lineup = balance(playing, config.weights)

    # Старые черновики из этого меню больше не актуальны.
    for old in await db.open_drafts(picker.id):
        if await db.change_draft_status(old.id, "stale") and old.message_id:
            try:
                await bot.edit_message_text(
                    await _draft_text(db, old, "<i>⏳ Неактуально: ниже собран новый состав.</i>"),
                    chat_id=old.chat_id,
                    message_id=old.message_id,
                    reply_markup=None,
                )
            except TelegramBadRequest:
                pass

    draft_id = await db.create_draft(
        picker, call.message.chat.id, [p.id for p in playing], [p.id for p in bench], lineup
    )
    draft = await db.get_draft(draft_id)
    sent = await call.message.answer(await _draft_text(db, draft), reply_markup=draft_kb(draft_id))
    await db.set_draft_message(draft_id, sent.message_id)
    await call.answer()


@router.callback_query(DraftCb.filter())
async def on_draft(call: CallbackQuery, callback_data: DraftCb, db: Database, config: Config, bot: Bot) -> None:
    draft = await db.get_draft(callback_data.draft_id)
    if draft is None:
        await call.answer("Состав не найден", show_alert=True)
        return
    if not _is_owner(config, call.from_user.id, draft.owner_id):
        await call.answer("Утверждать и перемешивать может только тот, кто собирал игру.", show_alert=True)
        return
    if draft.status != "draft":
        await call.answer("Этот состав уже утверждён или устарел", show_alert=True)
        return

    if callback_data.action == "again":
        await _reshuffle(call, db, config, draft)
    elif callback_data.action == "ok":
        await _confirm(call, db, bot, draft)


async def _reshuffle(call: CallbackQuery, db: Database, config: Config, draft: Draft) -> None:
    rows = await db.get_players(draft.players)
    players = [rows[uid].to_player() for uid in draft.players if uid in rows and rows[uid].is_complete]
    lineup = balance(players, config.weights, exclude=draft.seen, tolerance=config.reshuffle_tolerance)
    if lineup.key == draft.lineup.key:
        await call.answer("Других вариантов разбиения нет", show_alert=True)
        return
    # Все варианты уже показаны — начинаем круг заново.
    seen = [*draft.seen, lineup.key] if lineup.key not in draft.seen else [lineup.key]
    await db.update_draft_lineup(draft.id, lineup, seen)
    draft = await db.get_draft(draft.id)
    try:
        await call.message.edit_text(await _draft_text(db, draft), reply_markup=draft_kb(draft.id))
    except TelegramBadRequest:
        pass
    await call.answer()


async def _confirm(call: CallbackQuery, db: Database, bot: Bot, draft: Draft) -> None:
    if not await db.change_draft_status(draft.id, "confirmed"):
        await call.answer("Этот состав уже утверждён", show_alert=True)
        return
    await db.commit_bench(draft.players, draft.bench)

    full = await _draft_text(db, draft)
    await call.message.edit_text(full + "\n\n✅ <b>Состав утверждён</b>", reply_markup=None)

    # Каждому игроку в личку — его команда и позиция.
    recipients = [uid for uid in draft.players + draft.bench if uid != call.message.chat.id]
    delivered, failed = 0, []
    for uid in recipients:
        intro = texts.personal(draft.lineup, uid) or texts.BENCH_PERSONAL
        try:
            await bot.send_message(uid, f"{intro}\n\n{full}")
            delivered += 1
        except TelegramAPIError as exc:
            log.warning("Не удалось написать игроку %s: %s", uid, exc)
            failed.append(uid)

    if failed:
        names = await db.get_players(failed)
        listed = ", ".join(texts.name(names[uid].name) for uid in failed if uid in names)
        await call.message.answer(
            f"⚠️ Не смог написать в личку: {listed}. Видимо, они заблокировали бота — передай им состав сам."
        )
    await call.answer(f"Утверждено! Разослал игрокам: {delivered} из {len(recipients)}")
