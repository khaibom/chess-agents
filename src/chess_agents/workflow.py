import asyncio
import time
from typing import TypedDict

import chess.engine
from langgraph.graph import END, START, StateGraph

from chess_agents.config import Settings, username
from chess_agents.engine import Engine, construct_candidates, problem_evidence
from chess_agents.games import Archives, evidence_tree
from chess_agents.llm import InvalidOutput, Ledger, LLMUnavailable, Luna
from chess_agents.models import PrepDraft, ScoutDraft
from chess_agents.profile import require_profile
from chess_agents.report import fact_packet, render, validate_prep, validate_scout
from chess_agents.storage import Store


class State(TypedDict, total=False):
    stage: str


class Preparation:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        *,
        archives=None,
        engine_factory=Engine,
        llm_factory=Luna,
        single_stage: bool = False,
    ):
        self.store, self.settings = store, settings
        self.archives = archives
        self.engine_factory, self.llm_factory = engine_factory, llm_factory
        self.single_stage = single_stage
        self.lock = asyncio.Lock()

    async def run(
        self,
        opponent: str,
        color: str,
        *,
        cutoff: int | None = None,
        update_id: int | None = None,
        accepted_at: float | None = None,
    ) -> dict:
        opponent = username(opponent)
        if color not in ("white", "black"):
            raise ValueError("Choose white or black; board/FEN inputs are not accepted.")
        profile = require_profile(self.store)
        if self.lock.locked():
            raise ValueError("Busy with another job; try again when it finishes.")
        async with self.lock:
            return await self._run(opponent, color, profile, cutoff, update_id, accepted_at)

    async def _run(self, opponent, color, profile, cutoff, update_id, accepted_at) -> dict:
        started = accepted_at if accepted_at is not None else time.monotonic()
        deadline = started + self.settings.seconds
        work_end = deadline - min(30, self.settings.seconds * 0.1)
        cutoff = cutoff if cutoff is not None else int(time.time())
        run_id = self.store.start_run(
            "prep", {"opponent": opponent, "color": color}, cutoff, profile.id, update_id
        )
        ledger = Ledger(self.store, run_id, self.settings.budget)
        luna = self.llm_factory(self.settings, ledger, deadline)
        api = self.archives or Archives(
            self.store, self.settings.user_agent, deadline=min(started + 44, work_end - 0.005)
        )
        result = {
            "run_id": run_id,
            "opponent": opponent,
            "color": color,
            "cutoff": cutoff,
            "profile_id": profile.id,
            "status": "partial",
            "coverage": {},
            "facts": {},
            "selected": {},
            "limitations": [],
            "validation_failures": [],
        }
        context = {
            "candidates": {},
            "problems": {},
            "scout": None,
            "prep": None,
            "corrected": False,
            "repair_stage": None,
            "error": "",
        }

        def snapshot():
            result["accounting"] = ledger.summary()
            result["duration_seconds"] = time.monotonic() - started
            result["text"] = render(result)
            self.store.snapshot(run_id, result)

        def invalid(stage, error):
            context["error"] = str(error)
            context["repair_stage"] = stage
            result["validation_failures"].append(f"{stage}: {error}")

        def packet():
            compact_facts = {
                eid: {key: value for key, value in fact.items() if key != "games"}
                for eid, fact in result["facts"].items()
            }
            compact_problems = {
                eid: {key: value for key, value in fact.items() if key not in ("games", "check")}
                for eid, fact in context["problems"].items()
            }
            return {
                "color": color,
                "facts": compact_facts,
                "problems": compact_problems,
                "candidates": {
                    cid: {
                        k: value
                        for k, value in candidate.items()
                        if k in ("id", "moves", "familiar", "unfamiliar_at")
                    }
                    for cid, candidate in context["candidates"].items()
                },
                "confirmed_profile": [
                    {"id": line.id, "color": line.color, "moves": line.moves, "choice": line.choice}
                    for line in profile.lines
                ],
                "scout": context["scout"].model_dump() if context["scout"] else None,
            }

        async def evidence(_state):
            async with asyncio.timeout(min(45, max(0.001, work_end - time.monotonic()))):
                games, result["coverage"] = await api.games(
                    opponent,
                    "black" if color == "white" else "white",
                    cutoff,
                )
            tree = evidence_tree(games)
            result["facts"] = fact_packet(tree, color)
            snapshot()
            if not games:
                result["limitations"].append("Insufficient completed opponent data.")
                return {"stage": "end"}
            try:
                async with self.engine_factory(self.settings.stockfish, work_end) as engine:
                    candidates = await construct_candidates(profile, tree, color, engine)
                    context["candidates"] = {item["id"]: item for item in candidates}
                    # Keep checked candidates even if optional problem search exhausts its budget.
                    try:
                        context["problems"] = await problem_evidence(tree, color, engine)
                    except (TimeoutError, ValueError):
                        result["limitations"].append(
                            "Optional repeated-problem search was bounded."
                        )
                    for record in engine.cache.values():
                        self.store.save_engine(run_id, record)
                    result["problems"] = context["problems"]
            except (OSError, chess.engine.EngineError, TimeoutError, ValueError):
                result["limitations"].append(
                    "Stockfish unavailable or analysis incomplete; evidence-only fallback."
                )
                context["candidates"] = {}
                return {"stage": "end"}
            if not context["candidates"]:
                result["limitations"].append("No sound familiar response survived bounded checks.")
                return {"stage": "end"}
            return {"stage": "prep" if self.single_stage else "scout"}

        async def scout(_state):
            try:
                draft = await luna.generate("scout", packet(), ScoutDraft)
                validate_scout(draft, result["facts"], context["problems"])
                context["scout"] = draft
                return {"stage": "prep"}
            except InvalidOutput as error:
                invalid("scout", error)
                return {"stage": "repair"}

        async def prep(_state):
            try:
                context["prep"] = await luna.generate("prep", packet(), PrepDraft)
                return {"stage": "verify"}
            except InvalidOutput as error:
                invalid("prep", error)
                return {"stage": "repair"}

        async def verify(_state):
            draft = context["prep"]
            main = context["candidates"].get(draft.main_id)
            if main is not None:
                if draft.remembered_id is None:
                    draft.remembered_id = draft.main_id
                if not draft.tip_evidence_ids:
                    relevant = {(fact["position_id"], fact["move"]) for fact in main["facts"]}
                    draft.tip_evidence_ids = [
                        eid
                        for eid, fact in result["facts"].items()
                        if (fact["position_id"], fact["move"]) in relevant
                    ][:1]
            try:
                validate_prep(draft, context["candidates"], result["facts"], color)
            except InvalidOutput as error:
                invalid("prep", error)
                return {"stage": "repair"}
            chosen = context["candidates"]
            result["selected"] = {
                "main": chosen.get(draft.main_id),
                "deviations": [chosen[cid] for cid in draft.deviation_ids],
                "unfamiliar": chosen.get(draft.unfamiliar_id),
                "remember": chosen.get(draft.remembered_id),
            }
            result["tip_evidence_ids"] = draft.tip_evidence_ids
            result["explanation"] = draft.explanation
            result["scout"] = context["scout"].model_dump() if context["scout"] else None
            if draft.explanation:
                result["limitations"].append(
                    "Free-text explanation still needs human quality review."
                )
            result["status"] = "full" if draft.main_id else "partial"
            snapshot()
            return {"stage": "end"}

        async def repair(_state):
            if context["corrected"] or time.monotonic() + 1 >= work_end:
                result["limitations"].append("Validation failed; retaining verified facts only.")
                return {"stage": "end"}
            context["corrected"] = True
            stage = context["repair_stage"]
            try:
                draft = await luna.generate(
                    stage,
                    packet(),
                    ScoutDraft if stage == "scout" else PrepDraft,
                    correction=context["error"],
                )
                if stage == "scout":
                    validate_scout(draft, result["facts"], context["problems"])
                    context["scout"] = draft
                    return {"stage": "prep"}
                context["prep"] = draft
                return {"stage": "verify"}
            except InvalidOutput as error:
                invalid(stage, error)
                result["limitations"].append("Correction rejected; retaining verified facts only.")
                return {"stage": "end"}

        graph = StateGraph(State)
        for name, node in (
            ("evidence", evidence),
            ("scout", scout),
            ("prep", prep),
            ("verify", verify),
            ("repair", repair),
        ):
            graph.add_node(name, node)
            graph.add_conditional_edges(
                name,
                lambda state: state["stage"],
                {
                    "scout": "scout",
                    "prep": "prep",
                    "verify": "verify",
                    "repair": "repair",
                    "end": END,
                },
            )
        graph.add_edge(START, "evidence")
        snapshot()
        try:
            async with asyncio.timeout(max(0.001, work_end - time.monotonic())):
                await graph.compile().ainvoke({}, config={"recursion_limit": 12})
        except (LLMUnavailable, TimeoutError, ValueError) as error:
            result["limitations"].append(
                str(error) or "Time exhausted; retained verified partial result."
            )
        except asyncio.CancelledError:
            result["limitations"].append(
                "Interrupted; no automatic resume or additional generation."
            )
            result["status"] = "interrupted"
            raise
        finally:
            await luna.close()
            if self.archives is None:
                await api.close()
            snapshot()
            self.store.finish(run_id, result, result["status"], result["validation_failures"])
        return result
