"""Stage 1: one sentence → structured requirements (features, power, connectivity, assumptions)."""
from __future__ import annotations

from ..schema import Alternative, Decision
from . import ALTERNATIVES_SCHEMA, Ctx, confidence_list, slug

STAGE = "requirements"

SYSTEM = """You are the requirements engineer of an autonomous hardware-project agent.
Turn a one-sentence idea into a minimal, buildable ESP32 project spec for a student with a breadboard.
Rules: the MCU is always an ESP32 DevKit V1. Prefer the smallest feature set that satisfies the idea.
The user has already answered up to 2 clarifying questions (if any); use their answers and make every
other assumption explicit. Respect every explicit user requirement (e.g. "a camera", "four wheels").
If the user stated a budget, return it as budget_gbp. Feature ids are short snake_case."""

SCHEMA = {
    "type": "object",
    "properties": {
        "project_name": {"type": "string"},
        "summary": {"type": "string", "description": "2 sentences"},
        "features": {
            "type": "array", "minItems": 1, "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                    "type": {"type": "string", "enum": ["sense", "actuate", "display", "input", "comms", "control", "storage"]},
                    "rationale": {"type": "string"},
                    "confidence": confidence_list(),
                },
                "required": ["id", "description", "type", "rationale", "confidence"],
            },
        },
        "power": {
            "type": "object",
            "properties": {"source": {"type": "string"}, "notes": {"type": "string"},
                           "alternatives": ALTERNATIVES_SCHEMA},
            "required": ["source", "notes"],
        },
        "connectivity": {
            "type": "object",
            "properties": {"wifi": {"type": "boolean"}, "ble": {"type": "boolean"},
                           "notes": {"type": "string"}},
            "required": ["wifi", "ble", "notes"],
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "budget_gbp": {"type": ["number", "null"], "description": "user's stated total budget in GBP, else null"},
    },
    "required": ["project_name", "summary", "features", "power", "connectivity", "assumptions"],
}


def run(ctx: Ctx) -> None:
    out = ctx.llm.call_tool(
        system=SYSTEM,
        prompt=f'Project idea: "{ctx.prompt}"' + ctx.qa_block() + "\nProduce the spec." + ctx.precedent_block(STAGE) + ctx.revision_block(STAGE),
        tool_name="record_requirements",
        tool_description="Record the structured project requirements.",
        schema=SCHEMA,
    )
    rec = ctx.store.record
    rec(Decision(run_id=ctx.run_id, key="req.summary", stage=STAGE, kind="summary",
                 title=out["project_name"],
                 choice={"project_name": out["project_name"], "summary": out["summary"]},
                 rationale="Derived from the user's one-sentence idea.", confidence=0.9))
    for f in out["features"]:
        fid = slug(f["id"])
        rec(Decision(run_id=ctx.run_id, key=f"req.feature.{fid}", stage=STAGE, kind="feature",
                     title=f["description"], choice={"description": f["description"], "type": f["type"]},
                     rationale=f.get("rationale", ""), confidence=float(f.get("confidence", 0.8))))
    p = out["power"]
    rec(Decision(run_id=ctx.run_id, key="req.power", stage=STAGE, kind="power", title=f"Power: {p['source']}",
                 choice={"source": p["source"], "notes": p["notes"]}, rationale=p["notes"],
                 alternatives=[Alternative(**a) for a in p.get("alternatives", [])], confidence=0.8))
    c = out["connectivity"]
    rec(Decision(run_id=ctx.run_id, key="req.connectivity", stage=STAGE, kind="connectivity",
                 title=f"Wi-Fi {'on' if c['wifi'] else 'off'}, BLE {'on' if c['ble'] else 'off'}",
                 choice={"wifi": bool(c["wifi"]), "ble": bool(c["ble"])}, rationale=c["notes"], confidence=0.85))
    rec(Decision(run_id=ctx.run_id, key="req.assumptions", stage=STAGE, kind="assumptions",
                 title=f"{len(out['assumptions'])} assumptions", choice=out["assumptions"],
                 rationale="Made explicit so the reviewer can challenge them.", confidence=0.7))
    if out.get("budget_gbp"):
        rec(Decision(run_id=ctx.run_id, key="req.budget", stage=STAGE, kind="budget",
                     title=f"Budget £{float(out['budget_gbp']):.2f}", choice={"gbp": float(out["budget_gbp"])},
                     rationale="Stated by the user.", confidence=0.95))
    ctx.emit(STAGE, f"{out['project_name']}: {len(out['features'])} features, power={p['source']}, wifi={c['wifi']}")


REFRESH_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string", "description": "2 sentences, same style as before"}},
    "required": ["summary"],
}


def refresh_summary(ctx: Ctx) -> None:
    """After a rebuild changed the parts, rewrite the 2-sentence summary so it doesn't describe parts that
    were removed (e.g. still saying 'OLED' after the user switched to an LCD)."""
    cur = ctx.store.get_active(ctx.run_id, "req.summary")
    if not cur:
        return
    parts = [f"- {d['choice']['name']} ({d['choice']['model']})" for d in ctx.store.active(ctx.run_id, prefix="part.")]
    out = ctx.llm.call_tool(
        system="Update a project summary so it matches the current parts list. Keep everything else the same.",
        prompt=f"Project: {cur['choice'].get('project_name')}\nCurrent summary: {cur['choice'].get('summary')}\n"
               f"Current parts:\n" + "\n".join(parts),
        tool_name="refresh_summary", tool_description="Record the updated summary.", schema=REFRESH_SCHEMA,
        max_tokens=1500)
    new = {**cur["choice"], "summary": out["summary"].strip()}
    if new["summary"] and new != cur["choice"]:
        ctx.store.record(Decision(run_id=ctx.run_id, key="req.summary", stage=STAGE, kind="summary",
                                  title=cur["title"], choice=new, rationale="Updated after the parts changed.",
                                  confidence=cur["confidence"]))
        ctx.emit(STAGE, "summary updated to match the new parts")
