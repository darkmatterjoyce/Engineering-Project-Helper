"""Stage 6: project guidance. One LLM call turns the final design into a plain-English build guide:
what the project is and how it works, then step-by-step how to actually build it from the generated parts,
wiring and firmware. It is grounded in the decision log (parts, validated pin map, support components,
firmware), so pins and parts in the guide always match the design. The UI adds an exact connection
checklist from the validated pin map next to it, so the guide never has to restate every wire.

Decision `guide.build` depends on every part, the support components, the sketch and the cart, so any
rebuild that changes them rewrites the guide too."""
from __future__ import annotations

import json

from ..schema import Decision
from . import Ctx, requirements_brief

STAGE = "guide"

SYSTEM = """You write the build guide for an ESP32 project that has already been designed. The reader is a
student with a breadboard who has never built this before. Be concrete and friendly, plain English, no
marketing. Use ONLY the parts, pins, support components and firmware details given; never invent a part or
a pin. Refer to parts by their names. When a step needs the exact wiring, say "follow the connection
checklist" instead of listing every wire again; do name the important ones (power, ground, the pin a
sensor or driver uses). Mention anything that is easy to get wrong (polarity, 5 V vs 3.3 V, flyback diode
direction, calibration values, Wi-Fi credentials in the sketch)."""

SCHEMA = {
    "type": "object",
    "properties": {
        "what_it_is": {"type": "string", "description": "2 short paragraphs: what it does for the user, and how it "
                       "works (sensing → decision in firmware → action), naming the main parts."},
        "you_will_need": {"type": "string", "description": "1-2 sentences: tools and skills (USB cable, computer "
                          "with Arduino IDE, no soldering if true, etc.)"},
        "build_steps": {"type": "array", "minItems": 5, "maxItems": 10, "items": {"type": "object", "properties": {
            "title": {"type": "string", "description": "imperative, a few words"},
            "body": {"type": "string", "description": "2-5 sentences of exactly what to do and how to check it"}},
            "required": ["title", "body"]}},
        "test_and_calibrate": {"type": "array", "items": {"type": "string"}, "description": "3-6 checks"},
        "safety": {"type": "array", "items": {"type": "string"}, "description": "1-4 items"},
        "troubleshooting": {"type": "array", "items": {"type": "object", "properties": {
            "symptom": {"type": "string"}, "fix": {"type": "string"}}, "required": ["symptom", "fix"]},
            "description": "3-5 likely problems"},
    },
    "required": ["what_it_is", "you_will_need", "build_steps", "test_and_calibrate", "safety", "troubleshooting"],
}


def run(ctx: Ctx) -> None:
    parts = ctx.store.active(ctx.run_id, prefix="part.")
    pins = ctx.store.active(ctx.run_id, prefix="pin.")
    support = ctx.store.get_active(ctx.run_id, "wiring.support")
    arts = ctx.store.latest_artifacts(ctx.run_id)
    fw = (arts.get("firmware") or {}).get("content", {})
    cart = (arts.get("cart") or {}).get("content", {})
    parts_txt = "\n".join(
        f"- {d['choice']['name']} ({d['choice']['model']}), qty {d['choice'].get('qty', 1)}, role {d['choice'].get('role')}, "
        f"supply {d['choice'].get('supply_voltage') or '?'}, logic {d['choice'].get('logic_voltage') or '?'}"
        for d in parts)
    pin_txt = "\n".join(f"- {d['choice']['part_id']}.{d['choice']['part_pin']} -> {d['choice']['esp32_pin']}"
                        for d in pins)
    sup_txt = "\n".join(f"- {s.get('name')} {s.get('value', '')}: {s.get('wiring', '')} ({s.get('purpose', '')})"
                        for s in (support["choice"] if support else []))
    code = fw.get("code", "")
    prompt = (f"{requirements_brief(ctx)}\n\nPARTS:\n{parts_txt}\n\nVALIDATED PIN MAP:\n{pin_txt}\n\n"
              f"SUPPORT COMPONENTS:\n{sup_txt or 'none'}\n\nFIRMWARE: {fw.get('filename', 'sketch.ino')}, libraries "
              f"{json.dumps(fw.get('libraries', []))}. Notes: {fw.get('notes', '')}\n"
              f"SKETCH (for constants, Wi-Fi credentials and behaviour):\n```cpp\n{code[:6000]}\n```\n\n"
              f"CART TOTAL: £{cart.get('total_gbp', '?')}\n\nWrite the build guide." + ctx.revision_block(STAGE))
    out = ctx.llm.call_tool(system=SYSTEM, prompt=prompt, tool_name="build_guide",
                            tool_description="Record the project guidance.", schema=SCHEMA, max_tokens=5000)
    keys = [d["key"] for d in parts] + (["wiring.support"] if support else []) + ["fw.sketch", "cart.total"]
    deps = ctx.store.refs(ctx.run_id, keys)
    ctx.store.record(Decision(
        run_id=ctx.run_id, key="guide.build", stage=STAGE, kind="guide",
        title=f"Build guide: {len(out['build_steps'])} steps", choice=out,
        rationale="Written from the final parts, validated pin map, support components and firmware.",
        depends_on=deps, confidence=0.8))
    ctx.store.save_artifact(ctx.run_id, "guide", out, deps)
    ctx.emit(STAGE, f"Build guide written: {len(out['build_steps'])} steps")
