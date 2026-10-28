import pytest
from conftest import record

from chess_agents.config import Settings
from chess_agents.games import select_games
from chess_agents.profile import build_profile, confirm, require_profile, set_line
from chess_agents.storage import Store


class OwnArchives:
    async def games(self, player, color, cutoff):
        games, _ = select_games([record(player="hero", color="white")], "hero", color, cutoff, 0)
        return games, {}


async def test_review_restart_rebuild_immutable(store, tmp_path):
    with pytest.raises(ValueError, match="confirm"):
        require_profile(store)
    draft = await build_profile(store, Settings(), "hero", archives=OwnArchives(), cutoff=200)
    assert draft.lines and all(
        not line.reviewed and line.choice == "learning" for line in draft.lines
    )
    assert "unavailable" in " ".join(draft.limitations)
    with pytest.raises(ValueError, match="Review"):
        confirm(store)
    set_line(store, draft.lines[0].id, "familiar")
    store.close()
    reopened = Store(tmp_path / "test.sqlite3")
    assert reopened.profile("draft").lines[0].reviewed
    confirmed = confirm(reopened)
    assert require_profile(reopened).id == confirmed.id
    second = await build_profile(reopened, Settings(), "hero", archives=OwnArchives(), cutoff=200)
    assert second.id != confirmed.id
    assert require_profile(reopened).id == confirmed.id
    with pytest.raises(ValueError, match="immutable"):
        reopened.save_profile(confirmed)
    reopened.close()
    # Restore fixture connection for its finalizer.
    store.connection = Store(tmp_path / "test.sqlite3").connection


async def test_no_games_and_stale_callback(store):
    class Empty:
        async def games(self, *_):
            return [], {}

    result = await build_profile(store, Settings(), "hero", archives=Empty(), cutoff=200)
    assert result.preferences and not result.lines and "No usable" in result.limitations[0]
    with pytest.raises(ValueError):
        confirm(store)
    with pytest.raises(ValueError, match="stale"):
        set_line(store, "x", "familiar", "old-draft")
