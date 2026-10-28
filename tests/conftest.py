from pathlib import Path

import pytest

from chess_agents.storage import Store


@pytest.fixture
def store(tmp_path: Path):
    value = Store(tmp_path / "test.sqlite3")
    yield value
    value.close()


def record(
    number=1, *, player="opponent", color="black", moves="1. d4 d5 2. c4 e6", end=100, **changes
):
    white, black = ("hero", player) if color == "black" else (player, "hero")
    value = {
        "url": f"https://www.chess.com/game/live/{number}",
        "end_time": end,
        "rules": "chess",
        "time_class": "rapid",
        "time_control": "600",
        "rated": True,
        "white": {"username": white, "result": "win"},
        "black": {"username": black, "result": "resigned"},
        "pgn": f'[White "{white}"]\n[Black "{black}"]\n[Result "1-0"]\n\n{moves} 1-0',
    }
    value.update(changes)
    return value
