"""Real-engine SIGTERM smoke, no network or paid work."""

import asyncio
import signal
import time
from pathlib import Path

import chess

from chess_agents.engine import Engine


async def main() -> None:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopped.set)
    async with Engine("/usr/games/stockfish", time.monotonic() + 90) as engine:
        result = await engine.analyze(chess.Board())
        assert result["best_move"]
        print("Engine ready; awaiting SIGTERM.", flush=True)
        await stopped.wait()
    assert engine.transport.get_returncode() is not None
    Path("/data/signal-check.txt").write_text(
        "Stockfish reaped after SIGTERM; returncode=" + str(engine.transport.get_returncode()),
        encoding="utf-8",
    )
    print("SIGTERM: Stockfish stopped and reaped.", flush=True)


asyncio.run(main())
