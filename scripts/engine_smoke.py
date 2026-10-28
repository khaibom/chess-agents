"""Unpaid real-engine smoke; run with the installed application environment."""

import asyncio
import json
import os
import time

import chess

from chess_agents.engine import Engine


async def main() -> None:
    async with Engine(
        os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish"), time.monotonic() + 10
    ) as engine:
        result = await engine.analyze(chess.Board())
        assert chess.Move.from_uci(result["best_move"]) in chess.Board().legal_moves
        assert result["depth"] > 0
        print(json.dumps(result))
    assert engine.transport.get_returncode() is not None


asyncio.run(main())
