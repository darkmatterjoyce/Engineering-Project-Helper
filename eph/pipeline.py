"""Orchestrator: runs stages against one run's decision log, then the Bar Raiser review loop.
Also the entry point for partial rebuilds (user change requests and Bar Raiser challenges)."""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field

from bson import ObjectId

from . import bar_raiser, clarify, engineer, models, precedents
from .config import Settings, get_settings
from .db import Store
from .llm import LLM
from .schema import STAGE_ORDER, now
from .search import CachedSearch, Search, make_search
from .stages import Ctx, diagram, firmware, guide, parts, procurement, requirements, wiring

STAGES = {
    "requirements": requirements.run,
    "parts": parts.run,
    "wiring": wiring.run,
    "procurement": procurement.run,
    "firmware": firmware.run,
    "diagram": diagram.run,
    "guide": guide.run,
}


@dataclass
class Services:
    settings: Settings
    store: Store
    search_real: Search          # Linkup / Tavily / none (used with real models)
    search_mock: Search          # canned sources (used with the offline demo model)
    embedder: precedents.Embedder
    memory: str = ""             # vector search status
    _llms: dict = field(default_factory=dict)

    def llm(self, model_id: str) -> LLM:
        """Client for a model id from the catalogue (cached)."""
        if model_id not in self._llms:
            self._llms[model_id] = models.build(self.settings, model_id)
        return self._llms[model_id]

    def default_llm(self, role: str = "engineer") -> LLM:
        return self.llm(models.defaults(self.settings)[role])

    @property
    def providers(self) -> dict[str, str]:
        return {"search": self.search_real.name, "db": self.store.backend,
                "embeddings": self.embedder.name, "memory": self.memory}


def make_services(settings: Settings | None = None) -> Services:
    s = settings or get_settings()
    store = Store(s)
    emb = precedents.make_embedder(s)
    from .mock import MockSearch

    real = make_search(s.__class__(**{**s.__dict__, "mock": False}), store)
    return Services(settings=s, store=store, search_real=real, search_mock=CachedSearch(MockSearch(), store),
                    embedder=emb, memory=precedents.ensure_vector_index(store, emb) if s.precedents else "off")


def run_models(svc: Services, run: dict | None) -> dict:
    """The run's chosen models (older runs fall back to the current defaults)."""
    m = dict((run or {}).get("models") or {})
    d = models.defaults(svc.settings)
    m.setdefault("engineer", d["engineer"])
    m.setdefault("bar_raiser", d["bar_raiser"])
    return m


PRECEDENT_STAGES = {"requirements", "parts", "wiring", "firmware"}


def ask(svc: Services, prompt: str, engineer_model: str | None = None) -> list[str]:
    """Step 1: 0-2 essential clarifying questions (asked by the chosen Engineer model)."""
    if not prompt.strip():
        raise ValueError("empty prompt")
    mid = engineer_model or models.defaults(svc.settings)["engineer"]
    return clarify.questions(svc.llm(mid), prompt.strip())


def create_run(svc: Services, prompt: str, clarifications: list[dict] | None = None,
               engineer_model: str | None = None, bar_raiser_model: str | None = None) -> str:
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("empty prompt")
    d = models.defaults(svc.settings)
    eng = models.validate(svc.settings, engineer_model or d["engineer"])
    br = models.validate(svc.settings, bar_raiser_model or d["bar_raiser"])
    chosen = {"engineer": eng.id, "engineer_label": eng.label, "bar_raiser": br.id, "bar_raiser_label": br.label}
    providers = {**svc.providers, "llm": eng.label, "reviewer": br.label,
                 "search": svc.search_mock.name if eng.provider == "mock" else svc.search_real.name}
    qa = [{"q": c.get("q", ""), "a": c.get("a", "")} for c in (clarifications or []) if c.get("a", "").strip()]
    return svc.store.create_run(prompt, eng.label, providers, qa, chosen)


def _ctx(svc: Services, run_id: str) -> Ctx:
    run = svc.store.get_run(run_id)
    if not run:
        raise KeyError(run_id)
    precs = []
    for ref in run.get("precedents", []):
        p = svc.store.precedents.find_one({"_id": ObjectId(ref["id"])}, {"embedding": 0, "text": 0})
        if p:
            p["score"] = ref["score"]
            precs.append(p)
    m = run_models(svc, run)
    offline = m["engineer"] == models.OFFLINE
    return Ctx(run_id=run_id, prompt=run["prompt"], store=svc.store, llm=svc.llm(m["engineer"]),
               reviewer=svc.llm(m["bar_raiser"]), search=svc.search_mock if offline else svc.search_real,
               settings=svc.settings, precedents=precs,
               clarifications=run.get("clarifications", []))


def _recall(svc: Services, ctx: Ctx) -> None:
    """Retrieve similar past projects before planning (Atlas Vector Search)."""
    if not svc.settings.precedents:
        return
    min_score = svc.settings.precedent_min_score
    if min_score is None:
        min_score = precedents.default_min_score(svc.embedder)
    hits, method = precedents.find_similar(svc.store, svc.embedder, ctx.prompt, svc.settings.precedent_k,
                                           min_score, exclude_run=ctx.run_id)
    refs = [{"id": str(h["_id"]), "prompt": h["prompt"], "project_name": h.get("project_name", ""),
             "score": round(h["score"], 3)} for h in hits]
    svc.store.update_run(ctx.run_id, precedents=refs, precedent_search=method)
    for h in hits:
        svc.store.precedents.update_one({"_id": h["_id"]}, {"$inc": {"uses": 1}})
        h["score"] = round(h["score"], 3)
    ctx.precedents = hits
    if hits:
        n_chal = sum(1 for h in hits for c in h.get("challenges", []) if c["severity"] in ("high", "medium"))
        listed = ", ".join('"%s" %.2f' % (h["prompt"][:40], h["score"]) for h in hits)
        ctx.emit("memory", f"{method}: {len(hits)} similar past project(s) ({listed}); "
                           f"{n_chal} past Bar Raiser challenge(s) to avoid")
    else:
        ctx.emit("memory", f"{method}: no similar past projects yet")


def _remember(svc: Services, ctx: Ctx) -> None:
    if not svc.settings.precedents:
        return
    try:
        rec = precedents.save_precedent(svc.store, svc.embedder, ctx.run_id)
    except Exception as e:  # noqa: BLE001  memory is a bonus; never fail a finished run over it
        ctx.emit("memory", f"couldn't save to memory ({type(e).__name__}: {str(e)[:160]})", "warn")
        return
    if rec:
        ctx.emit("memory", f"saved as a precedent ({len(rec['challenges'])} Bar Raiser challenge(s) recorded)")
    else:
        ctx.emit("memory", "not saved as a precedent (not approved by the Bar Raiser, or firmware failed)")


def _versions(store: Store, run_id: str) -> dict[str, int]:
    return {d["key"]: d["version"] for d in store.active(run_id)}


def _run_stages(svc: Services, ctx: Ctx, stages: list[str], trigger: dict | None = None) -> None:
    for name in stages:
        svc.store.update_run(ctx.run_id, current_stage=name)
        before = {d["key"] for d in svc.store.active(ctx.run_id, stage=name)}
        ctx.emit(name, "rebuilding" if ctx.rebuilding else "started")
        pids = [str(p["_id"]) for p in ctx.precedents] if name in PRECEDENT_STAGES else []
        svc.store.begin_stage(ctx.run_id, trigger, pids)
        stage_failed = False
        try:
            if name == "guide":   # the build guide is a write-up of the design, not part of it
                try:
                    STAGES[name](ctx)
                except Exception as e:  # noqa: BLE001
                    stage_failed = True
                    ctx.emit(name, f"build guide not written ({type(e).__name__}: {str(e)[:160]})", "warn")
            else:
                STAGES[name](ctx)
        finally:
            touched = svc.store.end_stage(ctx.run_id)
        dropped = set() if stage_failed else before - touched   # a failed write-up keeps its last version
        if dropped:
            svc.store.retire(ctx.run_id, dropped)
            ctx.emit(name, f"retired {len(dropped)} decision(s) no longer needed: {', '.join(sorted(dropped))}")


def plan_rebuild(store: Store, run_id: str, targets: list[dict]) -> tuple[list[str], dict[str, list[dict]]]:
    """targets: [{key, note, source}] → (stages to re-run in order, change requests per stage).
    Only the target's own stage and the stages owning its downstream decisions are re-run."""
    affected: set[str] = set()
    revisions: dict[str, list[dict]] = {}
    for t in targets:
        d = store.get_active(run_id, t["key"])
        if not d:
            raise KeyError(f"no active decision {t['key']}")
        affected.add(d["stage"])
        revisions.setdefault(d["stage"], []).append({"key": t["key"], "source": t["source"], "note": t["note"]})
        for k in store.downstream(run_id, t["key"]):
            dd = store.get_active(run_id, k)
            if dd:
                affected.add(dd["stage"])
    return [s for s in STAGE_ORDER if s in affected], revisions


def _rebuild(svc: Services, ctx: Ctx, targets: list[dict]) -> None:
    stages, revisions = plan_rebuild(svc.store, ctx.run_id, targets)
    before = _versions(svc.store, ctx.run_id)
    ctx.rebuilding, ctx.revisions = True, revisions
    src = targets[0]["source"]
    ctx.emit("rebuild", f"{src} change on {', '.join(t['key'] for t in targets)} → re-running {', '.join(stages)}")
    trigger = {"source": src, "key": targets[0]["key"], "note": targets[0]["note"][:300]}
    try:
        _run_stages(svc, ctx, stages, trigger)
    finally:
        ctx.rebuilding, ctx.revisions = False, {}
    if any(k.startswith("part.") and before.get(k) != v for k, v in _versions(svc.store, ctx.run_id).items()) or \
            any(k.startswith("part.") for k in set(before) - set(_versions(svc.store, ctx.run_id))):
        svc.store.begin_stage(ctx.run_id, trigger)
        try:
            requirements.refresh_summary(ctx)   # keep the one-line description true to the new parts
        except Exception as e:  # noqa: BLE001  cosmetic: keep the old summary rather than fail the run
            ctx.emit("rebuild", f"summary not refreshed ({type(e).__name__}: {str(e)[:160]})", "warn")
        finally:
            svc.store.end_stage(ctx.run_id)
    after = _versions(svc.store, ctx.run_id)
    changed = sorted(k for k, v in after.items() if before.get(k) != v)
    retired = sorted(set(before) - set(after))
    kept = len([k for k in after if before.get(k) == after[k]])
    svc.store.push_run(ctx.run_id, "rebuilds", {"at": now(), "source": src, "targets": targets,
                                                "stages": stages, "changed": changed, "retired": retired})
    ctx.emit("rebuild", f"done: {len(changed)} decisions changed, {len(retired)} retired, {kept} kept unchanged")


def _review_loop(svc: Services, ctx: Ctx) -> None:
    """Review never throws away a finished design: if a reviewer/engineer model fails part-way (quota,
    timeout, a cut-off answer), the run is delivered with the review marked unfinished."""
    try:
        _review_loop_inner(svc, ctx)
    except Exception as e:  # noqa: BLE001
        ctx.emit("review", f"review couldn't finish ({type(e).__name__}: {str(e)[:200]}); the design is kept "
                 "and listed as not fully re-reviewed", "warn")
        svc.store.update_run(ctx.run_id, review_status="unresolved")


def _review_loop_inner(svc: Services, ctx: Ctx) -> None:
    """(A) blind review → (B) Bar Raiser reads the logbook → (C) engineer accepts/rejects →
    accepted ones are rebuilt and the changed decisions reviewed again (up to EPH_REVIEW_ROUNDS)."""
    if not svc.settings.bar_raiser:
        svc.store.update_run(ctx.run_id, review_status="off")
        return
    start = max((r["round"] for r in svc.store.get_run(ctx.run_id).get("review_rounds", [])), default=0)
    unreviewed_changes = False
    for i in range(1, svc.settings.review_rounds + 1):
        rnd = start + i
        svc.store.update_run(ctx.run_id, current_stage="review")
        challenged = bar_raiser.review_round(ctx, ctx.reviewer, rnd)
        standing = bar_raiser.reconsider(ctx, ctx.reviewer, challenged)
        if not standing:
            break
        svc.store.update_run(ctx.run_id, current_stage="engineer")
        targets = engineer.respond(ctx, ctx.llm, standing)
        if not targets:
            break
        _rebuild(svc, ctx, targets)
        unreviewed_changes = i == svc.settings.review_rounds   # last round: fixes applied, not re-reviewed
    status, disagreements = _review_status(svc.store, ctx.run_id, unreviewed_changes)
    svc.store.update_run(ctx.run_id, review_status=status)
    msg = (f"{len(disagreements)} disagreement(s) between engineer and Bar Raiser for you to judge"
           if disagreements else f"Bar Raiser verdict: {status.replace('_', ' ')}")
    ctx.emit("review", msg, "warn" if status in ("disagreements", "unresolved") else "info")


def _review_status(store: Store, run_id: str, unreviewed_changes: bool = False) -> tuple[str, list[dict]]:
    """Overall review status. A disagreement the user has already judged no longer blocks."""
    active = store.active(run_id)
    open_rev = [d for d in active if (d.get("review") or {}).get("verdict") not in (None, "approve")]
    rejected = [d for d in open_rev if d["review"].get("engineer_response") == "reject"
                and d["review"].get("severity") in ("high", "medium")]
    pending = [d for d in rejected if not d["review"].get("user_verdict")]
    status = ("disagreements" if pending else "unresolved" if unreviewed_changes
              else "judged" if rejected else "approved_with_notes" if open_rev else "approved")
    return status, pending


def _finalize(svc: Services, ctx: Ctx) -> None:
    """Step 4 gate: score acceptance criteria on the final delivery and list every gap honestly."""
    try:
        crit = bar_raiser.final_assessment(ctx, ctx.reviewer) if svc.settings.bar_raiser else []
    except Exception as e:  # noqa: BLE001  the design is finished; a failed last check must not lose it
        ctx.emit("delivery", f"final acceptance check couldn't run ({type(e).__name__}: {str(e)[:200]}); "
                 "criteria left unverified", "warn")
        crit = svc.store.get_run(ctx.run_id).get("acceptance_criteria", [])
        for c in crit:
            c["status"], c["note"] = "unverified", "Final check couldn't run (model unavailable)."
        svc.store.update_run(ctx.run_id, acceptance_criteria=crit)
    run = svc.store.get_run(ctx.run_id)
    arts = svc.store.latest_artifacts(ctx.run_id)
    gaps = [f"{c['id']} {c['status'].replace('_', ' ')}: {c['criterion']}" for c in crit
            if c.get("priority", "must") == "must" and c.get("status") in ("not_met", "partial", "unverified")]
    gaps += [f"Deviation: {d['title']}" for d in svc.store.active(ctx.run_id, prefix="deviation.")]
    cl = (arts.get("compile_log") or {}).get("content", {})
    if cl.get("status") == "skipped":
        gaps.append("Firmware NOT compiled (arduino-cli not available): unverified")
    elif cl.get("status") == "failed":
        gaps.append("Firmware does not compile")
    cart = (arts.get("cart") or {}).get("content", {})
    n_est = sum(1 for l in cart.get("lines", []) if l.get("price_source") == "estimate")
    if n_est:
        gaps.append(f"{n_est} cart price(s) are estimates, not live listings")
    if cart.get("over_budget"):
        gaps.append(f"Cart £{cart.get('total_gbp')} is over the £{cart.get('budget_gbp')} budget")
    if run.get("review_status") == "unresolved":
        gaps.append("Fixes from the last review round were applied but not re-reviewed")
    status = "delivered_with_gaps" if gaps else "delivered"
    svc.store.update_run(ctx.run_id, delivery_status=status, gaps=gaps)
    met = sum(1 for c in crit if c.get("status") == "met")
    ctx.emit("delivery", f"{status.replace('_', ' ')}: {met}/{len(crit)} criteria met"
             + (f"; {len(gaps)} gap(s)" if gaps else ""), "warn" if gaps else "info")


def _guarded(svc: Services, run_id: str, body) -> bool:
    svc.store.active_runs.add(run_id)
    try:
        return _guarded_inner(svc, run_id, body)
    finally:
        svc.store.active_runs.discard(run_id)


def _guarded_inner(svc: Services, run_id: str, body) -> bool:
    ctx = _ctx(svc, run_id)
    svc.store.update_run(run_id, status="running", error=None)
    try:
        body(ctx)
        svc.store.update_run(run_id, status="done", current_stage=None, finished_at=now())
        return True
    except Exception as e:  # noqa: BLE001
        ctx.emit(svc.store.get_run(run_id).get("current_stage") or "pipeline", f"{type(e).__name__}: {e}", "error")
        svc.store.update_run(run_id, status="failed", error=traceback.format_exc()[-3000:], finished_at=now())
        return False


def execute(svc: Services, run_id: str, stages: list[str] | None = None, review: bool = True) -> bool:
    """Full run: all stages, then Bar Raiser review → rebuild rounds."""
    def body(ctx: Ctx) -> None:
        if not stages:
            _recall(svc, ctx)
            if review and svc.settings.bar_raiser:
                bar_raiser.brief(ctx, ctx.reviewer)        # step 2: criteria before any design exists
        _run_stages(svc, ctx, stages or STAGE_ORDER)
        if review:
            _review_loop(svc, ctx)
            _finalize(svc, ctx)
        _remember(svc, ctx)
    return _guarded(svc, run_id, body)


def rebuild(svc: Services, run_id: str, key: str, instruction: str, source: str = "user",
            review: bool = True) -> bool:
    """Partial rebuild from one decision with a change request, then re-review only what changed."""
    if not instruction.strip():
        raise ValueError("empty instruction")
    if not svc.store.get_active(run_id, key):
        raise KeyError(f"no active decision {key}")

    def body(ctx: Ctx) -> None:
        _rebuild(svc, ctx, [{"key": key, "note": instruction.strip(), "source": source}])
        if review:
            _review_loop(svc, ctx)
            _finalize(svc, ctx)
        _remember(svc, ctx)
    return _guarded(svc, run_id, body)


def improve_questions(svc: Services, run_id: str, feedback: str) -> list[str]:
    run = svc.store.get_run(run_id)
    return clarify.improvement_questions(svc.llm(run_models(svc, run)["engineer"]), run["prompt"], feedback)


def improve(svc: Services, run_id: str, feedback: str, clarifications: list[dict] | None = None) -> bool:
    """'Improve project': the Bar Raiser records what the user disliked first, the feedback is mapped onto
    decisions, only their downstream is rebuilt, then review → engineer → final check run again."""
    note = feedback.strip() + "".join(f" ({c.get('q')} → {c.get('a')})" for c in (clarifications or []) if c.get("a"))
    if not note:
        raise ValueError("empty feedback")

    def body(ctx: Ctx) -> None:
        lesson = {}
        if svc.settings.bar_raiser:
            lesson = bar_raiser.record_feedback(ctx, ctx.reviewer, note)
        targets = engineer.map_feedback(ctx, ctx.llm, note)
        svc.store.push_run(run_id, "improvements", {"at": now(), "feedback": note, "lesson": lesson.get("lesson", ""),
                                                    "targets": [t["key"] for t in targets]})
        _rebuild(svc, ctx, targets)
        _review_loop(svc, ctx)
        _finalize(svc, ctx)
        _remember(svc, ctx)
    return _guarded(svc, run_id, body)


def judge(svc: Services, run_id: str, key: str, version: int, verdict: str, reason: str = "") -> dict:
    """The user settles a disagreement. The verdict is stored on the review and in precedent memory.
    If the user sides with the Bar Raiser (or rejects both and says what they want instead), the change is
    returned as a rebuild target; the caller runs `apply_verdict` with it."""
    if verdict not in ("bar_raiser_right", "engineer_right", "neither"):
        raise ValueError("bad verdict")
    d = svc.store.get_active(run_id, key)
    if not d or d["version"] != version or not d.get("review"):
        raise ValueError("that decision has changed since it was reviewed")
    reason = reason.strip()
    svc.store.update_review(run_id, key, version, user_verdict=verdict, user_reason=reason)
    svc.store.push_run(run_id, "events", {"at": now(), "stage": "user", "level": "info",
                                          "message": f"verdict on {key}: {verdict.replace('_', ' ')}"
                                                     + (f" ({reason})" if reason else "")})
    target = None
    rev = d["review"]
    if verdict == "bar_raiser_right":
        target = {"key": key, "source": "user",
                  "note": f"The user sided with the Bar Raiser: {rev.get('notes', '')} "
                          f"Apply this fix: {rev.get('suggested_fix', '')}".strip()
                          + (f" User's note: {reason}" if reason else "")}
    elif verdict == "neither" and reason:
        target = {"key": key, "source": "user",
                  "note": f"The user rejected both the original choice and the Bar Raiser's fix. Do this instead: {reason}"}
    if target is None:
        status, _ = _review_status(svc.store, run_id)
        svc.store.update_run(run_id, review_status=status)
        rec = precedents.save_precedent(svc.store, svc.embedder, run_id) if svc.settings.precedents else None
        return {"ok": True, "rebuilding": False, "precedent_saved": bool(rec)}
    return {"ok": True, "rebuilding": True, "target": target}


def apply_verdict(svc: Services, run_id: str, target: dict) -> bool:
    """Rebuild from the judged decision, then review → final check → memory, like an improvement."""
    def body(ctx: Ctx) -> None:
        _rebuild(svc, ctx, [target])
        _review_loop(svc, ctx)
        _finalize(svc, ctx)
        _remember(svc, ctx)
    return _guarded(svc, run_id, body)
