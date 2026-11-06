from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    TelegramObject,
)

from .config import Config, load_config
from .db import Database
from .handlers import mvp, organizer, profile
from .opendota import OpenDota

log = logging.getLogger("bot")


class RefreshNameMiddleware(BaseMiddleware):
    """Держим имена игроков актуальными, если они сменили их в Telegram."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        db: Database = data["db"]
        if user is not None and not user.is_bot:
            await db.refresh_identity(user.id, user.full_name, user.username)
        return await handler(event, data)


def build_dispatcher(db: Database, config: Config, opendota: OpenDota | None = None) -> Dispatcher:
    dp = Dispatcher(db=db, config=config, opendota=opendota or OpenDota(config.opendota_api_key))
    dp.update.outer_middleware(RefreshNameMiddleware())
    dp.include_routers(profile.router, organizer.router, mvp.router, profile.fallback)
    return dp


async def set_commands(bot: Bot) -> None:
    private = [
        BotCommand(command="start", description="Регистрация / профиль"),
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="mmr", description="Изменить ММР: /mmr 4500"),
        BotCommand(command="roles", description="Выбрать роли"),
        BotCommand(command="online", description="Я играю — показывать меня"),
        BotCommand(command="offline", description="Я не играю — скрыть меня"),
        BotCommand(command="game", description="Собрать игру (организатор)"),
        BotCommand(command="players", description="Все игроки"),
        BotCommand(command="mvp", description="MVP вечера: /mvp id1 id2 id3"),
        BotCommand(command="help", description="Как это работает"),
    ]
    group = [
        BotCommand(command="game", description="Собрать игру"),
        BotCommand(command="players", description="Все игроки"),
        BotCommand(command="online", description="Я играю"),
        BotCommand(command="offline", description="Я не играю"),
        BotCommand(command="mvp", description="MVP вечера: /mvp id1 id2 id3"),
        BotCommand(command="help", description="Как это работает"),
    ]
    await bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(group, scope=BotCommandScopeAllGroupChats())


async def connect(bot: Bot) -> None:
    """Проверить токен и связь с Telegram, понятно объяснить, если что-то не так."""
    log.info("Подключаюсь к Telegram…")
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        raise SystemExit(
            "Telegram отклонил BOT_TOKEN — токен неверный или отозван. "
            "Скопируй его заново из @BotFather в .env и перезапусти бота."
        )
    except TelegramNetworkError as exc:
        raise SystemExit(
            f"Нет связи с api.telegram.org ({exc}). Сервер не может достучаться до Telegram: "
            "проверь интернет/фаервол на VPS, например: curl -I https://api.telegram.org"
        )
    # Если на токене когда-то висел webhook, getUpdates не работает — снимаем его.
    await bot.delete_webhook(drop_pending_updates=False)
    await set_commands(bot)
    log.info("✅ Бот @%s запущен и ждёт сообщений: https://t.me/%s", me.username, me.username)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    try:
        bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    except TokenValidationError:
        raise SystemExit(
            "BOT_TOKEN в .env записан неправильно. Нужно ровно так, без кавычек и пробелов: "
            "BOT_TOKEN=123456789:AAH... (цифры, двоеточие, буквы)"
        )
    db = Database(config.db_path)
    await db.connect()
    dp = build_dispatcher(db, config)
    try:
        await connect(bot)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await dp["opendota"].close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
