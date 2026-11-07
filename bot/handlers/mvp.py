"""/mvp — MVP вечера по номерам матчей."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import LinkPreviewOptions, Message

from .. import mvp_texts, replay
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

    # Реплеи, которые положили на сервер (лобби, которых нет в OpenDota), разбираем сами.
    folder = Path(config.replays_dir)
    loads = {mid: MatchLoad(mid) for mid in match_ids}
    local_ids = [mid for mid in match_ids if replay.find_replay(folder, mid)]
    for mid in local_ids:
        loads[mid].status = "реплей с сервера"
    status = await message.answer(mvp_texts.progress(list(loads.values()), 0))

    async def show_progress(updated: list[MatchLoad], elapsed: int) -> None:
        for item in updated:
            loads[item.match_id] = item
        try:
            await status.edit_text(mvp_texts.progress(list(loads.values()), elapsed))
        except TelegramBadRequest:
            pass  # текст не изменился

    warnings: list[str] = []
    found: dict[int, dict] = {}
    for mid in local_ids:
        loads[mid].status = "разбираю файл реплея"
        await show_progress([], 0)
        try:
            data = await replay.load_local(mid, folder, config.parser_url, opendota)
            found[mid] = data
            loads[mid].data, loads[mid].status = data, "готово"
        except replay.ReplayError as exc:
            loads[mid].status = "ошибка реплея"
            warnings.append(f"Реплей {mid}: {exc}")
        except Exception:
            log.exception("Не удалось разобрать реплей %s", mid)
            loads[mid].status = "ошибка реплея"
            warnings.append(f"Реплей {mid}: не удалось разобрать, подробности в логах бота.")

    remote_ids = [mid for mid in match_ids if mid not in local_ids]
    if remote_ids:
        try:
            loaded = await load_matches(
                opendota, remote_ids, show_progress, parse_timeout=PARSE_TIMEOUT, poll_interval=POLL_INTERVAL
            )
        except Exception:
            log.exception("Не удалось загрузить матчи %s", remote_ids)
            await status.edit_text("❌ Не получилось загрузить матчи из OpenDota. Попробуй ещё раз через минуту.")
            return
        warnings += loaded.warnings
        found.update({int(m["match_id"]): m for m in loaded.matches})

    matches = []
    for match in (found[mid] for mid in match_ids if mid in found):
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
    alternative = compute_mvp(matches, formula="fantasy")
    await status.edit_text(mvp_texts.summary(result, heroes, warnings), link_preview_options=NO_PREVIEW)
    await message.answer(mvp_texts.breakdown(result, heroes), link_preview_options=NO_PREVIEW)
    await message.answer(mvp_texts.fantasy(alternative, heroes, result.mvp.key), link_preview_options=NO_PREVIEW)
