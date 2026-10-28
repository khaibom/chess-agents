# Chess Agents command reference

Telegram and CLI commands. See the [README](README.md) for setup and Docker usage.

Replace `<my_username>`, `<opponent_username>`, and `<line_id>` with actual values; omit the angle brackets. Usernames are Chess.com usernames. `<color>` is `white` or `black` and always means **your color**.

## Telegram

Send these messages to your own bot from the configured Telegram account in a private chat.

| Command | What it does |
| --- | --- |
| `/start` | Show help and available commands. |
| `/help` | Show the same help. |
| `/setup <my_username>` | Build a draft profile from your completed games and open the review. |
| `/profile` | Open profile review; label lines and confirm your choices. |
| `/prep <opponent_username> <color>` | Generate and save pre-game preparation. |
| `/last` | Retrieve the latest saved report without another model call. |

Use the review buttons to mark lines **Familiar**, **Learning**, or **Exclude**, then tap **Confirm profile**. Familiar means you know the line, Learning means you are still studying it, and Exclude means you do not want it recommended. See [Profile labels](README.md#profile-labels) for details. Review at least one line before confirming; a familiar main-line recommendation requires a Familiar line.

Typical order: `/setup` → review buttons → **Confirm profile** → `/prep` → `/last`.

## CLI

Run these from the repository root after configuring the environment described in the README.

| Command | What it does |
| --- | --- |
| `uv run chess-agents --help` | List top-level commands. Append `--help` to any command or subcommand for its syntax. |
| `uv run chess-agents doctor` | Check SQLite and Stockfish without paid model calls. |
| `uv run chess-agents profile build <my_username>` | Build a draft profile and print its JSON. |
| `uv run chess-agents profile show` | Print the current draft, or the confirmed profile if no draft exists. |
| `uv run chess-agents profile set-line <line_id> <choice>` | Review a line with `familiar`, `learning`, or `exclude`. |
| `uv run chess-agents profile confirm` | Confirm the reviewed draft. |
| `uv run chess-agents prep <opponent_username> <color>` | Generate, print, and save pre-game preparation. |
| `uv run chess-agents last` | Print the latest saved report without another model call. |
| `uv run chess-agents bot` | Start Telegram polling; leave the process running. Requires Telegram credentials and the allowed user ID. |
| `uv run chess-agents evaluate evals/cases.json` | Run the supplied offline evaluation cases. |
| `uv run chess-agents evaluate evals/cases.json --live` | Run paid evaluation with the real model and engine. Prints the case count and maximum total charge before starting. |

For profile review, copy a line's `id` from the profile JSON, rather than the profile's own `id`. Review at least one line, then confirm. A familiar main-line recommendation requires a line marked `familiar`.
