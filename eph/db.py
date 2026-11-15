"""Persistence (MongoDB, Databricks Delta tables, or in-memory): runs, the versioned decision log, artifacts and a search cache.

Collections
  runs          one doc per run (prompt, status, event feed)
  decisions     the decision log (versioned, dependency-linked)
  artifacts     rendered outputs (parts list, wiring, firmware, compile log, flow diagram)
  search_cache  Linkup/Tavily responses, TTL 7 days (saves credits on reruns)
  precedents    (build step 4) finished runs + embeddings for Atlas Vector Search
"""
from __future__ import annotations

import hashlib
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Iterable

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from .config import Settings
from .schema import Artifact, Decision, DecisionStatus, DepRef, Run, RunEvent, now


def clean(doc: Any) -> Any:
    """Make Mongo docs JSON-safe (ObjectId -> str, datetime -> iso)."""
    if isinstance(doc, dict):
        return {("id" if k == "_id" else k): clean(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [clean(v) for v in doc]
    if isinstance(doc, ObjectId):
        return str(doc)
    if isinstance(doc, datetime):
        return doc.isoformat()
    return doc


class Store:
    def __init__(self, settings: Settings, databricks_connect=None):
        self.mirror = None
        if settings.mongodb_uri:
            from pymongo import MongoClient

            self.client = MongoClient(settings.mongodb_uri, appname="engineering-project-helper")
            self.backend = "mongodb"
            self.db = self.client[settings.mongodb_db]
        else:
            import mongomock  # in-memory engine: zero-setup fallback, and the query engine for Databricks

            self.client = mongomock.MongoClient()
            if settings.use_databricks or databricks_connect is not None:
                from .databricks_store import open_mirrored_db

                self.db, self.mirror = open_mirrored_db(settings, self.client, connect=databricks_connect)
                self.backend = "databricks"
            else:
                self.backend = "mongomock (in-memory)"
                self.db = self.client[settings.mongodb_db]
        self.runs = self.db["runs"]
        self.decisions = self.db["decisions"]
        self.artifacts = self.db["artifacts"]
        self.search_cache = self.db["search_cache"]
        self.reviews = self.db["reviews"]
        self.precedents = self.db["precedents"]
        self._stage_ctx: dict[str, dict] = {}  # run_id -> {"touched": set, "trigger": dict|None}
        self._lock = threading.Lock()  # serialises version allocation within this process
        self.ensure_indexes()

    def ensure_indexes(self) -> None:
        self.decisions.create_index(
            [("run_id", ASCENDING), ("key", ASCENDING), ("version", ASCENDING)], unique=True
        )
        self.decisions.create_index([("run_id", ASCENDING), ("status", ASCENDING), ("stage", ASCENDING)])
        self.decisions.create_index([("run_id", ASCENDING), ("depends_on.key", ASCENDING)])
        self.artifacts.create_index(
            [("run_id", ASCENDING), ("kind", ASCENDING), ("version", ASCENDING)], unique=True
        )
        self.runs.create_index([("created_at", DESCENDING)])
        self.reviews.create_index([("run_id", ASCENDING), ("round", ASCENDING)])
        self.precedents.create_index("source_run_id", unique=True)
        self.db["explanations"].create_index([("run_id", ASCENDING), ("code_sha", ASCENDING),
                                              ("start", ASCENDING), ("end", ASCENDING)])
        self.search_cache.create_index("qhash", unique=True)
        self.search_cache.create_index("created_at", expireAfterSeconds=7 * 24 * 3600)

    # ------------------------------------------------------------------ runs
    def create_run(self, prompt: str, model: str, providers: dict[str, str],
                   clarifications: list[dict] | None = None, models: dict | None = None) -> str:
        doc = Run(prompt=prompt, model=model, providers=providers, clarifications=clarifications or [],
                  models=models or {}).model_dump()
        return str(self.runs.insert_one(doc).inserted_id)

    def update_run(self, run_id: str, **fields: Any) -> None:
        self.runs.update_one({"_id": ObjectId(run_id)}, {"$set": fields})

    def push_run(self, run_id: str, field: str, value: Any) -> None:
        self.runs.update_one({"_id": ObjectId(run_id)}, {"$push": {field: value}})

    def log_event(self, run_id: str, stage: str, message: str, level: str = "info") -> None:
        ev = RunEvent(stage=stage, message=message, level=level).model_dump()  # type: ignore[arg-type]
        self.runs.update_one({"_id": ObjectId(run_id)}, {"$push": {"events": ev}})

    def get_run(self, run_id: str) -> dict | None:
        return self.runs.find_one({"_id": ObjectId(run_id)})

    def delete_run(self, run_id: str, forget_memory: bool = True) -> dict[str, int]:
        """Delete a project and everything recorded for it (every decision version, outputs, review rounds,
        code explanations, and, unless kept, its memory precedent). Returns rows removed per collection."""
        n = {"runs": self.runs.delete_one({"_id": ObjectId(run_id)}).deleted_count}
        for name, coll in (("decisions", self.decisions), ("artifacts", self.artifacts),
                           ("reviews", self.reviews), ("explanations", self.db["explanations"])):
            n[name] = coll.delete_many({"run_id": run_id}).deleted_count
        if forget_memory:
            n["precedents"] = self.precedents.delete_many({"source_run_id": run_id}).deleted_count
        self._stage_ctx.pop(run_id, None)
        return n

    def list_runs(self, limit: int = 20) -> list[dict]:
        return list(
            self.runs.find({}, {"events": 0}).sort("created_at", DESCENDING).limit(limit)
        )

    # ------------------------------------------------------------- decisions
    # A stage run opens a scope so the store can (a) report which keys the stage (re)decided, so
    # anything it didn't re-decide can be retired, and (b) stamp rebuild triggers on new versions.
    def begin_stage(self, run_id: str, trigger: dict | None = None, precedent_ids: list[str] | None = None) -> None:
        self._stage_ctx[run_id] = {"touched": set(), "trigger": trigger, "precedent_ids": precedent_ids or []}

    def end_stage(self, run_id: str) -> set[str]:
        return self._stage_ctx.pop(run_id, {"touched": set()})["touched"]

    # Fields that describe *how* a decision was reached, not *what* was decided.
    VOLATILE = {"attempts", "size", "verification_notes"}

    @classmethod
    def _same(cls, prev: dict, d: Decision) -> bool:
        def core(c):
            return {k: v for k, v in c.items() if k not in cls.VOLATILE} if isinstance(c, dict) else c
        deps_prev = sorted((x["key"], x["version"]) for x in prev.get("depends_on", []))
        deps_new = sorted((x.key, x.version) for x in d.depends_on)
        return core(prev.get("choice")) == core(d.choice) and deps_prev == deps_new

    def record(self, d: Decision) -> dict:
        """Insert a decision. If the key already exists, this becomes version N+1 and the previous
        active version is marked superseded. Re-deciding the *same* choice from the *same* inputs is a
        no-op that keeps the existing version, so rebuilds don't cascade through unchanged decisions."""
        scope = self._stage_ctx.get(d.run_id)
        if scope is not None:
            scope["touched"].add(d.key)
        with self._lock:
            prev = self.decisions.find_one(
                {"run_id": d.run_id, "key": d.key}, sort=[("version", DESCENDING)]
            )
            if prev and prev["status"] == DecisionStatus.active.value and self._same(prev, d):
                return prev
            d.version = (prev["version"] + 1) if prev else 1
            if prev and prev["status"] == DecisionStatus.active.value:
                self.decisions.update_one(
                    {"_id": prev["_id"]}, {"$set": {"status": DecisionStatus.superseded.value}}
                )
            doc = d.model_dump(mode="python")
            doc["status"] = d.status.value
            doc["author"] = d.author.value
            if scope and scope.get("precedent_ids") and not doc.get("precedent_ids"):
                doc["precedent_ids"] = scope["precedent_ids"]
            if scope and scope.get("trigger") and d.trigger is None:
                doc["trigger"] = scope["trigger"]
                if scope["trigger"]["key"] == d.key and scope["trigger"]["source"] in ("user", "bar_raiser"):
                    doc["author"] = scope["trigger"]["source"]
            doc["_id"] = self.decisions.insert_one(doc).inserted_id
            return doc

    def retire(self, run_id: str, keys: Iterable[str]) -> int:
        res = self.decisions.update_many(
            {"run_id": run_id, "key": {"$in": list(keys)}, "status": DecisionStatus.active.value},
            {"$set": {"status": DecisionStatus.retired.value}},
        )
        return res.modified_count

    def set_review(self, run_id: str, key: str, version: int, review: dict) -> None:
        self.decisions.update_one({"run_id": run_id, "key": key, "version": version}, {"$set": {"review": review}})
        self.reviews.insert_one({"run_id": run_id, "key": key, "version": version, **review})

    def update_review(self, run_id: str, key: str, version: int, **fields: Any) -> None:
        """Add stage-B / engineer / user fields to an existing review (decision copy + audit copy)."""
        self.decisions.update_one({"run_id": run_id, "key": key, "version": version},
                                  {"$set": {f"review.{k}": v for k, v in fields.items()}})
        latest = self.reviews.find_one({"run_id": run_id, "key": key, "version": version},
                                       sort=[("round", DESCENDING)])
        if latest:
            self.reviews.update_one({"_id": latest["_id"]}, {"$set": fields})

    def list_reviews(self, run_id: str) -> list[dict]:
        return list(self.reviews.find({"run_id": run_id}).sort([("round", ASCENDING), ("key", ASCENDING)]))

    def active(self, run_id: str, stage: str | None = None, prefix: str | None = None) -> list[dict]:
        q: dict[str, Any] = {"run_id": run_id, "status": DecisionStatus.active.value}
        if stage:
            q["stage"] = stage
        if prefix:
            q["key"] = {"$regex": "^" + prefix.replace(".", r"\.")}
        return list(self.decisions.find(q).sort("key", ASCENDING))

    def get_active(self, run_id: str, key: str) -> dict | None:
        return self.decisions.find_one(
            {"run_id": run_id, "key": key, "status": DecisionStatus.active.value}
        )

    def ref(self, run_id: str, key: str) -> DepRef | None:
        d = self.get_active(run_id, key)
        return DepRef(key=key, version=d["version"]) if d else None

    def refs(self, run_id: str, keys: Iterable[str]) -> list[DepRef]:
        out, seen = [], set()
        for k in keys:
            if k in seen:
                continue
            seen.add(k)
            r = self.ref(run_id, k)
            if r:
                out.append(r)
        return out

    def history(self, run_id: str, key: str) -> list[dict]:
        return list(self.decisions.find({"run_id": run_id, "key": key}).sort("version", ASCENDING))

    def dependents(self, run_id: str, key: str) -> list[dict]:
        return list(
            self.decisions.find(
                {"run_id": run_id, "status": DecisionStatus.active.value, "depends_on.key": key}
            )
        )

    def downstream(self, run_id: str, key: str) -> list[str]:
        """Transitive closure of active decisions that depend on `key` (BFS order)."""
        out: list[str] = []
        seen = {key}
        queue = deque([key])
        while queue:
            k = queue.popleft()
            for d in self.dependents(run_id, k):
                if d["key"] not in seen:
                    seen.add(d["key"])
                    out.append(d["key"])
                    queue.append(d["key"])
        return out

    def stale(self, run_id: str) -> list[dict]:
        """Active decisions built on an out-of-date (or no longer active) dependency version."""
        current = {d["key"]: d["version"] for d in self.active(run_id)}
        return [
            d
            for d in self.active(run_id)
            if any(current.get(dep["key"]) != dep["version"] for dep in d.get("depends_on", []))
        ]

    def invalidate(self, run_id: str, keys: Iterable[str]) -> int:
        res = self.decisions.update_many(
            {"run_id": run_id, "key": {"$in": list(keys)}, "status": DecisionStatus.active.value},
            {"$set": {"status": DecisionStatus.invalidated.value}},
        )
        return res.modified_count

    def graph(self, run_id: str) -> dict:
        nodes, edges = [], []
        for d in self.active(run_id):
            nodes.append(
                {"key": d["key"], "stage": d["stage"], "title": d["title"],
                 "version": d["version"], "confidence": d["confidence"]}
            )
            for dep in d.get("depends_on", []):
                edges.append({"from": dep["key"], "to": d["key"], "version": dep["version"]})
        return {"nodes": nodes, "edges": edges}

    # ------------------------------------------------------------- artifacts
    def save_artifact(self, run_id: str, kind: str, content: Any, built_from: list[DepRef]) -> int:
        with self._lock:
            prev = self.artifacts.find_one({"run_id": run_id, "kind": kind}, sort=[("version", DESCENDING)])
            version = (prev["version"] + 1) if prev else 1
            a = Artifact(run_id=run_id, kind=kind, version=version, content=content, built_from=built_from)  # type: ignore[arg-type]
            self.artifacts.insert_one(a.model_dump(mode="python"))
            return version

    def latest_artifacts(self, run_id: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for a in self.artifacts.find({"run_id": run_id}).sort("version", ASCENDING):
            out[a["kind"]] = a
        return out

    # ---------------------------------------------------------- search cache
    @staticmethod
    def _qhash(provider: str, query: str) -> str:
        return hashlib.sha256(f"{provider}::{query.strip().lower()}".encode()).hexdigest()

    def cache_get(self, provider: str, query: str) -> dict | None:
        hit = self.search_cache.find_one({"qhash": self._qhash(provider, query)})
        if hit and hit["created_at"].replace(tzinfo=None) > (now() - timedelta(days=7)).replace(tzinfo=None):
            return hit["payload"]
        return None

    def cache_put(self, provider: str, query: str, payload: dict) -> None:
        self.search_cache.find_one_and_update(
            {"qhash": self._qhash(provider, query)},
            {"$set": {"provider": provider, "query": query, "payload": payload, "created_at": now()}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
