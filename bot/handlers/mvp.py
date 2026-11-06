"""/mvp — MVP вечера по номерам матчей."""

from __future__ import annotations

import logging
import re

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import LinkPreviewOptions, Message

from .. import mvp_texts
from ..config import Config
from ..mvp import compute_mvp
from ..opendota import MatchLoad, OpenDota, OpenDotaError, load_matches

log = logging.getLogger(__name__)
router = Router(name="mvp")

MATCH_ID_RE = re.compile(r"(?<!\d)(\d{8,12})(?!\d)")
MAX_MATCHES = 10
PARSE_TIMEOUT = 300  # сколько ждать разбора реплеев, секунд
POLL_INTERVAL = 10
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


@router.message(Command("mvphelp"))
async def cmd_mvphelp(message: Message) -> None:
    await message.answer(mvp_texts.HELP)


@router.message(Command("mvp"))
async def cmd_mvp(message: Message, command: CommandObject, config: Config, opendota: OpenDota) -> None:
    if not config.can_organize(message.from_user.id):
        await message.reply("Считать MVP могут только организаторы.")
        return
    match_ids = list(dict.fromkeys(int(x) for x in MATCH_ID_RE.findall(command.args or "")))
    if not match_ids:
        await message.answer(mvp_texts.USAGE)
        return
    if len(match_ids) > MAX_MATCHES:
        await message.answer(f"Не больше {MAX_MATCHES} матчей за раз.")
        return

    status = await message.answer(mvp_texts.progress([MatchLoad(m) for m in match_ids], 0))

    async def show_progress(loads: list[MatchLoad], elapsed: int) -> None:
        try:
            await status.edit_text(mvp_texts.progress(loads, elapsed))
        except TelegramBadRequest:
            pass  # текст не изменился

    try:
        loaded = await load_matches(
            opendota, match_ids, show_progress, parse_timeout=PARSE_TIMEOUT, poll_interval=POLL_INTERVAL
        )
    except Exception:
        log.exception("Не удалось загрузить матчи %s", match_ids)
        await status.edit_text("❌ Не получилось загрузить матчи из OpenDota. Попробуй ещё раз через минуту.")
        return

    warnings = list(loaded.warnings)
    matches = []
    for match in loaded.matches:
        if (match.get("duration") or 0) <= 0:
            warnings.append(f"Матч {match.get('match_id')} пропущен: он не доигран или пустой.")
        else:
            matches.append(match)
    if not matches:
        await status.edit_text("❌ Нечего считать.\n\n" + "\n".join(f"⚠️ {w}" for w in warnings))
        return

    try:
        heroes = await opendota.heroes()
    except OpenDotaError:
        heroes = {}
    result = compute_mvp(matches)
    await status.edit_text(mvp_texts.summary(result, heroes, warnings), link_preview_options=NO_PREVIEW)
    await message.answer(mvp_texts.breakdown(result, heroes), link_preview_options=NO_PREVIEW)
