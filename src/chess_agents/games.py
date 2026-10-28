import asyncio
import hashlib
import io
import json
import logging
import re
import time
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import urlparse

import chess
import chess.pgn
import httpx

from chess_agents.config import username
from chess_agents.storage import Store

MAX_GAMES = 200
MAX_PLY = 24
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PGN_BYTES = 256 * 1024
API = "https://api.chess.com/pub/player"


def position_key(board: chess.Board) -> str:
    return " ".join(board.fen(en_passant="legal").split()[:4])


def position_id(board: chess.Board) -> str:
    return hashlib.sha256(position_key(board).encode()).hexdigest()[:16]


def source_url(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"https://www\.chess\.com/game/(?:live|daily)/[0-9]+", value) is not None
    )


def parse_record(
    raw: dict, player: str, color: str, cutoff: int, earliest: int
) -> tuple[dict | None, str | None]:
    if (
        raw.get("rules") != "chess"
        or raw.get("time_class") != "rapid"
        or raw.get("rated") is not True
    ):
        return None, "not rated standard rapid"
    if type(raw.get("end_time")) is not int or not earliest <= raw["end_time"] < cutoff:
        return None, "outside cutoff/window"
    if any(
        not isinstance(raw.get(side), dict)
        or not isinstance(raw[side].get("username"), str)
        or not raw[side]["username"]
        or not isinstance(raw[side].get("result"), str)
        for side in ("white", "black")
    ):
        return None, "malformed player metadata"
    if (
        not isinstance(raw.get(color), dict)
        or str(raw[color].get("username", "")).lower() != player
    ):
        return None, "wrong color"
    terminal = {
        "win",
        "checkmated",
        "agreed",
        "repetition",
        "timeout",
        "resigned",
        "stalemate",
        "lose",
        "insufficient",
        "50move",
        "abandoned",
        "timevsinsufficient",
        "bughousepartnerlose",
    }
    if any(raw[side]["result"] not in terminal for side in ("white", "black")):
        return None, "unfinished metadata"
    if not source_url(raw.get("url")):
        return None, "invalid game URL"
    pgn = raw.get("pgn")
    if not isinstance(pgn, str) or len(pgn.encode()) > MAX_PGN_BYTES:
        return None, "invalid/oversized PGN"
    movetext = re.sub(r"^\s*\[.*\]\s*$", "", pgn, flags=re.MULTILINE)
    movetext = re.sub(r"\{[^}]*\}|;[^\n]*", " ", movetext, flags=re.DOTALL)
    movetext = re.sub(r"\d+\.(?:\.\.)?|\$\d+|[()!?+#]", " ", movetext)
    token = r"(?:[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=?[QRBN])?|O-O(?:-O)?|0-0(?:-0)?|1-0|0-1|1/2-1/2|\*)"
    if any(re.fullmatch(token, word) is None for word in movetext.split()):
        return None, "malformed PGN tokens"
    try:
        # PGN errors are recorded rather than logged with untrusted/private input.
        game = chess.pgn.read_game(io.StringIO(pgn))
        if (
            game is None
            or game.errors
            or game.headers.get("Result")
            not in (
                "1-0",
                "0-1",
                "1/2-1/2",
            )
        ):
            return None, "unfinished/malformed PGN"
        if game.headers.get("SetUp") == "1" or "FEN" in game.headers or game.board().chess960:
            return None, "nonstandard start"
        if game.headers.get("Variant", "Standard").lower() not in ("standard", "chess"):
            return None, "variant PGN"
        result = game.headers["Result"]
        if (
            (result == "1-0" and raw["white"]["result"] != "win")
            or (result == "0-1" and raw["black"]["result"] != "win")
            or (result == "1/2-1/2" and "win" in (raw["white"]["result"], raw["black"]["result"]))
        ):
            return None, "contradictory result metadata"
        board = chess.Board()
        moves = []
        for move in game.mainline_moves():
            if move not in board.legal_moves:
                return None, "illegal move"
            board.push(move)
            moves.append(move.uci())
        if not moves:
            return None, "empty PGN"
        # Metadata and PGN must identify the same players; headers never enter prompts.
        for side in ("white", "black"):
            if (
                not isinstance(raw.get(side), dict)
                or game.headers.get(side.title(), "").lower()
                != str(raw[side].get("username", "")).lower()
            ):
                return None, "player mismatch"
    except (ValueError, TypeError, KeyError, IndexError):
        return None, "malformed PGN"
    return {
        "id": raw["url"],
        "url": raw["url"],
        "end_time": raw["end_time"],
        "white": raw["white"]["username"].lower(),
        "black": raw["black"]["username"].lower(),
        "time_control": str(raw.get("time_control", "unknown"))[:40],
        "moves": moves[:MAX_PLY],
        "pgn": pgn,
    }, None


def select_games(
    records: list[dict], player: str, color: str, cutoff: int, earliest: int
) -> tuple[list[dict], dict[str, int]]:
    selected = {}
    excluded = Counter()
    for raw in records:
        collect_record(raw, player, color, cutoff, earliest, selected, excluded)
    return sorted(selected.values(), key=lambda game: game["end_time"], reverse=True)[
        :MAX_GAMES
    ], dict(excluded)


def collect_record(
    raw, player: str, color: str, cutoff: int, earliest: int, selected: dict, excluded: Counter
) -> None:
    if not isinstance(raw, dict):
        excluded["malformed record"] += 1
        return
    game, reason = parse_record(raw, player, color, cutoff, earliest)
    if game is None:
        excluded[reason] += 1
    elif game["id"] in selected:
        excluded["duplicate"] += 1
    else:
        selected[game["id"]] = game


async def select_games_async(
    records: list[dict], player: str, color: str, cutoff: int, earliest: int, deadline: float
) -> tuple[list[dict], dict, int]:
    selected, excluded = {}, Counter()
    processed = 0
    for raw in records:
        if time.monotonic() >= deadline:
            break
        collect_record(raw, player, color, cutoff, earliest, selected, excluded)
        processed += 1
        if processed % 8 == 0:
            await asyncio.sleep(0)
    games = sorted(selected.values(), key=lambda game: game["end_time"], reverse=True)[:MAX_GAMES]
    return games, dict(excluded), len(records) - processed


def evidence_tree(games: list[dict]) -> dict[str, dict]:
    positions = {}
    for game in games:
        board = chess.Board()
        seen = set()
        for ply, uci in enumerate(game["moves"][:MAX_PLY]):
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves:
                raise ValueError("Illegal normalized game.")
            key = position_id(board)
            if key not in seen:
                entry = positions.setdefault(
                    key,
                    {
                        "id": key,
                        "key": position_key(board),
                        "fen": board.fen(),
                        "prefix": game["moves"][:ply],
                        "games": {},
                        "moves": {},
                    },
                )
                entry["games"][game["id"]] = ply
                entry["moves"].setdefault(uci, {})[game["id"]] = ply
                seen.add(key)
            board.push(move)
    return positions


def months_before(cutoff: int) -> tuple[list[str], int]:
    date = datetime.fromtimestamp(cutoff, UTC)
    index = date.year * 12 + date.month - 1
    months = [f"{(index - i) // 12:04d}/{(index - i) % 12 + 1:02d}" for i in range(12)]
    year, month = map(int, months[-1].split("/"))
    return months, int(datetime(year, month, 1, tzinfo=UTC).timestamp())


class Archives:
    def __init__(
        self,
        store: Store,
        user_agent: str,
        client: httpx.AsyncClient | None = None,
        *,
        deadline: float | None = None,
    ):
        self.store = store
        self.deadline = deadline
        self.client = client or httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            follow_redirects=False,
            timeout=10,
        )
        self.owns_client = client is None

    async def close(self) -> None:
        if self.owns_client:
            await self.client.aclose()

    async def fetch(self, url: str) -> tuple[dict, bool]:
        cached = self.store.archive(url)
        headers = {}
        if cached is not None:
            if cached["etag"]:
                headers["If-None-Match"] = cached["etag"]
            if cached["modified"]:
                headers["If-Modified-Since"] = cached["modified"]
        for attempt in range(2):
            try:
                async with self.client.stream("GET", url, headers=headers) as response:
                    if response.status_code == 304 and cached is not None:
                        self.store.save_archive(
                            url, json.loads(cached["payload"]), cached["etag"], cached["modified"]
                        )
                        return json.loads(cached["payload"]), False
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt == 0:
                            await asyncio.sleep(0.2)
                            continue
                    response.raise_for_status()
                    data = bytearray()
                    async for part in response.aiter_bytes():
                        data.extend(part)
                        if len(data) > MAX_RESPONSE_BYTES:
                            raise ValueError("Archive response exceeds size limit.")
                    payload = json.loads(data)
                    if not isinstance(payload, dict):
                        raise ValueError("Invalid archive response.")
                    self.store.save_archive(
                        url,
                        payload,
                        response.headers.get("etag"),
                        response.headers.get("last-modified"),
                    )
                    return payload, False
            except (httpx.HTTPError, ValueError):
                if cached is not None:
                    return json.loads(cached["payload"]), True
                if attempt == 1:
                    raise ValueError("Chess.com archive unavailable; no cached copy.") from None
        raise ValueError("Chess.com archive unavailable.")

    async def games(self, player: str, color: str, cutoff: int) -> tuple[list[dict], dict]:
        fetch_deadline = min(
            time.monotonic() + 40, self.deadline if self.deadline is not None else float("inf")
        )
        player = username(player)
        months, earliest = months_before(cutoff)
        listing_url = f"{API}/{player}/games/archives"
        async with asyncio.timeout(max(0.001, fetch_deadline - time.monotonic())):
            listing, stale = await self.fetch(listing_url)
        urls = listing.get("archives")
        if not isinstance(urls, list) or len(urls) > 1000:
            raise ValueError("Invalid archive list.")
        allowed = {f"{API}/{player}/games/{month}" for month in months}
        for url in urls:
            if (
                not isinstance(url, str)
                or urlparse(url).netloc != "api.chess.com"
                or re.fullmatch(
                    rf"{re.escape(API)}/{re.escape(player)}/games/\d{{4}}/(?:0[1-9]|1[0-2])", url
                )
                is None
            ):
                raise ValueError("Unsafe archive URL rejected.")
        records, unavailable = [], 0
        retrieval_times = []
        for url in sorted(set(urls) & allowed, reverse=True):
            try:
                async with asyncio.timeout(max(0.001, fetch_deadline - time.monotonic())):
                    archive, was_stale = await self.fetch(url)
                stale |= was_stale
                values = archive.get("games")
                if not isinstance(values, list):
                    raise ValueError("Invalid games archive.")
                records.extend(values)
                retrieval_times.append(self.store.archive(url)["fetched"])
            except (ValueError, TimeoutError):
                unavailable += 1
        games, excluded, unprocessed = await select_games_async(
            records,
            player,
            color,
            cutoff,
            earliest,
            fetch_deadline,
        )
        self.store.save_games(games)
        dates = [game["end_time"] for game in games]
        return games, {
            "count": len(games),
            "from": min(dates) if dates else None,
            "to": max(dates) if dates else None,
            "cutoff": cutoff,
            "time_controls": sorted({game["time_control"] for game in games}),
            "stale": stale,
            "unavailable_archives": unavailable,
            "excluded": excluded,
            "unprocessed_records": unprocessed,
            "archive_retrieved_at": retrieval_times,
            "note": "Rated standard rapid only; rapid is a proxy for ten-minute games. "
            "Chess.com published data may lag completed games.",
        }


# python-chess otherwise logs malformed PGNs, including private/untrusted text.
logging.getLogger("chess.pgn").addHandler(logging.NullHandler())
logging.getLogger("chess.pgn").propagate = False
