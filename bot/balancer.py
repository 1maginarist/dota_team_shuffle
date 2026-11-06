"""Балансировка команд Dota 2.

Модуль не зависит от Telegram: на вход список игроков (ММР + роли по
приоритету), на выход два состава с позициями 1–5.

Как считается «хороший» вариант
-------------------------------
Для каждого разбиения игроков на две команды мы:
  1. внутри каждой команды подбираем раскладку по позициям с минимальным
     штрафом за роли (перебор всех перестановок — их максимум 120);
  2. считаем стоимость = |ΣММР₁ − ΣММР₂| + штраф за роли обеих команд.

Штраф за роль задаётся в «ММР-эквиваленте»: например, штраф 100 за вторую
по приоритету роль значит, что ради того, чтобы человек играл любимую роль,
мы готовы потерпеть лишние 100 ММР разницы между командами.

Для 10 игроков всего 126 разбиений, так что перебор полный и мгновенный —
находим точный оптимум, а не приближение.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from typing import Iterable, Sequence

POSITIONS: tuple[int, ...] = (1, 2, 3, 4, 5)
TEAM_SIZE = 5
MAX_PLAYERS = TEAM_SIZE * 2
MIN_PLAYERS = 2

POSITION_NAMES: dict[int, str] = {
    1: "Керри",
    2: "Мид",
    3: "Оффлейн",
    4: "Семи-сап",
    5: "Фулл-сап",
}


@dataclass(frozen=True)
class Player:
    id: int
    name: str
    mmr: int
    # Позиции в порядке приоритета: (2, 3, 5) — «больше всего хочу мид,
    # потом оффлейн, потом пятёрку». Не перечисленные — «не хочу».
    roles: tuple[int, ...]

    def rank_of(self, position: int) -> int | None:
        """0 — основная роль, 1 — вторая и т.д.; None — роль не из списка."""
        try:
            return self.roles.index(position)
        except ValueError:
            return None


@dataclass(frozen=True)
class Weights:
    # Штраф за роль с индексом i в списке игрока (0 — самая любимая).
    rank_penalties: tuple[int, ...] = (0, 100, 250, 400, 600)
    # Штраф, если игрок встал на роль, которую не выбирал.
    off_role_penalty: int = 1500

    def penalty(self, player: Player, position: int) -> int:
        rank = player.rank_of(position)
        if rank is None:
            return self.off_role_penalty
        return self.rank_penalties[min(rank, len(self.rank_penalties) - 1)]


@dataclass(frozen=True)
class Slot:
    position: int
    player: Player | None  # None — пустая позиция (команда из 4 человек)

    @property
    def rank(self) -> int | None:
        return None if self.player is None else self.player.rank_of(self.position)


@dataclass(frozen=True)
class Team:
    slots: tuple[Slot, ...]  # всегда 5 слотов, отсортированы по позиции

    @property
    def players(self) -> list[Player]:
        return [s.player for s in self.slots if s.player is not None]

    @property
    def total_mmr(self) -> int:
        return sum(p.mmr for p in self.players)

    @property
    def avg_mmr(self) -> int:
        players = self.players
        return round(self.total_mmr / len(players)) if players else 0

    def position_of(self, player_id: int) -> int | None:
        for slot in self.slots:
            if slot.player is not None and slot.player.id == player_id:
                return slot.position
        return None


@dataclass(frozen=True)
class Lineup:
    teams: tuple[Team, Team]  # (Свет, Тьма)
    mmr_diff: int
    role_penalty: int
    cost: int
    # Ключ разбиения (без учёта ролей) — чтобы «Другой вариант» не повторялся.
    key: str

    def find(self, player_id: int) -> tuple[int, int] | None:
        """(индекс команды, позиция) игрока или None."""
        for team_index, team in enumerate(self.teams):
            position = team.position_of(player_id)
            if position is not None:
                return team_index, position
        return None

    # --- сериализация для хранения в БД ---------------------------------

    def to_dict(self) -> dict:
        return {
            "teams": [
                [
                    [
                        slot.position,
                        None
                        if slot.player is None
                        else {
                            "id": slot.player.id,
                            "name": slot.player.name,
                            "mmr": slot.player.mmr,
                            "roles": list(slot.player.roles),
                        },
                    ]
                    for slot in team.slots
                ]
                for team in self.teams
            ],
            "mmr_diff": self.mmr_diff,
            "role_penalty": self.role_penalty,
            "cost": self.cost,
            "key": self.key,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Lineup":
        teams = []
        for raw_team in data["teams"]:
            slots = []
            for position, raw_player in raw_team:
                player = None
                if raw_player is not None:
                    player = Player(
                        id=raw_player["id"],
                        name=raw_player["name"],
                        mmr=raw_player["mmr"],
                        roles=tuple(raw_player["roles"]),
                    )
                slots.append(Slot(position=position, player=player))
            teams.append(Team(slots=tuple(slots)))
        return cls(
            teams=(teams[0], teams[1]),
            mmr_diff=data["mmr_diff"],
            role_penalty=data["role_penalty"],
            cost=data["cost"],
            key=data["key"],
        )


@dataclass
class _Candidate:
    cost: int
    mmr_diff: int
    role_penalty: int
    team_a: tuple[int, ...]
    perm_a: tuple[int, ...]
    team_b: tuple[int, ...]
    perm_b: tuple[int, ...]
    key: str = field(default="")


def split_key(ids_a: Iterable[int], ids_b: Iterable[int]) -> str:
    """Канонический ключ разбиения: состав команды, где игрок с минимальным id."""
    a, b = sorted(ids_a), sorted(ids_b)
    side = a if a and (not b or a[0] < b[0]) else b
    return ",".join(map(str, side))


def pick_players(
    candidates: Sequence[Player],
    bench_debt: dict[int, int],
    limit: int = MAX_PLAYERS,
) -> tuple[list[Player], list[Player]]:
    """Кто играет, а кто на скамейке, если выбрано больше 10 человек.

    Сначала играют те, кто дольше всех сидел на скамейке (bench_debt),
    при равенстве — в порядке, в котором их отметил организатор.
    """
    order = sorted(
        range(len(candidates)),
        key=lambda i: (-bench_debt.get(candidates[i].id, 0), i),
    )
    chosen = set(order[:limit])
    playing = [p for i, p in enumerate(candidates) if i in chosen]
    bench = [p for i, p in enumerate(candidates) if i not in chosen]
    return playing, bench


def balance(
    players: Sequence[Player],
    weights: Weights | None = None,
    *,
    exclude: Iterable[str] = (),
    tolerance: int = 0,
    rng: random.Random | None = None,
) -> Lineup:
    """Собрать два сбалансированных состава.

    exclude   — ключи разбиений, которые уже показывали (для «Другой вариант»).
    tolerance — насколько вариант может быть хуже лучшего, чтобы попасть в
                случайный выбор. 0 — всегда берём оптимальный.
    """
    weights = weights or Weights()
    rng = rng or random.Random()
    n = len(players)
    if not MIN_PLAYERS <= n <= MAX_PLAYERS:
        raise ValueError(f"Нужно от {MIN_PLAYERS} до {MAX_PLAYERS} игроков, а не {n}")
    if len({p.id for p in players}) != n:
        raise ValueError("Игроки повторяются")

    # penalty[i][pos] — штраф игрока i на позиции pos
    penalty = [{pos: weights.penalty(p, pos) for pos in POSITIONS} for p in players]
    mmr = [p.mmr for p in players]

    assignments: dict[tuple[int, ...], tuple[int, tuple[int, ...]]] = {}

    def best_assignment(members: tuple[int, ...]) -> tuple[int, tuple[int, ...]]:
        if members not in assignments:
            perms = list(itertools.permutations(POSITIONS, len(members)))
            rng.shuffle(perms)  # случайный выбор среди равноценных раскладок
            best_pen, best_perm = None, None
            for perm in perms:
                pen = sum(penalty[i][pos] for i, pos in zip(members, perm))
                if best_pen is None or pen < best_pen:
                    best_pen, best_perm = pen, perm
            assignments[members] = (best_pen, best_perm)
        return assignments[members]

    size_a = n // 2
    everyone = range(n)
    if n % 2 == 0:
        # Фиксируем игрока 0 в команде A, чтобы не считать зеркальные варианты.
        splits = ((0, *rest) for rest in itertools.combinations(range(1, n), size_a - 1))
    else:
        # Команды разного размера, зеркал нет.
        splits = itertools.combinations(everyone, size_a)

    candidates: list[_Candidate] = []
    for team_a in splits:
        team_b = tuple(i for i in everyone if i not in team_a)
        pen_a, perm_a = best_assignment(team_a)
        pen_b, perm_b = best_assignment(team_b)
        diff = abs(sum(mmr[i] for i in team_a) - sum(mmr[i] for i in team_b))
        candidate = _Candidate(
            cost=diff + pen_a + pen_b,
            mmr_diff=diff,
            role_penalty=pen_a + pen_b,
            team_a=team_a,
            perm_a=perm_a,
            team_b=team_b,
            perm_b=perm_b,
        )
        candidate.key = split_key(
            (players[i].id for i in team_a), (players[i].id for i in team_b)
        )
        candidates.append(candidate)

    excluded = set(exclude)
    best_cost = min(c.cost for c in candidates)
    fresh = [c for c in candidates if c.key not in excluded] or candidates
    threshold = max(min(c.cost for c in fresh), best_cost + tolerance)
    chosen = rng.choice([c for c in fresh if c.cost <= threshold])

    def build_team(members: tuple[int, ...], perm: tuple[int, ...]) -> Team:
        by_position = {pos: players[i] for i, pos in zip(members, perm)}
        return Team(slots=tuple(Slot(pos, by_position.get(pos)) for pos in POSITIONS))

    teams = [build_team(chosen.team_a, chosen.perm_a), build_team(chosen.team_b, chosen.perm_b)]
    rng.shuffle(teams)  # кто за Свет, а кто за Тьму — случайно
    return Lineup(
        teams=(teams[0], teams[1]),
        mmr_diff=chosen.mmr_diff,
        role_penalty=chosen.role_penalty,
        cost=chosen.cost,
        key=chosen.key,
    )
