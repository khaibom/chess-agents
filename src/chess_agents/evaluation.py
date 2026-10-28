import asyncio
import json
import math
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import chess
from pydantic import BaseModel

from chess_agents.config import Settings
from chess_agents.engine import Engine, legal_line
from chess_agents.games import months_before, position_id, select_games
from chess_agents.llm import InvalidOutput, LLMUnavailable, Luna
from chess_agents.models import PrepDraft, Profile, ScoutDraft
from chess_agents.profile import infer_lines
from chess_agents.storage import Store
from chess_agents.workflow import Preparation


class FixtureArchives:
    def __init__(self, records: list[dict], scenario: str):
        self.records, self.scenario = records, scenario

    async def games(self, player: str, color: str, cutoff: int):
        if self.scenario == "deadline":
            await asyncio.sleep(1)
        if self.scenario == "archive_failure":
            raise ValueError("Fixture archive unavailable.")
        _, earliest = months_before(cutoff)
        games, excluded = select_games(self.records, player, color, cutoff, earliest)
        dates = [game["end_time"] for game in games]
        return games, {
            "count": len(games),
            "from": min(dates) if dates else None,
            "to": max(dates) if dates else None,
            "cutoff": cutoff,
            "excluded": excluded,
            "stale": self.scenario == "stale",
        }


class FixtureEngine:
    """Synthetic scores for deterministic offline contract checks, never engine quality."""

    def __init__(self, *_):
        self.cache = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def analyze(self, board, move=None):
        return {"best_move": next(iter(board.legal_moves)).uci(), "cp_white": 0, "mate_white": None}

    async def check(self, board, move):
        return {
            "accepted": True,
            "loss_cp": 0,
            "losing_mate": False,
            "move": move.uci(),
            "root": {},
            "played": {},
        }


class FixtureLuna:
    def __init__(self, settings, ledger, deadline):
        self.ledger = ledger
        self.scenario = "normal"

    async def close(self):
        pass

    async def generate(self, role: str, packet: dict, schema: type[BaseModel], correction=None):
        call = self.ledger.reserve(1000)
        if self.scenario == "unknown_charge":
            self.ledger.unknown(call, "fixture ambiguous timeout")
            raise LLMUnavailable("Fixture unknown charge; stopped.")
        self.ledger.settle(call, Decimal("0.0001"), {"synthetic": True})
        if self.scenario == "invalid_output":
            raise InvalidOutput("Fixture invalid structured output.")
        if schema is ScoutDraft:
            return ScoutDraft(observations=[], limitations=[])
        main = next((cid for cid, value in packet["candidates"].items() if value["familiar"]), None)
        deviations = [
            cid for cid, value in packet["candidates"].items() if value["familiar"] and cid != main
        ][:2]
        return PrepDraft(
            main_id=main,
            deviation_ids=deviations,
            unfamiliar_id=None,
            remembered_id=main,
            tip_evidence_ids=list(packet["facts"])[:1],
            explanation="",
        )


class FrequentMoves(FixtureLuna):
    async def generate(self, role, packet, schema, correction=None):
        if schema is ScoutDraft:
            return ScoutDraft(observations=[], limitations=[])
        main = next(
            (cid for cid, candidate in packet["candidates"].items() if candidate["familiar"]), None
        )
        return PrepDraft(
            main_id=main,
            deviation_ids=[],
            unfamiliar_id=None,
            remembered_id=main,
            tip_evidence_ids=[],
            explanation="",
        )


def prediction_counts(result: dict, target: list[str], color: str) -> tuple[int, int, int]:
    predicted = {}
    selected = result.get("selected", {})
    for candidate in ([selected["main"]] if selected.get("main") else []) + selected.get(
        "deviations", []
    ):
        board = chess.Board()
        for move in candidate["moves"]:
            if board.turn != (color == "white"):
                predicted.setdefault(position_id(board), set()).add(move)
            board.push_uci(move)
    reached, covered, correct = 0, 0, 0
    board = chess.Board()
    for move in target[:16]:
        if board.turn != (color == "white"):
            reached += 1
            choices = predicted.get(position_id(board), set())
            if choices:
                covered += 1
                correct += move in choices
        board.push_uci(move)
    return reached, covered, correct


async def evaluate(manifest: str, *, live: bool = False, settings: Settings | None = None) -> dict:
    path = Path(manifest)
    document = json.loads(path.read_text(encoding="utf-8"))
    cases = document["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError("Evaluation needs 1–100 explicit frozen cases.")
    settings = settings or Settings()
    if live:
        if settings.openai_key is None:
            raise ValueError("Live evaluation requires an explicitly configured OpenAI key.")
        print(
            f"Live evaluation: {len(cases)} cases, two paid runs each, maximum modeled spend "
            f"USD {len(cases) * 2 * settings.budget}; only {document.get('source', 'frozen evidence')}.",
            file=sys.stderr,
            flush=True,
        )
    rows = []
    with tempfile.TemporaryDirectory(prefix="chess-eval-") as directory:
        for case in cases:
            records = json.loads((path.parent / case["fixture"]).read_text(encoding="utf-8"))
            cutoff, color = case["cutoff"], case["color"]
            _, earliest = months_before(cutoff)
            own = {
                side: select_games(records, "hero", side, cutoff, earliest)[0]
                for side in ("white", "black")
            }
            for strategy in ("frequent_move", "single_luna", "two_stage"):
                store = Store(Path(directory) / f"{case['id']}-{strategy}.sqlite3")
                try:
                    profile = Profile(
                        id=case["id"],
                        username="hero",
                        cutoff=cutoff,
                        status="confirmed",
                        lines=infer_lines(own),
                    )
                    for line in profile.lines:
                        # Fixture manifest explicitly simulates review; runtime never auto-confirms.
                        line.choice, line.reviewed = "familiar", True
                    store.save_profile(profile)
                    scenario = case.get("scenario", "normal")

                    def fixture_factory(*args, scenario=scenario):
                        fixture = FixtureLuna(*args)
                        fixture.scenario = scenario
                        return fixture

                    llm_factory = (
                        FrequentMoves
                        if strategy == "frequent_move"
                        else (Luna if live else fixture_factory)
                    )

                    def engine_factory(*args, scenario=scenario):
                        if scenario == "engine_failure":
                            raise FileNotFoundError("Fixture missing engine.")
                        return Engine(*args) if live else FixtureEngine(*args)

                    case_settings = (
                        replace(settings, budget=Decimal("0.0005"))
                        if scenario == "budget"
                        else (
                            replace(settings, seconds=0.01) if scenario == "deadline" else settings
                        )
                    )
                    service = Preparation(
                        store,
                        case_settings,
                        archives=FixtureArchives(records, scenario),
                        engine_factory=engine_factory,
                        llm_factory=llm_factory,
                        single_stage=strategy != "two_stage",
                    )
                    started = time.monotonic()
                    result = await service.run("opponent", color, cutoff=cutoff)
                    selected = result.get("selected", {})
                    lines = ([selected["main"]] if selected.get("main") else []) + selected.get(
                        "deviations", []
                    )
                    if selected.get("unfamiliar"):
                        lines.append(selected["unfamiliar"])
                    reached, covered, correct = prediction_counts(
                        result, case["target_moves"], color
                    )
                    evidence_ids = {
                        game["id"]
                        for game in select_games(
                            records,
                            "opponent",
                            "black" if color == "white" else "white",
                            cutoff,
                            earliest,
                        )[0]
                    }
                    invalid_refs = sum(
                        not set(fact["games"]) <= evidence_ids for fact in result["facts"].values()
                    )
                    rows.append(
                        {
                            "case": case["id"],
                            "strategy": strategy,
                            "status": result["status"],
                            "seconds": time.monotonic() - started,
                            "reached": reached,
                            "covered": covered,
                            "correct": correct,
                            "abstentions": reached - covered,
                            "delivered_lines": len(lines),
                            "illegal_lines": sum(not legal_line(line["moves"]) for line in lines),
                            "unchecked_lines": sum(
                                not all(check["accepted"] for check in line["checks"])
                                for line in lines
                            ),
                            "invalid_citations": invalid_refs,
                            "post_cutoff_evidence": sum(
                                game["end_time"] >= cutoff
                                for records_by_color in own.values()
                                for game in records_by_color
                            ),
                            "false_weakness_claims": sum(
                                any(
                                    eid not in result.get("problems", {})
                                    or result["problems"][eid]["n"] < 3
                                    or result["problems"][eid]["total"] < 5
                                    for eid in finding["evidence_ids"]
                                )
                                for finding in (result.get("scout") or {}).get("observations", [])
                                if finding["kind"] == "repeated_problem"
                            ),
                            "characters": len(result["text"]),
                            **result["accounting"],
                        }
                    )
                finally:
                    store.close()
    summaries = {}
    for strategy in ("frequent_move", "single_luna", "two_stage"):
        group = [row for row in rows if row["strategy"] == strategy]
        durations = sorted(row["seconds"] for row in group)
        covered, reached = (
            sum(row["covered"] for row in group),
            sum(row["reached"] for row in group),
        )
        delivered = sum(row["delivered_lines"] for row in group)
        illegal = sum(row["illegal_lines"] for row in group)
        summaries[strategy] = {
            "cases": len(group),
            "opening_prediction_at_covered_positions": sum(row["correct"] for row in group)
            / covered
            if covered
            else None,
            "prediction_coverage": covered / reached if reached else None,
            "abstentions": reached - covered,
            "legal_line_rate": (delivered - illegal) / delivered if delivered else None,
            "engine_check_pass_rate": (delivered - sum(row["unchecked_lines"] for row in group))
            / delivered
            if delivered
            else None,
            "evidence_validity": all(
                row["invalid_citations"] == row["post_cutoff_evidence"] == 0 for row in group
            ),
            "false_weakness_claims": sum(row["false_weakness_claims"] for row in group),
            "full_rate": sum(row["status"] == "full" for row in group) / len(group),
            "partial_rate": sum(row["status"] != "full" for row in group) / len(group),
            "median_seconds": statistics.median(durations),
            "p95_seconds": durations[max(0, math.ceil(0.95 * len(group)) - 1)],
            "modeled_spend_usd": str(sum((Decimal(row["spent_usd"]) for row in group), Decimal(0))),
            "unknown_reservations_usd": str(
                sum((Decimal(row["unknown_reserved_usd"]) for row in group), Decimal(0))
            ),
        }
    gates = all(
        row["illegal_lines"]
        == row["invalid_citations"]
        == row["post_cutoff_evidence"]
        == row["false_weakness_claims"]
        == 0
        and Decimal(row["spent_usd"]) + Decimal(row["unknown_reserved_usd"])
        <= Decimal(row["ceiling_usd"])
        and row["characters"] <= 3800
        for row in rows
    )
    return {
        "mode": "live_model" if live else "offline_synthetic",
        "correctness_gates_pass": gates,
        "limitations": "Small synthetic sample. Offline fake model/engine results measure contracts only; "
        "they do not establish model or chess quality. Explanations require manual review.",
        "strategies": summaries,
        "runs": rows,
    }
