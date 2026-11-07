import bz2
import gzip
import json
from pathlib import Path

import pytest
import zstandard

from bot import replay
from bot.mvp import compute_mvp

FIXTURES = Path(__file__).parent / "fixtures" / "replay"
MATCH_ID = 9073589444


def events():
    with gzip.open(FIXTURES / f"{MATCH_ID}.events.jsonl.gz", "rt") as f:
        return [json.loads(line) for line in f]


@pytest.fixture
def match():
    return replay.events_to_match(events())


def test_stats_match_opendota(match):
    """Выжимка настоящего реплея: цифры совпадают с тем, что OpenDota посчитала по этому же матчу."""
    expected = {p["player_slot"]: p for p in json.loads((FIXTURES / f"{MATCH_ID}.opendota.json").read_text())}
    assert match["match_id"] == MATCH_ID and match["radiant_win"] is False
    assert match["duration"] == 2774
    for p in match["players"]:
        want = expected[p["player_slot"]]
        for key in ("account_id", "hero_id", "kills", "deaths", "assists", "obs_placed", "sen_placed",
                    "camps_stacked", "position_est"):
            assert p[key] == want[key], (p["player_slot"], key)
        assert p["stuns"] == pytest.approx(want["stuns"], rel=0.01)
        assert p["teamfight_participation"] == pytest.approx(want["teamfight_participation"], abs=0.01)
        assert abs(p["observer_kills"] - want["observer_kills"]) <= 1


def test_positions_are_one_to_five_per_team(match):
    for radiant in (True, False):
        team = [p for p in match["players"] if p["isRadiant"] == radiant]
        assert sorted(p["position_est"] for p in team) == [1, 2, 3, 4, 5]


def test_replay_match_scores(match):
    result = compute_mvp([match])
    games = result.matches[0].games
    assert len(result.ranking) == 10 and not any(g.position_guessed for g in games)
    # Поля для фэнтези-очков есть в разобранном реплее
    assert sum(g.fantasy["objectives"] for g in games) > 0
    assert sum(g.fantasy["runes"] for g in games) > 0
    assert sum(g.fantasy["first_blood"] for g in games) == 4


def test_find_and_decompress(tmp_path):
    raw = b"PBDEMS2\x00" + b"x" * 1000
    (tmp_path / f"{MATCH_ID}.dem.bz2").write_bytes(zstandard.ZstdCompressor().compress(raw))  # так отдаёт Valve
    (tmp_path / "123.dem.bz2").write_bytes(bz2.compress(raw))
    (tmp_path / "456_999.dem").write_bytes(raw)
    (tmp_path / "789.dem").write_bytes(b"not a replay")

    zst = replay.find_replay(tmp_path, MATCH_ID)
    assert zst is not None and replay.decompress(zst, tmp_path / "a.dem").read_bytes() == raw
    assert replay.decompress(replay.find_replay(tmp_path, 123), tmp_path / "b.dem").read_bytes() == raw
    plain = replay.find_replay(tmp_path, 456)
    assert replay.decompress(plain, tmp_path / "c.dem") == plain  # уже распакован
    assert replay.find_replay(tmp_path, 111) is None
    with pytest.raises(replay.ReplayError):
        replay.decompress(replay.find_replay(tmp_path, 789), tmp_path / "d.dem")


def test_broken_replay_is_reported():
    with pytest.raises(replay.ReplayError):
        replay.events_to_match([e for e in events() if e["type"] != "epilogue"])
