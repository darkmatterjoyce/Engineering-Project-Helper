"""Offline tests: mock LLM + mock search + in-memory Mongo.  Run: pytest -q"""
import os

os.environ["EPH_MOCK"] = "1"
os.environ.pop("MONGODB_URI", None)

import pytest  # noqa: E402

from eph import esp32  # noqa: E402
from eph.compiler import static_check, trim_errors  # noqa: E402
from eph.pipeline import create_run, execute, make_services  # noqa: E402
from eph.schema import Decision  # noqa: E402


@pytest.fixture(scope="module")
def done_run():
    svc = make_services()
    run_id = create_run(svc, "an ESP32 that waters my plant when the soil is dry")
    assert execute(svc, run_id), svc.store.get_run(run_id)["error"]
    return svc, run_id


def test_every_stage_writes_decisions(done_run):
    svc, run_id = done_run
    stages = {d["stage"] for d in svc.store.active(run_id)}
    assert stages == {"requirements", "parts", "wiring", "procurement", "firmware", "diagram", "guide"}


def test_dependencies_resolve_and_nothing_is_stale(done_run):
    svc, run_id = done_run
    keys = {d["key"] for d in svc.store.active(run_id)}
    for d in svc.store.active(run_id):
        for dep in d["depends_on"]:
            assert dep["key"] in keys, f"{d['key']} depends on missing {dep['key']}"
    assert svc.store.stale(run_id) == []


def test_artifacts_present(done_run):
    svc, run_id = done_run
    arts = svc.store.latest_artifacts(run_id)
    assert {"parts_list", "wiring", "firmware", "compile_log", "flow_diagram"} <= set(arts)
    assert "void setup()" in arts["firmware"]["content"]["code"]
    assert arts["flow_diagram"]["content"]["mermaid"].startswith("flowchart")


def test_validator_fix_loop_moved_adc_off_adc2(done_run):
    svc, run_id = done_run
    aout = svc.store.get_active(run_id, "pin.soil_sensor.aout")
    assert aout["choice"]["gpio"] in esp32.ADC1  # mock's first answer used ADC2 GPIO4


def test_parts_have_citations(done_run):
    svc, run_id = done_run
    sensor = svc.store.get_active(run_id, "part.soil_sensor")
    assert sensor["choice"]["verified"] and sensor["evidence"][0]["url"]


def test_versioning_supersede_and_staleness(done_run):
    svc, run_id = done_run
    old = svc.store.get_active(run_id, "part.soil_sensor")
    downstream = svc.store.downstream(run_id, "part.soil_sensor")
    assert "pin.soil_sensor.aout" in downstream and "fw.sketch" in downstream and "diag.flow" in downstream
    svc.store.record(Decision(run_id=run_id, key="part.soil_sensor", stage="parts", kind="part",
                              title="changed", choice={**old["choice"], "model": "other"}))
    assert svc.store.get_active(run_id, "part.soil_sensor")["version"] == old["version"] + 1
    assert [h["status"] for h in svc.store.history(run_id, "part.soil_sensor")] == ["superseded", "active"]
    stale = {d["key"] for d in svc.store.stale(run_id)}
    assert "pin.soil_sensor.aout" in stale  # direct dependents now point at an old version


def test_search_cache_hits_on_rerun(done_run):
    svc, _ = done_run
    res = svc.search_mock.search("Capacitive Soil Moisture Sensor v1.2 pinout operating voltage 3.3V output")
    assert res.cached


@pytest.mark.parametrize("pin,direction,signal,wifi,expect", [
    ("GPIO6", "output", "digital", False, "flash"),
    ("GPIO34", "output", "digital", False, "input-only"),
    ("GPIO4", "analog_in", "analog", True, "ADC2"),
    ("GPIO4", "analog_in", "analog", False, None),
    ("GPIO12", "output", "digital", False, "strapping"),
    ("GPIO99", "output", "digital", False, "not broken out"),
])
def test_esp32_validator(pin, direction, signal, wifi, expect):
    issues = esp32.validate([{"part_id": "x", "part_pin": "S", "esp32_pin": pin,
                              "direction": direction, "signal_type": signal}], wifi=wifi)
    text = " ".join(i.message for i in issues)
    assert (expect in text) if expect else not issues


def test_shared_i2c_bus_allowed_but_gpio_clash_rejected():
    ok = esp32.validate([
        {"part_id": "a", "part_pin": "SDA", "esp32_pin": "21", "direction": "bidirectional", "signal_type": "i2c_sda"},
        {"part_id": "b", "part_pin": "SDA", "esp32_pin": "GPIO21", "direction": "bidirectional", "signal_type": "i2c_sda"},
    ], wifi=True)
    assert not ok
    clash = esp32.validate([
        {"part_id": "a", "part_pin": "IN", "esp32_pin": "GPIO26", "direction": "output", "signal_type": "digital"},
        {"part_id": "b", "part_pin": "IN", "esp32_pin": "GPIO26", "direction": "output", "signal_type": "digital"},
    ], wifi=False)
    assert any("several" in i.message for i in clash)


def test_static_check_and_error_trim():
    assert "GPIO26 from the pin map never appears in the sketch" in static_check("void setup(){} void loop(){}", [26])
    log = "noise\nfoo.ino:31:3: error: 'ledcSetup' was not declared in this scope\nmore noise"
    assert "ledcSetup" in trim_errors(log) and "noise" not in trim_errors(log)


# ---------------------------------------------------------------- Bar Raiser + partial rebuilds
from eph.pipeline import plan_rebuild, rebuild  # noqa: E402


@pytest.fixture(scope="module")
def reviewed_run():
    svc = make_services()
    run_id = create_run(svc, "an ESP32 that waters my plant when the soil is dry")
    assert execute(svc, run_id), svc.store.get_run(run_id)["error"]
    return svc, run_id


def test_every_decision_gets_reviewed(reviewed_run):
    svc, run_id = reviewed_run
    assert all(d.get("review") for d in svc.store.active(run_id) if d["stage"] != "guide")  # the build guide is documentation, not reviewed
    # the mock engineer rejects the medium OLED challenge → a disagreement for the user
    assert svc.store.get_run(run_id)["review_status"] in ("approved", "approved_with_notes", "disagreements")


def test_high_challenge_is_rebuilt_automatically(reviewed_run):
    svc, run_id = reviewed_run
    hist = svc.store.history(run_id, "wiring.support")
    assert [h["status"] for h in hist] == ["superseded", "active"]
    assert hist[0]["review"]["severity"] == "high" and hist[0]["review"]["verdict"] == "challenge"
    v2 = hist[1]
    assert v2["author"] == "bar_raiser" and v2["trigger"]["source"] == "bar_raiser"
    assert any("capacitor" in s["name"].lower() for s in v2["choice"])
    assert v2["review"]["verdict"] == "approve"  # re-reviewed in round 2
    # only the wiring stage and its dependents changed; unrelated decisions kept version 1
    assert svc.store.get_active(run_id, "pin.soil_sensor.aout")["version"] == 1
    assert svc.store.get_active(run_id, "part.oled")["version"] == 1
    assert svc.store.stale(run_id) == []


def test_low_challenges_stay_as_notes(reviewed_run):
    svc, run_id = reviewed_run
    g = svc.store.get_active(run_id, "pin.mosfet.g")
    assert g["review"]["verdict"] == "challenge" and g["review"]["severity"] == "low" and g["version"] == 1


def test_plan_rebuild_only_touches_affected_stages(reviewed_run):
    svc, run_id = reviewed_run
    stages, revs = plan_rebuild(svc.store, run_id, [{"key": "fw.sketch", "note": "x", "source": "user"}])
    assert stages == ["firmware", "diagram", "guide"] and list(revs) == ["firmware"]
    stages, _ = plan_rebuild(svc.store, run_id, [{"key": "part.oled", "note": "x", "source": "user"}])
    assert stages == ["parts", "wiring", "procurement", "firmware", "diagram", "guide"]


def test_user_rebuild_swaps_part_and_retires_old_decisions(reviewed_run):
    svc, run_id = reviewed_run
    before = {d["key"]: d["version"] for d in svc.store.active(run_id)}
    assert rebuild(svc, run_id, "part.oled", "Use a 16x2 I2C LCD instead of the OLED"), svc.store.get_run(run_id)["error"]
    after = {d["key"]: d["version"] for d in svc.store.active(run_id)}
    assert "part.oled" not in after and "part.lcd" in after and "pin.lcd.sda" in after
    assert svc.store.history(run_id, "part.oled")[-1]["status"] == "retired"
    assert after["pin.soil_sensor.aout"] == before["pin.soil_sensor.aout"]   # untouched
    assert after["req.power"] == before["req.power"]                       # requirements untouched...
    summary = svc.store.get_active(run_id, "req.summary")["choice"]["summary"]
    assert "LCD" in summary and "OLED" not in summary                      # ...but the summary follows the parts
    assert "LiquidCrystal" in svc.store.latest_artifacts(run_id)["firmware"]["content"]["code"]
    rb = svc.store.get_run(run_id)["rebuilds"][-1]
    assert rb["source"] == "user" and "part.oled" in rb["retired"]
    assert all(d.get("review") for d in svc.store.active(run_id) if d["stage"] != "guide")  # the build guide is documentation, not reviewed  # new versions were re-reviewed
    assert svc.store.stale(run_id) == []


def test_identical_redecision_keeps_version(reviewed_run):
    svc, run_id = reviewed_run
    d = svc.store.get_active(run_id, "req.power")
    from eph.schema import DepRef
    again = Decision(run_id=run_id, key="req.power", stage="requirements", kind="power", title=d["title"],
                     choice=d["choice"], depends_on=[DepRef(**x) for x in d["depends_on"]])
    assert svc.store.record(again)["version"] == d["version"]


# ---------------------------------------------------------------- precedents (memory) + breadboard view
from eph import breadboard  # noqa: E402
from eph.precedents import HashEmbedder, _cosine  # noqa: E402


def test_second_similar_run_learns_from_first():
    svc = make_services()
    svc.store.precedents.delete_many({})
    r1 = create_run(svc, "an ESP32 that waters my plant when the soil is dry")
    assert execute(svc, r1)
    assert svc.store.get_run(r1)["rebuilds"], "first run should need a Bar Raiser rebuild"
    prec = svc.store.precedents.find_one({"source_run_id": r1})
    assert any(c["key"] == "wiring.support" and c["severity"] == "high" and c["resolution"] == "fixed"
               for c in prec["challenges"])
    r2 = create_run(svc, "water my garden when the soil is dry")
    assert execute(svc, r2)
    run2 = svc.store.get_run(r2)
    assert [p["prompt"] for p in run2["precedents"]] == ["an ESP32 that waters my plant when the soil is dry"]
    assert run2["rebuilds"] == []                     # capacitor designed in from the start
    sup = svc.store.get_active(r2, "wiring.support")
    assert sup["version"] == 1 and any("capacitor" in s["name"].lower() for s in sup["choice"])
    assert sup["precedent_ids"] == [str(prec["_id"])]
    assert svc.store.precedents.find_one({"_id": prec["_id"]})["uses"] == 1
    r3 = create_run(svc, "a doorbell that sends a phone notification")
    execute(svc, r3)
    assert svc.store.get_run(r3)["precedents"] == []  # unrelated idea: nothing recalled


def test_hash_embedding_ranks_related_above_unrelated():
    e = HashEmbedder()
    doc = e.embed(["an ESP32 that waters my plant when the soil is dry | Smart Plant Waterer"])[0]
    related = _cosine(e.embed(["water my garden when the soil is dry"])[0], doc)
    unrelated = _cosine(e.embed(["a desk air-quality monitor with CO2 readings"])[0], doc)
    assert related > 0.2 > unrelated


def test_breadboard_svg_has_every_wire(done_run):
    svc, run_id = done_run
    w = svc.store.latest_artifacts(run_id)["wiring"]["content"]
    svg = w["breadboard_svg"]
    assert svg.startswith("<svg") and w["breadboard_notes"] == []
    for c in w["connections"]:
        if isinstance(esp32.parse_pin(c["esp32_pin"]), int):
            assert f"GPIO{esp32.parse_pin(c['esp32_pin'])} → {c['part_id']}.{c['part_pin']}" in svg
    assert "ESP32 DevKit V1" in svg and "Pump return" in svg


def test_breadboard_reports_unplaceable_things():
    svg, notes = breadboard.render(
        [{"part_id": "x", "part_pin": "S", "esp32_pin": "GPIO32", "direction": "input", "signal_type": "digital"}],
        {"x": {"name": "Thing <&>", "pins": ["S"]}},
        [{"name": "Mystery", "kind": "resistor", "ends": ["nope.A", "GND"]}])
    assert "&lt;&amp;&gt;" in svg                      # labels are escaped
    assert any("Mystery" in n for n in notes)


# ---------------------------------------------------------------- interactive firmware walkthrough
from eph import codemap  # noqa: E402
from eph.explain import explain_lines  # noqa: E402


def test_codemap_blocks_cover_functions():
    code = "#include <X.h>\n\nconst int A = 1;\n\n// does a thing\nvoid foo() {\n  if (A) { bar(\"}\"); }\n}\n\nvoid loop() {\n}\n"
    bl = {b["id"]: (b["start"], b["end"]) for b in codemap.blocks(code)}
    assert bl["preamble1"] == (1, 1) and bl["globals1"] == (3, 3)
    assert bl["fn_foo"] == (5, 8)      # leading comment belongs to the function; brace in a string ignored
    assert bl["fn_loop"] == (10, 11)


def test_clean_steps_rejects_bad_llm_ranges():
    steps = [{"node_id": "A", "start_line": 9, "end_line": 3, "explanation": "x"},     # reversed → fixed
             {"node_id": "B", "start_line": 5, "end_line": 999, "explanation": "y"},   # clamped
             {"node_id": "Z", "start_line": 1, "end_line": 2, "explanation": "z"},     # unknown node → dropped
             {"node_id": "C", "start_line": 500, "end_line": 600, "explanation": "w"}] # outside file → dropped
    out = codemap.clean_steps(steps, 20, {"A", "B", "C"})
    assert [(s["node_id"], s["start"], s["end"]) for s in out] == [("A", 3, 9), ("B", 5, 20)]


def test_walkthrough_links_every_diagram_node(done_run):
    svc, run_id = done_run
    arts = svc.store.latest_artifacts(run_id)
    walk = arts["flow_diagram"]["content"]["walkthrough"]
    code = arts["firmware"]["content"]["code"].splitlines()
    assert {s["node_id"] for s in walk["steps"]} == set(walk["nodes"])
    assert all(b["explanation"] for b in walk["blocks"])
    pump_off = next(s for s in walk["steps"] if s["node_id"] == "F")
    assert "setPump(false)" in "\n".join(code[pump_off["start"] - 1:pump_off["end"]])


def test_explain_lines_is_cached_and_bounded(done_run):
    svc, run_id = done_run
    first = explain_lines(svc.store, svc.default_llm(), run_id, 31, 35)
    again = explain_lines(svc.store, svc.default_llm(), run_id, 35, 31)   # reversed range = same selection
    assert not first["cached"] and again["cached"] and again["explanation"] == first["explanation"]
    with pytest.raises(ValueError):
        explain_lines(svc.store, svc.default_llm(), run_id, 1, 200)


# ---------------------------------------------------------------- model menu (Engineer / Bar Raiser pickers)
from eph import models as model_menu  # noqa: E402
from eph.config import get_settings  # noqa: E402


def _settings(monkeypatch, **env):
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
              "EPH_LLM_PROVIDER", "EPH_MODEL", "EPH_REVIEW_MODEL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return get_settings()


def test_model_menu_greys_out_models_without_keys(monkeypatch):
    s = _settings(monkeypatch, EPH_MOCK="0", GEMINI_API_KEY="g")
    rows = {r["id"]: r for r in model_menu.listing(s)}
    assert rows["offline"]["available"] and rows["gemini:gemini-3.8-flash"]["available"]
    assert not rows["anthropic:claude-opus-5-5"]["available"]
    assert "ANTHROPIC_API_KEY" in rows["anthropic:claude-opus-5-5"]["reason"]
    with pytest.raises(ValueError):
        model_menu.validate(s, "anthropic:claude-opus-5-5")


def test_bar_raiser_defaults_to_a_different_ai(monkeypatch):
    s = _settings(monkeypatch, EPH_MOCK="0", ANTHROPIC_API_KEY="a", GEMINI_API_KEY="g")
    d = model_menu.defaults(s)
    assert d["engineer"].startswith("anthropic:") and d["bar_raiser"].startswith("gemini:")
    s = _settings(monkeypatch, EPH_MOCK="0", GEMINI_API_KEY="g")          # one company only → different model
    d = model_menu.defaults(s)
    assert d["engineer"] != d["bar_raiser"] and d["bar_raiser"].startswith("gemini:")
    s = _settings(monkeypatch, EPH_MOCK="1", GEMINI_API_KEY="g")          # offline mode → offline defaults
    assert model_menu.defaults(s) == {"engineer": "offline", "bar_raiser": "offline"}


def test_run_records_chosen_models_and_rejects_unusable(monkeypatch):
    svc = make_services()
    rid = create_run(svc, "an ESP32 that waters my plant when the soil is dry", [], "offline", "offline")
    run = svc.store.get_run(rid)
    assert run["models"]["engineer"] == "offline" and run["models"]["bar_raiser_label"].startswith("Offline")
    with pytest.raises(ValueError):
        create_run(svc, "x", [], "anthropic:claude-opus-5-5", "offline")   # no key in the test env
