"""Synthetic persistence smoke, on a separate disposable test volume."""

import sys

from chess_agents.config import Settings
from chess_agents.models import Line, Profile
from chess_agents.storage import Store

store = Store(Settings.load().db)
try:
    if sys.argv[1] == "write":
        profile = Profile(
            id="container-smoke",
            username="synthetic",
            cutoff=100,
            status="confirmed",
            lines=[
                Line(
                    id="synthetic-line",
                    color="white",
                    moves=["d2d4", "d7d5"],
                    choice="familiar",
                    reviewed=True,
                )
            ],
        )
        store.save_profile(profile)
        run_id = store.start_run("prep", {"synthetic": True}, 100, profile.id)
        store.finish(run_id, {"text": "Synthetic persisted result"}, "partial", [])
    else:
        assert store.profile("confirmed").id == "container-smoke"
        assert store.last()["result"]["text"] == "Synthetic persisted result"
    print("Synthetic profile/result persistence passed.")
finally:
    store.close()
