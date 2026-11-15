"""Pipeline stages. Each stage is `run(ctx)`: read inputs from the decision log, call the LLM /
tools, write decisions + artifacts back. Because stages never pass data in memory, any stage can be
re-run alone later (partial rebuild)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..config import Settings
from ..db import Store
from ..llm import LLM
from ..search import Search


@dataclass
class Ctx:
    run_id: str
    prompt: str
    store: Store
    llm: LLM                 # the Engineer (builder) model chosen for this run
    search: Search
    settings: Settings
    reviewer: LLM | None = None   # the Bar Raiser model chosen for this run
    rebuilding: bool = False
    revisions: dict[str, list[dict]] = field(default_factory=dict)  # stage -> [{key, source, note}]
    precedents: list[dict] = field(default_factory=list)             # similar past projects (with score)
    clarifications: list[dict] = field(default_factory=list)         # [{q, a}] answered in step 1

    def qa_block(self) -> str:
        if not self.clarifications:
            return ""
        return "\nClarifications from the user:\n" + "\n".join(
            f"- Q: {c.get('q')}  A: {c.get('a')}" for c in self.clarifications)

    def precedent_block(self, stage: str) -> str:
        from ..precedents import builder_block

        return builder_block(self.precedents, stage)

    def revision_block(self, stage: str) -> str:
        """Extra prompt text when a stage is re-run in a rebuild: the change requests aimed at it, plus
        its current decisions so the model keeps everything that doesn't need to change."""
        if not self.rebuilding:
            return ""
        out = []
        reqs = self.revisions.get(stage, [])
        if reqs:
            out.append("CHANGE REQUESTS FOR THIS STAGE (must be applied):")
            out += [f"- [{r['source']} on {r['key']}] {r['note']}" for r in reqs]
        else:
            out.append("An upstream decision changed. Update only what that change requires.")
        current = self.store.active(self.run_id, stage=stage)
        if current:
            out.append("CURRENT DECISIONS FOR THIS STAGE (keep them exactly unless a change request or "
                       "upstream change requires otherwise):")
            out += [f"- {d['key']}: {d['title']} {json.dumps(d['choice'], default=str, ensure_ascii=False)[:400]}" for d in current]
        return "\n\n" + "\n".join(out)

    def emit(self, stage: str, message: str, level: str = "info") -> None:
        self.store.log_event(self.run_id, stage, message, level)
        print(f"[{stage}] {level.upper()}: {message}", flush=True)


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")
    return s or "x"


def requirements_brief(ctx: Ctx) -> str:
    """Compact text view of the active requirement decisions, used as context by later stages."""
    lines = [f'User request: "{ctx.prompt}"']
    lines += [f"- user clarified: {c.get('q')} → {c.get('a')}" for c in ctx.clarifications]
    for d in ctx.store.active(ctx.run_id, stage="requirements"):
        c = d["choice"]
        if d["kind"] == "feature":
            lines.append(f"- feature {d['key'].split('.')[-1]}: {c.get('description')} ({c.get('type')})")
        elif d["kind"] == "budget":
            lines.append(f"- budget: £{c.get('gbp')} (hard limit for the whole cart)")
        elif d["kind"] == "summary":
            lines.append(f"Project: {c.get('project_name')}: {c.get('summary')}")
        else:
            lines.append(f"- {d['kind']}: {c}")
    return "\n".join(lines)


def confidence_list() -> dict:
    return {"type": "number", "minimum": 0, "maximum": 1, "description": "0..1 confidence in this choice"}


ALTERNATIVES_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"option": {"type": "string"}, "reason_rejected": {"type": "string"}},
        "required": ["option", "reason_rejected"],
    },
}
