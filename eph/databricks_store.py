"""Databricks as the durable store.

How it works
  The app keeps using the same document API it always has (an in-memory mongomock engine answers
  every query, so the decision log, dependency walks and rebuilds behave exactly as before). Every
  write is mirrored to a Delta table in Databricks, one table per collection:

      <catalog>.<schema>.eph_<collection>   (id STRING, doc STRING (JSON), updated_at TIMESTAMP)

  On startup the tables are read back, so runs, decisions, reviews and precedents survive restarts
  and can be queried in Databricks SQL (e.g. `SELECT get_json_object(doc, '$.key') FROM eph_decisions`).

  Writes are queued and flushed by a background thread every couple of seconds with one MERGE per
  collection, so the agent never waits on the SQL warehouse. Several updates to the same document in
  one window collapse into a single row write.
"""
from __future__ import annotations

import atexit
import logging
import re
import threading
import time
from typing import Any

from bson import json_util

log = logging.getLogger("eph.databricks")

COLLECTIONS = ["runs", "decisions", "artifacts", "search_cache", "reviews", "precedents", "explanations"]
_IDENT = re.compile(r"^[A-Za-z0-9_]+$")
_DELETE = object()


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"invalid Databricks identifier: {name!r} (letters, digits and _ only)")
    return f"`{name}`"


class DatabricksMirror:
    """Write-behind mirror of document collections into Delta tables."""

    def __init__(self, settings, connect=None, flush_interval: float = 2.0):
        if connect is None:
            from databricks import sql as dbsql

            def connect():
                return dbsql.connect(server_hostname=settings.databricks_host,
                                     http_path=settings.databricks_http_path,
                                     access_token=settings.databricks_token,
                                     # fail in ~2 min rather than the default 15 (a waking warehouse takes ~1)
                                     _socket_timeout=120, _retry_stop_after_attempts_count=4,
                                     _retry_stop_after_attempts_duration=150)
        self._connect = connect
        self.catalog, self.schema = settings.databricks_catalog, settings.databricks_schema
        self._conn = None
        self._pending: dict[str, dict[str, Any]] = {}  # collection -> {id: doc | _DELETE}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self.flush_interval = flush_interval
        self.last_error: str | None = None
        self.last_sync_at: float | None = None   # time of the last successful write batch
        self.rows_written = 0
        self._sql_lock = threading.RLock()        # one statement at a time on the shared connection
        self._counts_cache: dict = {}
        self._ensure_tables()
        self._thread = threading.Thread(target=self._loop, name="databricks-mirror", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    # ----------------------------------------------------------------- sql
    def table(self, coll: str) -> str:
        return f"{_ident(self.catalog)}.{_ident(self.schema)}.{_ident('eph_' + coll)}"

    def _cursor(self):
        if self._conn is None:
            self._conn = self._connect()
        return self._conn.cursor()

    def _exec(self, sql: str, params: dict | None = None, fetch: bool = False):
        with self._sql_lock:
            try:
                with self._cursor() as cur:
                    cur.execute(sql, params or {})
                    return cur.fetchall() if fetch else None
            except Exception:
                # A dropped connection (idle warehouse, token refresh) gets one reconnect.
                self._reset()
                with self._cursor() as cur:
                    cur.execute(sql, params or {})
                    return cur.fetchall() if fetch else None

    def _reset(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def _ensure_tables(self) -> None:
        self._exec(f"CREATE SCHEMA IF NOT EXISTS {_ident(self.catalog)}.{_ident(self.schema)}")
        for c in COLLECTIONS:
            self._exec(f"CREATE TABLE IF NOT EXISTS {self.table(c)} "
                       "(id STRING NOT NULL, doc STRING, updated_at TIMESTAMP) USING DELTA")

    # -------------------------------------------------------------- reads
    def load(self, coll: str) -> list[dict]:
        rows = self._exec(f"SELECT doc FROM {self.table(coll)}", fetch=True) or []
        out = []
        for r in rows:
            try:
                out.append(json_util.loads(r[0]))
            except Exception as e:  # a hand-edited row should not stop the app starting
                log.warning("skipping unreadable row in eph_%s: %s", coll, e)
        return out

    # ------------------------------------------------------------- writes
    def upsert(self, coll: str, doc: dict | None) -> None:
        if doc is None:
            return
        with self._lock:
            self._pending.setdefault(coll, {})[str(doc["_id"])] = doc
        self._wake.set()

    def delete(self, coll: str, ids) -> None:
        with self._lock:
            bucket = self._pending.setdefault(coll, {})
            for i in ids:
                bucket[str(i)] = _DELETE
        self._wake.set()

    def flush(self) -> None:
        with self._lock:
            batch, self._pending = self._pending, {}
            self._inflight = bool(batch)
        try:
            self._flush_batch(batch)
        finally:
            self._inflight = False

    def busy(self) -> bool:
        """True while local writes are queued or being written (don't reload over them)."""
        with self._lock:
            return bool(self._pending) or getattr(self, "_inflight", False)

    def _flush_batch(self, batch: dict) -> None:
        failed: dict[str, dict] = {}
        for coll, items in batch.items():
            try:
                self._write(coll, items)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("Databricks write to eph_%s failed (will retry): %s", coll, self.last_error)
                failed[coll] = items
        if failed:
            with self._lock:  # newer writes queued meanwhile win over the failed ones
                for coll, items in failed.items():
                    bucket = self._pending.setdefault(coll, {})
                    for k, v in items.items():
                        bucket.setdefault(k, v)
        elif batch:
            self.last_error = None
            self.last_sync_at = time.time()
            self.rows_written += sum(len(v) for v in batch.values())

    def _write(self, coll: str, items: dict[str, Any], chunk: int = 50) -> None:
        dels = [k for k, v in items.items() if v is _DELETE]
        ups = [(k, json_util.dumps(v)) for k, v in items.items() if v is not _DELETE]
        t = self.table(coll)
        for i in range(0, len(dels), chunk):
            part = dels[i:i + chunk]
            names = ", ".join(f":d{j}" for j in range(len(part)))
            self._exec(f"DELETE FROM {t} WHERE id IN ({names})", {f"d{j}": v for j, v in enumerate(part)})
        for i in range(0, len(ups), chunk):
            part = ups[i:i + chunk]
            values = ", ".join(f"(:i{j}, :v{j})" for j in range(len(part)))
            params: dict[str, Any] = {}
            for j, (k, v) in enumerate(part):
                params[f"i{j}"], params[f"v{j}"] = k, v
            self._exec(
                f"MERGE INTO {t} AS t USING (SELECT * FROM VALUES {values} AS s(id, doc)) AS s ON t.id = s.id "
                "WHEN MATCHED THEN UPDATE SET t.doc = s.doc, t.updated_at = current_timestamp() "
                "WHEN NOT MATCHED THEN INSERT (id, doc, updated_at) VALUES (s.id, s.doc, current_timestamp())",
                params)

    def _loop(self) -> None:
        while not self._stop:
            self._wake.wait(self.flush_interval)
            self._wake.clear()
            time.sleep(0.3)  # let a burst of writes collapse into one batch
            self.flush()

    def live_counts(self, run_id: str = "", max_age: float = 10.0) -> dict[str, tuple[int, int]]:
        """Row counts read from Databricks itself: {collection: (all rows, rows for run_id)}. Cached briefly."""
        key = run_id or ""
        hit = self._counts_cache.get(key)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
        mine = ("count_if(id = :r OR get_json_object(doc, '$.run_id') = :r "
                "OR get_json_object(doc, '$.source_run_id') = :r)")
        sql = " UNION ALL ".join(f"SELECT '{c}' AS c, count(*) AS n, {mine} AS m FROM {self.table(c)}"
                                 for c in COLLECTIONS)
        rows = self._exec(sql, {"r": key}, fetch=True) or []
        out = {str(r[0]): (int(r[1] or 0), int(r[2] or 0)) for r in rows}
        self._counts_cache[key] = (time.time(), out)
        return out

    def pending(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._pending.values())

    def close(self) -> None:
        if self._stop:
            return
        self._stop = True
        self._wake.set()
        try:
            self.flush()
        finally:
            self._reset()


class MirroredCollection:
    """A mongomock collection whose writes are copied to Databricks. Reads pass straight through."""

    def __init__(self, coll, mirror: DatabricksMirror, name: str):
        self._c, self._m, self._name = coll, mirror, name

    def __getattr__(self, attr):
        return getattr(self._c, attr)

    def _sync(self, _id) -> None:
        if _id is not None:
            self._m.upsert(self._name, self._c.find_one({"_id": _id}))

    def insert_one(self, doc, *a, **kw):
        r = self._c.insert_one(doc, *a, **kw)
        self._sync(r.inserted_id)
        return r

    def insert_many(self, docs, *a, **kw):
        r = self._c.insert_many(docs, *a, **kw)
        for i in r.inserted_ids:
            self._sync(i)
        return r

    def _write_one(self, op, flt, *a, **kw):
        hit = self._c.find_one(flt, {"_id": 1})
        r = getattr(self._c, op)(flt, *a, **kw)
        self._sync(hit["_id"] if hit else getattr(r, "upserted_id", None))
        return r

    def update_one(self, flt, update, *a, **kw):
        return self._write_one("update_one", flt, update, *a, **kw)

    def replace_one(self, flt, doc, *a, **kw):
        return self._write_one("replace_one", flt, doc, *a, **kw)

    def update_many(self, flt, update, *a, **kw):
        ids = [d["_id"] for d in self._c.find(flt, {"_id": 1})]
        r = self._c.update_many(flt, update, *a, **kw)
        for i in ids:
            self._sync(i)
        if getattr(r, "upserted_id", None) is not None:
            self._sync(r.upserted_id)
        return r

    def find_one_and_update(self, flt, update, *a, **kw):
        r = self._c.find_one_and_update(flt, update, *a, **kw)
        hit = r or self._c.find_one(flt, {"_id": 1})
        if hit:
            self._sync(hit["_id"])
        return r

    def delete_one(self, flt, *a, **kw):
        hit = self._c.find_one(flt, {"_id": 1})
        r = self._c.delete_one(flt, *a, **kw)
        if hit:
            self._m.delete(self._name, [hit["_id"]])
        return r

    def delete_many(self, flt, *a, **kw):
        ids = [d["_id"] for d in self._c.find(flt, {"_id": 1})]
        r = self._c.delete_many(flt, *a, **kw)
        self._m.delete(self._name, ids)
        return r


class MirroredDB:
    """Database handle whose collections are mirrored (so `store.db["explanations"]` is mirrored too)."""

    def __init__(self, db, mirror: DatabricksMirror):
        self._db, self._m = db, mirror
        self._cache: dict[str, MirroredCollection] = {}

    def __getitem__(self, name: str) -> MirroredCollection:
        if name not in self._cache:
            self._cache[name] = MirroredCollection(self._db[name], self._m, name)
        return self._cache[name]

    def __getattr__(self, attr):
        return getattr(self._db, attr)


def open_mirrored_db(settings, client, connect=None):
    """Create the mirror, load every table into the in-memory engine and return (db, mirror)."""
    mirror = DatabricksMirror(settings, connect=connect)
    raw = client[settings.mongodb_db]
    total = 0
    for c in COLLECTIONS:
        docs = mirror.load(c)
        if docs:
            raw[c].insert_many(docs)
            total += len(docs)
    log.info("Databricks: loaded %d documents from %s.%s", total, settings.databricks_catalog, settings.databricks_schema)
    return MirroredDB(raw, mirror), mirror
