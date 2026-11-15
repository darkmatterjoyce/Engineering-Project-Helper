"""The Bar Raiser: an independent reviewer of the decision log.

Independence comes from three things:
  * a separate model (EPH_REVIEW_MODEL, Opus by default while the builder runs on Sonnet),
  * a separate adversarial prompt,
  * it never sees the builder's prompts or reasoning chain, only the recorded decisions, their
    evidence (Linkup sources, validator output, compiler log) and the final code.

It reviews each decision *version* once. Verdicts are written onto the decision (`review`) and kept
in the `reviews` collection as an audit trail. Challenges at or above EPH_AUTOFIX_SEVERITY are turned
into change requests for the stage that owns the decision, and the pipeline rebuilds just that part.
"""
from __future__ import annotations

import json
from collections import defaultdict

from .precedents import reviewer_block
from .schema import Review
from .stages import Ctx, requirements_brief

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}

# Review groups: one reviewer call each (keeps cost to ~3 calls per round).
GROUPS = {
    "design": ["requirements", "parts", "procurement"],
    "hardware": ["wiring"],
    "software": ["firmware", "diagram"],
}

SYSTEM = """You are the Bar Raiser: an independent senior hardware and embedded engineer reviewing another
agent's design decisions before a student builds the project on a breadboard. You did not make these
decisions and owe them nothing. Your job: catch anything that would make the build fail, damage parts,
be unsafe, or not meet the requirements, and plainly approve what is fine.

Check: claims against the cited evidence (distrust unverified parts and low confidence); voltage levels
(ESP32 GPIO is 3.3 V, not 5 V tolerant); current (GPIO ~12 mA max; loads need a driver; USB budget ~500 mA);
pin conflicts and ESP32 pin rules; missing protection (flyback diodes, gate pull-downs, pull-ups, bulk
capacitance for motors/pumps); and that the firmware implements every feature on exactly the wired pins.

You also hold the ACCEPTANCE CRITERIA you wrote from the user's request before any design existed:
challenge anything that fails them, and cite their ids in "criteria". You see results only (choices,
evidence, code), not the builder's reasoning; you will get to read its reasoning afterwards.

Verdicts: approve | challenge (must change; give a concrete suggested_fix) | reject (wrong choice entirely).
Severity: high = will not work, damages hardware, or unsafe → must be fixed before building;
medium = works but fragile; low = nitpick. Only use high when you can name the failure.
Review EVERY decision key listed, once. Be terse."""

SCHEMA = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["approve", "challenge", "reject"]},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "notes": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                    "criteria": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["key", "verdict", "severity", "notes", "suggested_fix"],
            },
        },
        "summary": {"type": "string", "description": "2-3 sentences on the overall risk"},
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["reviews", "summary", "risk_level"],
}


def _decision_text(d: dict, blind: bool = True) -> str:
    """blind=True: results only (stage A). blind=False adds the builder's rationale + alternatives (logbook)."""
    ev = [f"    - {e.get('source')}: {e.get('url') or ''} {(e.get('snippet') or '')[:220]}" for e in d.get("evidence", [])]
    alts = [f"{a['option']} ({a['reason_rejected']})" for a in d.get("alternatives", [])]
    return "\n".join(filter(None, [
        f"KEY: {d['key']}  (v{d['version']}, confidence {d['confidence']})",
        f"  decision: {d['title']}",
        f"  choice: {json.dumps(d['choice'], default=str, ensure_ascii=False)[:700]}",
        "" if blind else f"  builder's rationale: {d.get('rationale', '')[:500]}",
        f"  rejected alternatives: {'; '.join(alts)}" if (alts and not blind) else "",
        f"  depends on: {', '.join(x['key'] for x in d.get('depends_on', []))}" if d.get("depends_on") else "",
        "  evidence:\n" + "\n".join(ev) if ev else "  evidence: none",
    ]))


def criteria_block(ctx: Ctx) -> str:
    crit = (ctx.store.get_run(ctx.run_id) or {}).get("acceptance_criteria", [])
    if not crit:
        return ""
    return "\n\nYOUR ACCEPTANCE CRITERIA (written before the design):\n" + "\n".join(
        f"- {c['id']} [{c.get('priority', 'must')}] {c['criterion']}" for c in crit)


def _context(ctx: Ctx, group: str) -> str:
    parts = [requirements_brief(ctx) + criteria_block(ctx)]
    arts = ctx.store.latest_artifacts(ctx.run_id)
    if group in ("hardware", "software"):
        parts.append("PARTS:\n" + "\n".join(
            f"- {d['key']}: {d['title']} logic={d['choice'].get('logic_voltage')} supply={d['choice'].get('supply_voltage')}"
            for d in ctx.store.active(ctx.run_id, prefix="part.")))
    if group == "software":
        pins = ctx.store.active(ctx.run_id, prefix="pin.")
        parts.append("PIN TABLE:\n" + "\n".join(f"- {d['title']}" for d in pins))
        if "firmware" in arts:
            parts.append("FIRMWARE:\n```cpp\n" + arts["firmware"]["content"]["code"] + "\n```")
        if "compile_log" in arts:
            cl = arts["compile_log"]["content"]
            parts.append(f"COMPILE: {cl['status']} {cl.get('size') or ''}\n{cl['log'][-600:]}")
    return "\n\n".join(parts) + reviewer_block(ctx.precedents)


def review_round(ctx: Ctx, reviewer, round_no: int) -> list[dict]:
    """Stage A (blind): review every active decision version that has no review yet.
    Returns every non-approved review as {key, version, stage, severity, notes, suggested_fix}."""
    pending = [d for d in ctx.store.active(ctx.run_id) if not d.get("review")]
    by_group: dict[str, list[dict]] = defaultdict(list)
    for d in pending:
        for g, stages in GROUPS.items():
            if d["stage"] in stages:
                by_group[g].append(d)
    challenged: list[dict] = []
    for group, decisions in by_group.items():
        prompt = (_context(ctx, group) + "\n\nDECISIONS TO REVIEW (results only):\n\n" +
                  "\n\n".join(_decision_text(d, blind=True) for d in decisions))
        out = reviewer.call_tool(system=SYSTEM, prompt=prompt, tool_name="review_decisions",
                                 tool_description="Record your review of each decision.", schema=SCHEMA)
        got = {r["key"]: r for r in out.get("reviews", [])}
        n_chal = 0
        for d in decisions:
            r = got.get(d["key"]) or {"verdict": "approve", "severity": "low", "notes": "(no comment)", "suggested_fix": ""}
            rev = Review(verdict=r["verdict"], severity=r.get("severity", "low"), notes=r.get("notes", ""),
                         suggested_fix=r.get("suggested_fix", ""), criteria=r.get("criteria") or [],
                         model=reviewer.model, round=round_no)
            ctx.store.set_review(ctx.run_id, d["key"], d["version"], rev.model_dump(mode="python"))
            if rev.verdict != "approve":
                n_chal += 1
                lvl = "warn" if SEVERITY_RANK[rev.severity] >= 1 else "info"
                ctx.emit("review", f"{rev.verdict} [{rev.severity}] {d['key']}: {rev.notes[:160]}", lvl)
                challenged.append({"key": d["key"], "version": d["version"], "stage": d["stage"],
                                   "severity": rev.severity, "notes": rev.notes,
                                   "suggested_fix": rev.suggested_fix})
        ctx.store.push_run(ctx.run_id, "review_rounds", {
            "round": round_no, "group": group, "summary": out.get("summary", ""),
            "risk_level": out.get("risk_level", "low"), "reviewed": len(decisions), "challenges": n_chal})
        ctx.emit("review", f"round {round_no} {group}: {len(decisions)} reviewed, {n_chal} challenged "
                           f"(risk {out.get('risk_level', '?')})")
    return challenged


# ------------------------------------------------------------------ step 2: criteria up front
CRITERIA_SCHEMA = {
    "type": "object",
    "properties": {"criteria": {"type": "array", "minItems": 2, "maxItems": 7, "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "criterion": {"type": "string"}, "why": {"type": "string"},
                       "priority": {"type": "string", "enum": ["must", "should"]}},
        "required": ["id", "criterion", "why", "priority"]}}},
    "required": ["criteria"],
}


def brief(ctx: Ctx, reviewer) -> list[dict]:
    """Before any design exists: write the acceptance criteria the delivery will be held to.
    Stored on the run; the builder stages never see them."""
    qa = "".join(f"\n- Q: {c.get('q')}  A: {c.get('a')}" for c in ctx.clarifications)
    out = reviewer.call_tool(
        system=SYSTEM, tool_name="acceptance_criteria", tool_description="Record acceptance criteria.",
        schema=CRITERIA_SCHEMA, max_tokens=2000,
        prompt=(f'A new project. The builder is starting now; you will not see the design until it is done.\n'
                f'User request: "{ctx.prompt}"' + (f"\nUser's answers:{qa}" if qa else "") +
                "\n\nWrite 3-6 concrete, checkable acceptance criteria: what must be true for THIS user to be "
                "happy (ids AC1, AC2, ...)." + reviewer_block(ctx.precedents)))
    crit = [{**c, "source": "proposal", "status": None, "note": ""} for c in out.get("criteria", [])]
    ctx.store.update_run(ctx.run_id, acceptance_criteria=crit)
    ctx.emit("review", f"Bar Raiser wrote {len(crit)} acceptance criteria before the design (hidden from the builder)")
    return crit


# ------------------------------------------------------------------ stage B: read the logbook
RECONSIDER_SCHEMA = {
    "type": "object",
    "properties": {"decisions": {"type": "array", "items": {
        "type": "object",
        "properties": {"key": {"type": "string"},
                       "action": {"type": "string", "enum": ["keep", "downgrade", "withdraw"]},
                       "new_severity": {"type": "string", "enum": ["low", "medium", "high"]},
                       "note": {"type": "string"}},
        "required": ["key", "action", "note"]}}},
    "required": ["decisions"],
}


def reconsider(ctx: Ctx, reviewer, challenged: list[dict]) -> list[dict]:
    """Stage B: now read the builder's rationale for each challenged decision. Keep / downgrade / withdraw.
    Returns the challenges that still stand (with updated severity)."""
    if not challenged:
        return []
    by_key = {d["key"]: d for d in ctx.store.active(ctx.run_id)}
    blocks = []
    for c in challenged:
        d = by_key.get(c["key"])
        if d:
            blocks.append(f"YOUR CHALLENGE [{c['severity']}] on {c['key']}: {c['notes']}\n" + _decision_text(d, blind=False))
    out = reviewer.call_tool(
        system=SYSTEM, tool_name="reconsider_after_logbook", tool_description="Keep, downgrade or withdraw.",
        schema=RECONSIDER_SCHEMA, max_tokens=3000,
        prompt=("You raised the challenges below from the results alone. Now read the builder's rationale "
                "(its logbook). Does it change your mind? Be fair, but do not be talked out of a real user need."
                + criteria_block(ctx) + "\n\n" + "\n\n".join(blocks)))
    got = {r["key"]: r for r in out.get("decisions", [])}
    standing = []
    for c in challenged:
        r = got.get(c["key"], {"action": "keep", "note": ""})
        action = {"keep": "kept", "downgrade": "downgraded", "withdraw": "withdrawn"}.get(r["action"], "kept")
        sev = r.get("new_severity") if action == "downgraded" and r.get("new_severity") else c["severity"]
        fields = {"blind_severity": c["severity"], "after_logbook": action, "after_logbook_note": r.get("note", ""),
                  "severity": sev}
        if action == "withdrawn":
            fields["verdict"] = "approve"
        ctx.store.update_review(ctx.run_id, c["key"], c["version"], **fields)
        if action != "kept":
            ctx.emit("review", f"{c['key']}: {action} after reading the logbook. {r.get('note', '')[:140]}")
        if action != "withdrawn":
            standing.append({**c, "severity": sev})
    return standing


# ------------------------------------------------------------------ step 4: final acceptance check
FINAL_SCHEMA = {
    "type": "object",
    "properties": {"assessment": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"},
                       "status": {"type": "string", "enum": ["met", "partial", "not_met", "unverified"]},
                       "note": {"type": "string"}},
        "required": ["id", "status", "note"]}}},
    "required": ["assessment"],
}


def final_assessment(ctx: Ctx, reviewer) -> list[dict]:
    """Score every criterion against the FINAL delivery. Agreement between agents is not evidence."""
    run = ctx.store.get_run(ctx.run_id)
    crit = run.get("acceptance_criteria", [])
    if not crit:
        return []
    arts = ctx.store.latest_artifacts(ctx.run_id)
    decisions = "\n".join(f"- {d['key']}: {d['title']}" for d in ctx.store.active(ctx.run_id)
                          if not d["key"].startswith("pin."))
    cl = (arts.get("compile_log") or {}).get("content", {})
    cart = (arts.get("cart") or {}).get("content", {})
    out = reviewer.call_tool(
        system=SYSTEM, tool_name="final_acceptance", tool_description="Score each acceptance criterion.",
        schema=FINAL_SCHEMA, max_tokens=2500,
        prompt=("FINAL ACCEPTANCE CHECK before delivery. Score EVERY criterion against the final delivery. "
                "'met' only if the delivered design/cart/firmware actually satisfies it; a requirement that was "
                "replaced (see deviation.* decisions) is 'not_met' even if everyone agreed; 'unverified' if it "
                "depends on something not verified (firmware not compiled, estimated prices).\n"
                + criteria_block(ctx) + f"\n\nFINAL DECISIONS:\n{decisions}\n\nCOMPILE: {cl.get('status', 'n/a')}"
                f"\nCART: total £{cart.get('total_gbp')} budget £{cart.get('budget_gbp')} "
                f"over_budget={cart.get('over_budget')}"))
    got = {a["id"]: a for a in out.get("assessment", [])}
    for c in crit:
        a = got.get(c["id"])
        c["status"], c["note"] = (a["status"], a.get("note", "")) if a else ("unverified", "Not assessed.")
    ctx.store.update_run(ctx.run_id, acceptance_criteria=crit)
    return crit


# ------------------------------------------------------------------ improve: learn from the user
FEEDBACK_SCHEMA = {
    "type": "object",
    "properties": {"lesson": {"type": "string"}, "missed_by_review": {"type": "boolean"},
                   "criteria": CRITERIA_SCHEMA["properties"]["criteria"]},
    "required": ["lesson", "missed_by_review", "criteria"],
}


def record_feedback(ctx: Ctx, reviewer, feedback: str) -> dict:
    """The user was not happy. The Bar Raiser records it first and raises its criteria."""
    run = ctx.store.get_run(ctx.run_id)
    crit = run.get("acceptance_criteria", [])
    start = len(crit) + 1
    out = reviewer.call_tool(
        system=SYSTEM, tool_name="record_feedback", tool_description="Record the user's dissatisfaction.",
        schema={**FEEDBACK_SCHEMA, "properties": {**FEEDBACK_SCHEMA["properties"],
                "criteria": {**CRITERIA_SCHEMA["properties"]["criteria"], "minItems": 0, "maxItems": 3}}},
        max_tokens=1500,
        prompt=(f'The user received the delivery of "{ctx.prompt}" and is NOT fully happy. Their feedback: '
                f'"{feedback}".' + criteria_block(ctx) + f"\nDid your review miss it? Add or tighten criteria "
                f"(new ids start at AC{start}) so the next delivery meets it."))
    new = [{**c, "source": "improvement", "status": None, "note": ""} for c in out.get("criteria", [])]
    for c in crit:
        c["status"], c["note"] = None, ""
    ctx.store.update_run(ctx.run_id, acceptance_criteria=crit + new)
    ctx.emit("review", f"Bar Raiser recorded the feedback ({'missed by review' if out.get('missed_by_review') else 'new need'}); "
                       f"+{len(new)} criteria. Lesson: {out.get('lesson', '')[:140]}")
    return out
