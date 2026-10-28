import json
import sqlite3
import time
import uuid
from pathlib import Path

from chess_agents.models import Profile


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise ValueError("Unsupported database version.")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS archives (
                url TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched REAL NOT NULL,
                etag TEXT, modified TEXT);
            CREATE TABLE IF NOT EXISTS games (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
                created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                inputs TEXT NOT NULL, cutoff INTEGER NOT NULL, profile_id TEXT,
                result TEXT, errors TEXT NOT NULL DEFAULT '[]',
                created REAL NOT NULL, finished REAL, delivery TEXT,
                update_id INTEGER UNIQUE, message_id INTEGER);
            CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, reserved TEXT NOT NULL,
                actual TEXT, status TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS engine_evidence (
                run_id TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (run_id, id));
            CREATE TABLE IF NOT EXISTS updates (
                id INTEGER PRIMARY KEY, received REAL NOT NULL);
            PRAGMA user_version=1;
        """)

    def close(self) -> None:
        self.connection.close()

    def archive(self, url: str) -> dict | None:
        row = self.connection.execute("SELECT * FROM archives WHERE url=?", (url,)).fetchone()
        return dict(row) if row is not None else None

    def save_archive(self, url: str, payload: dict, etag: str | None, modified: str | None) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO archives VALUES (?,?,?,?,?)",
                (url, json.dumps(payload), time.time(), etag, modified),
            )

    def save_games(self, games: list[dict]) -> None:
        with self.connection:
            self.connection.executemany(
                "INSERT OR REPLACE INTO games VALUES (?,?)",
                [(game["id"], json.dumps(game)) for game in games],
            )

    def save_profile(self, profile: Profile) -> None:
        row = self.connection.execute(
            "SELECT status FROM profiles WHERE id=?", (profile.id,)
        ).fetchone()
        if row is not None and row[0] == "confirmed":
            raise ValueError("Confirmed profiles are immutable; build or reopen a draft.")
        with self.connection:
            if profile.status == "draft":
                self.connection.execute(
                    "DELETE FROM profiles WHERE status='draft' AND id<>?", (profile.id,)
                )
            self.connection.execute(
                "INSERT OR REPLACE INTO profiles VALUES (?,?,?,?)",
                (
                    profile.id,
                    profile.status,
                    profile.model_dump_json(),
                    time.time(),
                ),
            )

    def profile(self, status: str) -> Profile | None:
        row = self.connection.execute(
            "SELECT payload FROM profiles WHERE status=? ORDER BY created DESC LIMIT 1", (status,)
        ).fetchone()
        return Profile.model_validate_json(row[0]) if row is not None else None

    def start_run(
        self,
        kind: str,
        inputs: dict,
        cutoff: int,
        profile_id: str | None = None,
        update_id: int | None = None,
    ) -> str:
        run_id = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                "INSERT INTO runs (id,kind,status,inputs,cutoff,profile_id,created,update_id) "
                "VALUES (?,?,'running',?,?,?,?,?)",
                (run_id, kind, json.dumps(inputs), cutoff, profile_id, time.time(), update_id),
            )
        return run_id

    def finish(self, run_id: str, result: dict, status: str, errors: list[str]) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET result=?,status=?,errors=?,finished=? WHERE id=?",
                (json.dumps(result), status, json.dumps(errors), time.time(), run_id),
            )

    def snapshot(self, run_id: str, result: dict) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET result=? WHERE id=?", (json.dumps(result), run_id)
            )

    def last(self) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE kind='prep' AND result IS NOT NULL "
            "ORDER BY created DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = json.loads(result["result"])
        return result

    def delivery(self, run_id: str, status: str, message_id: int | None = None) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET delivery=?,message_id=? WHERE id=?", (status, message_id, run_id)
            )

    def interrupt_running(self) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status='interrupted',finished=? WHERE status='running'",
                (time.time(),),
            )

    def claim_update(self, update_id: int) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO updates VALUES (?,?)", (update_id, time.time())
            )
        return cursor.rowcount == 1

    def save_call(
        self,
        call_id: str,
        run_id: str,
        reserved: str,
        actual: str | None,
        status: str,
        payload: dict,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO calls VALUES (?,?,?,?,?,?)",
                (
                    call_id,
                    run_id,
                    reserved,
                    actual,
                    status,
                    json.dumps(payload),
                ),
            )

    def save_engine(self, run_id: str, evidence: dict) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO engine_evidence VALUES (?,?,?)",
                (run_id, evidence["id"], json.dumps(evidence)),
            )
