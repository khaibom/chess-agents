import asyncio
import json
import time

import httpx
from conftest import record

from chess_agents.games import Archives, select_games_async


async def test_large_normalization_respects_deadline_and_yields_partial_records():
    records = [record(index) for index in range(1, 5001)]
    heartbeats = []
    finished = asyncio.Event()

    async def heartbeat():
        while not finished.is_set():
            heartbeats.append(time.monotonic())
            await asyncio.sleep(0.001)

    task = asyncio.create_task(heartbeat())
    started = time.monotonic()
    games, excluded, unprocessed = await select_games_async(
        records, "opponent", "black", 200, 0, started + 0.02
    )
    finished.set()
    await task
    assert 0 < len(games) <= 200 and unprocessed > 0 and not excluded
    assert time.monotonic() - started < 0.08
    assert len(heartbeats) >= 2


async def test_live_archive_path_discloses_partial_normalization(store):
    cutoff = 1792972800
    records = [record(index, end=cutoff - 100) for index in range(1, 5001)]
    payload = json.dumps({"games": records}).encode()
    archive = "https://api.chess.com/pub/player/opponent/games/2026/10"

    def handler(request):
        if request.url.path.endswith("archives"):
            return httpx.Response(200, json={"archives": [archive]})
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        started = time.monotonic()
        api = Archives(store, "test", client, deadline=started + 0.05)
        games, coverage = await api.games("opponent", "black", cutoff)
    assert time.monotonic() - started < 0.12
    assert coverage["unprocessed_records"] > 0
    assert len(games) == coverage["count"]
    assert all(game["end_time"] < cutoff for game in games)
