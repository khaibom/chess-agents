import re
from datetime import UTC, datetime

import chess

from chess_agents.engine import MIN_ISSUE_GAMES, MIN_POSITION_GAMES, legal_line
from chess_agents.games import source_url
from chess_agents.llm import InvalidOutput
from chess_agents.models import PrepDraft, ScoutDraft

MAX_REPORT = 3800


def notation(moves: list[str]) -> str:
    if not legal_line(moves):
        raise ValueError("Illegal report line.")
    return chess.Board().variation_san([chess.Move.from_uci(move) for move in moves])


def safe_prose(text: str) -> bool:
    return not re.search(
        r"[0-9]|https?://|\b(?:mate|checkmate|wins?|loses?|blunder|weakness|tactic|forced|always|never|guarantee)\b",
        text,
        re.IGNORECASE,
    )


def validate_scout(draft: ScoutDraft, facts: dict, problems: dict) -> None:
    for observation in draft.observations:
        if not safe_prose(observation.explanation):
            raise InvalidOutput("Unsupported numerical/tactical scout prose.")
        for evidence_id in observation.evidence_ids:
            source = (
                problems.get(evidence_id)
                if observation.kind == "repeated_problem"
                else facts.get(evidence_id)
            )
            if source is None or source["position_id"] != observation.position_id:
                raise InvalidOutput("Unsupported scout evidence reference.")
            if observation.kind == "repeated_problem" and (
                source["n"] < MIN_ISSUE_GAMES or source["total"] < MIN_POSITION_GAMES
            ):
                raise InvalidOutput("Unsupported repeated-problem threshold.")


def validate_candidate(candidate: dict, color: str) -> None:
    if not legal_line(candidate["moves"]):
        raise InvalidOutput("Illegal candidate line.")
    board = chess.Board()
    checks = {check["ply"]: check for check in candidate["checks"]}
    for ply, uci in enumerate(candidate["moves"]):
        if board.turn == (color == "white"):
            check = checks.get(ply)
            if check is None or check["move"] != uci or not check["accepted"]:
                raise InvalidOutput("Unchecked/unsound earlier user move.")
        board.push_uci(uci)


def branch(main: dict, alternative: dict, color: str) -> bool:
    board = chess.Board()
    for left, right in zip(main["moves"], alternative["moves"], strict=False):
        if left != right:
            return board.turn != (color == "white")
        board.push_uci(left)
    return False


def validate_prep(draft: PrepDraft, candidates: dict, facts: dict, color: str) -> None:
    ids = ([draft.main_id] if draft.main_id else []) + draft.deviation_ids
    ids += [draft.unfamiliar_id] if draft.unfamiliar_id else []
    if len(set(ids)) != len(ids):
        raise InvalidOutput("Duplicate prep candidate.")
    if any(cid not in candidates for cid in ids):
        raise InvalidOutput("Unknown prep candidate ID.")
    for cid in ids:
        validate_candidate(candidates[cid], color)
    if draft.main_id and not candidates[draft.main_id]["familiar"]:
        raise InvalidOutput("Main line must be confirmed familiar throughout.")
    if draft.deviation_ids and not draft.main_id:
        raise InvalidOutput("Deviations require a main line.")
    for cid in draft.deviation_ids:
        if not candidates[cid]["familiar"] or not branch(
            candidates[draft.main_id], candidates[cid], color
        ):
            raise InvalidOutput("Unsupported deviation relationship/familiarity.")
    if draft.unfamiliar_id and candidates[draft.unfamiliar_id]["familiar"]:
        raise InvalidOutput("Alternative must be explicitly unfamiliar.")
    if draft.remembered_id is not None and draft.remembered_id not in ids:
        raise InvalidOutput("Remembered position must belong to a selected candidate.")
    if draft.main_id and (draft.remembered_id is None or not draft.tip_evidence_ids):
        raise InvalidOutput("A full prep needs a remembered position and supported tip.")
    if not set(draft.tip_evidence_ids) <= facts.keys():
        raise InvalidOutput("Unsupported tip evidence.")
    relevant = {
        (fact["position_id"], fact["move"])
        for cid in ids
        for fact in candidates[cid].get("facts", [])
    }
    if any(
        (facts[eid]["position_id"], facts[eid]["move"]) not in relevant
        for eid in draft.tip_evidence_ids
    ):
        raise InvalidOutput("Tip evidence must describe a selected line's observed reply.")
    if not safe_prose(draft.explanation):
        raise InvalidOutput("Unsupported numerical/tactical prep prose.")


def fact_packet(tree: dict, color: str) -> dict[str, dict]:
    facts = {}
    for entry in sorted(tree.values(), key=lambda value: (-len(value["games"]), value["id"])):
        if len(entry["prefix"]) >= 16:
            continue
        board = chess.Board(entry["fen"])
        if board.turn == (color == "white"):
            continue
        for move, sources in sorted(entry["moves"].items(), key=lambda pair: -len(pair[1]))[:3]:
            identifier = entry["id"] + ":" + move
            facts[identifier] = {
                "id": identifier,
                "position_id": entry["id"],
                "move": move,
                "n": len(sources),
                "total": len(entry["games"]),
                "games": list(sources),
                "prefix": entry["prefix"],
            }
        if len(facts) >= 18:
            break
    return facts


def date_text(timestamp: int | None) -> str:
    return datetime.fromtimestamp(timestamp, UTC).date().isoformat() if timestamp else "none"


def render(result: dict) -> str:
    coverage = result.get("coverage", {})
    lines = [
        f"Pre-game prep: {result['opponent']} · you play {result['color']}",
        f"Status: {result['status']} · run {result['run_id'][:8]}",
        f"Coverage: {coverage.get('count', 0)} completed rated standard rapid games, "
        f"{date_text(coverage.get('from'))} to {date_text(coverage.get('to'))} UTC.",
        "Rapid is a proxy for ten-minute play; published data can lag.",
    ]
    if coverage.get("time_controls"):
        lines.append("Time controls: " + ", ".join(coverage["time_controls"][:5]) + " seconds.")
    selected = result.get("selected", {})
    main = selected.get("main")
    if main:
        lines.append("Familiar main line (conditional on your moves): " + notation(main["moves"]))
    else:
        lines.append("No checked familiar main line available.")
    for item in selected.get("deviations", []):
        lines.append("Likely deviation: " + notation(item["moves"]))
    alternative = selected.get("unfamiliar")
    if alternative:
        prefix = alternative["moves"][: alternative["unfamiliar_at"] + 1]
        lines.append(
            "Unfamiliar alternative, new at "
            + notation(prefix)
            + ": "
            + notation(alternative["moves"])
        )
    remember = selected.get("remember")
    if remember:
        lines.append(
            "Remember: after "
            + notation(remember["moves"])
            + "; "
            + remember["side_to_move"]
            + " to move."
        )
    facts = result.get("facts", {})
    tip_ids = result.get("tip_evidence_ids", [])
    for identifier in tip_ids[:1] or list(facts)[:1]:
        fact = facts[identifier]
        lines.append(
            f"Observed reply: {notation(fact['prefix'] + [fact['move']])} "
            f"({fact['n']}/{fact['total']} distinct games reaching this position)."
        )
        if tip_ids:
            lines.append(
                "Tip: review this observed reply before the game; its frequency depends on reaching the position."
            )
    if result.get("explanation"):
        lines.append("Explanation: " + result["explanation"])
    limits = list(dict.fromkeys(result.get("limitations", [])))
    if coverage.get("stale") or coverage.get("unavailable_archives"):
        limits.append("Partial/stale archive coverage.")
    if coverage.get("unprocessed_records"):
        limits.append(
            f"Partial normalization: {coverage['unprocessed_records']} records not processed within the data deadline."
        )
    limits.append("Engine checks use bounded search, not a proof of optimal play.")
    mandatory = "Limits: " + " ".join(item[:180] for item in limits[:6])
    links = []
    for fact in facts.values():
        for link in fact["games"]:
            if source_url(link) and link not in links:
                links.append(link)
    # Remove optional prose and links as whole units, preserving complete chess lines.
    body = "\n".join(lines + [mandatory])
    if len(body) > MAX_REPORT:
        lines = [line for line in lines if not line.startswith("Explanation:")]
        body = "\n".join(lines + [mandatory])
    for link in links[:3]:
        if len(body) + len(link) + 1 <= MAX_REPORT:
            body += "\n" + link
    if len(body) > MAX_REPORT:
        raise ValueError("Required report content exceeds message limit.")
    return body
