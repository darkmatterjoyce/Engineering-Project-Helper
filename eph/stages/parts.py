"""Stage 2: choose parts, then verify each against live web sources (Linkup) with citations."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from ..schema import Alternative, Decision
from . import ALTERNATIVES_SCHEMA, Ctx, confidence_list, requirements_brief, slug

STAGE = "parts"
PASSIVE_ROLES = {"passive", "wiring", "prototyping", "mechanical"}

SYSTEM = """You are the hardware engineer of an autonomous ESP32 project agent.
Pick real, cheap, hobbyist-available modules (breakout boards, not bare ICs) with exact model names.
The microcontroller is fixed: id "mcu", "ESP32 DevKit V1", model "ESP32-WROOM-32 DevKit V1 (30-pin)".
Include required passives/drivers (MOSFET, flyback diode, resistors) as parts with role "passive".
Prefer 3.3 V logic parts. Every non-MCU part must list which feature ids it satisfies.
The parts list is the shopping list: include EVERYTHING needed to physically build it (chassis, wheels,
enclosure, tubing, reservoir, battery holder, power switch, phone tray…) with role "mechanical".
Stay within the user's budget if one is given. Never silently drop or weaken an explicit user requirement
(e.g. "a camera"): if you truly cannot meet one, list it under "deviations" so the user sees it.
"library" is the exact Arduino Library Manager name, or null if no library is needed."""

SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "parts": {
            "type": "array", "minItems": 2,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "short snake_case id"},
                    "name": {"type": "string"},
                    "model": {"type": "string", "description": "exact module/part number"},
                    "qty": {"type": "integer", "minimum": 1},
                    "role": {"type": "string", "enum": ["mcu", "sensor", "actuator", "display", "input", "driver", "power", "passive", "comms", "mechanical"]},
                    "interface": {"type": "string", "enum": ["analog", "digital", "pwm", "i2c", "spi", "uart", "onewire", "power", "none"]},
                    "satisfies": {"type": "array", "items": {"type": "string"}},
                    "library": {"type": ["string", "null"]},
                    "search_query": {"type": "string", "description": "web query to verify pinout + voltage"},
                    "est_price_gbp": {"type": "number"},
                    "rationale": {"type": "string"},
                    "alternatives": ALTERNATIVES_SCHEMA,
                    "confidence": confidence_list(),
                },
                "required": ["id", "name", "model", "qty", "role", "interface", "satisfies", "library",
                             "search_query", "rationale", "alternatives", "confidence"],
            },
        },
        "deviations": {
            "type": "array",
            "description": "explicit user requirements you could NOT meet as asked (usually empty)",
            "items": {"type": "object", "properties": {
                "requirement": {"type": "string"}, "instead": {"type": "string"}, "why": {"type": "string"}},
                "required": ["requirement", "instead", "why"]},
        },
    },
    "required": ["parts"],
}

VERIFY_SYSTEM = """You verify hardware part claims against web search evidence.
For each part, use ONLY the evidence given (plus well-established facts) to confirm pin names,
supply voltage and logic level. If evidence contradicts the claim, say so and correct it.
Pin names must be the labels printed on the module (e.g. VCC, GND, SDA, SCL, AOUT, SIG, G/D/S)."""

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verified": {"type": "boolean", "description": "evidence supports the part choice"},
                    "supply_voltage": {"type": "string"},
                    "logic_voltage": {"type": "string", "enum": ["3.3V", "5V", "3.3V/5V", "n/a"]},
                    "pins": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string", "description": "what the evidence says; corrections; gotchas"},
                    "cited_urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "verified", "supply_voltage", "logic_voltage", "pins", "notes", "cited_urls"],
            },
        }
    },
    "required": ["parts"],
}


def run(ctx: Ctx) -> None:
    brief = requirements_brief(ctx)
    feature_keys = {d["key"].split(".")[-1]: d["key"] for d in ctx.store.active(ctx.run_id, prefix="req.feature.")}

    sel = ctx.llm.call_tool(
        system=SYSTEM,
        prompt=f"{brief}\n\nValid feature ids: {sorted(feature_keys)}\nSelect the parts." + ctx.precedent_block(STAGE) + ctx.revision_block(STAGE),
        tool_name="select_parts", tool_description="Record the bill of materials.", schema=SELECT_SCHEMA,
    )
    parts = sel["parts"]
    for p in parts:
        p["id"] = "mcu" if p["role"] == "mcu" else slug(p["id"])
    if not any(p["id"] == "mcu" for p in parts):
        parts.insert(0, {"id": "mcu", "name": "ESP32 DevKit V1", "model": "ESP32-WROOM-32 DevKit V1 (30-pin)",
                         "qty": 1, "role": "mcu", "interface": "none", "satisfies": [], "library": None,
                         "search_query": "ESP32 DevKit V1 30 pin pinout", "rationale": "Fixed MCU.",
                         "alternatives": [], "confidence": 0.95})
    ctx.emit(STAGE, f"Selected {len(parts)} parts; verifying with {ctx.search.name} search")

    # ---- verify with live search (parallel)
    to_check = [p for p in parts if p["role"] not in PASSIVE_ROLES]
    results = {}
    if ctx.search.name != "none":
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = {p["id"]: pool.submit(ctx.search.search, p["search_query"]) for p in to_check}
            for pid, fut in futs.items():
                try:
                    results[pid] = fut.result()
                except Exception as e:  # noqa: BLE001
                    ctx.emit(STAGE, f"search failed for {pid}: {e}", "warn")
        cached = sum(1 for r in results.values() if r.cached)
        ctx.emit(STAGE, f"{ctx.search.name}: {len(results)} searches ({cached} from Mongo cache)")
    else:
        ctx.emit(STAGE, "No search provider configured (set LINKUP_API_KEY); parts are unverified", "warn")

    evidence_text = []
    for p in parts:
        r = results.get(p["id"])
        claim = {k: p[k] for k in ("id", "name", "model", "interface")}
        if r:
            srcs = "\n".join(f"  [{s.url}] {s.title}: {(s.snippet or '')[:300]}" for s in r.sources[:4])
            evidence_text.append(f"PART {json.dumps(claim)}\nSEARCH ANSWER: {r.answer[:1500]}\nSOURCES:\n{srcs}")
        else:
            evidence_text.append(f"PART {json.dumps(claim)}\n(no search evidence)")
    ver = ctx.llm.call_tool(
        system=VERIFY_SYSTEM, prompt="\n\n".join(evidence_text) + "\n\nVerify every part listed.",
        tool_name="verify_parts", tool_description="Record verified specs for each part.", schema=VERIFY_SCHEMA,
    )
    vmap = {slug(v["id"]) if v["id"] != "mcu" else "mcu": v for v in ver["parts"]}

    # ---- record decisions
    bom = []
    for p in parts:
        v = vmap.get(p["id"], {})
        r = results.get(p["id"])
        verified = bool(v.get("verified")) and r is not None and bool(r.sources)
        cited = set(v.get("cited_urls", []))
        evidence = []
        if r:
            evidence = sorted(r.sources, key=lambda s: s.url not in cited)[:3]
        sat = [feature_keys[slug(f)] for f in p.get("satisfies", []) if slug(f) in feature_keys]
        deps = ctx.store.refs(ctx.run_id, sat + ["req.power"])
        conf = float(p.get("confidence", 0.7)) * (1.0 if verified or p["role"] in PASSIVE_ROLES else 0.7)
        choice = {
            "name": p["name"], "model": p["model"], "qty": p["qty"], "role": p["role"],
            "interface": p["interface"], "library": p.get("library"),
            "supply_voltage": v.get("supply_voltage"), "logic_voltage": v.get("logic_voltage"),
            "pins": v.get("pins", []), "verified": verified,
            "verification_notes": v.get("notes", ""), "est_price_gbp": p.get("est_price_gbp"),
            "satisfies": [k.split(".")[-1] for k in sat],
        }
        ctx.store.record(Decision(
            run_id=ctx.run_id, key=f"part.{p['id']}", stage=STAGE, kind="part",
            title=f"{p['name']} ({p['model']})", choice=choice, rationale=p.get("rationale", ""),
            alternatives=[Alternative(**a) for a in p.get("alternatives", [])],
            depends_on=deps, evidence=evidence, confidence=round(min(conf, 1.0), 2),
        ))
        bom.append({"id": p["id"], **choice})
        if not verified and p["role"] not in PASSIVE_ROLES:
            ctx.emit(STAGE, f"{p['id']} not verified by search: {v.get('notes', 'no evidence')[:160]}", "warn")

    for dv in sel.get("deviations") or []:
        ctx.store.record(Decision(
            run_id=ctx.run_id, key=f"deviation.{slug(dv['requirement'])[:40]}", stage=STAGE, kind="deviation",
            title=f"Asked: {dv['requirement']} → delivering: {dv['instead']}",
            choice={"requirement": dv["requirement"], "instead": dv["instead"]}, rationale=dv["why"],
            confidence=0.6))
        ctx.emit(STAGE, f"Deviation from the request: {dv['requirement']} → {dv['instead']}", "warn")

    refs = ctx.store.refs(ctx.run_id, [f"part.{p['id']}" for p in parts])
    ctx.store.save_artifact(ctx.run_id, "parts_list", {"parts": bom}, refs)
    n_ver = sum(1 for b in bom if b["verified"])
    ctx.emit(STAGE, f"Parts list saved: {len(bom)} parts, {n_ver} verified with citations")
