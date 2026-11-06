"""Immortal Draft: два капитана по очереди выбирают себе игроков.

Правила как в режиме Immortal Draft в Dota 2: 10 участников, два капитана
(в Dota это двое с самым высоким ММР), остальные 8 разбираются по схеме
1-2-2-1-1-2-2-1. Кто из капитанов выбирает первым — решает жребий.

Здесь только логика и отрисовка, без обработчиков Telegram.
"""

from __future__ import annotations

import random
from typing import Sequence

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import texts
from .balancer import Lineup, lineup_of
from .db import CaptainDraft, PlayerRow
from .keyboards import player_button_text

DRAFT_SIZE = 10
# Чей пик: 0 — первый капитан, 1 — второй. 8 пиков, по 4 игрока каждому.
PICK_ORDER = (0, 1, 1, 0, 0, 1, 1, 0)
SIDE_ICONS = ("🌞", "🌚")
SIDE_NAMES = ("Свет", "Тьма")


class CaptainDraftCb(CallbackData, prefix="imm"):
    draft_id: int
    action: str  # toggle|all|clear|view|refresh|next|cap|top2|back|start|pick|undo|ok|cancel
    user_id: int = 0


# --- логика ---------------------------------------------------------------------


def current_side(draft: CaptainDraft) -> int | None:
    """Чей сейчас пик (0 или 1), None — драфт не идёт или закончен."""
    if draft.stage != "picking" or len(draft.picks) >= len(PICK_ORDER):
        return None
    return PICK_ORDER[len(draft.picks)]


def current_captain(draft: CaptainDraft) -> int | None:
    side = current_side(draft)
    return None if side is None else draft.captains[side]


def picks_in_a_row(draft: CaptainDraft) -> int:
    """Сколько пиков подряд у текущего капитана, включая этот (1 или 2)."""
    side = current_side(draft)
    if side is None:
        return 0
    count = 0
    for owner in PICK_ORDER[len(draft.picks):]:
        if owner != side:
            break
        count += 1
    return count


def team(draft: CaptainDraft, side: int) -> list[int]:
    if len(draft.captains) < 2:
        return []
    picked = [uid for uid, owner in zip(draft.picks, PICK_ORDER) if owner == side]
    return [draft.captains[side], *picked]


def pool(draft: CaptainDraft) -> list[int]:
    taken = set(draft.captains) | set(draft.picks)
    return [uid for uid in draft.selected if uid not in taken]


def top_two_by_mmr(draft: CaptainDraft, players: dict[int, PlayerRow]) -> list[int]:
    ranked = sorted(
        (players[uid] for uid in draft.selected if uid in players),
        key=lambda p: (-(p.mmr or 0), p.name.lower()),
    )
    return [p.user_id for p in ranked[:2]]


def flip_coin(captains: list[int], rng: random.Random | None = None) -> list[int]:
    order = list(captains)
    (rng or random.Random()).shuffle(order)
    return order


def final_lineup(draft: CaptainDraft, players: dict[int, PlayerRow], weights) -> Lineup:
    """Позиции внутри команд по приоритетам ролей. Сид от id драфта — результат стабилен."""
    first = [players[uid].to_player() for uid in team(draft, 0)]
    second = [players[uid].to_player() for uid in team(draft, 1)]
    return lineup_of(first, second, weights, rng=random.Random(draft.id))


# --- тексты ---------------------------------------------------------------------


def _nick(player: PlayerRow) -> str:
    handle = f" (@{texts.name(player.username)})" if player.username else ""
    return f"{texts.name(player.name)}{handle}"


def _short(player: PlayerRow) -> str:
    return f"{texts.name(player.name)} · {texts.fmt_mmr(player.mmr or 0)} · {texts.roles_short(player.roles)}"


def _captain_name(draft: CaptainDraft, players: dict[int, PlayerRow], side: int) -> str:
    uid = draft.captains[side]
    return texts.name(players[uid].name) if uid in players else "капитан"


def _side_title(draft: CaptainDraft, players: dict[int, PlayerRow], side: int) -> str:
    return f"{SIDE_ICONS[side]} <b>{SIDE_NAMES[side]}</b> · капитан {_captain_name(draft, players, side)}"


def _team_block(draft: CaptainDraft, players: dict[int, PlayerRow], side: int) -> list[str]:
    members = [players[uid] for uid in team(draft, side) if uid in players]
    total = sum(p.mmr or 0 for p in members)
    lines = [f"{_side_title(draft, players, side)} · Σ {texts.fmt_mmr(total)}"]
    for i, p in enumerate(members):
        lines.append(f"{'👑' if i == 0 else '   •'} {_short(p)}")
    return lines


def _pool_block(draft: CaptainDraft, players: dict[int, PlayerRow]) -> list[str]:
    lines = ["🎯 <b>Свободные игроки</b>"]
    for uid in pool(draft):
        p = players.get(uid)
        if p is not None:
            lines.append(f"• {_nick(p)} — <b>{texts.fmt_mmr(p.mmr or 0)}</b> ММР — {texts.roles_long(p.roles)}")
    return lines


def board_text(draft: CaptainDraft, players: dict[int, PlayerRow], weights) -> str:
    head = "🧠 <b>Immortal Draft</b>"
    if draft.stage == "select":
        return "\n".join([
            f"{head} · шаг 1/3",
            "Отметь 10 участников: 2 капитана и 8 игроков, которых они будут выбирать.",
            "",
            f"Отмечено: <b>{len(draft.selected)}/{DRAFT_SIZE}</b>",
        ] + (["Показаны все игроки, ⚫ — оффлайн."] if draft.show_all else []))

    if draft.stage == "captains":
        lines = [
            f"{head} · шаг 2/3",
            "Выбери двух капитанов 👑. В Dota капитанами становятся двое с самым высоким ММР — "
            "для этого есть кнопка «Топ-2 по ММР».",
            "",
        ]
        for uid in draft.selected:
            p = players.get(uid)
            if p is not None:
                mark = "👑" if uid in draft.captains else "•"
                lines.append(f"{mark} {_nick(p)} — {texts.fmt_mmr(p.mmr or 0)} — {texts.roles_long(p.roles)}")
        return "\n".join(lines)

    if draft.stage == "cancelled":
        return f"{head}\n\n❌ Драфт отменён."

    if draft.stage in ("done", "confirmed"):
        result = final_lineup(draft, players, weights)
        titles = tuple(_side_title(draft, players, side) for side in (0, 1))
        text = texts.lineup(result, [], 0, title=f"{head} — составы готовы", team_titles=titles)
        text += "\n<i>Позиции расставлены по приоритетам ролей — капитаны могут договориться иначе.</i>"
        if draft.stage == "confirmed":
            text += "\n\n✅ <b>Состав утверждён</b>"
        return text

    # picking
    side = current_side(draft)
    lines = [
        f"{head} · пик {len(draft.picks) + 1}/{len(PICK_ORDER)}",
        f"🪙 Жребий: первым выбирает <b>{_captain_name(draft, players, 0)}</b>. Порядок: 1-2-2-1-1-2-2-1",
    ]
    if side is not None:
        in_row = picks_in_a_row(draft)
        extra = " (два игрока подряд)" if in_row == 2 else ""
        lines.append(f"👉 Сейчас выбирает 👑 <b>{_captain_name(draft, players, side)}</b>{extra}")
    lines.append("")
    lines += _team_block(draft, players, 0) + [""] + _team_block(draft, players, 1) + [""]
    lines += _pool_block(draft, players)
    return "\n".join(lines)


def captain_dm_text(draft: CaptainDraft, players: dict[int, PlayerRow], weights, captain_id: int) -> str:
    board = board_text(draft, players, weights)
    if draft.stage == "picking":
        if current_captain(draft) == captain_id:
            return "👑 <b>Твой ход!</b> Выбери игрока кнопкой ниже.\n\n" + board
        return "⏳ Сейчас выбирает соперник.\n\n" + board
    return board


# --- клавиатуры -----------------------------------------------------------------


def _pool_buttons(kb: InlineKeyboardBuilder, draft: CaptainDraft, players: dict[int, PlayerRow]) -> int:
    count = 0
    for uid in pool(draft):
        p = players.get(uid)
        if p is not None:
            kb.button(text=_short_button(p), callback_data=CaptainDraftCb(draft_id=draft.id, action="pick", user_id=uid))
            count += 1
    return count


def _short_button(player: PlayerRow) -> str:
    return player_button_text(player, selected=False).removeprefix("▫️ ")


def board_kb(draft: CaptainDraft, players: dict[int, PlayerRow],
             shown: Sequence[PlayerRow] = ()) -> InlineKeyboardMarkup | None:
    kb = InlineKeyboardBuilder()

    def btn(text: str, action: str, user_id: int = 0) -> None:
        kb.button(text=text, callback_data=CaptainDraftCb(draft_id=draft.id, action=action, user_id=user_id))

    if draft.stage == "select":
        selected = set(draft.selected)
        for p in shown:
            btn(player_button_text(p, p.user_id in selected), "toggle", p.user_id)
        btn("☑️ Отметить всех онлайн", "all")
        btn("🧹 Снять все", "clear")
        btn("👁 Только онлайн" if draft.show_all else "👁 Показать всех", "view")
        btn("🔄 Обновить", "refresh")
        btn(f"➡️ Далее: капитаны ({len(draft.selected)}/{DRAFT_SIZE})", "next")
        btn("❌ Отменить", "cancel")
        kb.adjust(*([1] * len(shown)), 2, 2, 1, 1)
    elif draft.stage == "captains":
        for uid in draft.selected:
            p = players.get(uid)
            if p is not None:
                mark = "👑" if uid in draft.captains else "▫️"
                btn(f"{mark} {_short_button(p)}", "cap", uid)
        btn("👑 Топ-2 по ММР", "top2")
        btn("⬅️ Назад", "back")
        btn(f"▶️ Начать драфт ({len(draft.captains)}/2)", "start")
        btn("❌ Отменить", "cancel")
        kb.adjust(*([1] * len(draft.selected)), 1, 2, 1)
    elif draft.stage == "picking":
        count = _pool_buttons(kb, draft, players)
        if draft.picks:
            btn("↩️ Отменить пик", "undo")
        btn("❌ Отменить драфт", "cancel")
        kb.adjust(*([1] * count), 2 if draft.picks else 1)
    elif draft.stage == "done":
        btn("✅ Утвердить и разослать", "ok")
        btn("↩️ Отменить пик", "undo")
        btn("❌ Отменить", "cancel")
        kb.adjust(1, 2)
    else:
        return None
    return kb.as_markup()


def captain_dm_kb(draft: CaptainDraft, players: dict[int, PlayerRow], captain_id: int) -> InlineKeyboardMarkup | None:
    if draft.stage != "picking" or current_captain(draft) != captain_id:
        return None
    kb = InlineKeyboardBuilder()
    count = _pool_buttons(kb, draft, players)
    kb.adjust(*([1] * count))
    return kb.as_markup()
