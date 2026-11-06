from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import aiosqlite

from .balancer import Lineup, Player

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    user_id     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    username    TEXT,
    mmr         INTEGER,
    roles       TEXT    NOT NULL DEFAULT '',   -- "2,3,5" в порядке приоритета
    online      INTEGER NOT NULL DEFAULT 1,
    bench_debt  INTEGER NOT NULL DEFAULT 0,    -- сколько игр подряд сидел на скамейке
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Меню выбора игроков, которое открывает организатор
CREATE TABLE IF NOT EXISTS pickers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    owner_id    INTEGER NOT NULL,
    selected    TEXT    NOT NULL DEFAULT '[]', -- JSON: user_id в порядке выбора
    show_all    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Предложенный состав (пока не утверждён — можно перемешивать)
CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    picker_id   INTEGER NOT NULL,
    owner_id    INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER,
    players     TEXT    NOT NULL,              -- JSON: кто играет
    bench       TEXT    NOT NULL,              -- JSON: кто на скамейке
    seen        TEXT    NOT NULL DEFAULT '[]', -- JSON: уже показанные разбиения
    lineup      TEXT    NOT NULL,              -- JSON: текущий состав
    status      TEXT    NOT NULL DEFAULT 'draft', -- draft | confirmed | stale
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


@dataclass(frozen=True)
class PlayerRow:
    user_id: int
    name: str
    username: str | None
    mmr: int | None
    roles: tuple[int, ...]
    online: bool
    bench_debt: int

    @property
    def is_complete(self) -> bool:
        return self.mmr is not None and bool(self.roles)

    def to_player(self) -> Player:
        assert self.mmr is not None
        return Player(id=self.user_id, name=self.name, mmr=self.mmr, roles=self.roles)


@dataclass(frozen=True)
class Picker:
    id: int
    chat_id: int
    owner_id: int
    selected: list[int]
    show_all: bool


@dataclass(frozen=True)
class Draft:
    id: int
    picker_id: int
    owner_id: int
    chat_id: int
    message_id: int | None
    players: list[int]
    bench: list[int]
    seen: list[str]
    lineup: Lineup
    status: str


def _parse_roles(raw: str) -> tuple[int, ...]:
    return tuple(int(x) for x in raw.split(",") if x)


def _player(row: aiosqlite.Row) -> PlayerRow:
    return PlayerRow(
        user_id=row["user_id"],
        name=row["name"],
        username=row["username"],
        mmr=row["mmr"],
        roles=_parse_roles(row["roles"]),
        online=bool(row["online"]),
        bench_debt=row["bench_debt"],
    )


def _draft(row: aiosqlite.Row) -> Draft:
    return Draft(
        id=row["id"],
        picker_id=row["picker_id"],
        owner_id=row["owner_id"],
        chat_id=row["chat_id"],
        message_id=row["message_id"],
        players=json.loads(row["players"]),
        bench=json.loads(row["bench"]),
        seen=json.loads(row["seen"]),
        lineup=Lineup.from_dict(json.loads(row["lineup"])),
        status=row["status"],
    )


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База не подключена")
        return self._conn

    # --- игроки -----------------------------------------------------------

    async def get_player(self, user_id: int) -> PlayerRow | None:
        async with self.conn.execute("SELECT * FROM players WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return _player(row) if row else None

    async def refresh_identity(self, user_id: int, name: str, username: str | None) -> None:
        """Обновить имя, если игрок уже есть в базе (новых не создаёт)."""
        await self.conn.execute(
            "UPDATE players SET name = ?, username = ? WHERE user_id = ? AND (name != ? OR username IS NOT ?)",
            (name, username, user_id, name, username),
        )
        await self.conn.commit()

    async def set_mmr(self, user_id: int, name: str, username: str | None, mmr: int) -> PlayerRow:
        await self.conn.execute(
            """
            INSERT INTO players (user_id, name, username, mmr) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name = excluded.name, username = excluded.username, mmr = excluded.mmr
            """,
            (user_id, name, username, mmr),
        )
        await self.conn.commit()
        player = await self.get_player(user_id)
        assert player is not None
        return player

    async def set_roles(self, user_id: int, roles: Iterable[int]) -> None:
        await self.conn.execute(
            "UPDATE players SET roles = ? WHERE user_id = ?",
            (",".join(map(str, roles)), user_id),
        )
        await self.conn.commit()

    async def set_online(self, user_id: int, online: bool) -> None:
        await self.conn.execute("UPDATE players SET online = ? WHERE user_id = ?", (int(online), user_id))
        await self.conn.commit()

    async def list_players(self, only_online: bool = False) -> list[PlayerRow]:
        """Только полностью зарегистрированные (есть ММР и роли)."""
        query = "SELECT * FROM players WHERE mmr IS NOT NULL AND roles != ''"
        if only_online:
            query += " AND online = 1"
        query += " ORDER BY online DESC, name COLLATE NOCASE"
        async with self.conn.execute(query) as cur:
            return [_player(row) for row in await cur.fetchall()]

    async def get_players(self, user_ids: Iterable[int]) -> dict[int, PlayerRow]:
        ids = list(user_ids)
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        async with self.conn.execute(f"SELECT * FROM players WHERE user_id IN ({marks})", ids) as cur:
            return {row["user_id"]: _player(row) for row in await cur.fetchall()}

    async def commit_bench(self, playing: Iterable[int], bench: Iterable[int]) -> None:
        playing, bench = list(playing), list(bench)
        if playing:
            marks = ",".join("?" * len(playing))
            await self.conn.execute(f"UPDATE players SET bench_debt = 0 WHERE user_id IN ({marks})", playing)
        if bench:
            marks = ",".join("?" * len(bench))
            await self.conn.execute(
                f"UPDATE players SET bench_debt = bench_debt + 1 WHERE user_id IN ({marks})", bench
            )
        await self.conn.commit()

    # --- меню выбора ------------------------------------------------------

    async def create_picker(self, chat_id: int, owner_id: int, selected: list[int]) -> Picker:
        cur = await self.conn.execute(
            "INSERT INTO pickers (chat_id, owner_id, selected) VALUES (?, ?, ?)",
            (chat_id, owner_id, json.dumps(selected)),
        )
        await self.conn.commit()
        return Picker(id=cur.lastrowid, chat_id=chat_id, owner_id=owner_id, selected=selected, show_all=False)

    async def get_picker(self, picker_id: int) -> Picker | None:
        async with self.conn.execute("SELECT * FROM pickers WHERE id = ?", (picker_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return Picker(
            id=row["id"],
            chat_id=row["chat_id"],
            owner_id=row["owner_id"],
            selected=json.loads(row["selected"]),
            show_all=bool(row["show_all"]),
        )

    async def update_picker(self, picker_id: int, *, selected: list[int] | None = None,
                            show_all: bool | None = None) -> None:
        if selected is not None:
            await self.conn.execute("UPDATE pickers SET selected = ? WHERE id = ?",
                                    (json.dumps(selected), picker_id))
        if show_all is not None:
            await self.conn.execute("UPDATE pickers SET show_all = ? WHERE id = ?", (int(show_all), picker_id))
        await self.conn.commit()

    # --- составы ----------------------------------------------------------

    async def create_draft(self, picker: Picker, chat_id: int, players: list[int], bench: list[int],
                           lineup: Lineup) -> int:
        cur = await self.conn.execute(
            """
            INSERT INTO drafts (picker_id, owner_id, chat_id, players, bench, seen, lineup)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (picker.id, picker.owner_id, chat_id, json.dumps(players), json.dumps(bench),
             json.dumps([lineup.key]), json.dumps(lineup.to_dict())),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def set_draft_message(self, draft_id: int, message_id: int) -> None:
        await self.conn.execute("UPDATE drafts SET message_id = ? WHERE id = ?", (message_id, draft_id))
        await self.conn.commit()

    async def get_draft(self, draft_id: int) -> Draft | None:
        async with self.conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)) as cur:
            row = await cur.fetchone()
        return _draft(row) if row else None

    async def update_draft_lineup(self, draft_id: int, lineup: Lineup, seen: list[str]) -> None:
        await self.conn.execute(
            "UPDATE drafts SET lineup = ?, seen = ? WHERE id = ?",
            (json.dumps(lineup.to_dict()), json.dumps(seen), draft_id),
        )
        await self.conn.commit()

    async def open_drafts(self, picker_id: int) -> list[Draft]:
        async with self.conn.execute(
            "SELECT * FROM drafts WHERE picker_id = ? AND status = 'draft'", (picker_id,)
        ) as cur:
            return [_draft(row) for row in await cur.fetchall()]

    async def change_draft_status(self, draft_id: int, new_status: str) -> bool:
        """Перевести черновик в новый статус. False — если он уже не черновик."""
        cur = await self.conn.execute(
            "UPDATE drafts SET status = ? WHERE id = ? AND status = 'draft'", (new_status, draft_id)
        )
        await self.conn.commit()
        return cur.rowcount == 1
