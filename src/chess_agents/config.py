import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

MODEL = "gpt-6-luna"
MAX_COST = Decimal("0.01")
MAX_SECONDS = 300.0


def username(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{3,25}", value) is None:
        raise ValueError("Use a Chess.com username (3–25 letters, digits, _ or -). No board input.")
    return value.lower()


def secret(name: str) -> str | None:
    value, file = os.environ.get(name), os.environ.get(f"{name}_FILE")
    if value is not None and file is not None:
        raise ValueError(f"Configure only one source for {name}.")
    if file is not None:
        try:
            value = Path(file).read_text(encoding="utf-8").strip()
        except OSError:
            raise ValueError(f"Cannot read {name} secret file.") from None
    if value is not None and (not value.strip() or "\n" in value):
        raise ValueError(f"Invalid {name} secret.")
    return value


@dataclass(frozen=True)
class Settings:
    db: Path = Path("/data/chess-agents.sqlite3")
    stockfish: str = "/usr/games/stockfish"
    user_agent: str = ""
    allowed_user: int | None = None
    budget: Decimal = MAX_COST
    seconds: float = MAX_SECONDS
    openai_key: str | None = field(default=None, repr=False)
    telegram_token: str | None = field(default=None, repr=False)

    @classmethod
    def load(cls) -> "Settings":
        if os.environ.get("OPENAI_MODEL", MODEL) != MODEL:
            raise ValueError(f"Runtime model must be {MODEL}.")
        try:
            budget = Decimal(os.environ.get("CHESS_BUDGET_USD", str(MAX_COST)))
            seconds = float(os.environ.get("CHESS_DEADLINE_SECONDS", str(MAX_SECONDS)))
            user = (
                int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
                if os.environ.get("TELEGRAM_ALLOWED_USER_ID")
                else None
            )
        except (ValueError, InvalidOperation):
            raise ValueError("Invalid numeric configuration.") from None
        if not budget.is_finite() or not Decimal(0) < budget <= MAX_COST:
            raise ValueError("Budget must be positive and at most USD 0.01.")
        if not 0 < seconds <= MAX_SECONDS:
            raise ValueError("Deadline must be positive and at most 300 seconds.")
        if user is not None and user <= 0:
            raise ValueError("Allowed Telegram user ID must be positive.")
        return cls(
            db=Path(os.environ.get("CHESS_DB", "/data/chess-agents.sqlite3")),
            stockfish=os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish"),
            user_agent=os.environ.get("CHESS_USER_AGENT", ""),
            allowed_user=user,
            budget=budget,
            seconds=seconds,
            openai_key=secret("OPENAI_API_KEY"),
            telegram_token=secret("TELEGRAM_BOT_TOKEN"),
        )

    def require_network(self) -> None:
        if not self.user_agent or "\n" in self.user_agent or len(self.user_agent) > 250:
            raise ValueError("Set an identifying CHESS_USER_AGENT with contact information.")

    def require_bot(self) -> None:
        if self.allowed_user is None or self.telegram_token is None:
            raise ValueError("Bot requires TELEGRAM_ALLOWED_USER_ID and TELEGRAM_BOT_TOKEN.")
