"""Клиент OpenDota: загрузка матчей и разбор реплеев.

У Dotabuff нет открытого API, поэтому данные берём из OpenDota — там те же
матчи, плюс разобранные реплеи (варды, стаки, оглушения, позиции).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp

from .mvp import is_parsed

log = logging.getLogger(__name__)

API = "https://api.opendota.com/api"
USER_AGENT = "dota-team-shuffle-bot/1.0 (+https://github.com/1maginarist/dota_team_shuffle)"


class OpenDotaError(Exception):
    pass


class OpenDota:
    def __init__(self, api_key: str | None = None, base_url: str = API) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._heroes: dict[int, str] | None = None
        self._benchmarks: dict[int, dict[str, list]] = {}

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=60),
                trust_env=True,  # учитывать HTTPS_PROXY, если он задан на сервере
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def _call(self, method: str, path: str) -> tuple[int, Any]:
        params = {"api_key": self.api_key} if self.api_key else None
        session = await self._http()
        for attempt in range(3):
            try:
                async with session.request(method, f"{self.base_url}{path}", params=params) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        raise OpenDotaError(f"OpenDota ответил {resp.status}")
                    if resp.status == 404:
                        return 404, None
                    if resp.status >= 400:
                        raise OpenDotaError(f"OpenDota ответил {resp.status}: {(await resp.text())[:200]}")
                    return resp.status, await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, OpenDotaError) as exc:
                if attempt == 2:
                    raise OpenDotaError(str(exc) or exc.__class__.__name__) from exc
                await asyncio.sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")

    async def match(self, match_id: int) -> dict[str, Any] | None:
        status, data = await self._call("GET", f"/matches/{match_id}")
        if status == 404 or not data or "players" not in data:
            return None
        return data

    async def request_parse(self, match_id: int) -> int | None:
        _, data = await self._call("POST", f"/request/{match_id}")
        return ((data or {}).get("job") or {}).get("jobId")

    async def job_finished(self, job_id: int) -> bool:
        """OpenDota возвращает null, когда задачи разбора больше нет в очереди."""
        _, data = await self._call("GET", f"/request/{job_id}")
        return not data

    async def benchmarks(self, hero_id: int) -> dict[str, list]:
        """Таблицы перцентилей OpenDota по герою (для реплеев, которых нет в OpenDota)."""
        if hero_id not in self._benchmarks:
            _, data = await self._call("GET", f"/benchmarks?hero_id={hero_id}")
            self._benchmarks[hero_id] = (data or {}).get("result") or {}
        return self._benchmarks[hero_id]

    async def heroes(self) -> dict[int, str]:
        if self._heroes is None:
            _, data = await self._call("GET", "/heroes")
            self._heroes = {int(h["id"]): h["localized_name"] for h in data or []}
        return self._heroes


@dataclass
class MatchLoad:
    match_id: int
    data: dict[str, Any] | None = None
    job_id: int | None = None
    status: str = "загружаю"
    error: str | None = None

    @property
    def parsed(self) -> bool:
        return self.data is not None and is_parsed(self.data)


@dataclass
class LoadResult:
    matches: list[dict[str, Any]]  # в порядке, в котором их прислали
    warnings: list[str] = field(default_factory=list)


Progress = Callable[[list[MatchLoad], int], Awaitable[None]]


async def load_matches(
    client: OpenDota,
    match_ids: list[int],
    progress: Progress | None = None,
    parse_timeout: float = 300,
    poll_interval: float = 10,
) -> LoadResult:
    """Загрузить матчи; неразобранные — отправить на разбор и подождать."""
    loads = [MatchLoad(mid) for mid in match_ids]
    started = time.monotonic()

    async def report() -> None:
        if progress is not None:
            await progress(loads, int(time.monotonic() - started))

    await report()
    for item in loads:
        try:
            item.data = await client.match(item.match_id)
        except OpenDotaError as exc:
            item.error = str(exc)
            item.status = "ошибка OpenDota"
            continue
        if item.parsed:
            item.status = "готово"
            continue
        # Нет в OpenDota или нет разбора — просим разобрать реплей.
        try:
            item.job_id = await client.request_parse(item.match_id)
            item.status = "разбираю реплей"
        except OpenDotaError as exc:
            item.status = "без разбора" if item.data else "не найден"
            item.error = str(exc)
    await report()

    while any(i.job_id for i in loads) and time.monotonic() - started < parse_timeout:
        await asyncio.sleep(poll_interval)
        for item in loads:
            if item.job_id is None:
                continue
            try:
                finished = await client.job_finished(item.job_id)
                if not finished:
                    continue
                item.job_id = None
                fresh = await client.match(item.match_id)
                if fresh is not None and not is_parsed(fresh):
                    await asyncio.sleep(3)  # даём OpenDota записать результат
                    fresh = await client.match(item.match_id) or fresh
                item.data = fresh or item.data
                item.status = "готово" if item.parsed else ("без разбора" if item.data else "не найден")
            except OpenDotaError as exc:
                log.warning("OpenDota: матч %s: %s", item.match_id, exc)
        await report()

    for item in loads:
        if item.job_id is not None:  # не дождались
            item.job_id = None
            try:
                item.data = await client.match(item.match_id) or item.data
            except OpenDotaError:
                pass
            item.status = "готово" if item.parsed else ("без разбора" if item.data else "не найден")
    await report()

    result = LoadResult(matches=[i.data for i in loads if i.data is not None])
    for item in loads:
        if item.data is None and item.status == "ошибка OpenDota":
            result.warnings.append(f"Матч {item.match_id}: не удалось загрузить из OpenDota ({item.error}).")
        elif item.data is None:
            result.warnings.append(
                f"Матч {item.match_id} не найден в OpenDota — проверь номер. "
                "Если он только что закончился, подожди пару минут и попробуй снова."
            )
        elif not item.parsed:
            result.warnings.append(
                f"Матч {item.match_id} посчитан без разбора реплея: нет вардов, стаков и оглушений, "
                "позиции определены по богатству. Попробуй пересчитать через несколько минут."
            )
    return result
