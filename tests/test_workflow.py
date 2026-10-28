import asyncio
import time

import pytest
from conftest import record
from test_engine import FakeEngine, profile

from chess_agents.config import Settings
from chess_agents.games import select_games
from chess_agents.llm import InvalidOutput, LLMUnavailable
from chess_agents.models import PrepDraft, ScoutDraft
from chess_agents.report import validate_prep, validate_scout
from chess_agents.workflow import Preparation


class EngineContext(FakeEngine):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class OpponentArchives:
    async def games(self, player, color, cutoff):
        games, _ = select_games(
            [record(color=color), record(2, color=color, moves="1. d4 Nf6 2. c4 e6")],
            player,
            color,
            cutoff,
            0,
        )
        return games, {"count": len(games), "from": 100, "to": 100, "cutoff": cutoff}


class FakeLuna:
    calls = []
    failures = []

    def __init__(self, *_):
        self.calls = []
        self.failures = list(type(self).failures)
        type(self).latest = self

    async def close(self):
        pass

    async def generate(self, role, packet, schema, correction=None):
        self.calls.append(role)
        if self.failures:
            error = self.failures.pop(0)
            if error:
                raise error
        if schema == ScoutDraft:
            return ScoutDraft(observations=[], limitations=[])
        main = next((cid for cid, value in packet["candidates"].items() if value["familiar"]), None)
        return PrepDraft(
            main_id=main,
            deviation_ids=[],
            unfamiliar_id=None,
            remembered_id=main,
            tip_evidence_ids=list(packet["facts"])[:1],
            explanation="Review the familiar structure and the observed reply.",
        )


def preparation(store, color="white", **kwargs):
    store.save_profile(profile(color))
    return Preparation(
        store,
        Settings(**kwargs),
        archives=OpponentArchives(),
        engine_factory=lambda *_: EngineContext(),
        llm_factory=FakeLuna,
    )


@pytest.mark.parametrize("color", ["white", "black"])
async def test_full_workflow_both_colors_and_last(store, color):
    service = preparation(store, color)
    result = await service.run("opponent", color, cutoff=200)
    assert result["status"] == "full"
    assert len(result["text"]) <= 3800
    assert store.last()["result"]["text"] == result["text"]
    assert FakeLuna.latest.calls == ["scout", "prep"]


async def test_global_one_correction(store):
    class Bad(FakeLuna):
        failures = [InvalidOutput("bad scout"), None, InvalidOutput("bad prep")]

    service = preparation(store)
    service.llm_factory = Bad
    result = await service.run("opponent", "white", cutoff=200)
    assert Bad.latest.calls == ["scout", "scout", "prep"]
    assert result["status"] == "partial" and not result["selected"]


async def test_budget_failure_and_engine_failure_keep_evidence(store):
    class Unavailable(FakeLuna):
        failures = [LLMUnavailable("budget exhausted")]

    service = preparation(store)
    service.llm_factory = Unavailable
    result = await service.run("opponent", "white", cutoff=200)
    assert result["facts"] and result["status"] == "partial"

    def missing(*_):
        raise FileNotFoundError()

    service.engine_factory = missing
    result = await service.run("opponent", "white", cutoff=200)
    assert result["facts"] and "Stockfish unavailable" in result["text"]


async def test_stall_cancellation_and_busy(store):
    service = preparation(store, seconds=0.05)

    class Stalled:
        async def games(self, *_):
            await asyncio.sleep(10)

    service.archives = Stalled()
    started = time.monotonic()
    task = asyncio.create_task(service.run("opponent", "white", cutoff=200))
    await asyncio.sleep(0.005)
    with pytest.raises(ValueError, match="Busy"):
        await service.run("other", "white")
    result = await task
    assert result["status"] == "partial" and time.monotonic() - started < 0.5
    assert store.last()["result"] == result


def test_unsupported_claims_and_references():
    bad = ScoutDraft(
        observations=[
            {
                "kind": "frequency",
                "position_id": "none",
                "evidence_ids": ["invented"],
                "explanation": "Likely reply.",
            }
        ],
        limitations=[],
    )
    with pytest.raises(InvalidOutput):
        validate_scout(bad, {}, {})
    with pytest.raises(InvalidOutput):
        validate_prep(
            PrepDraft(
                main_id="unknown",
                deviation_ids=[],
                unfamiliar_id=None,
                remembered_id=None,
                tip_evidence_ids=[],
                explanation="",
            ),
            {},
            {},
            "white",
        )


async def test_no_history_no_llm(store):
    service = preparation(store)

    class Empty:
        async def games(self, *_):
            return [], {"count": 0}

    service.archives = Empty()
    assert (await service.run("opponent", "white", cutoff=200))["status"] == "partial"
    assert FakeLuna.latest.calls == []


async def test_missing_remember_and_tip_get_code_owned_defaults(store):
    class Omitted(FakeLuna):
        async def generate(self, *args, **kwargs):
            draft = await super().generate(*args, **kwargs)
            if isinstance(draft, PrepDraft):
                draft.remembered_id, draft.tip_evidence_ids = None, []
            return draft

    service = preparation(store)
    service.llm_factory = Omitted
    result = await service.run("opponent", "white", cutoff=200)
    assert result["status"] == "full"
    assert result["selected"]["remember"]["id"] == result["selected"]["main"]["id"]
    assert result["tip_evidence_ids"]
    assert "Remember:" in result["text"] and "Tip:" in result["text"]
    main_replies = {
        (fact["position_id"], fact["move"]) for fact in result["selected"]["main"]["facts"]
    }
    assert all(
        (result["facts"][eid]["position_id"], result["facts"][eid]["move"]) in main_replies
        for eid in result["tip_evidence_ids"]
    )
