"""Регистрация и профиль игрока: ММР, роли, статус онлайн."""

from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message, User

from .. import texts
from ..db import Database, PlayerRow
from ..keyboards import ProfileCb, RolesCb, decode_order, profile_kb, roles_kb

MIN_MMR, MAX_MMR = 1, 20000
NUMBER_RE = re.compile(r"^\s*\d[\d\s]{0,6}$")

# Обработчики разложены по роутерам: в aiogram собственные обработчики
# роутера срабатывают раньше вложенных, поэтому у родителя своих нет.
private = Router(name="profile-private")
private.message.filter(F.chat.type == ChatType.PRIVATE)
private.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

common = Router(name="profile-common")

group = Router(name="profile-group")
group.message.filter(F.chat.type != ChatType.PRIVATE)

router = Router(name="profile")
router.include_routers(private, common, group)

# Подключается последним, после всех остальных роутеров (см. __main__).
fallback = Router(name="fallback")
fallback.message.filter(F.chat.type == ChatType.PRIVATE)

WELCOME = (
    "👋 Привет! Я распределяю нас по командам в Dota 2.\n"
    "Регистрация в два шага: ММР и роли по приоритету.\n\n"
)
ASK_MMR = "Пришли свой ММР числом, например <code>3500</code>."


async def _send_next_step(message: Message, db: Database, user: User) -> None:
    """Показать то, чего не хватает игроку: ММР → роли → профиль."""
    player = await db.get_player(user.id)
    if player is None or player.mmr is None:
        await message.answer(WELCOME + ASK_MMR)
    elif not player.roles:
        await message.answer(texts.roles_prompt([]), reply_markup=roles_kb([]))
    else:
        await message.answer(texts.profile(player), reply_markup=profile_kb(player))


async def _redirect_to_private(message: Message, bot: Bot) -> None:
    me = await bot.me()
    await message.reply(f"Профиль настраивается в личке со мной: https://t.me/{me.username}?start=profile")


# --- команды в личке --------------------------------------------------------

@private.message(CommandStart())
@private.message(Command("profile", "me"))
async def cmd_start(message: Message, db: Database) -> None:
    await _send_next_step(message, db, message.from_user)


@private.message(Command("mmr"))
async def cmd_mmr(message: Message, command: CommandObject, db: Database) -> None:
    if not command.args:
        await message.answer(ASK_MMR)
        return
    await _apply_mmr(message, db, command.args)


@private.message(F.text.regexp(NUMBER_RE))
async def plain_number(message: Message, db: Database) -> None:
    await _apply_mmr(message, db, message.text)


async def _apply_mmr(message: Message, db: Database, raw: str) -> None:
    digits = re.sub(r"\s", "", raw)
    if not digits.isdigit() or not MIN_MMR <= int(digits) <= MAX_MMR:
        await message.answer(f"ММР должен быть числом от {MIN_MMR} до {texts.fmt_mmr(MAX_MMR)}. Попробуй ещё раз.")
        return
    user = message.from_user
    player = await db.set_mmr(user.id, user.full_name, user.username, int(digits))
    if not player.roles:
        await message.answer(
            f"ММР сохранён: <b>{texts.fmt_mmr(player.mmr)}</b>.\n\n" + texts.roles_prompt([]),
            reply_markup=roles_kb([]),
        )
    else:
        await message.answer(
            f"✅ ММР обновлён: <b>{texts.fmt_mmr(player.mmr)}</b>\n\n" + texts.profile(player),
            reply_markup=profile_kb(player),
        )


@private.message(Command("roles"))
async def cmd_roles(message: Message, db: Database) -> None:
    player = await db.get_player(message.from_user.id)
    if player is None or player.mmr is None:
        await message.answer("Сначала пришли свой ММР числом, а потом выберешь роли.")
        return
    await message.answer(texts.roles_prompt(player.roles), reply_markup=roles_kb(player.roles))


@private.callback_query(RolesCb.filter(F.action == "set"))
async def roles_set(call: CallbackQuery, callback_data: RolesCb) -> None:
    order = decode_order(callback_data.order)
    try:
        await call.message.edit_text(texts.roles_prompt(order), reply_markup=roles_kb(order))
    except TelegramBadRequest:
        pass  # сообщение не изменилось (например, «Сбросить» при пустом списке)
    await call.answer()


@private.callback_query(RolesCb.filter(F.action == "save"))
async def roles_save(call: CallbackQuery, callback_data: RolesCb, db: Database) -> None:
    order = decode_order(callback_data.order)
    if not order:
        await call.answer("Выбери хотя бы одну роль", show_alert=True)
        return
    player = await db.get_player(call.from_user.id)
    if player is None or player.mmr is None:
        await call.answer("Сначала пришли свой ММР числом", show_alert=True)
        return
    first_time = not player.roles
    await db.set_roles(player.user_id, order)
    player = await db.get_player(player.user_id)
    header = (
        "🎉 <b>Готово, ты зарегистрирован!</b>\n"
        "Ты сейчас онлайн: организатор увидит тебя в списке. "
        "Если сегодня не играешь, нажми «Уйти в оффлайн».\n\n"
        if first_time
        else "✅ Роли сохранены\n\n"
    )
    await call.message.edit_text(header + texts.profile(player), reply_markup=profile_kb(player))
    await call.answer()


@private.callback_query(ProfileCb.filter())
async def profile_action(call: CallbackQuery, callback_data: ProfileCb, db: Database) -> None:
    player = await db.get_player(call.from_user.id)
    if player is None or not player.is_complete:
        await call.answer("Сначала закончи регистрацию: /start", show_alert=True)
        return

    if callback_data.action == "mmr":
        await call.message.answer("Пришли новый ММР числом.")
    elif callback_data.action == "roles":
        await call.message.answer(texts.roles_prompt(player.roles), reply_markup=roles_kb(player.roles))
    elif callback_data.action in ("online", "offline"):
        await db.set_online(player.user_id, callback_data.action == "online")
        player = await db.get_player(player.user_id)
        try:
            await call.message.edit_text(texts.profile(player), reply_markup=profile_kb(player))
        except TelegramBadRequest:
            pass
    await call.answer()


# --- работают в любом чате --------------------------------------------------

@common.message(Command("online", "offline"))
async def cmd_online(message: Message, command: CommandObject, db: Database, bot: Bot) -> None:
    player = await db.get_player(message.from_user.id)
    if player is None or not player.is_complete:
        if message.chat.type == ChatType.PRIVATE:
            await _send_next_step(message, db, message.from_user)
        else:
            await _redirect_to_private(message, bot)
        return
    online = command.command == "online"
    await db.set_online(player.user_id, online)
    status = "🟢 Ты онлайн — организатор видит тебя в списке." if online else "⚫ Ты оффлайн — в списке тебя не будет."
    await message.reply(status)


@common.message(Command("players"))
async def cmd_players(message: Message, db: Database) -> None:
    players = await db.list_players()
    if not players:
        await message.answer("Пока никто не зарегистрировался. Пусть каждый напишет мне /start в личку.")
        return
    online = sum(1 for p in players if p.online)
    lines = [f"👥 <b>Игроки</b>: {len(players)}, онлайн: {online}", ""]
    lines += [_player_line(p) for p in players]
    await message.answer("\n".join(lines))


def _player_line(player: PlayerRow) -> str:
    status = "🟢" if player.online else "⚫"
    return (
        f"{status} {texts.name(player.name)} — {texts.fmt_mmr(player.mmr)} · "
        f"роли {texts.roles_short(player.roles)}"
    )


@common.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


@common.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.reply(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


@group.message(CommandStart())
@group.message(Command("profile", "me", "mmr", "roles"))
async def group_redirect(message: Message, bot: Bot) -> None:
    await _redirect_to_private(message, bot)


@fallback.message()
async def unknown(message: Message) -> None:
    await message.answer("Не понял 🤔 Пришли ММР числом или открой /help.")
