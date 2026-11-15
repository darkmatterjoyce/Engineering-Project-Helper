"""Stage 5: Mermaid code-flow diagram of the firmware, plus the code walkthrough that links them:
an explanation for every block of the sketch and the line range behind every diagram node."""
from __future__ import annotations

import hashlib
import re

from .. import codemap
from ..schema import Decision
from . import Ctx

STAGE = "diagram"

SYSTEM = """You turn an Arduino sketch into a clear Mermaid flowchart of its control flow.
Rules: start with `flowchart TD`. Show setup() steps, then the loop() with its timed branches and
decisions (diamonds), and the hardware each step touches. 8-25 nodes. Node ids are alphanumeric;
put labels in double quotes, e.g. A["Read soil sensor (GPIO34)"]. No markdown fences, no styling, no HTML.

You also write the walkthrough a student sees when they click on the code:
- blocks: for EVERY block id listed, a one-line summary and a 2-4 sentence plain-English explanation of what
  it does and why (name the hardware and GPIO pins it touches, units, timing, and any gotcha).
- steps: for EVERY diagram node, the line range (numbers from the numbered listing) of the code that
  implements it, and one sentence on how those lines do it. Use tight ranges (usually 1-6 lines)."""

SCHEMA = {
    "type": "object",
    "properties": {
        "mermaid": {"type": "string"},
        "explanation": {"type": "string", "description": "3-5 sentences"},
        "blocks": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "summary": {"type": "string"}, "explanation": {"type": "string"}},
            "required": ["id", "summary", "explanation"]}},
        "steps": {"type": "array", "items": {"type": "object", "properties": {
            "node_id": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"},
            "explanation": {"type": "string"}}, "required": ["node_id", "start_line", "end_line", "explanation"]}},
    },
    "required": ["mermaid", "explanation", "blocks", "steps"],
}


def sanitize(m: str) -> str:
    m = re.sub(r"^```(?:mermaid)?\s*|\s*```$", "", m.strip())
    if not re.match(r"^(flowchart|graph)\s", m):
        m = "flowchart TD\n" + m
    return m


def run(ctx: Ctx) -> None:
    fw = ctx.store.latest_artifacts(ctx.run_id).get("firmware")
    if not fw:
        raise RuntimeError("no firmware artifact to diagram")
    code = fw["content"]["code"]
    block_list = "\n".join(f"- {b['id']}: {b['name']} (lines {b['start']}-{b['end']})" for b in codemap.blocks(code))
    out = ctx.llm.call_tool(system=SYSTEM,
                            prompt=f"NUMBERED SKETCH:\n{codemap.numbered(code)}\n\nBLOCKS TO EXPLAIN:\n{block_list}"
                                   + ctx.revision_block(STAGE),
                            tool_name="flow_diagram", tool_description="Record the code flow diagram and walkthrough.",
                            schema=SCHEMA, max_tokens=8000)
    mermaid = sanitize(out["mermaid"])
    walk = codemap.build_walkthrough(code, mermaid, out.get("blocks", []), out.get("steps", []))
    unmapped = sorted(set(walk["nodes"]) - {st["node_id"] for st in walk["steps"]})
    if unmapped:
        ctx.emit(STAGE, f"{len(unmapped)} diagram step(s) not linked to code lines: {', '.join(unmapped)}", "warn")
    ctx.store.record(Decision(
        run_id=ctx.run_id, key="diag.flow", stage=STAGE, kind="diagram",
        title=f"Code flow diagram ({mermaid.count(chr(10))} lines)", choice={"mermaid": mermaid},
        rationale=out["explanation"], depends_on=ctx.store.refs(ctx.run_id, ["fw.sketch"]), confidence=0.85,
    ))
    ctx.store.save_artifact(ctx.run_id, "flow_diagram", {"mermaid": mermaid, "explanation": out["explanation"],
                                                         "walkthrough": walk,
                                                         "code_sha": hashlib.sha256(code.encode()).hexdigest()},
                            ctx.store.refs(ctx.run_id, ["diag.flow"]))
    ctx.emit(STAGE, "Code flow diagram saved")
