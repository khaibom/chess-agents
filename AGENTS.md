# AI agent instructions

This file gives Codex and other AI coding agents the project context and rules for making changes. The [README](README.md) is the user guide for understanding and running the project.

## Project context and map

- This app provides private, pre-game chess preparation from completed standard Chess.com games through a CLI and a private Telegram bot. It rejects live-game, board, FEN, and screenshot requests.
- [README.md](README.md) describes the shipped workflow, architecture, runtime limits, and verification status; [COMMANDS.md](COMMANDS.md) lists supported commands.
- `src/chess_agents/` contains the implementation. `cli.py` and `telegram_bot.py` are the interfaces; `games.py`, `profile.py`, `workflow.py`, `engine.py`, `llm.py`, `report.py`, and `storage.py` hold the core flow. `tests/` covers offline behavior.
- Treat mocks and synthetic evaluation as offline evidence only. Keep verification status and open external checks in the README.

## Ownership and scope

- Follow the user's latest instruction, then this file; reconcile docs when they disagree.
- Keep implementation, tests, and documentation aligned. Record actual checks and their limits when reporting changes.
- Keep scope to private, pre-game preparation from completed standard games. Do not add a web UI, public inbound port, or unrelated features without a user request.

## Shell and runtime

- Prefix shell commands with `rtk` when it is available. Never bake an author's local paths or RTK installation into runtime/container files.
- Runtime model is only `gpt-6-luna`; no automatic model/provider fallback. Enforce at most USD 0.01 modeled OpenAI charges for each prep run and separate profile build, and a 300-second prep deadline including bounded correction/retry work. Settings may lower, not raise, those limits.
- Telegram access is limited to the configured user in a private chat. Use completed standard games for preparation before a game; reject live-game, ongoing-board, FEN, and screenshot inputs.
- Keep Python and Stockfish in one Compose service, SQLite in its named volume, and credentials in mounted secret files. Normal `docker compose down` preserves data; `down -v` deletes it.
- Keep secrets, real credentials, and personal game data out of source, logs, fixtures, and image build inputs. Do not claim mocks establish live service behavior.

## Checks and documentation

- Offline suite: `uv run pytest -q`; lint: `uv run ruff check src tests scripts`; formatter: `uv run ruff format src tests scripts`.
- Container checks: `docker compose config --quiet`, `docker compose build`, and `docker compose run --rm chess-agents chess-agents doctor`. Report only checks actually run; distinguish previous recorded results from current checks.
- Keep setup instructions aligned with implemented CLI/container behavior. Clearly label external or user acceptance checks that have not been run.
