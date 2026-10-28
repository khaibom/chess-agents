import asyncio
import time

import chess
import chess.engine
import pytest
from conftest import record

from chess_agents.engine import (
    Engine,
    construct_candidates,
    legal_line,
    move_loss,
    problem_evidence,
)
from chess_agents.games import evidence_tree, select_games
from chess_agents.models import Line, Profile


class FakeEngine:
    def __init__(self, reject=None):
        self.reject = reject
        self.cache = {}

    async def analyze(self, board, move=None):
        return {"best_move": next(iter(board.legal_moves)).uci(), "cp_white": 0, "mate_white": None}

    async def check(self, board, move):
        return {
            "accepted": move.uci() != self.reject,
            "loss_cp": 200 if move.uci() == self.reject else 0,
            "losing_mate": False,
            "move": move.uci(),
            "root": {},
            "played": {},
        }


def profile(color="white"):
    return Profile(
        id="profile",
        username="hero",
        cutoff=200,
        status="confirmed",
        lines=[
            Line(id="line", color=color, moves=["d2d4", "d7d5", "c2c4", "e7e6"], choice="familiar")
        ],
    )


def test_scores_and_legality():
    assert move_loss(
        {"cp_white": 80, "mate_white": None}, {"cp_white": 10, "mate_white": None}, chess.WHITE
    ) == (70, False)
    assert move_loss(
        {"cp_white": -80, "mate_white": None}, {"cp_white": -10, "mate_white": None}, chess.BLACK
    ) == (70, False)
    assert move_loss(
        {"cp_white": 0, "mate_white": None}, {"cp_white": None, "mate_white": -3}, chess.WHITE
    ) == (None, True)
    assert not legal_line(["e2e5", "e7e5"])


@pytest.mark.parametrize("color", ["white", "black"])
async def test_all_user_moves_checked_and_sparse_not_weakness(color):
    games, _ = select_games(
        [record(color="black" if color == "white" else "white")],
        "opponent",
        "black" if color == "white" else "white",
        200,
        0,
    )
    tree = evidence_tree(games)
    candidates = await construct_candidates(profile(color), tree, color, FakeEngine())
    assert candidates and candidates[0]["familiar"]
    assert await problem_evidence(tree, color, FakeEngine("d7d5")) == {}
    assert (
        await construct_candidates(
            profile(color), tree, color, FakeEngine("d2d4" if color == "white" else "d7d5")
        )
        == []
    )


async def test_engine_cleanup_on_cancellation():
    class Transport:
        killed = False
        closed = False

        def get_returncode(self):
            return None

        def kill(self):
            self.killed = True

        def close(self):
            self.closed = True

    class Protocol:
        async def quit(self):
            raise chess.engine.EngineError("crashed")

    engine = Engine("missing", time.monotonic() + 5)
    engine.transport, engine.protocol = Transport(), Protocol()
    await engine.close()
    assert engine.transport.killed and engine.transport.closed


async def test_engine_budget_and_deadline():
    engine = Engine("missing", time.monotonic() - 1)
    with pytest.raises(TimeoutError):
        await engine.analyze(chess.Board())
    with pytest.raises((OSError, TimeoutError)):
        async with Engine("/no/such/engine", time.monotonic() + 1):
            await asyncio.sleep(0)
