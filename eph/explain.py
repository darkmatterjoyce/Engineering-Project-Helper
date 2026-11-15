"""On-demand "explain these lines" for the interactive Firmware view.

One small LLM call per new selection, cached in MongoDB (`explanations`, keyed by run + code hash + line
range), so clicking the same lines again, or reopening the run later, costs nothing."""
from __future__ import annotations

import hashlib

from . import codemap
from .schema import now

SYSTEM = """You explain Arduino/ESP32 firmware to an engineering student who is about to build the circuit.
Explain ONLY the selected lines, in the context of the whole sketch: what they do, why they are written that
way, which hardware/GPIO pins they touch, and units/timing. Plain English, no filler. If a line has a subtle
gotcha (blocking call, integer overflow, ADC2 with Wi-Fi, strapping pin...), say so in watch_out."""

SCHEMA = {
    "type": "object",
    "properties": {
        "explanation": {"type": "string", "description": "3-8 sentences"},
        "hardware": {"type": "array", "items": {"type": "object", "properties": {
            "pin": {"type": "string"}, "what": {"type": "string"}}, "required": ["pin", "what"]}},
        "watch_out": {"type": "string", "description": "one gotcha, or empty"},
    },
    "required": ["explanation", "hardware", "watch_out"],
}


def explain_lines(store, llm, run_id: str, start: int, end: int) -> dict:
    arts = store.latest_artifacts(run_id)
    fw = arts.get("firmware")
    if not fw:
        raise ValueError("no firmware yet")
    code = fw["content"]["code"]
    n = len(code.splitlines())
    start, end = max(1, min(start, end)), min(n, max(start, end))
    if start > n:
        raise ValueError("selection is outside the sketch")
    if end - start > 80:
        raise ValueError("select at most 80 lines")
    sha = hashlib.sha256(code.encode()).hexdigest()
    key = {"run_id": run_id, "code_sha": sha, "start": start, "end": end}
    hit = store.db["explanations"].find_one(key, {"_id": 0})
    if hit:
        return {**hit, "cached": True}
    pins = "\n".join(f"- {d['title']}" for d in store.active(run_id, prefix="pin."))
    lines = code.splitlines()
    selected = "\n".join(f"{i:>3}| {lines[i - 1]}" for i in range(start, end + 1))
    out = llm.call_tool(
        system=SYSTEM, tool_name="explain_code", tool_description="Explain the selected lines.", schema=SCHEMA,
        max_tokens=1200,
        prompt=f"PIN TABLE:\n{pins}\n\nWHOLE SKETCH:\n{codemap.numbered(code)}\n\nSELECTED LINES {start}-{end}:\n{selected}")
    doc = {**key, "explanation": out.get("explanation", ""), "hardware": out.get("hardware", []),
           "watch_out": out.get("watch_out", ""), "model": getattr(llm, "model", ""), "created_at": now()}
    store.db["explanations"].insert_one(dict(doc))
    doc.pop("created_at")
    return {**doc, "cached": False}
