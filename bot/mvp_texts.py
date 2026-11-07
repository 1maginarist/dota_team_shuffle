from __future__ import annotations

from html import escape as _escape

from .balancer import POSITION_NAMES
from .mvp import FANTASY_ITEMS, WIN_BONUS, MvpResult, PlayerGame, PlayerTotal
from .opendota import MatchLoad

STEAM64_BASE = 76561197960265728


def escape(text: str) -> str:
    return _escape(text, quote=False)

MAX_MESSAGE = 4000

USAGE = (
    "Пришли номера матчей после команды, например:\n"
    "<code>/mvp 8123456789 8123456790 8123456791</code>\n\n"
    "Подойдут и ссылки на Dotabuff/OpenDota/Stratz. Номер матча есть в клиенте Dota: "
    "профиль → недавние матчи → нужный матч."
)

HELP = (
    "🏆 <b>Как считается MVP</b>\n\n"
    "Статистика — из OpenDota или из реплея, который лежит на сервере (для приватных лобби).\n\n"
    "<b>1. Фэнтези-очки Valve</b> — классическая система The International:\n"
    "• 0.3 за убийство\n"
    "• 3 − 0.3 за каждую смерть (не меньше 0)\n"
    "• 0.003 за добивание и денай, 0.002 за единицу GPM\n"
    "• 1 за башню или Рошана\n"
    "• 3 × участие в драках\n"
    "• 0.5 за обсервер, 0.5 за стак, 0.25 за руну\n"
    "• 4 за первую кровь, 0.05 за секунду оглушения\n\n"
    "<b>2. Сравнение внутри позиции.</b> Очки пятёрки сравниваются с пятёрками, керри — с керри. "
    "Без этого почти всегда выигрывал бы мидер. Балл матча 0–100: 50 — средний уровень позиции. "
    f"Победа ×{WIN_BONUS:.1f}.\n\n"
    "<b>3. Итог вечера</b> — средний балл с поправкой: к играм каждого добавляются две «средние игры лобби». "
    "Одна случайная удачная игра не перевешивает три стабильно сильные, а пропуск матча из-за скамейки "
    "стоит всего несколько баллов.\n\n"
    "⚠️ Чтобы игрока можно было узнать по матчам, у всех в Dota должна быть включена "
    "«Общедоступная история матчей» (Expose Public Match Data). Иначе он будет анонимом."
)


def _hero(heroes: dict[int, str], hero_id: int) -> str:
    return escape(heroes.get(hero_id, f"герой #{hero_id}"))


def _name(name: str | None, account_id: int | None, game: PlayerGame, heroes: dict[int, str]) -> str:
    if account_id is None:
        return f"Аноним ({_hero(heroes, game.hero_id)})"
    if name:
        return escape(name.strip()[:24] or f"id {account_id}")
    return f"id {account_id}"


def player_name(total: PlayerTotal, heroes: dict[int, str]) -> str:
    return _name(total.name, total.account_id, total.games[0], heroes)


def _position(game: PlayerGame) -> str:
    suffix = "?" if game.position_guessed else ""
    return f"поз. {game.position}{suffix}"


def progress(loads: list[MatchLoad], elapsed: int) -> str:
    icons = {"готово": "✅", "разбираю реплей": "🔄", "без разбора": "⚠️", "не найден": "❌", "ошибка OpenDota": "❌",
             "реплей с сервера": "📼", "разбираю файл реплея": "🔄", "ошибка реплея": "❌"}
    lines = [f"⏳ <b>Считаю MVP по матчам: {len(loads)}</b>", ""]
    for i, item in enumerate(loads, start=1):
        status = item.status
        if status == "разбираю реплей":
            status += f"… {elapsed // 60}:{elapsed % 60:02d}"
        lines.append(f"#{i} <code>{item.match_id}</code> — {icons.get(item.status, '⏳')} {status}")
    if any(i.status == "разбираю реплей" for i in loads):
        lines += ["", "<i>OpenDota разбирает реплеи: обычно это 1–3 минуты.</i>"]
    return "\n".join(lines)


def summary(result: MvpResult, heroes: dict[int, str], warnings: list[str]) -> str:
    mvp = result.mvp
    best = max(mvp.games, key=lambda g: g.score)
    lines = [
        f"🏆 <b>MVP вечера — {player_name(mvp, heroes)}</b>",
        f"Итоговый балл <b>{mvp.adjusted:.1f}</b> · матчей: {len(mvp.games)} · средний {mvp.average:.1f}",
        f"Лучшая игра: {_hero(heroes, best.hero_id)}, {_position(best)} — {best.score:.1f} "
        f"(матч #{best.match_index}{', победа' if best.won else ''})",
    ]
    if mvp.account_id is not None:
        lines.append(
            f'<a href="https://www.dotabuff.com/players/{mvp.account_id}">Dotabuff</a> · '
            f'<a href="https://steamcommunity.com/profiles/{mvp.account_id + STEAM64_BASE}">Steam</a>'
        )

    lines += ["", "📊 <b>Рейтинг вечера</b>"]
    for place, total in enumerate(result.ranking, start=1):
        games = " · ".join(f"#{g.match_index} {g.score:.0f} ({g.points:.1f} очк.)"
                           for g in sorted(total.games, key=lambda g: g.match_index))
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(place, f"{place}.")
        lines.append(f"{medal} {player_name(total, heroes)} — <b>{total.adjusted:.1f}</b> <i>({games})</i>")

    lines += ["", "🎮 <b>Матчи</b>"]
    for match in result.matches:
        top = match.mvp
        winner = "Свет" if match.radiant_win else "Тьма"
        lines.append(
            f'#{match.index} <a href="https://www.dotabuff.com/matches/{match.match_id}">{match.match_id}</a> · '
            f"{match.duration_min:.0f} мин · победа: {winner} — лучший: "
            f"{_name(top.name, top.account_id, top, heroes)} ({_hero(heroes, top.hero_id)}) {top.score:.1f}"
        )

    notes = list(warnings)
    if result.anonymous_count:
        notes.append(
            f"Анонимов в матчах: {result.anonymous_count}. Их игры не склеиваются между матчами — "
            "попроси включить в Dota «Общедоступная история матчей»."
        )
    if notes:
        lines += [""] + [f"⚠️ {escape(n)}" for n in notes]
    lines += [
        "",
        f"<i>Фэнтези-очки Valve, сравнение внутри позиции: 50 — средний уровень позиции, победа ×{WIN_BONUS:.1f}. "
        "Итог — среднее за матчи с поправкой на число игр. Как считается: /mvphelp</i>",
    ]
    return _fit("\n".join(lines))


def breakdown(result: MvpResult, heroes: dict[int, str], top: int = 3) -> str:
    lines = ["🔎 <b>Откуда очки у лидеров</b> (фэнтези-очки Valve за матч)"]
    for total in result.ranking[:top]:
        lines += ["", f"<b>{player_name(total, heroes)}</b> — {total.adjusted:.1f}"]
        for g in sorted(total.games, key=lambda g: g.match_index):
            outcome = f"победа ×{WIN_BONUS:.1f}" if g.won else "поражение"
            lines.append(
                f"#{g.match_index} {_hero(heroes, g.hero_id)} · {_position(g)} {POSITION_NAMES[g.position]} · "
                f"{g.points:.1f} очк. · {outcome} → <b>{g.score:.1f}</b>"
            )
            parts = [f"{FANTASY_ITEMS[k][0]} {v:.1f}"
                     for k, v in sorted(g.fantasy.items(), key=lambda kv: -kv[1]) if v > 0]
            lines.append("   " + " · ".join(parts))
    return _fit("\n".join(lines))


def _fit(text: str) -> str:
    if len(text) <= MAX_MESSAGE:
        return text
    cut = text[:MAX_MESSAGE].rsplit("\n", 1)[0]
    return cut + "\n…"
