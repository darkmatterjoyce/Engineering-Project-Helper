"""Precedents: the agent's long-term memory, stored in MongoDB.

When a run finishes and the Bar Raiser approves it, a compact *precedent* is saved: the prompt, what was
built (parts, pin map), and every challenge the Bar Raiser raised and how it was fixed. At the start of
a new run, similar precedents are retrieved with Atlas Vector Search ($vectorSearch) and shown to:
  * the builder stages: "similar projects used these parts / hit these problems", and
  * the Bar Raiser: "on similar projects these issues came up; check they're handled".
So a mistake caught once (e.g. pump inrush browning out the ESP32) is designed out next time.

Embeddings: Voyage AI (VoyageAI by MongoDB) when VOYAGE_API_KEY is set, otherwise an offline
feature-hashing embedding so everything still works with no key.
Search: Atlas $vectorSearch when the index exists, otherwise exact cosine similarity in Python.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Protocol

import httpx

from .config import Settings
from .db import Store
from .schema import now

STOP = set("a an the and or of to for with my in on at by is it that this when from as be into your our i "
           "me using use make build want need can should esp32 esp".split())


# ------------------------------------------------------------------ embeddings
class Embedder(Protocol):
    name: str
    dims: int

    def embed(self, texts: list[str], kind: str) -> list[list[float]]: ...


class HashEmbedder:
    """Offline fallback: hashed bag of words + bigrams, L2-normalised. Keyword-level similarity only,
    but deterministic, free, and good enough to match 'plant watering' with 'watering my plants'."""
    name = "hash-512"
    dims = 512

    @staticmethod
    def _stem(w: str) -> str:
        for suf in ("ically", "ation", "ing", "ers", "ies", "ed", "ly", "er", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                return w[: -len(suf)]
        return w

    @classmethod
    def _features(cls, text: str) -> list[tuple[str, float]]:
        words = [cls._stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 1]
        feats = [(w, 1.0) for w in words] + [(f"{a}_{b}", 0.5) for a, b in zip(words, words[1:])]
        for w in words:  # character 4-grams so 'houseplants' still overlaps 'plant'
            padded = f"^{w}$"
            feats += [("#" + padded[i:i + 4], 0.25) for i in range(len(padded) - 3)]
        return feats

    def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dims
            for tok, wt in self._features(t):
                h = int(hashlib.sha1(tok.encode()).hexdigest(), 16)
                v[h % self.dims] += wt if (h >> 9) & 1 else -wt
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class VoyageEmbedder:
    """Voyage AI (by MongoDB). Keys starting 'al-' are MongoDB Atlas model-API keys and go to
    ai.mongodb.com; Voyage dashboard keys ('pa-') go to api.voyageai.com."""

    def __init__(self, api_key: str, model: str, dims: int = 1024):
        self.api_key, self.model, self.dims = api_key, model, dims
        self.name = f"{model}-{dims}"
        self.url = ("https://ai.mongodb.com/v1/embeddings" if api_key.startswith("al-")
                    else "https://api.voyageai.com/v1/embeddings")

    def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        r = httpx.post(self.url, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=60,
                       json={"input": texts, "model": self.model, "input_type": kind, "output_dimension": self.dims})
        r.raise_for_status()
        return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]


def make_embedder(settings: Settings) -> Embedder:
    if settings.voyage_api_key and not settings.mock:
        return VoyageEmbedder(settings.voyage_api_key, settings.voyage_model, settings.voyage_dims)
    return HashEmbedder()


def index_name(embedder: Embedder) -> str:
    return f"precedent_vectors_{embedder.dims}"


def index_definition(embedder: Embedder) -> dict:
    return {"fields": [
        {"type": "vector", "path": "embedding", "numDimensions": embedder.dims, "similarity": "cosine"},
        {"type": "filter", "path": "embedding_model"},
        {"type": "filter", "path": "review_status"},
    ]}


def ensure_vector_index(store: Store, embedder: Embedder) -> str:
    """Create the Atlas Vector Search index if possible. Returns a status string."""
    if store.backend != "mongodb":
        return "local cosine (no Atlas)"
    try:
        names = {ix["name"] for ix in store.precedents.list_search_indexes()}
        if index_name(embedder) in names:
            return "atlas"
        from pymongo.operations import SearchIndexModel

        store.precedents.create_search_index(SearchIndexModel(
            definition=index_definition(embedder), name=index_name(embedder), type="vectorSearch"))
        return "atlas (index building)"
    except Exception as e:  # noqa: BLE001  (e.g. older shared tiers can't create search indexes from drivers)
        return f"local cosine (create the index in the Atlas UI: {type(e).__name__})"


# -------------------------------------------------------------- build a record
def _precedent_text(rec: dict) -> str:
    # What the project *is* (not its bill of materials), so it matches how people phrase new ideas.
    return " | ".join([rec["prompt"], rec.get("project_name", ""), rec.get("summary", ""),
                       "; ".join(rec.get("features", []))])


def default_min_score(embedder: Embedder) -> float:
    # hashed bag-of-words gives low absolute scores (unrelated ≈ 0.0-0.05); Voyage cosine is higher
    return 0.12 if isinstance(embedder, HashEmbedder) else 0.45


def resolution(review: dict, current_version: int | None) -> str:
    """What became of a challenge: withdrawn by the Bar Raiser, fixed in a later version, decision removed,
    kept on purpose (user sided with the engineer), or still open."""
    if review.get("after_logbook") == "withdrawn":
        return "withdrawn"
    if current_version is None:
        return "retired"
    if current_version > review["version"]:
        return "fixed"
    if review.get("user_verdict") == "engineer_right":
        return "kept"
    return "open"


def build_record(store: Store, run_id: str) -> dict:
    run = store.get_run(run_id)
    active = store.active(run_id)
    by = {d["key"]: d for d in active}
    summary = (by.get("req.summary") or {}).get("choice", {})
    current = {d["key"]: d["version"] for d in active}
    challenges = []
    for r in store.list_reviews(run_id):
        if r["verdict"] == "approve":
            continue
        cur = current.get(r["key"])
        res = resolution(r, cur)
        challenges.append({"key": r["key"], "severity": r["severity"], "notes": r["notes"],
                           "suggested_fix": r.get("suggested_fix", ""), "resolution": res,
                           "engineer_response": r.get("engineer_response"),
                           "engineer_reason": r.get("engineer_reason", ""),
                           "user_verdict": r.get("user_verdict"), "user_reason": r.get("user_reason", "")})
    fw = by.get("fw.sketch", {}).get("choice", {})
    return {
        "source_run_id": run_id,
        "prompt": run["prompt"],
        "project_name": summary.get("project_name", ""),
        "summary": summary.get("summary", ""),
        "features": [d["choice"]["description"] for d in active if d["kind"] == "feature"],
        "parts": [{"id": d["key"].split(".", 1)[1], "name": d["choice"]["name"], "model": d["choice"]["model"],
                   "interface": d["choice"]["interface"], "library": d["choice"].get("library")}
                  for d in active if d["kind"] == "part"],
        "pin_map": [{"part_id": d["choice"]["part_id"], "part_pin": d["choice"]["part_pin"],
                     "esp32_pin": d["choice"]["esp32_pin"]} for d in active if d["kind"] == "pin"],
        "support_components": [f"{s.get('name')} {s.get('value')}".strip()
                               for s in (by.get("wiring.support") or {}).get("choice", [])],
        "challenges": challenges,
        "review_status": run.get("review_status"),
        "compile_status": fw.get("compile_status"),
        "rebuilds": len(run.get("rebuilds", [])),
    }


def save_precedent(store: Store, embedder: Embedder, run_id: str) -> dict | None:
    """Upsert this run as a precedent (only if the Bar Raiser approved it and it didn't fail to compile)."""
    rec = build_record(store, run_id)
    if rec["review_status"] not in ("approved", "approved_with_notes", "disagreements", "judged") or rec["compile_status"] == "failed":
        return None
    rec["text"] = _precedent_text(rec)
    rec["embedding"] = embedder.embed([rec["text"]], "document")[0]
    rec["embedding_model"] = embedder.name
    rec["updated_at"] = now()
    store.precedents.update_one({"source_run_id": run_id},
                                {"$set": rec, "$setOnInsert": {"created_at": now(), "uses": 0}}, upsert=True)
    return store.precedents.find_one({"source_run_id": run_id})


# ------------------------------------------------------------------- retrieval
def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def find_similar(store: Store, embedder: Embedder, prompt: str, k: int, min_score: float,
                 exclude_run: str | None = None) -> tuple[list[dict], str]:
    """Returns (precedents with 'score', method used)."""
    qv = embedder.embed([prompt], "query")[0]
    proj = {"embedding": 0, "text": 0}
    hits: list[dict] = []
    method = "local cosine"
    if store.backend == "mongodb":
        try:
            pipeline = [
                {"$vectorSearch": {"index": index_name(embedder), "path": "embedding", "queryVector": qv,
                                   "numCandidates": max(50, k * 20), "limit": k + 1,
                                   "filter": {"embedding_model": embedder.name}}},
                {"$addFields": {"score": {"$meta": "vectorSearchScore"}}},
                {"$project": proj},
            ]
            hits = list(store.precedents.aggregate(pipeline))
            method = "atlas $vectorSearch"
            # Atlas cosine score is (1 + cos) / 2; convert back so thresholds mean the same everywhere
            for h in hits:
                h["score"] = 2 * h["score"] - 1
        except Exception:  # noqa: BLE001  index missing / still building → exact search
            hits = []
            method = "local cosine (vector index unavailable)"
    if method.startswith("local"):
        for p in store.precedents.find({"embedding_model": embedder.name}):
            p["score"] = _cosine(qv, p["embedding"])
            for f in proj:
                p.pop(f, None)
            hits.append(p)
        hits.sort(key=lambda p: -p["score"])
    hits = [h for h in hits if h.get("source_run_id") != exclude_run and h["score"] >= min_score][:k]
    return hits, method


# ---------------------------------------------------------------- prompt text
def builder_block(precs: list[dict], stage: str) -> str:
    """What the builder sees about similar past projects (per stage)."""
    if not precs:
        return ""
    lines = ["\n\nPRECEDENTS: similar projects this agent built before (learn from them, don't copy blindly):"]
    for i, p in enumerate(precs, 1):
        lines.append(f"[P{i}] \"{p['prompt']}\" (similarity {p['score']:.2f}, Bar Raiser: {p.get('review_status')})")
        if stage in ("parts", "wiring"):
            lines.append("  parts: " + "; ".join(f"{x['name']} ({x['model']})" for x in p.get("parts", [])))
        if stage == "wiring":
            lines.append("  pins: " + ", ".join(f"{x['part_id']}.{x['part_pin']}→{x['esp32_pin']}" for x in p.get("pin_map", [])))
            if p.get("support_components"):
                lines.append("  support components: " + "; ".join(p["support_components"]))
        for c in p.get("challenges", []):
            if c["severity"] in ("high", "medium"):
                lines.append(f"  Bar Raiser {c['severity']} challenge on {c['key']} ({c['resolution']}): "
                             f"{c['notes']} Fix: {c.get('suggested_fix', '')}" + _verdict_text(c))
    lines.append("Avoid repeating any problem a precedent's Bar Raiser flagged.")
    return "\n".join(lines)


def _verdict_text(c: dict) -> str:
    """The user's ruling on a past disagreement: the strongest signal either side can cite."""
    v = c.get("user_verdict")
    if not v:
        return ""
    who = {"bar_raiser_right": "the USER SIDED WITH THE BAR RAISER",
           "engineer_right": "the USER SIDED WITH THE ENGINEER",
           "neither": "the USER REJECTED BOTH (treat their reason as a hard requirement)"}[v]
    return f" | engineer rejected it ({c.get('engineer_reason', '')[:120]}); {who}" + (
        f': "{c["user_reason"]}"' if c.get("user_reason") else "")


def reviewer_block(precs: list[dict]) -> str:
    items = [f"- on \"{p['prompt']}\": [{c['severity']}] {c['key']}: {c['notes']}" + _verdict_text(c)
             for p in precs for c in p.get("challenges", []) if c["severity"] in ("high", "medium")]
    if not items:
        return ""
    return "\n\nISSUES FOUND ON SIMILAR PAST PROJECTS (check whether this design handles them):\n" + "\n".join(items)


def public(p: dict) -> dict:
    return {k: v for k, v in p.items() if k not in ("embedding", "text")}


def as_json(p: dict) -> str:
    return json.dumps(public(p), default=str)
