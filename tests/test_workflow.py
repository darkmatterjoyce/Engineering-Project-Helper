"""Merged workflow (offline, mock LLM): questions → criteria before design → build → procurement →
blind review → logbook reconsider → engineer accept/reject → final acceptance check → verdicts → improve."""
import os

os.environ["EPH_MOCK"] = "1"
os.environ.pop("MONGODB_URI", None)

import pytest  # noqa: E402

from eph.pipeline import apply_verdict, ask, create_run, execute, improve, judge, make_services  # noqa: E402
from eph.precedents import reviewer_block  # noqa: E402

PROMPT = "an ESP32 that waters my plant when the soil is dry"


@pytest.fixture(scope="module")
def flow():
    svc = make_services()
    qs = ask(svc, PROMPT)
    run_id = create_run(svc, PROMPT, [{"q": qs[0], "a": "indoors"}, {"q": qs[1], "a": "£30"}])
    assert execute(svc, run_id), svc.store.get_run(run_id)["error"]
    return svc, run_id, qs


def test_step1_asks_at_most_two_questions(flow):
    _, _, qs = flow
    assert 1 <= len(qs) <= 2


def test_budget_from_clarification(flow):
    svc, run_id, _ = flow
    assert svc.store.get_active(run_id, "req.budget")["choice"]["gbp"] == 30


def test_criteria_written_before_design(flow):
    svc, run_id, _ = flow
    run = svc.store.get_run(run_id)
    msgs = [e["message"] for e in run["events"]]
    i_crit = next(i for i, m in enumerate(msgs) if "acceptance criteria before the design" in m)
    i_req = next(i for i, e in enumerate(run["events"]) if e["stage"] == "requirements")
    assert i_crit < i_req
    assert len(run["acceptance_criteria"]) == 5


def test_cart_contains_mechanical_parts_and_total(flow):
    svc, run_id, _ = flow
    keys = {d["key"] for d in svc.store.active(run_id, prefix="cart.")}
    assert {"cart.reservoir", "cart.tubing", "cart.total", "cart.support"} <= keys
    total = svc.store.get_active(run_id, "cart.total")["choice"]
    assert total["budget_gbp"] == 30 and not total["over_budget"]


def test_two_stage_review_and_engineer_voice(flow):
    svc, run_id, _ = flow
    revs = {r["key"]: r for r in svc.store.list_reviews(run_id) if r["version"] == 1}
    assert revs["req.assumptions"]["after_logbook"] == "withdrawn"
    assert revs["wiring.support"]["engineer_response"] == "accept"
    oled = svc.store.get_active(run_id, "part.oled")["review"]
    assert oled["after_logbook"] == "kept" and oled["engineer_response"] == "reject"
    # accepted challenge was actually applied
    sup = svc.store.get_active(run_id, "wiring.support")
    assert sup["version"] == 2 and any("470" in s.get("value", "") for s in sup["choice"])
    assert svc.store.get_run(run_id)["review_status"] == "disagreements"


def test_final_acceptance_is_honest(flow):
    svc, run_id, _ = flow
    run = svc.store.get_run(run_id)
    st = {c["id"]: c["status"] for c in run["acceptance_criteria"]}
    assert st["AC4"] == "unverified"                     # firmware never compiled
    assert run["delivery_status"] == "delivered_with_gaps"
    assert any("NOT compiled" in g for g in run["gaps"])
    assert any("estimates" in g for g in run["gaps"])


def _fresh_run():
    svc = make_services()
    run_id = create_run(svc, PROMPT, [])
    assert execute(svc, run_id), svc.store.get_run(run_id)["error"]
    assert svc.store.get_run(run_id)["review_status"] == "disagreements"
    return svc, run_id


def test_bar_raiser_right_applies_the_fix_and_becomes_precedent():
    svc, run_id = _fresh_run()
    oled = svc.store.get_active(run_id, "part.oled")
    out = judge(svc, run_id, "part.oled", oled["version"], "bar_raiser_right", "I never look at the screen")
    assert out["rebuilding"] and "Drop the OLED" in out["target"]["note"]
    assert apply_verdict(svc, run_id, out["target"]), svc.store.get_run(run_id)["error"]
    assert not svc.store.get_active(run_id, "part.oled")                 # the fix was applied
    assert not svc.store.active(run_id, prefix="pin.oled.")
    assert "OLED" not in svc.store.get_active(run_id, "req.summary")["choice"]["summary"]
    assert "SSD1306" not in svc.store.latest_artifacts(run_id)["firmware"]["content"]["code"]
    assert svc.store.get_run(run_id)["review_status"] != "disagreements"
    rec = svc.store.precedents.find_one({"source_run_id": run_id})
    ch = next(c for c in rec["challenges"] if c["key"] == "part.oled")
    assert ch["user_verdict"] == "bar_raiser_right" and ch["resolution"] == "retired"
    assert "USER SIDED WITH THE BAR RAISER" in reviewer_block([rec])


def test_engineer_right_keeps_design_and_clears_disagreement():
    svc, run_id = _fresh_run()
    oled = svc.store.get_active(run_id, "part.oled")
    out = judge(svc, run_id, "part.oled", oled["version"], "engineer_right", "I like having a screen")
    assert not out["rebuilding"] and out["precedent_saved"]
    assert svc.store.get_active(run_id, "part.oled")["version"] == oled["version"]
    assert svc.store.get_run(run_id)["review_status"] == "judged"
    rec = svc.store.precedents.find_one({"source_run_id": run_id})
    assert next(c for c in rec["challenges"] if c["key"] == "part.oled")["resolution"] == "kept"
    with pytest.raises(ValueError):                                      # stale version is refused
        judge(svc, run_id, "part.oled", oled["version"] - 1, "engineer_right")


def test_neither_with_reason_applies_users_instruction():
    svc, run_id = _fresh_run()
    oled = svc.store.get_active(run_id, "part.oled")
    out = judge(svc, run_id, "part.oled", oled["version"], "neither", "use a 16x2 I2C LCD instead")
    assert out["rebuilding"] and "use a 16x2 I2C LCD instead" in out["target"]["note"]
    assert apply_verdict(svc, run_id, out["target"])
    assert svc.store.get_active(run_id, "part.lcd")


def test_cart_does_not_double_count_support_parts(flow):
    svc, run_id, _ = flow
    lines = svc.store.latest_artifacts(run_id)["cart"]["content"]["lines"]
    diode_parts = [l for l in lines if "1N4007" in l["model"]]
    assert len(diode_parts) == 1, [l["model"] for l in lines]


def test_withdrawn_challenge_is_not_open(flow):
    svc, run_id, _ = flow
    from eph.precedents import resolution
    for r in svc.store.list_reviews(run_id):
        if r.get("after_logbook") == "withdrawn":
            assert resolution(r, svc.store.get_active(run_id, r["key"])["version"]) == "withdrawn"


def test_improve_resumes_same_run(flow):
    svc, run_id, _ = flow
    assert improve(svc, run_id, "use a 16x2 I2C LCD instead of the OLED")
    run = svc.store.get_run(run_id)
    assert run["improvements"][-1]["targets"] == ["part.oled"]
    assert svc.store.get_active(run_id, "part.lcd") and not svc.store.get_active(run_id, "part.oled")
    assert any(c["source"] == "improvement" for c in run["acceptance_criteria"])
    assert svc.store.get_active(run_id, "cart.lcd")           # procurement followed the change


def test_memory_learns_from_users_verdict():
    """User sides with the Bar Raiser on the OLED in one project; a NEW similar project then starts without
    an OLED, because the precedent records the user's verdict."""
    from eph.pipeline import apply_verdict
    svc = make_services()
    run_id = create_run(svc, PROMPT, [{"q": "budget?", "a": "£30"}])
    assert execute(svc, run_id)
    oled = svc.store.get_active(run_id, "part.oled")
    out = judge(svc, run_id, "part.oled", oled["version"], "bar_raiser_right", "I never look at the screen")
    assert apply_verdict(svc, run_id, out["target"]), svc.store.get_run(run_id)["error"]
    assert not svc.store.get_active(run_id, "part.oled")
    run2 = create_run(svc, "an ESP32 that waters my garden plants when the soil is dry")
    assert execute(svc, run2)
    assert svc.store.get_run(run2)["precedents"], "should recall the first project"
    assert not svc.store.get_active(run2, "part.oled")      # learned from the user's verdict


def test_build_guide_follows_the_design(flow):
    svc, run_id, _ = flow
    g = svc.store.get_active(run_id, "guide.build")
    assert g and len(g["choice"]["build_steps"]) >= 5 and g["choice"]["what_it_is"]
    deps = {d["key"] for d in g["depends_on"]}
    assert {"fw.sketch", "cart.total", "part.soil_sensor"} <= deps        # rewritten when the design changes
    assert "guide" in svc.store.latest_artifacts(run_id)


def test_build_guide_is_rewritten_after_a_part_change():
    from eph.pipeline import rebuild
    svc = make_services()
    run_id = create_run(svc, PROMPT)
    assert execute(svc, run_id)
    assert "OLED" in svc.store.get_active(run_id, "guide.build")["choice"]["what_it_is"]
    assert rebuild(svc, run_id, "part.oled", "Use a 16x2 I2C LCD instead of the OLED")
    g = svc.store.get_active(run_id, "guide.build")
    assert g["version"] >= 2 and "16x2 I2C LCD" in g["choice"]["what_it_is"]


def test_lcd_feedback_works_after_oled_was_removed():
    from eph.pipeline import apply_verdict
    svc = make_services()
    run_id = create_run(svc, PROMPT)
    assert execute(svc, run_id)
    oled = svc.store.get_active(run_id, "part.oled")
    out = judge(svc, run_id, "part.oled", oled["version"], "bar_raiser_right", "")
    assert apply_verdict(svc, run_id, out["target"])
    assert not svc.store.get_active(run_id, "part.oled")
    assert improve(svc, run_id, "use a 16x2 I2C LCD for the status")
    assert svc.store.get_active(run_id, "part.lcd")
