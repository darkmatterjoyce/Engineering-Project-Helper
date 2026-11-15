"""Stage 3: pin assignment. LLM proposes, a deterministic ESP32 validator checks, errors go back
to the LLM until the map is clean (or attempts run out)."""
from __future__ import annotations

import json

from .. import breadboard, esp32
from ..schema import Alternative, Decision, Evidence
from . import ALTERNATIVES_SCHEMA, Ctx, slug

STAGE = "wiring"

SYSTEM = f"""You are the wiring engineer of an autonomous ESP32 project agent.
Produce a complete breadboard wiring map: EVERY pin of every module (including VCC and GND) gets a connection.
{esp32.PIN_GUIDE}
direction is from the ESP32's point of view. For power connections use direction "power" and signal_type "power".
Loads (pumps, motors, relays, LED strips) are driven through a driver part (MOSFET/transistor/driver board) from the parts list:
wire the GPIO to the driver's gate/input and describe the load side in support_components (one entry per
2-terminal component or wire, with exact `ends`, so the breadboard drawing can be generated from it).
Only reference part ids and pin names that exist in the parts list."""

SCHEMA = {
    "type": "object",
    "properties": {
        "connections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "part_id": {"type": "string"},
                    "part_pin": {"type": "string"},
                    "esp32_pin": {"type": "string", "description": "e.g. GPIO21, 3V3, GND, VIN, EXT"},
                    "direction": {"type": "string", "enum": ["input", "output", "pwm", "analog_in", "analog_out", "bidirectional", "power"]},
                    "signal_type": {"type": "string", "enum": ["digital", "analog", "pwm", "i2c_sda", "i2c_scl", "spi_mosi", "spi_miso", "spi_sck", "spi_cs", "uart_tx", "uart_rx", "onewire", "power"]},
                    "rationale": {"type": "string"},
                    "alternatives": ALTERNATIVES_SCHEMA,
                },
                "required": ["part_id", "part_pin", "esp32_pin", "direction", "signal_type", "rationale"],
            },
        },
        "support_components": {
            "type": "array",
            "description": "everything not wired to an ESP32 pin: resistors, diodes, capacitors, and plain wires "
                           "for load-side connections (e.g. supply → pump, pump → MOSFET drain)",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"}, "value": {"type": "string"},
                    "kind": {"type": "string", "enum": ["resistor", "capacitor", "diode", "led", "wire", "other"]},
                    "ends": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "string"},
                             "description": "the two connection points, each 'part_id.PIN' or a rail: 3V3, VIN, GND, EXT. "
                                            "Polarity: diode ends[0]=cathode (stripe); capacitor/LED ends[0]=positive"},
                    "purpose": {"type": "string"}, "wiring": {"type": "string", "description": "human-readable"},
                    "connects": {"type": "array", "items": {"type": "string"}, "description": "part ids"},
                },
                "required": ["name", "value", "kind", "ends", "purpose", "wiring", "connects"],
            },
        },
    },
    "required": ["connections", "support_components"],
}


def _parts_brief(parts: list[dict]) -> str:
    rows = []
    for d in parts:
        c = d["choice"]
        rows.append(json.dumps({"id": d["key"].split(".", 1)[1], "name": c["name"], "model": c["model"],
                                "interface": c["interface"], "logic_voltage": c.get("logic_voltage"),
                                "pins": c.get("pins"), "notes": (c.get("verification_notes") or "")[:200]}))
    return "\n".join(rows)


def run(ctx: Ctx) -> None:
    parts = ctx.store.active(ctx.run_id, prefix="part.")
    part_ids = {d["key"].split(".", 1)[1] for d in parts}
    conn = ctx.store.get_active(ctx.run_id, "req.connectivity")
    wifi = bool(conn and (conn["choice"].get("wifi") or conn["choice"].get("ble")))
    five_v = {d["key"].split(".", 1)[1] for d in parts if d["choice"].get("logic_voltage") == "5V"}

    base_prompt = (f"Wi-Fi/BLE in use: {wifi}\nPARTS:\n{_parts_brief(parts)}\n\nProduce the wiring map."
                   + ctx.precedent_block(STAGE) + ctx.revision_block(STAGE))
    prompt, out, issues = base_prompt, None, []
    for attempt in range(1, ctx.settings.max_fix_attempts + 1):
        out = ctx.llm.call_tool(system=SYSTEM, prompt=prompt, tool_name="assign_pins",
                                tool_description="Record the wiring map.", schema=SCHEMA)
        for c in out["connections"]:
            c["part_id"] = "mcu" if c["part_id"] == "mcu" else slug(c["part_id"])
        issues = esp32.validate(out["connections"], wifi=wifi, five_volt_parts=five_v)
        issues += [esp32.Issue("error", i, c["esp32_pin"], f"unknown part id '{c['part_id']}'")
                   for i, c in enumerate(out["connections"]) if c["part_id"] not in part_ids]
        errors = [i for i in issues if i.severity == "error"]
        ctx.emit(STAGE, f"attempt {attempt}: {len(out['connections'])} connections, "
                        f"{len(errors)} errors, {len(issues) - len(errors)} warnings")
        if not errors:
            break
        prompt = (base_prompt + "\n\nYOUR PREVIOUS MAP:\n" + json.dumps(out["connections"]) +
                  "\n\nVALIDATOR ERRORS (fix all of them, keep everything else):\n" +
                  "\n".join(f"- connection {e.index} ({e.pin}): {e.message}" for e in errors))
    assert out is not None
    for e in issues:
        ctx.emit(STAGE, f"{e.pin}: {e.message}", "error" if e.severity == "error" else "warn")

    # ---- record decisions
    by_conn: dict[int, list[esp32.Issue]] = {}
    for e in issues:
        by_conn.setdefault(e.index, []).append(e)
    seen_keys: dict[str, int] = {}
    for i, c in enumerate(out["connections"]):
        base = f"pin.{c['part_id']}.{slug(c['part_pin'])}"
        seen_keys[base] = seen_keys.get(base, 0) + 1
        key = base if seen_keys[base] == 1 else f"{base}_{seen_keys[base]}"
        p = esp32.parse_pin(c["esp32_pin"])
        deps = [f"part.{c['part_id']}", "part.mcu"]
        if c["direction"] == "analog_in":
            deps.append("req.connectivity")
        my_issues = by_conn.get(i, [])
        conf = 0.9 - 0.15 * sum(1 for e in my_issues if e.severity == "warning") \
                   - 0.5 * sum(1 for e in my_issues if e.severity == "error")
        ctx.store.record(Decision(
            run_id=ctx.run_id, key=key, stage=STAGE, kind="pin",
            title=f"{c['part_id']}.{c['part_pin']} → {c['esp32_pin']}",
            choice={**{k: c[k] for k in ("part_id", "part_pin", "esp32_pin", "direction", "signal_type")},
                    "gpio": p if isinstance(p, int) else None},
            rationale=c.get("rationale", ""),
            alternatives=[Alternative(**a) for a in c.get("alternatives", []) or []],
            depends_on=ctx.store.refs(ctx.run_id, deps),
            evidence=[Evidence(source="validator", snippet=f"{e.severity}: {e.message}") for e in my_issues],
            confidence=round(max(conf, 0.05), 2),
        ))
    support = out.get("support_components", [])
    if support:
        involved = sorted({pid for s in support for pid in s.get("connects", []) if pid in part_ids})
        ctx.store.record(Decision(
            run_id=ctx.run_id, key="wiring.support", stage=STAGE, kind="support_components",
            title=", ".join(f"{s['name']} {s['value']}".strip() for s in support)[:120],
            choice=support, rationale="; ".join(s["purpose"] for s in support),
            depends_on=ctx.store.refs(ctx.run_id, [f"part.{p}" for p in involved]), confidence=0.8,
        ))

    labels = {d["key"].split(".", 1)[1]: d["choice"]["name"] for d in parts}
    mermaid = esp32.wiring_mermaid(out["connections"], labels, support)
    try:
        bb_svg, bb_notes = breadboard.render(out["connections"],
                                             {d["key"].split(".", 1)[1]: d["choice"] for d in parts}, support)
        for n in bb_notes:
            ctx.emit(STAGE, f"breadboard view: {n}", "warn")
    except Exception as e:  # noqa: BLE001  the picture must never break the pipeline
        bb_svg, bb_notes = "", [f"breadboard view failed: {e}"]
        ctx.emit(STAGE, bb_notes[0], "warn")
    refs = ctx.store.refs(ctx.run_id, [d["key"] for d in ctx.store.active(ctx.run_id, stage=STAGE)])
    ctx.store.save_artifact(ctx.run_id, "wiring", {
        "connections": out["connections"], "support_components": support,
        "issues": [e.to_dict() for e in issues], "mermaid": mermaid, "board": esp32.BOARD,
        "breadboard_svg": bb_svg, "breadboard_notes": bb_notes,
    }, refs)
