import chess
import pytest
from conftest import record

from chess_agents.engine import Engine, problem_evidence
from chess_agents.games import evidence_tree, select_games


class ScoreEngine(Engine):
    def __init__(self, scenario):
        self.scenario = scenario

    async def analyze(self, board, move=None):
        direction = 1 if board.turn else -1
        if self.scenario == "already_lost":
            return {"cp_white": None, "mate_white": -3 * direction}
        if self.scenario == "allows_mate":
            return {
                "cp_white": 0 if move is None else None,
                "mate_white": None if move is None else -3 * direction,
            }
        return {
            "cp_white": None if move is None else 0,
            "mate_white": 3 * direction if move is None else None,
        }


@pytest.mark.parametrize("mover", ["white", "black"])
@pytest.mark.parametrize("scenario", ["already_lost", "allows_mate", "loses_winning_mate"])
async def test_repeated_problem_requires_move_caused_mate_change(mover, scenario):
    records = [record(index, color=mover) for index in range(1, 6)]
    games, _ = select_games(records, "opponent", mover, 200, 0)
    engine = ScoreEngine(scenario)
    problems = await problem_evidence(
        evidence_tree(games), "black" if mover == "white" else "white", engine
    )
    assert bool(problems) is (scenario != "already_lost")
    board = chess.Board()
    if mover == "black":
        board.push_uci("d2d4")
    check = await engine.check(board, next(iter(board.legal_moves)))
    if scenario == "already_lost":
        assert check["losing_mate"] and not check["mate_transition"]
        assert not check["accepted"]
    elif scenario == "allows_mate":
        assert check["mate_transition"] and not check["accepted"]
    else:
        assert check["loss_cp"] == 100 and not check["accepted"]
