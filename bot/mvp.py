"""Выбор MVP по нескольким матчам Dota 2 — по фэнтези-очкам Valve.

Как считается балл игрока за матч
---------------------------------
1. Фэнтези-очки по классической таблице Valve с The International
   (FANTASY_ITEMS): убийства, смерти, фарм, башни и Рошан, участие в драках,
   варды, стаки, руны, первая кровь, оглушения.
2. Очки сравниваются только с игроками ТОЙ ЖЕ ПОЗИЦИИ за вечер — и по
   среднему, и по разбросу. Без этого MVP почти всегда доставался бы мидеру
   (на 34 реальных матчах — 16 раз из 34). Балл 0–100, 50 — средний игрок
   позиции.
3. Победа умножает балл матча на WIN_BONUS.

Итог вечера — средний балл с поправкой на число игр: к играм каждого
добавляется SHRINK_GAMES «виртуальной средней игры». Одна случайная
удачная игра не перевешивает три стабильно сильные.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Iterable

WIN_BONUS = 1.10
SHRINK_GAMES = 2.0
Z_CLIP = 2.5
# При нормировке разброса внутри позиции подмешиваем общий разброс как
# столько «виртуальных игр» — чтобы при 1 матче (2 игрока на позицию)
# не терялась информация о том, насколько один лучше другого.
SPREAD_PRIOR_GAMES = 1.0

# Классические фэнтези-очки Valve (The International).
# Ключ → (подпись, функция от статистики игрока в формате OpenDota)
FANTASY_ITEMS: dict[str, tuple[str, Any]] = {
    "kills": ("Убийства", lambda p: 0.3 * _num(p.get("kills"))),
    "deaths": ("Смерти", lambda p: max(0.0, 3 - 0.3 * _num(p.get("deaths")))),
    "farm": ("Фарм", lambda p: 0.003 * (_num(p.get("last_hits")) + _num(p.get("denies")))
             + 0.002 * _num(p.get("gold_per_min"))),
    "objectives": ("Башни/Рошан", lambda p: _num(p.get("towers_killed")) + _num(p.get("roshans_killed"))),
    "teamfight": ("Драки", lambda p: 3 * _num(p.get("teamfight_participation"))),
    "wards": ("Варды", lambda p: 0.5 * _num(p.get("obs_placed"))),
    "stacks": ("Стаки", lambda p: 0.5 * _num(p.get("camps_stacked"))),
    "runes": ("Руны", lambda p: 0.25 * _num(p.get("rune_pickups"))),
    "first_blood": ("Первая кровь", lambda p: 4 * _num(p.get("firstblood_claimed"))),
    "stuns": ("Оглушения", lambda p: 0.05 * _num(p.get("stuns"))),
}


@dataclass
class PlayerGame:
    match_id: int
    match_index: int  # 1, 2, 3 — в порядке, в котором прислали
    key: str  # кто это: account_id или уникальная метка анонима
    account_id: int | None
    name: str | None  # ник Steam (может не быть)
    hero_id: int
    position: int
    position_guessed: bool  # позиция определена грубо (матч без разбора реплея)
    won: bool
    fantasy: dict[str, float] = field(default_factory=dict)  # фэнтези-очки по статьям
    base_score: float = 50.0
    score: float = 50.0

    @property
    def anonymous(self) -> bool:
        return self.account_id is None

    @property
    def points(self) -> float:
        return sum(self.fantasy.values())


@dataclass
class MatchInfo:
    match_id: int
    index: int
    duration_min: float
    radiant_win: bool
    parsed: bool
    games: list[PlayerGame]

    @property
    def mvp(self) -> PlayerGame:
        return max(self.games, key=lambda g: g.score)


@dataclass
class PlayerTotal:
    key: str
    account_id: int | None
    name: str | None
    games: list[PlayerGame]
    average: float
    adjusted: float


@dataclass
class MvpResult:
    matches: list[MatchInfo]
    ranking: list[PlayerTotal]
    lobby_mean: float

    @property
    def mvp(self) -> PlayerTotal:
        return self.ranking[0]

    @property
    def anonymous_count(self) -> int:
        return sum(1 for m in self.matches for g in m.games if g.anonymous)


# --- извлечение данных из ответа OpenDota -------------------------------------


def is_parsed(match: dict[str, Any]) -> bool:
    """Матч разобран (есть варды, позиции и т.д.), а не только базовая статистика."""
    return match.get("version") is not None


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _guess_positions(players: list[dict[str, Any]]) -> dict[int, int]:
    """Без разбора реплея: позиция = место по net worth в своей команде."""
    result: dict[int, int] = {}
    for radiant in (True, False):
        team = [p for p in players if bool(p.get("isRadiant")) == radiant]
        team.sort(key=lambda p: -_num(p.get("net_worth") or p.get("gold_per_min")))
        for pos, p in enumerate(team, start=1):
            result[p["player_slot"]] = min(pos, 5)
    return result


def extract_match(match: dict[str, Any], index: int) -> MatchInfo:
    duration_min = _num(match.get("duration")) / 60
    if duration_min <= 0:
        raise ValueError(f"Матч {match.get('match_id')} без длительности — похоже, он не доигран")
    parsed = is_parsed(match)
    players = match["players"]
    radiant_win = bool(match.get("radiant_win"))
    estimated = {p["player_slot"]: p.get("position_est") for p in players}
    guessed = not parsed or any(v not in (1, 2, 3, 4, 5) for v in estimated.values())
    positions = _guess_positions(players) if guessed else estimated

    games = []
    for p in players:
        account_id = p.get("account_id")
        if account_id in (None, 0, 4294967295):
            account_id = None
        key = str(account_id) if account_id is not None else f"anon-{match['match_id']}-{p['player_slot']}"
        is_radiant = bool(p.get("isRadiant", p["player_slot"] < 128))
        games.append(
            PlayerGame(
                match_id=int(match["match_id"]),
                match_index=index,
                key=key,
                account_id=account_id,
                name=p.get("personaname"),
                hero_id=int(p.get("hero_id") or 0),
                position=int(positions[p["player_slot"]]),
                position_guessed=guessed,
                won=is_radiant == radiant_win,
                fantasy={key: round(fn(p), 3) for key, (_, fn) in FANTASY_ITEMS.items()},
            )
        )
    return MatchInfo(
        match_id=int(match["match_id"]),
        index=index,
        duration_min=duration_min,
        radiant_win=radiant_win,
        parsed=parsed,
        games=games,
    )


# --- подсчёт ------------------------------------------------------------------


def _normal_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _clip(z: float) -> float:
    return max(-Z_CLIP, min(Z_CLIP, z))


def score_games(games: Iterable[PlayerGame]) -> None:
    """Проставить баллы 0–100: фэнтези-очки, нормированные внутри позиции."""
    games = list(games)

    # Нормировка внутри позиции: одинаковые среднее и разброс у каждой позиции
    by_position: dict[int, list[PlayerGame]] = defaultdict(list)
    for g in games:
        by_position[g.position].append(g)
    centers = {pos: mean(x.points for x in group) for pos, group in by_position.items()}
    pooled_var = mean((g.points - centers[g.position]) ** 2 for g in games)
    for pos, group in by_position.items():
        n = len(group)
        own_var = mean((x.points - centers[pos]) ** 2 for x in group)
        spread = math.sqrt((n * own_var + SPREAD_PRIOR_GAMES * pooled_var) / (n + SPREAD_PRIOR_GAMES))
        for g in group:
            z = 0.0 if spread < 1e-9 else _clip((g.points - centers[pos]) / spread)
            g.base_score = 100 * _normal_cdf(z)
            g.score = g.base_score * (WIN_BONUS if g.won else 1.0)


def compute_mvp(raw_matches: list[dict[str, Any]]) -> MvpResult:
    if not raw_matches:
        raise ValueError("Нет матчей для подсчёта")
    matches = [extract_match(m, i) for i, m in enumerate(raw_matches, start=1)]
    all_games = [g for m in matches for g in m.games]
    score_games(all_games)
    lobby_mean = mean(g.score for g in all_games)

    by_player: dict[str, list[PlayerGame]] = defaultdict(list)
    for g in all_games:
        by_player[g.key].append(g)

    ranking = []
    for key, games in by_player.items():
        scores = [g.score for g in games]
        named = next((g.name for g in games if g.name), None)
        ranking.append(
            PlayerTotal(
                key=key,
                account_id=games[0].account_id,
                name=named,
                games=games,
                average=mean(scores),
                adjusted=(sum(scores) + SHRINK_GAMES * lobby_mean) / (len(scores) + SHRINK_GAMES),
            )
        )
    ranking.sort(key=lambda t: (-t.adjusted, -t.average, -len(t.games)))
    return MvpResult(matches=matches, ranking=ranking, lobby_mean=lobby_mean)
