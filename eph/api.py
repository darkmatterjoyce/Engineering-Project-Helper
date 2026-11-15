"""HTTP API + web UI.  uvicorn eph.api:app --reload  →  http://localhost:8000"""
from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from .compiler import toolchain_status
from .db import clean
from .llm import LLMError
from .precedents import resolution
from .pipeline import (apply_verdict, ask, create_run, execute, improve, improve_questions, judge, make_services,
                       rebuild, run_models)

WEB = Path(__file__).resolve().parent.parent / "web"
app = FastAPI(title="Engineering Project Helper")
svc = make_services()
# A run can't still be building if the server just started: mark leftovers as interrupted so the GUI
# doesn't show "Building…" forever (runs are kept across restarts when Databricks/MongoDB is used).
svc.store.runs.update_many(
    {"status": "running"},
    {"$set": {"status": "failed", "current_stage": None,
              "error": "Interrupted: the server was restarted while this run was building. Start it again."}})


class NewRun(BaseModel):
    prompt: str
    clarifications: list[dict] = []
    engineer_model: str | None = None
    bar_raiser_model: str | None = None


class Prompt(BaseModel):
    prompt: str
    engineer_model: str | None = None


class Feedback(BaseModel):
    feedback: str
    clarifications: list[dict] = []


class ExplainReq(BaseModel):
    start: int
    end: int


class VerdictReq(BaseModel):
    key: str
    version: int
    verdict: str
    reason: str = ""


class RebuildReq(BaseModel):
    key: str
    instruction: str


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
def health():
    return {"providers": svc.providers, "toolchain": toolchain_status(svc.settings)}


@app.get("/api/storage")
def storage(run_id: str = ""):
    """Where the data lives and how much of it there is: live row counts read back from Databricks (or the
    in-memory / MongoDB counts), sync status, and ready-to-run SQL for the Databricks SQL editor."""
    import time as _t

    from .databricks_store import COLLECTIONS

    st = svc.store
    local = {c: st.db[c].count_documents({}) for c in COLLECTIONS}
    out: dict = {"backend": st.backend, "tables": [], "queries": []}
    m = st.mirror
    if m is None:
        out["tables"] = [{"collection": c, "rows": local[c]} for c in COLLECTIONS]
        return out
    out.update(catalog=m.catalog, schema=m.schema, pending=m.pending(), last_error=m.last_error,
               rows_written=m.rows_written,
               last_sync_seconds_ago=round(_t.time() - m.last_sync_at, 1) if m.last_sync_at else None)
    try:
        live, out["live"] = m.live_counts(run_id), True
    except Exception as e:  # noqa: BLE001
        live, out["live"], out["live_error"] = {}, False, f"{type(e).__name__}: {str(e)[:200]}"
    for c in COLLECTIONS:
        n, mine = live.get(c, (None, None))
        out["tables"].append({"collection": c, "table": f"{m.catalog}.{m.schema}.eph_{c}",
                              "rows": n if n is not None else local[c], "rows_this_run": mine})
    t = lambda c: f"{m.catalog}.{m.schema}.eph_{c}"  # noqa: E731
    j = lambda f: f"get_json_object(doc, '$.{f}')"  # noqa: E731
    rid = run_id.replace("'", "")
    out["queries"] = [
        {"title": "Recent projects",
         "sql": f"SELECT {j('prompt')} AS prompt, {j('status')} AS status, {j('delivery_status')} AS delivery,\n"
                f"       updated_at\nFROM {t('runs')}\nORDER BY updated_at DESC\nLIMIT 10;"},
        {"title": "This project's decision log (every version)",
         "sql": f"SELECT {j('key')} AS decision, CAST({j('version')} AS INT) AS version, {j('status')} AS status,\n"
                f"       {j('stage')} AS stage, {j('title')} AS title\nFROM {t('decisions')}\n"
                f"WHERE {j('run_id')} = '{rid}'\nORDER BY decision, version;"},
        {"title": "What partial rebuilds changed (decisions with more than one version)",
         "sql": f"SELECT {j('key')} AS decision, max(CAST({j('version')} AS INT)) AS versions\n"
                f"FROM {t('decisions')}\nWHERE {j('run_id')} = '{rid}'\nGROUP BY 1 HAVING versions > 1\nORDER BY versions DESC;"},
        {"title": "Everything the Bar Raiser challenged, across all projects",
         "sql": f"SELECT {j('run_id')} AS project, {j('key')} AS decision, {j('review.severity')} AS severity,\n"
                f"       {j('review.notes')} AS notes\nFROM {t('decisions')}\n"
                f"WHERE {j('review.verdict')} = 'challenge'\nORDER BY updated_at DESC;"},
    ]
    return out


@app.get("/api/models")
def list_models():
    """The model menu for the Engineer / Bar Raiser pickers (unavailable = no API key in .env)."""
    from . import models

    return {"models": models.listing(svc.settings), "defaults": models.defaults(svc.settings)}


@app.post("/api/clarify")
def clarify(body: Prompt):
    """Step 1: 0-2 essential questions before a run starts."""
    try:
        return {"questions": ask(svc, body.prompt, body.engineer_model)}
    except (ValueError, LLMError) as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/runs")
def new_run(body: NewRun):
    try:
        run_id = create_run(svc, body.prompt, body.clarifications, body.engineer_model, body.bar_raiser_model)
    except (ValueError, LLMError) as e:
        raise HTTPException(400, str(e)) from e
    threading.Thread(target=execute, args=(svc, run_id), daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/runs")
def list_runs():
    return clean(svc.store.list_runs())


def _run_or_404(run_id: str) -> dict:
    try:
        run = svc.store.get_run(run_id)
    except Exception:  # invalid ObjectId
        run = None
    if not run:
        raise HTTPException(404, "run not found")
    return run


def _redraw_breadboard(decisions: list[dict], artifacts: dict) -> None:
    """Draw the breadboard from the saved pin map with the current renderer, so projects built before a
    drawing improvement get it too. Nothing is saved; on any problem the stored picture is kept."""
    w = (artifacts.get("wiring") or {}).get("content")
    if not w or not w.get("connections"):
        return
    try:
        from . import breadboard

        parts = {d["key"].split(".", 1)[1]: d["choice"] for d in decisions if d["key"].startswith("part.")}
        w["breadboard_svg"], w["breadboard_notes"] = breadboard.render(
            w["connections"], parts, w.get("support_components") or [])
    except Exception:  # noqa: BLE001
        pass


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = _run_or_404(run_id)
    decisions, artifacts = svc.store.active(run_id), svc.store.latest_artifacts(run_id)
    _redraw_breadboard(decisions, artifacts)
    return clean({
        "run": run,
        "decisions": decisions,
        "artifacts": artifacts,
        "stale": [d["key"] for d in svc.store.stale(run_id)],
    })


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str, forget_memory: bool = True):
    """Delete a past project. A project that is still building can't be deleted (stop the server first)."""
    run = _run_or_404(run_id)
    if run["status"] == "running":
        raise HTTPException(409, "this project is still building; wait for it to finish")
    return {"deleted": svc.store.delete_run(run_id, forget_memory=forget_memory)}


@app.post("/api/runs/{run_id}/rebuild")
def rebuild_from(run_id: str, body: RebuildReq):
    """Partial rebuild: change one decision, re-run only what depends on it, re-review the changes."""
    run = _run_or_404(run_id)
    if run["status"] == "running":
        raise HTTPException(409, "run is busy")
    if not body.instruction.strip():
        raise HTTPException(400, "instruction is empty")
    if not svc.store.get_active(run_id, body.key):
        raise HTTPException(404, f"no active decision {body.key}")
    svc.store.update_run(run_id, status="running")  # claim it before the thread starts
    threading.Thread(target=rebuild, args=(svc, run_id, body.key, body.instruction), daemon=True).start()
    return {"ok": True}


@app.post("/api/runs/{run_id}/improve/questions")
def improve_qs(run_id: str, body: Feedback):
    _run_or_404(run_id)
    return {"questions": improve_questions(svc, run_id, body.feedback)}


@app.post("/api/runs/{run_id}/improve")
def improve_run(run_id: str, body: Feedback):
    """'Improve project': resume this run with free-text feedback (Bar Raiser records it first)."""
    run = _run_or_404(run_id)
    if run["status"] == "running":
        raise HTTPException(409, "run is busy")
    if not body.feedback.strip():
        raise HTTPException(400, "feedback is empty")
    svc.store.update_run(run_id, status="running")
    threading.Thread(target=improve, args=(svc, run_id, body.feedback, body.clarifications), daemon=True).start()
    return {"ok": True}


@app.post("/api/runs/{run_id}/verdicts")
def verdict(run_id: str, body: VerdictReq):
    """The user settles a disagreement between engineer and Bar Raiser (→ precedent memory)."""
    run = _run_or_404(run_id)
    if run["status"] == "running":
        raise HTTPException(409, "run is busy")
    try:
        out = judge(svc, run_id, body.key, body.version, body.verdict, body.reason)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if out.get("rebuilding"):
        svc.store.update_run(run_id, status="running")  # claim before the thread starts
        threading.Thread(target=apply_verdict, args=(svc, run_id, out["target"]), daemon=True).start()
    return out


@app.get("/api/runs/{run_id}/reviews")
def reviews(run_id: str):
    _run_or_404(run_id)
    current = {d["key"]: d["version"] for d in svc.store.active(run_id)}
    out = []
    for r in svc.store.list_reviews(run_id):
        r = clean(r)
        cur = current.get(r["key"])
        r["resolution"] = resolution(r, cur)
        out.append(r)
    return out


@app.get("/api/precedents")
def list_precedents():
    """The agent's memory: approved past projects (embeddings omitted)."""
    return clean(list(svc.store.precedents.find({}, {"embedding": 0, "text": 0}).sort("updated_at", -1).limit(100)))


@app.delete("/api/precedents/{precedent_id}")
def forget_precedent(precedent_id: str):
    from bson import ObjectId

    try:
        res = svc.store.precedents.delete_one({"_id": ObjectId(precedent_id)})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, "bad id") from e
    if not res.deleted_count:
        raise HTTPException(404, "not found")
    return {"ok": True}


@app.get("/api/runs/{run_id}/breadboard.svg")
def breadboard_svg(run_id: str):
    _run_or_404(run_id)
    arts = svc.store.latest_artifacts(run_id)
    _redraw_breadboard(svc.store.active(run_id), arts)
    w = arts.get("wiring")
    if not w or not w["content"].get("breadboard_svg"):
        raise HTTPException(404, "no breadboard view yet")
    from fastapi.responses import Response

    return Response(w["content"]["breadboard_svg"], media_type="image/svg+xml",
                    headers={"Content-Disposition": 'attachment; filename="breadboard.svg"'})


@app.post("/api/runs/{run_id}/explain")
def explain(run_id: str, body: ExplainReq):
    """Explain the selected firmware lines (cached in MongoDB)."""
    from .explain import explain_lines

    _run_or_404(run_id)
    try:
        run = svc.store.get_run(run_id)
        out = explain_lines(svc.store, svc.llm(run_models(svc, run)["engineer"]), run_id, body.start, body.end)
    except (ValueError, LLMError) as e:
        raise HTTPException(400, str(e)) from e
    out.pop("created_at", None)
    return clean(out)


@app.get("/api/runs/{run_id}/graph")
def graph(run_id: str):
    _run_or_404(run_id)
    return svc.store.graph(run_id)


@app.get("/api/runs/{run_id}/decisions/{key}/history")
def history(run_id: str, key: str):
    _run_or_404(run_id)
    return clean(svc.store.history(run_id, key))


@app.get("/api/runs/{run_id}/decisions/{key}/downstream")
def downstream(run_id: str, key: str):
    _run_or_404(run_id)
    return {"key": key, "downstream": svc.store.downstream(run_id, key)}


@app.get("/api/runs/{run_id}/firmware", response_class=PlainTextResponse)
def firmware(run_id: str):
    _run_or_404(run_id)
    fw = svc.store.latest_artifacts(run_id).get("firmware")
    if not fw:
        raise HTTPException(404, "no firmware yet")
    return PlainTextResponse(fw["content"]["code"], headers={
        "Content-Disposition": f'attachment; filename="{fw["content"]["filename"]}"'})
