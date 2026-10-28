import chess
import pytest
from conftest import record
from test_engine import FakeEngine, profile

from chess_agents.engine import construct_candidates, move_loss
from chess_agents.games import evidence_tree, select_games
from chess_agents.llm import InvalidOutput
from chess_agents.models import PrepDraft, ScoutDraft
from chess_agents.report import render, validate_prep, validate_scout


async def test_unfamiliar_is_never_a_familiar_main():
    own = profile()
    own.lines[0].moves = ["d2d4", "d7d5"]
    games, _ = select_games([record()], "opponent", "black", 200, 0)
    candidates = await construct_candidates(own, evidence_tree(games), "white", FakeEngine())
    unfamiliar = next(candidate for candidate in candidates if not candidate["familiar"])
    draft = PrepDraft(
        main_id=unfamiliar["id"],
        deviation_ids=[],
        unfamiliar_id=None,
        remembered_id=None,
        tip_evidence_ids=[],
        explanation="",
    )
    with pytest.raises(InvalidOutput, match="familiar"):
        validate_prep(draft, {candidate["id"]: candidate for candidate in candidates}, {}, "white")
    draft.main_id, draft.unfamiliar_id = None, unfamiliar["id"]
    validate_prep(draft, {candidate["id"]: candidate for candidate in candidates}, {}, "white")
    result = {
        "opponent": "opponent",
        "color": "white",
        "status": "partial",
        "run_id": "synthetic",
        "selected": {"unfamiliar": unfamiliar},
        "facts": {},
        "limitations": ["x" * 500] * 40,
    }
    assert "Unfamiliar alternative, new at" in render(result)
    assert len(render(result)) <= 3800


def test_mate_supported_and_earlier_bad_moves_rejected():
    assert move_loss(
        {"cp_white": None, "mate_white": 4}, {"cp_white": None, "mate_white": 5}, chess.WHITE
    ) == (0, False)
    draft = PrepDraft(
        main_id="bad",
        deviation_ids=[],
        unfamiliar_id=None,
        remembered_id=None,
        tip_evidence_ids=[],
        explanation="",
    )
    candidate = {
        "id": "bad",
        "moves": ["d2d4", "d7d5", "c2c4"],
        "familiar": True,
        "checks": [
            {"ply": 0, "move": "d2d4", "accepted": False},
            {"ply": 2, "move": "c2c4", "accepted": True},
        ],
    }
    with pytest.raises(InvalidOutput, match="earlier"):
        validate_prep(draft, {"bad": candidate}, {}, "white")


def test_recurring_problem_requires_both_sample_thresholds():
    draft = ScoutDraft(
        observations=[
            {
                "kind": "repeated_problem",
                "position_id": "position",
                "evidence_ids": ["evidence"],
                "explanation": "Review this reply.",
            }
        ],
        limitations=[],
    )
    for n, total in ((2, 5), (3, 4)):
        with pytest.raises(InvalidOutput, match="threshold"):
            validate_scout(
                draft, {}, {"evidence": {"position_id": "position", "n": n, "total": total}}
            )
