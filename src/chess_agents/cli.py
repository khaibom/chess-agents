import argparse
import asyncio
import json
import sys
import time

import chess

from chess_agents.config import Settings, username
from chess_agents.engine import Engine
from chess_agents.evaluation import evaluate
from chess_agents.profile import build_profile, confirm, set_line
from chess_agents.storage import Store
from chess_agents.workflow import Preparation


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="chess-agents",
        description="Private pre-game preparation; completed standard games only.",
    )
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check SQLite and local Stockfish without paid calls")
    sub.add_parser("last", help="Retrieve the most recent saved preparation")
    sub.add_parser("bot", help="Start private Telegram polling")
    prep = sub.add_parser("prep")
    prep.add_argument("username")
    prep.add_argument("color", choices=["white", "black"])
    profile = sub.add_parser("profile").add_subparsers(dest="action", required=True)
    profile.add_parser("build").add_argument("username")
    profile.add_parser("show")
    change = profile.add_parser("set-line")
    change.add_argument("id")
    change.add_argument("choice", choices=["familiar", "learning", "exclude"])
    profile.add_parser("confirm")
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("manifest")
    evaluate.add_argument("--live", action="store_true")
    return result


async def execute(args: argparse.Namespace, settings: Settings, store: Store) -> None:
    if args.command == "doctor":
        store.connection.execute("SELECT 1").fetchone()
        async with Engine(settings.stockfish, time.monotonic() + 10) as engine:
            analysis = await engine.analyze(chess.Board())
            if chess.Move.from_uci(analysis["best_move"]) not in chess.Board().legal_moves:
                raise ValueError("Engine returned no legal move.")
            print(
                f"SQLite schema v1 writable; {engine.version}; path {settings.stockfish}; no paid calls."
            )
        return
    if args.command == "last":
        last = store.last()
        print(last["result"]["text"] if last else "No saved preparation.")
    elif args.command == "profile":
        if args.action == "build":
            username(args.username)
            settings.require_network()
            profile = await build_profile(store, settings, args.username)
        elif args.action == "set-line":
            profile = set_line(store, args.id, args.choice)
        elif args.action == "confirm":
            profile = confirm(store)
        else:
            profile = store.profile("draft") or store.profile("confirmed")
        print(profile.model_dump_json(indent=2) if profile else "Build a profile first.")
    elif args.command == "prep":
        username(args.username)
        settings.require_network()
        print((await Preparation(store, settings).run(args.username, args.color))["text"])
    elif args.command == "evaluate":
        print(
            json.dumps(await evaluate(args.manifest, live=args.live, settings=settings), indent=2)
        )
    elif args.command == "bot":
        raise ValueError("Use bot startup through main.")


def main() -> None:
    args = parser().parse_args()
    store = None
    try:
        settings = Settings.load()
        if args.command == "evaluate":
            print(
                json.dumps(
                    asyncio.run(evaluate(args.manifest, live=args.live, settings=settings)),
                    indent=2,
                )
            )
            return
        if args.command == "bot":
            from chess_agents.telegram_bot import run_bot

            settings.require_bot()
            settings.require_network()
            run_bot(settings)
            return
        store = Store(settings.db)
        if args.command == "prep" or (args.command == "profile" and args.action == "build"):
            store.interrupt_running()
        asyncio.run(execute(args, settings, store))
    except (ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if store is not None:
            store.close()
