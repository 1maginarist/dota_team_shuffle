from __future__ import annotations

from html import escape
from typing import Sequence

from .balancer import MAX_PLAYERS, POSITION_NAMES, POSITIONS, Lineup
from .db import PlayerRow

TEAM_TITLES = ("🌞 <b>Свет</b>", "🌚 <b>Тьма</b>")
# В винительном падеже: «Ты за Свет / за Тьму»
TEAM_NAMES_ACC = ("🌞 Свет", "🌚 Тьму")
POSITION_EMOJI = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣"}
MAX_NAME = 24

HELP = (
    "🛡 <b>Бот для распределения по командам в Dota 2</b>\n\n"
    "<b>Игрокам</b> (всё в личке с ботом):\n"
    "• /start — регистрация: ММР и роли по приоритету\n"
    "• /profile — твой профиль, там же кнопки «Изменить»\n"
    "• /mmr 4500 — быстро поменять ММР (или просто пришли число)\n"
    "• /roles — заново выбрать роли\n"
    "• /online и /offline — видно ли тебя организатору. "
    "После регистрации ты онлайн, пока сам не выключишь\n\n"
    "<b>Организатору</b>:\n"
    "• /game — открыть список онлайн-игроков, отметить, кто играет, и нажать «Распределить»\n"
    "• /players — все зарегистрированные игроки\n"
    "• /id — узнать свой Telegram ID (для настройки ADMIN_IDS)\n\n"
    "<b>Как собираются команды</b>\n"
    "Бот перебирает все варианты разбиения и выбирает тот, где суммарный ММР команд "
    "ближе всего и максимум людей стоит на своих любимых ролях. Если отмечено больше 10 "
    "человек, лишние идут на скамейку, а в следующей утверждённой игре играют первыми."
)


def name(raw: str) -> str:
    raw = raw.strip() or "Без имени"
    if len(raw) > MAX_NAME:
        raw = raw[: MAX_NAME - 1] + "…"
    return escape(raw)


def fmt_mmr(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def roles_short(roles: Sequence[int]) -> str:
    return "·".join(map(str, roles)) if roles else "—"


def roles_long(roles: Sequence[int]) -> str:
    return " → ".join(f"{pos} {POSITION_NAMES[pos]}" for pos in roles) if roles else "не выбраны"


def profile(player: PlayerRow) -> str:
    unwanted = [POSITION_NAMES[pos] for pos in POSITIONS if pos not in player.roles]
    status = (
        "🟢 онлайн — организатор видит тебя в списке"
        if player.online
        else "⚫ оффлайн — тебя нет в списке у организатора"
    )
    lines = [
        f"👤 <b>{name(player.name)}</b>",
        f"ММР: <b>{fmt_mmr(player.mmr) if player.mmr is not None else 'не указан'}</b>",
        f"Роли: {roles_long(player.roles)}",
    ]
    if unwanted and player.roles:
        lines.append(f"Не хочу: {', '.join(unwanted)}")
    lines.append(f"Статус: {status}")
    return "\n".join(lines)


def roles_prompt(order: Sequence[int]) -> str:
    return (
        "🎭 <b>Выбери роли по приоритету</b>\n"
        "Нажимай в порядке от самой любимой. Повторное нажатие убирает роль.\n"
        "Роли, которые не выберешь, считаются «не хочу»: на них бот поставит, "
        "только если иначе никак.\n\n"
        f"Сейчас: <b>{roles_long(order)}</b>"
    )


def format_plan(count: int) -> str:
    if count < 2:
        return "отметь хотя бы двоих"
    if count <= MAX_PLAYERS:
        small = count // 2
        return f"{count - small} на {small}"
    return f"5 на 5, ещё {count - MAX_PLAYERS} на скамейку"


def picker(online_count: int, selected_count: int, show_all: bool) -> str:
    lines = [
        "🎯 <b>Сбор игры</b>",
        f"Онлайн: {online_count} · Отмечено: <b>{selected_count}</b> ({format_plan(selected_count)})",
        "",
        "Нажимай на игроков, чтобы отметить, кто играет, потом жми «Распределить».",
    ]
    if show_all:
        lines.append("Показаны все игроки, ⚫ — оффлайн.")
    elif online_count == 0:
        lines.append("Сейчас никого нет онлайн. Нажми «Показать всех», чтобы выбрать из всех игроков.")
    if selected_count > MAX_PLAYERS:
        lines.append("На скамейку первыми идут те, кто не сидел в прошлый раз, а среди равных — "
                     "те, кого ты отметил последними.")
    return "\n".join(lines)


def _slot_line(position: int, player, rank: int | None) -> str:
    head = f"{POSITION_EMOJI[position]} {POSITION_NAMES[position]} — "
    if player is None:
        return head + "<i>пусто</i>"
    if rank == 0:
        mark = " ⭐"
    elif rank is None:
        mark = " ⚠️"
    else:
        mark = f" ({rank + 1}-я)"
    return head + f"{name(player.name)} · {fmt_mmr(player.mmr)}{mark}"


def lineup(result: Lineup, bench_names: Sequence[str], variant: int) -> str:
    lines = [f"⚔️ <b>Составы</b> · вариант {variant}", ""]
    for title, team in zip(TEAM_TITLES, result.teams):
        lines.append(f"{title} — Σ {fmt_mmr(team.total_mmr)} · ср. {fmt_mmr(team.avg_mmr)}")
        for slot in team.slots:
            lines.append(_slot_line(slot.position, slot.player, slot.rank))
        lines.append("")

    ranks = [slot.rank for team in result.teams for slot in team.slots if slot.player is not None]
    main = sum(1 for r in ranks if r == 0)
    other = sum(1 for r in ranks if r is not None and r > 0)
    off = sum(1 for r in ranks if r is None)
    roles_line = f"🎭 На основной роли: {main} из {len(ranks)}"
    if other:
        roles_line += f" · на запасной: {other}"
    if off:
        roles_line += f" · не на своей: {off}"

    lines.append(f"⚖️ Разница: <b>{fmt_mmr(result.mmr_diff)}</b> ММР")
    lines.append(roles_line)
    if bench_names:
        lines.append(f"🪑 Скамейка: {', '.join(name(n) for n in bench_names)}")
    lines.append("")
    lines.append("<i>⭐ основная роль · (2-я) вторая по приоритету и т.д. · ⚠️ роль не из списка</i>")
    return "\n".join(lines)


def personal(result: Lineup, user_id: int) -> str | None:
    found = result.find(user_id)
    if found is None:
        return None
    team_index, position = found
    return (
        f"🎮 <b>Игра собрана!</b>\n"
        f"Ты за {TEAM_NAMES_ACC[team_index]}, позиция {position} — <b>{POSITION_NAMES[position]}</b>."
    )


BENCH_PERSONAL = "🪑 В этот раз ты на скамейке. В следующей игре играешь в приоритете."
