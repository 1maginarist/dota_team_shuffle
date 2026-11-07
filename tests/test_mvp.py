import copy
import json
from pathlib import Path
from statistics import mean

import pytest

from bot import mvp_texts
from bot.mvp import FANTASY_ITEMS, SHRINK_GAMES, WIN_BONUS, Z_CLIP, _normal_cdf, compute_mvp, extract_match

FIXTURES = Path(__file__).parent / "fixtures" / "opendota"
# 9077341933 и 9077497263 — одни и те же 10 игроков, 9077267013 — другие
IDS = (9077341933, 9077497263, 9077267013)


def load(match_id):
    return json.loads((FIXTURES / f"{match_id}.json").read_text())


@pytest.fixture
def matches():
    return [load(i) for i in IDS]


def all_games(result):
    return [g for m in result.matches for g in m.games]


def test_fantasy_points_follow_valve_table(matches):
    info = extract_match(matches[0], 1)
    for g, p in zip(info.games, matches[0]["players"]):
        assert set(g.fantasy) == set(FANTASY_ITEMS)
        assert g.fantasy["kills"] == pytest.approx(0.3 * p["kills"])
        assert g.fantasy["deaths"] == pytest.approx(max(0, 3 - 0.3 * p["deaths"]))
        assert g.fantasy["wards"] == pytest.approx(0.5 * p["obs_placed"])
        assert g.fantasy["stacks"] == pytest.approx(0.5 * p["camps_stacked"])
        assert g.fantasy["stuns"] == pytest.approx(0.05 * p["stuns"], abs=1e-3)
        assert g.points == pytest.approx(sum(g.fantasy.values()))


def test_deaths_never_go_negative():
    player = {"kills": 0, "deaths": 25}
    assert FANTASY_ITEMS["deaths"][1](player) == 0


def test_every_position_has_same_average(matches):
    result = compute_mvp(matches)
    for pos in range(1, 6):
        scores = [g.base_score for g in all_games(result) if g.position == pos]
        assert len(scores) == 6
        assert abs(mean(scores) - 50) < 8, (pos, mean(scores))


def test_more_points_within_position_means_higher_score(matches):
    result = compute_mvp(matches)
    for pos in range(1, 6):
        group = sorted((g for g in all_games(result) if g.position == pos), key=lambda g: g.points)
        assert [g.base_score for g in group] == sorted(g.base_score for g in group)


def test_players_are_merged_across_matches(matches):
    result = compute_mvp(matches)
    assert sorted(len(t.games) for t in result.ranking) == [1] * 10 + [2] * 10
    assert result.anonymous_count == 0


def test_win_bonus_and_final_formula(matches):
    result = compute_mvp(matches)
    for g in all_games(result):
        assert g.score == pytest.approx(g.base_score * (WIN_BONUS if g.won else 1.0))
    for total in result.ranking:
        scores = [g.score for g in total.games]
        expected = (sum(scores) + SHRINK_GAMES * result.lobby_mean) / (len(scores) + SHRINK_GAMES)
        assert total.adjusted == pytest.approx(expected)
    assert result.ranking == sorted(result.ranking, key=lambda t: -t.adjusted)


def test_one_lucky_game_never_beats_three_strong_ones():
    best_possible = 100 * _normal_cdf(Z_CLIP) * WIN_BONUS
    # Средний балл лобби по построению ≈ 52.5, на реальных матчах 52–54.
    for lobby_mean in (50, 52.5, 54):
        one = (best_possible + SHRINK_GAMES * lobby_mean) / (1 + SHRINK_GAMES)
        three = (3 * 85 + SHRINK_GAMES * lobby_mean) / (3 + SHRINK_GAMES)
        assert one < three


def test_positions_from_opendota(matches):
    info = extract_match(matches[0], 1)
    for radiant in (True, False):
        team = [g for g, p in zip(info.games, matches[0]["players"]) if p["isRadiant"] == radiant]
        assert sorted(g.position for g in team) == [1, 2, 3, 4, 5]
    assert not any(g.position_guessed for g in info.games)


def test_unparsed_match_still_works(matches):
    raw = copy.deepcopy(matches[2])
    raw["version"] = None
    for p in raw["players"]:
        for key in ("position_est", "obs_placed", "camps_stacked", "stuns", "teamfight_participation"):
            p[key] = None
    games = all_games(compute_mvp([raw]))
    assert all(g.position_guessed for g in games)
    assert all(g.fantasy["wards"] == 0 for g in games)
    for radiant in (True, False):
        team = [g for g, p in zip(games, raw["players"]) if p["isRadiant"] == radiant]
        assert sorted(g.position for g in team) == [1, 2, 3, 4, 5]


def test_anonymous_players_are_not_merged(matches):
    first, second = copy.deepcopy(matches[0]), copy.deepcopy(matches[1])
    first["players"][0]["account_id"] = None
    second["players"][0]["account_id"] = 4294967295  # так Valve помечает скрытый профиль
    result = compute_mvp([first, second])
    assert result.anonymous_count == 2
    anon = [t for t in result.ranking if t.account_id is None]
    assert len(anon) == 2 and all(len(t.games) == 1 for t in anon)


def test_unfinished_match_is_rejected(matches):
    raw = copy.deepcopy(matches[0])
    raw["duration"] = 0
    with pytest.raises(ValueError):
        compute_mvp([raw])


def test_texts_render(matches):
    result = compute_mvp(matches)
    heroes = {g.hero_id: f"Hero{g.hero_id}" for g in all_games(result)}
    summary = mvp_texts.summary(result, heroes, ["тестовое предупреждение"])
    assert "MVP вечера" in summary and "очк." in summary and "тестовое предупреждение" in summary
    assert mvp_texts.player_name(result.mvp, heroes) in summary
    assert len(summary) <= 4096
    details = mvp_texts.breakdown(result, heroes)
    assert "Откуда очки" in details and "Убийства" in details and len(details) <= 4096
    assert "0.3 за убийство" in mvp_texts.HELP
