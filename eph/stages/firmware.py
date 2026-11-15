"""Stage 4: write the Arduino sketch from the pin decisions, compile it with arduino-cli, and feed
compiler errors back to the LLM until it builds."""
from __future__ import annotations

import hashlib
import json

from ..compiler import compile_sketch, static_check, trim_errors
from ..schema import Decision, Evidence
from . import Ctx, requirements_brief, slug

STAGE = "firmware"

SYSTEM = """You are the firmware engineer of an autonomous ESP32 project agent.
Write ONE complete Arduino sketch (.ino) for the ESP32 DevKit V1 using the Arduino-ESP32 core 3.x.
Hard rules:
- Declare every GPIO from the pin table as a named constant at the top (e.g. `const int PIN_PUMP = 26;`)
  using EXACTLY those GPIO numbers. Do not invent pins.
- Arduino-ESP32 3.x APIs: PWM is `ledcAttach(pin, freq, resolution)` + `ledcWrite(pin, duty)`
  (NOT ledcSetup/ledcAttachPin). analogRead on ADC1 pins only when Wi-Fi is on.
- Non-blocking loop using millis(); no long delay() in loop.
- Serial.begin(115200) and short status logs.
- Wi-Fi credentials as placeholders: `const char* WIFI_SSID = "YOUR_SSID";`
- Only use libraries from the provided list (exact Library Manager names) plus the core (WiFi.h, Wire.h, SPI.h...).
- Small, named functions, one per feature where sensible, with a one-line comment each."""

SCHEMA = {
    "type": "object",
    "properties": {
        "filename": {"type": "string", "description": "snake_case.ino"},
        "sketch": {"type": "string", "description": "full .ino source"},
        "libraries": {"type": "array", "items": {"type": "string"}, "description": "exact Arduino Library Manager names"},
        "functions": {
            "type": "array",
            "items": {"type": "object", "properties": {"name": {"type": "string"}, "purpose": {"type": "string"}},
                      "required": ["name", "purpose"]},
        },
        "feature_map": {
            "type": "array",
            "items": {"type": "object", "properties": {"feature_id": {"type": "string"},
                                                       "functions": {"type": "array", "items": {"type": "string"}}},
                      "required": ["feature_id", "functions"]},
        },
        "notes": {"type": "string", "description": "calibration values / things the user must edit"},
    },
    "required": ["filename", "sketch", "libraries", "functions", "feature_map", "notes"],
}


def _pin_table(pins: list[dict]) -> tuple[str, list[int]]:
    rows, gpios = [], []
    for d in pins:
        c = d["choice"]
        rows.append(f"{c['part_id']}.{c['part_pin']} -> {c['esp32_pin']} ({c['direction']}, {c['signal_type']})")
        if c.get("gpio") is not None:
            gpios.append(int(c["gpio"]))
    return "\n".join(rows), gpios


def run(ctx: Ctx) -> None:
    parts = ctx.store.active(ctx.run_id, prefix="part.")
    pins = ctx.store.active(ctx.run_id, prefix="pin.")
    support = ctx.store.get_active(ctx.run_id, "wiring.support")
    pin_table, gpios = _pin_table(pins)
    libs = sorted({d["choice"]["library"] for d in parts if d["choice"].get("library")})
    parts_txt = "\n".join(f"- {d['key'].split('.', 1)[1]}: {d['choice']['name']} ({d['choice']['model']}) "
                          f"{(d['choice'].get('verification_notes') or '')[:150]}" for d in parts)
    base = (f"{requirements_brief(ctx)}\n\nPARTS:\n{parts_txt}\n\nPIN TABLE (authoritative):\n{pin_table}\n\n"
            f"SUPPORT COMPONENTS: {json.dumps(support['choice']) if support else 'none'}\n\n"
            f"ALLOWED LIBRARIES: {libs or 'none'}\n\nWrite the sketch." + ctx.precedent_block(STAGE))
    if ctx.rebuilding:
        prev = ctx.store.latest_artifacts(ctx.run_id).get("firmware")
        base += ctx.revision_block(STAGE)
        if prev:
            base += ("\n\nCURRENT SKETCH (edit it rather than rewriting; keep working code):\n```cpp\n"
                     + prev["content"]["code"] + "\n```")

    prompt, out, result, problems = base, None, None, []
    attempts = 0
    for attempts in range(1, ctx.settings.max_fix_attempts + 2):
        out = ctx.llm.call_tool(system=SYSTEM, prompt=prompt, tool_name="write_firmware",
                                tool_description="Record the firmware.", schema=SCHEMA, max_tokens=16000)
        problems = static_check(out["sketch"], gpios)
        result = compile_sketch(out["sketch"], out["filename"], out["libraries"], ctx.settings)
        ctx.emit(STAGE, f"attempt {attempts}: compile={result.status}"
                        + (f", {result.size}" if result.size else "")
                        + (f", static issues: {'; '.join(problems)}" if problems else ""))
        if result.status != "failed" and not problems:
            break
        if attempts > ctx.settings.max_fix_attempts:
            break
        feedback = []
        if result.status == "failed":
            feedback.append("COMPILER ERRORS:\n" + trim_errors(result.log))
        if problems:
            feedback.append("STATIC CHECK PROBLEMS:\n" + "\n".join(f"- {p}" for p in problems))
        prompt = base + "\n\nYOUR PREVIOUS SKETCH:\n```cpp\n" + out["sketch"] + "\n```\n\n" + \
            "\n\n".join(feedback) + "\n\nReturn the full corrected sketch."
    assert out is not None and result is not None
    if result.status == "skipped":
        ctx.emit(STAGE, result.log, "warn")
    if result.status == "failed":
        ctx.emit(STAGE, f"Firmware still fails to compile after {attempts} attempts", "error")

    filename = slug(out["filename"].removesuffix(".ino")) + ".ino"
    deps = [d["key"] for d in pins] + [d["key"] for d in parts]
    deps += [d["key"] for d in ctx.store.active(ctx.run_id, prefix="req.feature.")]
    if support:
        deps.append("wiring.support")
    conf = {"ok": 0.9, "skipped": 0.6, "failed": 0.2}[result.status] - 0.1 * len(problems)
    ctx.store.record(Decision(
        run_id=ctx.run_id, key="fw.sketch", stage=STAGE, kind="firmware",
        title=f"{filename}: compile {result.status}",
        choice={"filename": filename, "libraries": out["libraries"], "functions": out["functions"],
                "feature_map": out["feature_map"], "compile_status": result.status, "attempts": attempts,
                "size": result.size, "static_issues": problems,
                "sha256": hashlib.sha256(out["sketch"].encode()).hexdigest()},
        rationale=out.get("notes", ""),
        depends_on=ctx.store.refs(ctx.run_id, deps),
        evidence=[Evidence(source="arduino-cli", title=f"compile {result.status}", snippet=result.log[-800:])],
        confidence=round(max(conf, 0.05), 2),
    ))
    fw_ref = ctx.store.refs(ctx.run_id, ["fw.sketch"])
    ctx.store.save_artifact(ctx.run_id, "firmware", {"filename": filename, "code": out["sketch"],
                                                     "libraries": out["libraries"], "notes": out.get("notes", "")}, fw_ref)
    ctx.store.save_artifact(ctx.run_id, "compile_log", {"status": result.status, "log": result.log,
                                                        "size": result.size, "attempts": attempts,
                                                        "fqbn": ctx.settings.fqbn}, fw_ref)
