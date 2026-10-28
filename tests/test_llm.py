import time
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from chess_agents.config import MODEL, Settings
from chess_agents.llm import InvalidOutput, Ledger, LLMUnavailable, Luna, modeled_cost
from chess_agents.models import ScoutDraft


def usage(**changes):
    value = {
        "input_tokens": 1000,
        "output_tokens": 100,
        "total_tokens": 1100,
        "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 300},
        "output_tokens_details": {"reasoning_tokens": 50},
    }
    value.update(changes)
    return value


class Client:
    max_retries = 0

    def __init__(self, *, count=1000, response=None, timeout=False):
        self.responses = NS(input_tokens=NS(count=self.count), create=self.create)
        self.number = count
        self.response = response
        self.timeout = timeout
        self.calls = []
        self.counts = []

    async def count(self, **request):
        self.counts.append(request)
        return NS(input_tokens=self.number)

    async def create(self, **request):
        self.calls.append(request)
        if self.timeout:
            raise TimeoutError()
        data = usage()
        return self.response or NS(
            model=MODEL,
            service_tier="default",
            status="completed",
            id="fake",
            usage=NS(model_dump=lambda: data),
            output_text='{"observations":[],"limitations":[]}',
        )


def ledger(store, maximum=Decimal("0.01")):
    return Ledger(store, store.start_run("prep", {}, 100), maximum)


def test_cache_reasoning_and_missing_usage():
    assert modeled_cost(usage()) == Decimal("0.000177")
    # Reasoning is a subset of output, never an extra charge.
    assert modeled_cost(usage(output_tokens_details={"reasoning_tokens": 0})) == Decimal("0.000177")
    with pytest.raises(LLMUnavailable):
        modeled_cost(usage(input_tokens_details={"cached_tokens": 0}))
    with pytest.raises(LLMUnavailable):
        modeled_cost(None)


def test_exact_boundary_shared_ledger(store):
    value = ledger(store, Decimal("0.003"))
    value.reserve(8000)
    assert value.pending == Decimal("0.003")
    with pytest.raises(LLMUnavailable):
        value.reserve(0)
    assert len(value.holds) == 1


async def test_counts_schema_before_request_and_settles(store):
    client, budget = Client(), ledger(store)
    luna = Luna(Settings(), budget, time.monotonic() + 300, client)
    await luna.generate("scout", {}, ScoutDraft)
    assert client.counts[0] == {
        k: v
        for k, v in client.calls[0].items()
        if k not in ("max_output_tokens", "service_tier", "store")
    }
    assert budget.spent == Decimal("0.000177") and budget.pending == 0


async def test_no_generation_over_budget(store):
    client, budget = Client(), ledger(store, Decimal("0.001"))
    with pytest.raises(LLMUnavailable):
        await Luna(Settings(), budget, time.monotonic() + 300, client).generate(
            "scout", {}, ScoutDraft
        )
    assert not client.calls


@pytest.mark.parametrize("failure", ["timeout", "model", "tier", "usage"])
async def test_unknown_charge_stops_all_agents(store, failure):
    response = NS(model=MODEL, service_tier="default", status="completed", id="fake", usage=None)
    if failure == "model":
        response.model = "other"
    if failure == "tier":
        response.service_tier = "fast"
    client = Client(response=response, timeout=failure == "timeout")
    budget = ledger(store)
    luna = Luna(Settings(), budget, time.monotonic() + 300, client)
    with pytest.raises(LLMUnavailable):
        await luna.generate("scout", {}, ScoutDraft)
    with pytest.raises(LLMUnavailable):
        await luna.generate("prep", {}, ScoutDraft)
    assert len(client.calls) == 1 and budget.pending > 0 and budget.spent == 0


@pytest.mark.parametrize("status,text", [("incomplete", ""), ("completed", "refused")])
async def test_known_usage_invalid_output_is_settled(store, status, text):
    response = NS(
        model=MODEL,
        service_tier="default",
        status=status,
        id="fake",
        usage=NS(model_dump=lambda: usage()),
        output_text=text,
    )
    budget = ledger(store)
    with pytest.raises(InvalidOutput):
        await Luna(Settings(), budget, time.monotonic() + 300, Client(response=response)).generate(
            "scout",
            {},
            ScoutDraft,
        )
    assert budget.pending == 0 and budget.spent > 0
