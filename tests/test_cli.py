import argparse

from chess_agents.cli import execute
from chess_agents.config import Settings


async def test_last_does_not_mark_running_job_interrupted(store, capsys):
    run = store.start_run("prep", {}, 200)
    await execute(argparse.Namespace(command="last"), Settings(), store)
    assert "No saved preparation" in capsys.readouterr().out
    assert (
        store.connection.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[0]
        == "running"
    )
