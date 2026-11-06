from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    TelegramObject,
)

from .config import Config, load_config
from .db import Database
from .handlers import organizer, profile


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


def build_dispatcher(db: Database, config: Config) -> Dispatcher:
    dp = Dispatcher(db=db, config=config)
    dp.update.outer_middleware(RefreshNameMiddleware())
    dp.include_routers(profile.router, organizer.router, profile.fallback)
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
        BotCommand(command="help", description="Как это работает"),
    ]
    group = [
        BotCommand(command="game", description="Собрать игру"),
        BotCommand(command="players", description="Все игроки"),
        BotCommand(command="online", description="Я играю"),
        BotCommand(command="offline", description="Я не играю"),
        BotCommand(command="help", description="Как это работает"),
    ]
    await bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(group, scope=BotCommandScopeAllGroupChats())


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    db = Database(config.db_path)
    await db.connect()
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(db, config)
    try:
        await set_commands(bot)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
