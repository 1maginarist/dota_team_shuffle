from __future__ import annotations

from typing import Sequence

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .balancer import POSITION_NAMES, POSITIONS
from .db import Picker, PlayerRow
from .texts import MAX_NAME, fmt_mmr, roles_short

EMPTY_ORDER = "-"


class RolesCb(CallbackData, prefix="roles"):
    action: str  # set | save
    order: str   # текущий порядок ролей, например "235"; "-" — пусто


class ProfileCb(CallbackData, prefix="prof"):
    action: str  # mmr | roles | online | offline


class PickCb(CallbackData, prefix="pick"):
    picker_id: int
    action: str  # toggle | all | clear | view | refresh | go
    user_id: int = 0


class DraftCb(CallbackData, prefix="draft"):
    draft_id: int
    action: str  # again | ok


def encode_order(order: Sequence[int]) -> str:
    return "".join(map(str, order)) or EMPTY_ORDER


def decode_order(raw: str) -> list[int]:
    if raw == EMPTY_ORDER:
        return []
    order: list[int] = []
    for ch in raw:
        pos = int(ch)
        if pos in POSITIONS and pos not in order:
            order.append(pos)
    return order


def roles_kb(order: Sequence[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for pos in POSITIONS:
        if pos in order:
            new_order = [p for p in order if p != pos]
            text = f"✅ {pos} · {POSITION_NAMES[pos]} — {order.index(pos) + 1}-я"
        else:
            new_order = [*order, pos]
            text = f"{pos} · {POSITION_NAMES[pos]}"
        kb.button(text=text, callback_data=RolesCb(action="set", order=encode_order(new_order)))
    kb.button(text="🧹 Сбросить", callback_data=RolesCb(action="set", order=EMPTY_ORDER))
    kb.button(text="💾 Сохранить", callback_data=RolesCb(action="save", order=encode_order(order)))
    kb.adjust(1, 1, 1, 1, 1, 2)
    return kb.as_markup()


def profile_kb(player: PlayerRow) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить ММР", callback_data=ProfileCb(action="mmr"))
    kb.button(text="🎭 Изменить роли", callback_data=ProfileCb(action="roles"))
    if player.online:
        kb.button(text="⚫ Уйти в оффлайн", callback_data=ProfileCb(action="offline"))
    else:
        kb.button(text="🟢 Выйти в онлайн", callback_data=ProfileCb(action="online"))
    kb.adjust(2, 1)
    return kb.as_markup()


def _player_button_text(player: PlayerRow, selected: bool) -> str:
    raw_name = player.name.strip() or "Без имени"
    if len(raw_name) > MAX_NAME:
        raw_name = raw_name[: MAX_NAME - 1] + "…"
    mark = "✅" if selected else "▫️"
    status = "" if player.online else "⚫ "
    return f"{mark} {status}{raw_name} · {fmt_mmr(player.mmr or 0)} · {roles_short(player.roles)}"


def picker_kb(picker: Picker, shown: Sequence[PlayerRow]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    selected = set(picker.selected)
    for player in shown:
        kb.button(
            text=_player_button_text(player, player.user_id in selected),
            callback_data=PickCb(picker_id=picker.id, action="toggle", user_id=player.user_id),
        )
    kb.button(text="☑️ Отметить всех онлайн", callback_data=PickCb(picker_id=picker.id, action="all"))
    kb.button(text="🧹 Снять все", callback_data=PickCb(picker_id=picker.id, action="clear"))
    kb.button(
        text="👁 Только онлайн" if picker.show_all else "👁 Показать всех",
        callback_data=PickCb(picker_id=picker.id, action="view"),
    )
    kb.button(text="🔄 Обновить", callback_data=PickCb(picker_id=picker.id, action="refresh"))
    kb.button(
        text=f"⚖️ Распределить ({len(picker.selected)})",
        callback_data=PickCb(picker_id=picker.id, action="go"),
    )
    kb.adjust(*([1] * len(shown)), 2, 2, 1)
    return kb.as_markup()


def draft_kb(draft_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔀 Другой вариант", callback_data=DraftCb(draft_id=draft_id, action="again"))
    kb.button(text="✅ Утвердить", callback_data=DraftCb(draft_id=draft_id, action="ok"))
    kb.adjust(2)
    return kb.as_markup()
