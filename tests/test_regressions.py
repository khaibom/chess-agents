import asyncio
import time

import pytest
from test_llm import Client, ledger
from test_profile import OwnArchives

from chess_agents.config import Settings
from chess_agents.llm import LLMUnavailable, Luna
from chess_agents.models import ScoutDraft
from chess_agents.profile import build_profile, confirm, set_line


async def test_only_latest_draft_can_be_confirmed(store):
    await build_profile(store, Settings(), "hero", archives=OwnArchives(), cutoff=200)
    latest = await build_profile(store, Settings(), "hero", archives=OwnArchives(), cutoff=200)
    set_line(store, latest.lines[0].id, "familiar")
    assert confirm(store).id == latest.id
    assert store.profile("draft") is None
    assert store.profile("confirmed").id == latest.id


async def test_count_consuming_deadline_never_generates(store):
    class SlowCount(Client):
        async def count(self, **request):
            await asyncio.sleep(0.02)
            return await super().count(**request)

    client = SlowCount()
    luna = Luna(Settings(seconds=0.01), ledger(store), time.monotonic() + 0.01, client)
    with pytest.raises(LLMUnavailable):
        await luna.generate("scout", {}, ScoutDraft)
    assert not client.calls
