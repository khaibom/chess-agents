from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Color = Literal["white", "black"]
Choice = Literal["familiar", "learning", "exclude"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Line(Strict):
    id: str
    color: Color
    moves: list[str] = Field(max_length=16)
    games: list[str] = Field(default_factory=list)
    choice: Choice = "learning"
    reviewed: bool = False
    summary: str = "Code-derived; summary not generated."


class Profile(Strict):
    id: str
    username: str
    cutoff: int
    status: Literal["draft", "confirmed"] = "draft"
    lines: list[Line] = Field(default_factory=list, max_length=6)
    preferences: list[str] = Field(
        default_factory=lambda: [
            "Queen's Gambit",
            "Caro-Kann",
            "King's Indian Defence",
        ]
    )
    limitations: list[str] = Field(default_factory=list)


class Observation(Strict):
    kind: Literal["frequency", "repeated_problem"]
    position_id: str = Field(max_length=80)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)
    explanation: str = Field(max_length=200)


class ScoutDraft(Strict):
    observations: list[Observation] = Field(max_length=6)
    limitations: list[str] = Field(max_length=3)


class PrepDraft(Strict):
    main_id: str | None
    deviation_ids: list[str] = Field(max_length=2)
    unfamiliar_id: str | None
    remembered_id: str | None
    tip_evidence_ids: list[str] = Field(max_length=3)
    explanation: str = Field(max_length=250)


class ProfileSuggestion(Strict):
    line_id: str = Field(max_length=80)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)
    summary: str = Field(max_length=150)


class RepertoireDraft(Strict):
    suggestions: list[ProfileSuggestion] = Field(max_length=6)
