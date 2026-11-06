from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .balancer import Weights


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: str
    # Кто может собирать команды. Пусто — любой зарегистрированный игрок.
    admin_ids: frozenset[int]
    weights: Weights
    # «Другой вариант» выбирает случайно среди вариантов, которые хуже
    # лучшего не больше чем на столько (в ММР-эквиваленте).
    reshuffle_tolerance: int

    def can_organize(self, user_id: int) -> bool:
        return not self.admin_ids or user_id in self.admin_ids


def _int_list(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


def load_config() -> Config:
    load_dotenv()
    # Прощаем частые ошибки ручного ввода: пробелы и кавычки вокруг токена.
    token = os.getenv("BOT_TOKEN", "").strip().strip("\"'").strip()
    if not token:
        raise SystemExit("Не задан BOT_TOKEN. Скопируй .env.example в .env и впиши токен от @BotFather.")

    defaults = Weights()
    rank_penalties = os.getenv("ROLE_PENALTIES", "").strip()
    off_role = os.getenv("OFF_ROLE_PENALTY", "").strip()
    weights = Weights(
        rank_penalties=tuple(_int_list(rank_penalties)) if rank_penalties else defaults.rank_penalties,
        off_role_penalty=int(off_role) if off_role else defaults.off_role_penalty,
    )

    return Config(
        bot_token=token,
        db_path=os.getenv("DB_PATH", "data/bot.sqlite3"),
        admin_ids=frozenset(_int_list(os.getenv("ADMIN_IDS", ""))),
        weights=weights,
        reshuffle_tolerance=int(os.getenv("RESHUFFLE_TOLERANCE", "200")),
    )
