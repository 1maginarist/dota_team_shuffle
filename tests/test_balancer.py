import itertools
import random

import pytest

from bot.balancer import Lineup, Player, Weights, balance, pick_players


def make(pid, mmr, *roles):
    return Player(id=pid, name=f"p{pid}", mmr=mmr, roles=tuple(roles))


def all_slots(lineup):
    return [slot for team in lineup.teams for slot in team.slots]


def test_everyone_gets_main_role_when_possible():
    # По два игрока на каждую позицию — идеальный случай.
    players = [make(i, 3000 + i * 100, (i % 5) + 1) for i in range(10)]
    lineup = balance(players, rng=random.Random(1))
    assert lineup.role_penalty == 0
    assert all(slot.rank == 0 for slot in all_slots(lineup))
    # Позиции внутри команды уникальны и заняты все 5.
    for team in lineup.teams:
        assert sorted(s.position for s in team.slots if s.player) == [1, 2, 3, 4, 5]


def test_finds_optimal_mmr_split():
    rng = random.Random(42)
    # Роли все одинаково «любые», так что важен только ММР.
    players = [make(i, rng.randint(1000, 8000), 1, 2, 3, 4, 5) for i in range(10)]
    weights = Weights(rank_penalties=(0, 0, 0, 0, 0), off_role_penalty=0)
    lineup = balance(players, weights, rng=random.Random(0))

    total = sum(p.mmr for p in players)
    best = min(
        abs(total - 2 * sum(players[i].mmr for i in combo))
        for combo in itertools.combinations(range(10), 5)
    )
    assert lineup.mmr_diff == best
    assert abs(lineup.teams[0].total_mmr - lineup.teams[1].total_mmr) == best


def test_prefers_roles_over_small_mmr_gain():
    # Разницу 0 можно получить, только поставив кого-то не на его роль.
    # Бот должен предпочесть все роли на месте ценой 100 ММР разницы.
    players = [
        make(1, 3000, 1), make(2, 3100, 1),
        make(3, 5000, 2), make(4, 5000, 2),
        make(5, 3000, 3), make(6, 3000, 3),
        make(7, 3000, 4), make(8, 3000, 4),
        make(9, 3000, 5), make(10, 3000, 5),
    ]
    lineup = balance(players, rng=random.Random(3))
    assert lineup.role_penalty == 0
    assert lineup.mmr_diff == 100
    for team in lineup.teams:
        assert sorted(team.position_of(pid) for pid in (3, 4) if team.position_of(pid)) == [2]


def test_odd_number_gives_5_vs_4_with_empty_slot():
    players = [make(i, 4000, 1, 2, 3, 4, 5) for i in range(9)]
    lineup = balance(players, rng=random.Random(5))
    sizes = sorted(len(team.players) for team in lineup.teams)
    assert sizes == [4, 5]
    small = min(lineup.teams, key=lambda t: len(t.players))
    assert sum(1 for s in small.slots if s.player is None) == 1


def test_small_games():
    lineup = balance([make(1, 3000, 2), make(2, 3500, 2)], rng=random.Random(0))
    assert [len(t.players) for t in lineup.teams] == [1, 1]
    assert all(slot.rank == 0 for slot in all_slots(lineup) if slot.player)


def test_reshuffle_gives_different_split():
    players = [make(i, 3000 + i * 37, (i % 5) + 1) for i in range(10)]
    first = balance(players, rng=random.Random(0))
    second = balance(players, exclude=[first.key], tolerance=300, rng=random.Random(0))
    assert second.key != first.key


def test_all_seen_falls_back_to_something():
    players = [make(1, 3000, 1), make(2, 3000, 1)]
    first = balance(players)
    again = balance(players, exclude=[first.key])
    assert again.key == first.key  # других вариантов нет — возвращаем тот же


def test_validation():
    with pytest.raises(ValueError):
        balance([make(1, 3000, 1)])
    with pytest.raises(ValueError):
        balance([make(i, 3000, 1) for i in range(11)])
    with pytest.raises(ValueError):
        balance([make(1, 3000, 1), make(1, 3000, 1)])


def test_pick_players_bench_rotation():
    players = [make(i, 3000, 1) for i in range(12)]
    # Без долгов: играют первые 10 отмеченных.
    playing, bench = pick_players(players, {})
    assert [p.id for p in bench] == [10, 11]
    # Кто сидел в прошлый раз — играет в приоритете.
    playing, bench = pick_players(players, {10: 1, 11: 1})
    assert {10, 11} <= {p.id for p in playing}
    assert [p.id for p in bench] == [8, 9]


def test_serialization_roundtrip():
    players = [make(i, 3000 + i * 50, (i % 5) + 1, ((i + 1) % 5) + 1) for i in range(9)]
    lineup = balance(players, rng=random.Random(7))
    restored = Lineup.from_dict(lineup.to_dict())
    assert restored == lineup
    for p in players:
        assert restored.find(p.id) == lineup.find(p.id) is not None
