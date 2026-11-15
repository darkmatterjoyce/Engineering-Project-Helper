"""The builder's voice in the review: it answers every standing Bar Raiser challenge (accept / reject with
a reason). It keeps the final say; accepted challenges become rebuild targets, rejected high/medium ones
become disagreements for the user to judge. Also maps free-text "improve project" feedback onto decisions."""
from __future__ import annotations

from .bar_raiser import _decision_text
from .stages import Ctx, requirements_brief

SYSTEM = """You are the engineer who built this ESP32 project. An independent Bar Raiser reviewed your
results, then read your rationale, and still raises the challenges below. Answer EVERY one: accept (you
will change the design) or reject (you keep it) with a one-sentence reason. You keep the final decision,
but take each point seriously: reject only when you can say why the user is better served as it is."""

RESPOND_SCHEMA = {
    "type": "object",
    "properties": {"responses": {"type": "array", "items": {
        "type": "object",
        "properties": {"key": {"type": "string"}, "response": {"type": "string", "enum": ["accept", "reject"]},
                       "reason": {"type": "string"}},
        "required": ["key", "response", "reason"]}}},
    "required": ["responses"],
}

MAP_SYSTEM = """Map a user's change request for a delivered ESP32 project onto the decision keys it directly
changes (usually 1-2). Pick the most specific key (e.g. part.oled rather than req.summary). Write a short
instruction for each."""

MAP_SCHEMA = {
    "type": "object",
    "properties": {"targets": {"type": "array", "minItems": 1, "maxItems": 3, "items": {
        "type": "object", "properties": {"key": {"type": "string"}, "note": {"type": "string"}},
        "required": ["key", "note"]}}},
    "required": ["targets"],
}


def respond(ctx: Ctx, llm, standing: list[dict]) -> list[dict]:
    """Returns rebuild targets for the accepted challenges."""
    if not standing:
        return []
    by_key = {d["key"]: d for d in ctx.store.active(ctx.run_id)}
    blocks = []
    for c in standing:
        d = by_key.get(c["key"])
        if d:
            blocks.append(f"CHALLENGE [{c['severity']}] on {c['key']}: {c['notes']}\n  suggested fix: "
                          f"{c.get('suggested_fix', '')}\n" + _decision_text(d, blind=False))
    out = llm.call_tool(system=SYSTEM, tool_name="respond_to_review", tool_description="Answer every challenge.",
                        schema=RESPOND_SCHEMA, max_tokens=2500,
                        prompt=requirements_brief(ctx) + "\n\n" + "\n\n".join(blocks))
    got = {r["key"]: r for r in out.get("responses", [])}
    targets = []
    for c in standing:
        r = got.get(c["key"], {"response": "reject", "reason": "No answer given (treated as reject)."})
        ctx.store.update_review(ctx.run_id, c["key"], c["version"], engineer_response=r["response"],
                                engineer_reason=r.get("reason", ""))
        ctx.emit("engineer", f"{r['response']} [{c['severity']}] {c['key']}: {r.get('reason', '')[:150]}",
                 "info" if r["response"] == "accept" else "warn")
        if r["response"] == "accept":
            targets.append({"key": c["key"], "source": "bar_raiser",
                            "note": f"{c['notes']} Suggested fix: {c.get('suggested_fix', '')}".strip()})
    return targets


def map_feedback(ctx: Ctx, llm, feedback: str) -> list[dict]:
    keys = [d for d in ctx.store.active(ctx.run_id) if not d["key"].startswith(("pin.", "cart."))]
    out = llm.call_tool(system=MAP_SYSTEM, tool_name="map_feedback", tool_description="Pick decisions to change.",
                        schema=MAP_SCHEMA, max_tokens=2000,
                        prompt=f'Feedback: "{feedback}"\n\nDECISIONS:\n' +
                               "\n".join(f"- {d['key']}: {d['title']}" for d in keys))
    valid = {d["key"] for d in keys}
    targets = [{"key": t["key"], "note": t.get("note") or feedback, "source": "user"}
               for t in out.get("targets", []) if t["key"] in valid]
    return targets or [{"key": "req.summary", "note": feedback, "source": "user"}]
