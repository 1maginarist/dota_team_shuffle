from __future__ import annotations

from html import escape as _escape

from .balancer import POSITION_NAMES
from .mvp import COMPONENT_NAMES, POSITION_WEIGHTS, WIN_BONUS, MvpResult, PlayerGame, PlayerTotal
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
    "Статистику берём из OpenDota (у Dotabuff нет открытого API, данные те же). "
    "Если реплей ещё не разобран, бот сам отправляет его на разбор и ждёт.\n\n"
    "<b>1. Сравниваем с игроками своей позиции.</b> Пятёрку — с пятёрками, керри — с керри. "
    "Поэтому керри не становится MVP просто потому, что у него больше фарма.\n\n"
    "<b>2. Метрики.</b> Фарм, опыт, добивания, убийства, помощь, урон, смерти, урон по строениям, "
    "лечение — в перцентилях OpenDota относительно всех игр на этом же герое (средняя игра на "
    "Пудже = средняя игра на Антимаге). Плюс варды, девард, стаки, оглушения, урон, принятый от героев, "
    "и участие в драках.\n\n"
    "<b>3. Веса зависят от позиции:</b>\n"
    "• Керри: фарм 35%, драки 30%, объекты 20%, выживание 15%\n"
    "• Мид: драки 40%, фарм 25%, объекты 15%, выживание 15%, контроль 5%\n"
    "• Оффлейн: драки 30%, контроль 25% (оглушения + танкование), фарм 15%, объекты 15%, "
    "выживание 10%, саппорт 5%\n"
    "• Семи-сап: драки 35%, саппорт 25%, контроль 15%, выживание 15%, фарм 5%, объекты 5%\n"
    "• Фулл-сап: саппорт 40% (варды, девард, стаки, лечение), драки 25%, выживание 15%, "
    "контроль 10%, фарм 5%, объекты 5%\n\n"
    "<b>4. Балл матча</b> — насколько ты лучше остальных на своей позиции. 50 — средний уровень, "
    "у каждой позиции одинаковые шансы. "
    f"Победа ×{WIN_BONUS:.1f}.\n\n"
    "<b>5. Итог вечера</b> — средний балл с поправкой: к играм каждого добавляются две «средние игры лобби». "
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
    icons = {"готово": "✅", "разбираю реплей": "🔄", "без разбора": "⚠️", "не найден": "❌", "ошибка OpenDota": "❌"}
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
        games = " · ".join(f"#{g.match_index} {g.score:.0f}" for g in sorted(total.games, key=lambda g: g.match_index))
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
        f"<i>Балл матча: 50 — средний уровень своей позиции, победа ×{WIN_BONUS:.1f}. "
        "Итог — среднее за матчи с поправкой на число игр. Как считается: /mvphelp</i>",
    ]
    return _fit("\n".join(lines))


def breakdown(result: MvpResult, heroes: dict[int, str], top: int = 3) -> str:
    lines = ["🔎 <b>Разбор лидеров</b> (составляющие 0–100, в скобках — вес для позиции)"]
    for total in result.ranking[:top]:
        lines += ["", f"<b>{player_name(total, heroes)}</b> — {total.adjusted:.1f}"]
        for g in sorted(total.games, key=lambda g: g.match_index):
            outcome = f"победа ×{WIN_BONUS:.1f}" if g.won else "поражение"
            lines.append(
                f"#{g.match_index} {_hero(heroes, g.hero_id)} · {_position(g)} {POSITION_NAMES[g.position]} · "
                f"{outcome} → <b>{g.score:.1f}</b>"
            )
            weights = POSITION_WEIGHTS[g.position]
            parts = [
                f"{COMPONENT_NAMES[c]} {g.components[c]:.0f} ({weights[c]}%)"
                for c in sorted(weights, key=lambda c: -weights[c])
                if weights[c] > 0 and c in g.components
            ]
            lines.append("   " + " · ".join(parts))
    return _fit("\n".join(lines))


def _fit(text: str) -> str:
    if len(text) <= MAX_MESSAGE:
        return text
    cut = text[:MAX_MESSAGE].rsplit("\n", 1)[0]
    return cut + "\n…"
