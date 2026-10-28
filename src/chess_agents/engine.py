import asyncio
import hashlib
import time

import chess
import chess.engine

from chess_agents.games import position_id, position_key
from chess_agents.models import Profile

MAX_POSITIONS = 40
ENGINE_SECONDS = 90
SEARCH_SECONDS = 0.15
SEARCH_DEPTH = 12
MAX_LOSS_CP = 50
MISTAKE_CP = 100
MIN_POSITION_GAMES = 5
MIN_ISSUE_GAMES = 3


def score_record(score: chess.engine.PovScore) -> dict:
    white = score.white()
    return {"cp_white": white.score(), "mate_white": white.mate()}


def move_loss(root: dict, played: dict, mover: chess.Color) -> tuple[int | None, bool]:
    direction = 1 if mover else -1
    root_mate, played_mate = root["mate_white"], played["mate_white"]
    losing_mate = played_mate is not None and played_mate * direction <= 0
    root_losing = root_mate is not None and root_mate * direction <= 0
    if losing_mate:
        return None, True
    if (
        root_mate is not None
        and played_mate is not None
        and root_mate * direction > 0
        and played_mate * direction > 0
    ):
        return 0, False
    if root_mate is not None and root_mate * direction > 0 and played_mate is None:
        return MISTAKE_CP, False
    if root_losing or root["cp_white"] is None or played["cp_white"] is None:
        return None, False
    return max(0, (root["cp_white"] - played["cp_white"]) * direction), False


class Engine:
    def __init__(self, path: str, deadline: float):
        self.path = path
        self.deadline = min(deadline, time.monotonic() + ENGINE_SECONDS)
        self.transport = None
        self.protocol = None
        self.cache = {}
        self.positions = set()
        self.version = "unstarted"

    async def __aenter__(self):
        try:
            async with asyncio.timeout(max(0.001, min(5, self.deadline - time.monotonic()))):
                self.transport, self.protocol = await chess.engine.popen_uci(self.path)
                await self.protocol.configure({"Threads": 1, "Hash": 128})
                self.version = self.protocol.id.get("name", "unknown Stockfish")
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *_args):
        await self.close()

    async def close(self) -> None:
        if self.protocol is not None:
            try:
                await asyncio.wait_for(self.protocol.quit(), 1)
            except (TimeoutError, chess.engine.EngineError, ProcessLookupError):
                pass
        if self.transport is not None:
            if self.transport.get_returncode() is None:
                self.transport.kill()
                if self.protocol is not None and hasattr(self.protocol, "returncode"):
                    await asyncio.wait_for(asyncio.shield(self.protocol.returncode), 1)
            self.transport.close()
        self.protocol = None

    async def analyze(self, board: chess.Board, move: chess.Move | None = None) -> dict:
        if not board.is_valid() or len(board.move_stack) > 24:
            raise ValueError("Invalid/oversized engine position.")
        if move is not None and move not in board.legal_moves:
            raise ValueError("Illegal engine candidate.")
        key = (board.fen(), move.uci() if move else None)
        if key in self.cache:
            return self.cache[key]
        position = position_key(board)
        if position not in self.positions and len(self.positions) >= MAX_POSITIONS:
            raise ValueError("Engine position budget exhausted.")
        remaining = self.deadline - time.monotonic()
        if remaining <= SEARCH_SECONDS or self.protocol is None:
            raise TimeoutError("Engine time exhausted.")
        self.positions.add(position)
        async with asyncio.timeout(remaining):
            info = await self.protocol.analyse(
                board,
                chess.engine.Limit(time=SEARCH_SECONDS, depth=SEARCH_DEPTH),
                root_moves=[move] if move else None,
            )
        value = {
            "id": hashlib.sha256(repr(key).encode()).hexdigest()[:16],
            "fen": board.fen(),
            "prefix": [m.uci() for m in board.move_stack],
            "move": move.uci() if move else None,
            **score_record(info["score"]),
            "best_move": info["pv"][0].uci() if info.get("pv") else None,
            "depth": info.get("depth"),
            "engine": self.version,
            "settings": {
                "threads": 1,
                "hash_mib": 128,
                "seconds": SEARCH_SECONDS,
                "max_depth": SEARCH_DEPTH,
            },
        }
        self.cache[key] = value
        return value

    async def check(self, board: chess.Board, move: chess.Move) -> dict:
        root = await self.analyze(board)
        played = await self.analyze(board, move)
        loss, mate = move_loss(root, played, board.turn)
        root_losing_mate = (
            root["mate_white"] is not None and root["mate_white"] * (1 if board.turn else -1) <= 0
        )
        return {
            "position_id": position_id(board),
            "move": move.uci(),
            "loss_cp": loss,
            "losing_mate": mate,
            "mate_transition": mate and not root_losing_mate,
            "accepted": not mate and loss is not None and loss <= MAX_LOSS_CP,
            "root": root,
            "played": played,
        }


def familiar_moves(profile: Profile, color: str) -> dict[str, list[str]]:
    known = {}
    for line in profile.lines:
        if line.color != color or line.choice != "familiar":
            continue
        board = chess.Board()
        for uci in line.moves:
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves:
                raise ValueError("Illegal confirmed profile line.")
            if board.turn == (color == "white"):
                options = known.setdefault(position_id(board), [])
                if uci not in options:
                    options.append(uci)
            board.push(move)
    return known


def legal_line(moves: list[str]) -> bool:
    board = chess.Board()
    try:
        for uci in moves:
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves:
                return False
            board.push(move)
    except ValueError:
        return False
    return bool(moves) and len(moves) <= 16


async def candidate_line(
    known: dict,
    tree: dict,
    color: str,
    engine,
    forced: tuple[str, str] | None = None,
    allow_unfamiliar: bool = False,
) -> dict | None:
    board = chess.Board()
    moves, checks, facts = [], [], []
    unfamiliar_at = None
    while len(moves) < 16 and not board.is_game_over():
        pid = position_id(board)
        if board.turn == (color == "white"):
            options = known.get(pid, [])
            if options:
                uci = options[0]
            elif allow_unfamiliar and unfamiliar_at is None:
                result = await engine.analyze(board)
                uci = result["best_move"]
                if uci is None:
                    break
                unfamiliar_at = len(moves)
            else:
                break
            move = chess.Move.from_uci(uci)
            check = await engine.check(board, move)
            if not check["accepted"]:
                return None
            checks.append({"ply": len(moves), **check})
        else:
            entry = tree.get(pid)
            if entry is None or not entry["moves"]:
                break
            ranked = sorted(entry["moves"], key=lambda u: (-len(entry["moves"][u]), u))
            uci = forced[1] if forced is not None and forced[0] == pid else ranked[0]
            if uci not in entry["moves"]:
                return None
            move = chess.Move.from_uci(uci)
            facts.append(
                {
                    "position_id": pid,
                    "move": uci,
                    "n": len(entry["moves"][uci]),
                    "total": len(entry["games"]),
                    "games": list(entry["moves"][uci]),
                    "ply": len(moves),
                }
            )
        if move not in board.legal_moves:
            raise ValueError("Illegal candidate prefix.")
        moves.append(uci)
        board.push(move)
        if unfamiliar_at is not None and len(moves) >= unfamiliar_at + 2:
            break
    if not checks or not facts or not legal_line(moves):
        return None
    cid = hashlib.sha256(" ".join(moves).encode()).hexdigest()[:16]
    return {
        "id": cid,
        "moves": moves,
        "checks": checks,
        "facts": facts,
        "familiar": unfamiliar_at is None,
        "unfamiliar_at": unfamiliar_at,
        "final_fen": board.fen(),
        "side_to_move": "white" if board.turn else "black",
    }


async def construct_candidates(profile: Profile, tree: dict, color: str, engine) -> list[dict]:
    known = familiar_moves(profile, color)
    main = await candidate_line(known, tree, color, engine)
    result = [main] if main else []
    if main is not None:
        for fact in main["facts"]:
            entry = tree[fact["position_id"]]
            alternatives = sorted(
                (move for move in entry["moves"] if move != fact["move"]),
                key=lambda move: (-len(entry["moves"][move]), move),
            )
            for move in alternatives[:1]:
                deviation = await candidate_line(known, tree, color, engine, (entry["id"], move))
                if deviation and deviation["id"] not in {item["id"] for item in result}:
                    result.append(deviation)
            if len(result) == 3:
                break
    alternative = await candidate_line(known, tree, color, engine, allow_unfamiliar=True)
    if alternative and not alternative["familiar"]:
        result.append(alternative)
    return result


async def problem_evidence(tree: dict, color: str, engine) -> dict[str, dict]:
    problems = {}
    for entry in sorted(tree.values(), key=lambda item: -len(item["games"])):
        board = chess.Board()
        for uci in entry["prefix"]:
            board.push_uci(uci)
        if board.turn == (color == "white") or len(entry["games"]) < MIN_POSITION_GAMES:
            continue
        for uci, games in entry["moves"].items():
            if len(games) < MIN_ISSUE_GAMES:
                continue
            check = await engine.check(board, chess.Move.from_uci(uci))
            if check.get("mate_transition", False) or (
                check["loss_cp"] is not None and check["loss_cp"] >= MISTAKE_CP
            ):
                eid = entry["id"] + ":" + uci
                problems[eid] = {
                    "id": eid,
                    "position_id": entry["id"],
                    "move": uci,
                    "games": list(games),
                    "n": len(games),
                    "total": len(entry["games"]),
                    "check": check,
                }
    return problems
