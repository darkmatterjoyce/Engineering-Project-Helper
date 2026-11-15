"""Decision log schema.

Everything the agent decides is a `Decision` with a stable `key` (e.g. "part.soil_sensor").
Decisions are versioned and immutable: changing one inserts version N+1 and marks N superseded.
`depends_on` pins the exact *version* of each upstream decision used, so staleness is a pure
data question: a decision is stale when any dependency now has a newer active version.
That is what makes partial rebuilds (and Bar Raiser challenges) cheap later.

See docs/SCHEMA.md for the full write-up.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(timezone.utc)


Stage = Literal["requirements", "parts", "wiring", "procurement", "firmware", "diagram", "guide", "review"]

STAGE_ORDER: list[str] = ["requirements", "parts", "wiring", "procurement", "firmware", "diagram", "guide"]


class DecisionStatus(str, Enum):
    active = "active"            # current truth
    superseded = "superseded"    # replaced by a newer version of the same key
    invalidated = "invalidated"  # an upstream change made it stale; awaiting rebuild
    rejected = "rejected"        # Bar Raiser / user rejected it
    retired = "retired"          # dropped by a rebuild (e.g. the OLED after swapping to an LCD)


class Author(str, Enum):
    agent = "agent"
    bar_raiser = "bar_raiser"
    user = "user"
    precedent = "precedent"      # copied/adapted from a stored precedent


class DepRef(BaseModel):
    key: str
    version: int


class Evidence(BaseModel):
    source: str                  # "linkup" | "tavily" | "arduino-cli" | "validator" | ...
    url: str | None = None
    title: str | None = None
    snippet: str | None = None
    retrieved_at: datetime = Field(default_factory=now)


class Alternative(BaseModel):
    option: str
    reason_rejected: str = ""


class Review(BaseModel):
    """Written by the Bar Raiser onto the decision version it reviewed.

    Flow per challenge: (A) blind review on results only → (B) Bar Raiser reads the builder's rationale
    (the logbook) and keeps / downgrades / withdraws → (C) the engineer accepts or rejects with a reason
    (it keeps the final say) → (D) the user judges rejected high/medium challenges (→ precedent)."""
    verdict: Literal["approve", "challenge", "reject"]
    severity: Literal["low", "medium", "high"] = "low"
    notes: str = ""
    suggested_fix: str = ""
    criteria: list[str] = Field(default_factory=list)   # acceptance criteria ids the challenge is based on
    reviewer: str = "bar_raiser"
    model: str = ""
    round: int = 1
    at: datetime = Field(default_factory=now)
    # (B) after reading the logbook
    blind_severity: Literal["low", "medium", "high"] | None = None
    after_logbook: Literal["kept", "downgraded", "withdrawn"] | None = None
    after_logbook_note: str = ""
    # (C) engineer's reflection
    engineer_response: Literal["accept", "reject"] | None = None
    engineer_reason: str = ""
    # (D) the user's verdict on a disagreement
    user_verdict: Literal["bar_raiser_right", "engineer_right", "neither"] | None = None
    user_reason: str = ""


class AcceptanceCriterion(BaseModel):
    """Written by the Bar Raiser from the prompt + Q&A BEFORE any design exists; never shown to the builder.
    Scored against the final delivery."""
    id: str
    criterion: str
    why: str = ""
    priority: Literal["must", "should"] = "must"
    source: Literal["proposal", "improvement"] = "proposal"
    status: Literal["met", "partial", "not_met", "unverified"] | None = None
    note: str = ""


class Trigger(BaseModel):
    """Why a decision version exists, when it came from a rebuild."""
    source: Literal["user", "bar_raiser"]
    key: str                     # the decision the change request was about
    note: str


class Decision(BaseModel):
    run_id: str
    key: str                     # stable slug, unique per run: "<namespace>.<id>[.<sub>]"
    version: int = 1             # assigned by the store
    stage: Stage
    kind: str                    # feature | power | part | pin | support_components | firmware | diagram ...
    title: str                   # one-line human summary
    choice: Any                  # the decision payload (stage-specific JSON)
    rationale: str = ""
    alternatives: list[Alternative] = Field(default_factory=list)
    depends_on: list[DepRef] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(0.7, ge=0.0, le=1.0)
    status: DecisionStatus = DecisionStatus.active
    author: Author = Author.agent
    review: Review | None = None
    trigger: Trigger | None = None  # set on versions produced by a rebuild
    precedent_ids: list[str] = Field(default_factory=list)  # precedents shown to the model when deciding this
    created_at: datetime = Field(default_factory=now)


class RunEvent(BaseModel):
    at: datetime = Field(default_factory=now)
    stage: str
    level: Literal["info", "warn", "error"] = "info"
    message: str


class Run(BaseModel):
    prompt: str
    clarifications: list[dict] = Field(default_factory=list)          # [{q, a}] from step 1
    acceptance_criteria: list[dict] = Field(default_factory=list)     # AcceptanceCriterion dicts
    delivery_status: Literal["pending", "delivered", "delivered_with_gaps"] = "pending"
    gaps: list[str] = Field(default_factory=list)
    improvements: list[dict] = Field(default_factory=list)            # [{at, feedback, targets, lesson}]
    status: Literal["queued", "running", "done", "failed"] = "queued"
    current_stage: str | None = None
    model: str = ""
    providers: dict[str, str] = Field(default_factory=dict)
    events: list[RunEvent] = Field(default_factory=list)
    review_status: Literal["pending", "approved", "approved_with_notes", "disagreements", "judged", "unresolved", "off"] = "pending"
    review_rounds: list[dict] = Field(default_factory=list)  # [{round, group, summary, risk_level, challenges}]
    rebuilds: list[dict] = Field(default_factory=list)       # [{at, source, targets, stages, changed, retired}]
    precedents: list[dict] = Field(default_factory=list)     # [{id, prompt, project_name, score}] retrieved at start
    models: dict = Field(default_factory=dict)               # {engineer, engineer_label, bar_raiser, bar_raiser_label}
    precedent_search: str = ""                               # "atlas $vectorSearch" | "local cosine"
    error: str | None = None
    created_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None


ArtifactKind = Literal["parts_list", "wiring", "cart", "firmware", "compile_log", "flow_diagram", "guide"]


class Artifact(BaseModel):
    run_id: str
    kind: ArtifactKind
    version: int = 1
    content: Any
    built_from: list[DepRef] = Field(default_factory=list)  # decision versions it was rendered from
    created_at: datetime = Field(default_factory=now)
