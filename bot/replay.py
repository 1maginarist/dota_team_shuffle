"""Разбор реплея (.dem) через odota/parser — для лобби, которых нет в OpenDota.

Парсер (Docker-образ odota/parser) принимает .dem и возвращает поток событий
(JSON по строке). Здесь мы собираем из них статистику каждого игрока в том же
формате, что отдаёт OpenDota, — поэтому дальше работает тот же алгоритм MVP.
"""

from __future__ import annotations

import asyncio
import bz2
import json
import logging
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import aiohttp

log = logging.getLogger(__name__)

STEAM64_BASE = 76561197960265728
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
BZ2_MAGIC = b"BZh"
DEM_MAGIC = b"PBDEMS2"
REPLAY_SUFFIXES = (".dem", ".dem.bz2", ".dem.zst", ".bz2", ".zst")
LANE_PHASE = (60, 600)  # секунды игры, по которым определяем линию

# поле benchmarks OpenDota → как посчитать «сырое» значение
BENCHMARK_FIELDS = (
    "gold_per_min", "xp_per_min", "kills_per_min", "deaths_per_min", "assists_per_min",
    "last_hits_per_min", "hero_damage_per_min", "hero_healing_per_min", "tower_damage",
)


class ReplayError(Exception):
    pass


# --- файлы ------------------------------------------------------------------------


def find_replay(folder: Path, match_id: int) -> Path | None:
    for suffix in REPLAY_SUFFIXES:
        path = folder / f"{match_id}{suffix}"
        if path.is_file():
            return path
    for path in folder.glob(f"{match_id}*"):  # например, 8123456789_123456.dem.bz2
        if path.is_file() and path.name.endswith(REPLAY_SUFFIXES):
            return path
    return None


def decompress(path: Path, target: Path) -> Path:
    """Распаковать реплей. Valve отдаёт .dem.bz2, но внутри бывает и zstd."""
    with path.open("rb") as f:
        head = f.read(8)
    if head.startswith(DEM_MAGIC):
        return path
    if head.startswith(BZ2_MAGIC):
        with bz2.open(path, "rb") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        return target
    if head.startswith(ZSTD_MAGIC):
        import zstandard

        with path.open("rb") as src, target.open("wb") as dst:
            zstandard.ZstdDecompressor().copy_stream(src, dst)
        return target
    raise ReplayError(f"{path.name}: это не реплей Dota 2 (.dem / .dem.bz2)")


async def parse_with_parser(dem_path: Path, parser_url: str) -> dict[str, Any]:
    """Отправить .dem в odota/parser и собрать статистику из потока событий."""
    timeout = aiohttp.ClientTimeout(total=900)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Парсер начинает отвечать, не дочитав файл, а потоковая отправка файла
            # в aiohttp на этом зависает. Отправляем целиком (~130 МБ на время разбора).
            body = await asyncio.to_thread(dem_path.read_bytes)
            async with session.post(parser_url, data=body) as resp:
                if resp.status != 200:
                    raise ReplayError(f"Парсер ответил {resp.status}")
                aggregator = Aggregator()
                async for raw in resp.content:
                    if raw.strip():
                        aggregator.add(json.loads(raw))
    except aiohttp.ClientError as exc:
        raise ReplayError(f"Парсер реплеев недоступен ({exc}). Он запущен? См. README.") from exc
    return aggregator.result()


# --- сборка статистики ------------------------------------------------------------


def _text(value: Any) -> str:
    """В epilogue строки лежат как {"bytes": [...]}"""
    if isinstance(value, dict) and "bytes" in value:
        return bytes(b & 0xFF for b in value["bytes"]).decode("utf-8", "replace")
    return str(value or "")


class Aggregator:
    def __init__(self) -> None:
        self.last: dict[int, dict[str, Any]] = {}
        self.lane_points: dict[int, list[tuple[float, float]]] = defaultdict(list)
        self.epilogue: dict[str, Any] | None = None
        self.combat: list[dict[str, Any]] = []  # нужные события боевого лога, до разбора epilogue
        self.ward_kills: list[dict[str, Any]] = []

    def add(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "interval":
            slot = event.get("slot")
            if slot is None or event.get("hero_id") is None:
                return
            self.last[slot] = event
            if LANE_PHASE[0] <= event.get("time", 0) <= LANE_PHASE[1] and "x" in event:
                self.lane_points[slot].append((event["x"], event["y"]))
        elif kind in ("DOTA_COMBATLOG_HEAL", "DOTA_COMBATLOG_DAMAGE"):
            if event.get("attackerhero") and event.get("targethero"):
                self.combat.append({k: event.get(k) for k in ("type", "value", "attackername", "targetname",
                                                               "attackerillusion", "targetillusion")})
        elif kind in ("obs_left", "sen_left"):
            if event.get("attackername"):
                self.ward_kills.append(event)
        elif kind == "epilogue":
            self.epilogue = json.loads(event["key"])

    def result(self) -> dict[str, Any]:
        if self.epilogue is None or len(self.last) < 2:
            raise ReplayError("В реплее нет итогов матча — возможно, файл повреждён или матч не доигран")
        info = self.epilogue["gameInfo_"]["dota_"]
        players_info = info.get("playerInfo_", [])
        duration = max(e.get("time", 0) for e in self.last.values())
        if duration <= 0:
            raise ReplayError("Не удалось определить длительность матча")
        minutes = duration / 60

        hero_of: dict[int, str] = {}
        for slot in self.last:
            if slot < len(players_info):
                hero_of[slot] = _text(players_info[slot].get("heroName_"))
        slot_of_hero = {name: slot for slot, name in hero_of.items() if name}

        healing: dict[int, float] = defaultdict(float)
        taken: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for e in self.combat:
            attacker, target = slot_of_hero.get(e["attackername"]), slot_of_hero.get(e["targetname"])
            if e["type"] == "DOTA_COMBATLOG_HEAL":
                if attacker is not None and target is not None and attacker != target and not e["attackerillusion"]:
                    healing[attacker] += e["value"] or 0
            elif target is not None and not e["targetillusion"]:
                # как у OpenDota: урон от любых героев (и их иллюзий), но не по иллюзиям
                taken[target][e["attackername"]] += e["value"] or 0
        dewards = {"obs": defaultdict(int), "sen": defaultdict(int)}
        for e in self.ward_kills:
            killer = slot_of_hero.get(e.get("attackername"))
            if killer is not None and (killer < 5) != (e.get("slot", 0) < 5):
                dewards["obs" if e["type"] == "obs_left" else "sen"][killer] += 1

        radiant_win = info.get("gameWinner_") == 2
        players = []
        for slot in sorted(self.last):
            e = self.last[slot]
            pinfo = players_info[slot] if slot < len(players_info) else {}
            steamid = int(pinfo.get("steamid_") or 0)
            account_id = steamid - STEAM64_BASE if steamid > STEAM64_BASE else None
            is_radiant = slot < 5
            kills, deaths, assists = e.get("kills", 0), e.get("deaths", 0), e.get("assists", 0)
            raw = {
                "gold_per_min": e.get("gold", 0) / minutes,
                "xp_per_min": e.get("xp", 0) / minutes,
                "kills_per_min": kills / minutes,
                "deaths_per_min": deaths / minutes,
                "assists_per_min": assists / minutes,
                "last_hits_per_min": e.get("lh", 0) / minutes,
                "hero_damage_per_min": e.get("hero_damage", 0) / minutes,
                "hero_healing_per_min": healing[slot] / minutes,
                "tower_damage": e.get("tower_damage", 0),
            }
            players.append({
                "account_id": account_id,
                "personaname": _text(pinfo.get("playerName_")) or None,
                "hero_id": e["hero_id"],
                "isRadiant": is_radiant,
                "player_slot": slot if is_radiant else 128 + slot - 5,
                "win": int(is_radiant == radiant_win),
                "kills": kills, "deaths": deaths, "assists": assists,
                "last_hits": e.get("lh", 0), "denies": e.get("denies", 0),
                "net_worth": e.get("networth", 0),
                "gold_per_min": round(raw["gold_per_min"]),
                "xp_per_min": round(raw["xp_per_min"]),
                "hero_damage": e.get("hero_damage", 0),
                "hero_healing": round(healing[slot]),
                "tower_damage": e.get("tower_damage", 0),
                "obs_placed": e.get("obs_placed", 0),
                "sen_placed": e.get("sen_placed", 0),
                "observer_kills": dewards["obs"][slot],
                "sentry_kills": dewards["sen"][slot],
                "camps_stacked": e.get("camps_stacked", 0),
                "stuns": e.get("stuns", 0.0),
                "teamfight_participation": e.get("teamfight_participation", 0.0),
                "damage_taken": dict(taken[slot]),
                "benchmarks": {k: {"raw": v, "pct": None} for k, v in raw.items()},
                "_lane": self._lane(slot),
            })
        _estimate_positions(players)
        for p in players:
            p.pop("_lane")
        return {
            "match_id": int(info.get("matchId_") or 0),
            "duration": int(duration),
            "radiant_win": radiant_win,
            "version": "replay",  # данные из разобранного реплея
            "players": players,
        }

    def _lane(self, slot: int) -> str | None:
        """Линия в первые 10 минут: top / mid / bot по средней позиции героя."""
        points = self.lane_points.get(slot)
        if not points:
            return None
        x = sum(p[0] for p in points) / len(points)
        y = sum(p[1] for p in points) / len(points)
        diff = y - x  # на диагонали (мид) x ≈ y
        if abs(diff) < 18:
            return "mid"
        return "top" if diff > 0 else "bot"


def _estimate_positions(players: list[dict[str, Any]]) -> None:
    """Позиции 1–5: 3 самых богатых — коры (мид, лёгкая, сложная линия), 2 остальных — саппорты."""
    for radiant in (True, False):
        team = [p for p in players if p["isRadiant"] == radiant]
        team.sort(key=lambda p: -(p["net_worth"] or 0))
        cores, supports = team[:3], team[3:]
        safe, hard = ("bot", "top") if radiant else ("top", "bot")
        left = list(cores)

        def take(lane: str) -> dict[str, Any] | None:
            for p in left:
                if p["_lane"] == lane:
                    left.remove(p)
                    return p
            return None

        mid = take("mid")
        carry = take(safe)
        off = take(hard)
        # кто не нашёлся по линии — по богатству
        assigned = {2: mid, 1: carry, 3: off}
        for pos in (1, 2, 3):
            if assigned[pos] is None:
                assigned[pos] = left.pop(0)
        for pos, p in assigned.items():
            p["position_est"] = pos
        # Пятёрка — кто больше ставил вардов, четвёрка — второй саппорт
        supports.sort(key=lambda p: -((p["obs_placed"] or 0) + (p["sen_placed"] or 0)))
        for pos, p in zip((5, 4), supports):
            p["position_est"] = pos


# --- перцентили по героям (OpenDota /benchmarks) ------------------------------------


def percentile(table: list[dict[str, float]], value: float) -> float:
    """Доля игр на этом герое со значением ≤ value (линейная интерполяция по таблице)."""
    points = sorted((row["value"], row["percentile"]) for row in table)
    if not points:
        return 0.5
    if value <= points[0][0]:
        return points[0][1] * (value / points[0][0]) if points[0][0] > 0 else points[0][1]
    for (v1, p1), (v2, p2) in zip(points, points[1:]):
        if v1 <= value <= v2:
            return p1 if v2 == v1 else p1 + (p2 - p1) * (value - v1) / (v2 - v1)
    return points[-1][1]


def apply_benchmarks(match: dict[str, Any], tables: dict[int, dict[str, list]]) -> None:
    """Проставить pct как у OpenDota. Смерти инвертируем: меньше смертей — выше."""
    for p in match["players"]:
        table = tables.get(p["hero_id"]) or {}
        for key, bench in p["benchmarks"].items():
            rows = table.get(key)
            if not rows:
                continue
            pct = percentile(rows, bench["raw"])
            bench["pct"] = 1 - pct if key == "deaths_per_min" else pct


def events_to_match(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    aggregator = Aggregator()
    for event in events:
        aggregator.add(event)
    return aggregator.result()


async def load_local(match_id: int, folder: Path, parser_url: str, opendota) -> dict[str, Any] | None:
    """Матч из реплея, который лежит в folder. None — реплея нет.

    Разобранный результат кэшируется в folder/parsed/<id>.json, повторно не парсим.
    """
    path = find_replay(folder, match_id)
    if path is None:
        return None
    cache = folder / "parsed" / f"{match_id}.json"
    if cache.is_file() and cache.stat().st_mtime >= path.stat().st_mtime:
        match = json.loads(cache.read_text())
    else:
        tmp = folder / "parsed" / f"{match_id}.tmp.dem"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            dem = decompress(path, tmp)
            match = await parse_with_parser(dem, parser_url)
        finally:
            tmp.unlink(missing_ok=True)
        if match["match_id"] and match["match_id"] != match_id:
            raise ReplayError(f"В файле {path.name} матч {match['match_id']}, а не {match_id}")
        match["match_id"] = match_id
        cache.write_text(json.dumps(match))
    tables = {}
    for hero_id in {p["hero_id"] for p in match["players"]}:
        try:
            tables[hero_id] = await opendota.benchmarks(hero_id)
        except Exception as exc:  # без перцентилей метрики «по герою» просто не учтутся
            log.warning("Нет перцентилей для героя %s: %s", hero_id, exc)
    apply_benchmarks(match, tables)
    return match
