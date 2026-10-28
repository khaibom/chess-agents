import asyncio
import hashlib
import time
import uuid
from collections import defaultdict

from chess_agents.config import Settings, username
from chess_agents.games import Archives
from chess_agents.llm import InvalidOutput, Ledger, LLMUnavailable, Luna
from chess_agents.models import Line, Profile, RepertoireDraft
from chess_agents.report import safe_prose
from chess_agents.storage import Store


def infer_lines(games: dict[str, list[dict]]) -> list[Line]:
    lines = []
    for color, records in games.items():
        counts = defaultdict(list)
        for game in records:
            moves = tuple(game["moves"][:16])
            if moves:
                counts[moves].append(game["id"])
        for moves, sources in sorted(counts.items(), key=lambda pair: (-len(pair[1]), pair[0]))[:3]:
            identifier = hashlib.sha256((color + " ".join(moves)).encode()).hexdigest()[:16]
            lines.append(Line(id=identifier, color=color, moves=list(moves), games=sources))
    return lines


def validate_suggestions(draft: RepertoireDraft, profile: Profile) -> None:
    lines = {line.id: line for line in profile.lines}
    if len({item.line_id for item in draft.suggestions}) != len(draft.suggestions):
        raise InvalidOutput("Duplicate repertoire line reference.")
    for item in draft.suggestions:
        if item.line_id not in lines or not set(item.evidence_ids) <= set(
            lines[item.line_id].games
        ):
            raise InvalidOutput("Unknown repertoire line/evidence reference.")
        if not safe_prose(item.summary):
            raise InvalidOutput("Unsupported factual repertoire prose.")


async def build_profile(
    store: Store,
    settings: Settings,
    player: str,
    *,
    archives=None,
    llm_factory=Luna,
    cutoff: int | None = None,
) -> Profile:
    player = username(player)
    cutoff = cutoff if cutoff is not None else int(time.time())
    run_id = store.start_run("profile", {"username": player}, cutoff)
    budget = Ledger(store, run_id, settings.budget)
    profile = Profile(id=uuid.uuid4().hex, username=player, cutoff=cutoff)
    deadline = time.monotonic() + settings.seconds
    api = archives or Archives(
        store, settings.user_agent, deadline=min(time.monotonic() + 44, deadline - 0.005)
    )
    luna = llm_factory(settings, budget, deadline)
    try:
        records = {}
        async with asyncio.timeout(min(45, settings.seconds * 0.6)):
            for color in ("white", "black"):
                try:
                    records[color], coverage = await api.games(player, color, cutoff)
                except ValueError:
                    profile.limitations.append(f"{color}: source unavailable.")
                    continue
                profile.lines = infer_lines(records)
                if coverage.get("stale") or coverage.get("unavailable_archives"):
                    profile.limitations.append(f"{color}: partial/stale source coverage.")
                if coverage.get("unprocessed_records"):
                    profile.limitations.append(
                        f"{color}: game normalization stopped at the data deadline; partial evidence."
                    )
        profile.lines = infer_lines(records)
        if not profile.lines:
            profile.limitations.append(
                "No usable completed rated standard rapid games; no known variations."
            )
        else:
            packet = {
                "lines": [
                    line.model_dump(exclude={"summary", "choice", "reviewed"})
                    for line in profile.lines
                ],
                "preferences": profile.preferences,
            }
            for line in packet["lines"]:
                line["games"] = line["games"][:10]
            for attempt in range(2):
                try:
                    draft = await luna.generate(
                        "repertoire",
                        packet,
                        RepertoireDraft,
                        correction="Use only supplied references." if attempt else None,
                    )
                    validate_suggestions(draft, profile)
                    summaries = {item.line_id: item.summary for item in draft.suggestions}
                    for line in profile.lines:
                        if line.id in summaries:
                            line.summary = summaries[line.id]
                    break
                except InvalidOutput:
                    if attempt:
                        profile.limitations.append(
                            "LLM summary rejected; review code-derived lines."
                        )
    except (LLMUnavailable, ValueError, TimeoutError):
        profile.limitations.append(
            "Source/summary unavailable; review available code-derived evidence."
        )
    finally:
        await luna.close()
        if archives is None:
            await api.close()
    store.save_profile(profile)
    store.finish(
        run_id,
        {"profile": profile.model_dump(), "accounting": budget.summary()},
        "partial" if profile.limitations else "complete",
        profile.limitations,
    )
    return profile


def set_line(
    store: Store, line_id: str, choice: str, expected_profile: str | None = None
) -> Profile:
    if choice not in ("familiar", "learning", "exclude"):
        raise ValueError("Choose familiar, learning, or exclude.")
    draft = store.profile("draft")
    if draft is None:
        confirmed = store.profile("confirmed")
        if confirmed is None:
            raise ValueError("Build a profile first.")
        draft = confirmed.model_copy(deep=True)
        draft.id, draft.status = uuid.uuid4().hex, "draft"
    if expected_profile is not None and draft.id != expected_profile:
        raise ValueError("This review is stale; open /profile again.")
    matching = [line for line in draft.lines if line.id == line_id]
    if not matching:
        raise ValueError("Unknown line ID in current draft.")
    matching[0].choice, matching[0].reviewed = choice, True
    store.save_profile(draft)
    return draft


def confirm(store: Store, expected_profile: str | None = None) -> Profile:
    draft = store.profile("draft")
    if draft is None:
        raise ValueError("No draft to confirm.")
    if expected_profile is not None and expected_profile != draft.id:
        raise ValueError("This review is stale; open /profile again.")
    if not any(line.reviewed for line in draft.lines):
        raise ValueError("Review at least one line before confirming.")
    draft.status = "confirmed"
    store.save_profile(draft)
    return draft


def require_profile(store: Store) -> Profile:
    profile = store.profile("confirmed")
    if profile is None:
        raise ValueError("Build, review, and confirm a profile before preparation.")
    return profile
