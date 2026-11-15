"""Databricks backend, tested against a fake SQL connection that stores rows in a dict.

Checks: tables are created, every write reaches Databricks (batched MERGE / DELETE), and a fresh
Store loads a finished run back so rebuilds keep working after a restart.
"""
import os
import re

os.environ["EPH_MOCK"] = "1"
os.environ.pop("MONGODB_URI", None)

from eph.config import get_settings  # noqa: E402
from eph.db import Store  # noqa: E402
from eph.pipeline import Services, create_run, execute, make_services, rebuild  # noqa: E402


class FakeWarehouse:
    """Understands exactly the statements DatabricksMirror sends."""

    def __init__(self):
        self.tables: dict[str, dict[str, str]] = {}
        self.statements: list[str] = []

    def connect(self):
        wh = self

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False

            def execute(self, sql, params=None):
                params = params or {}
                wh.statements.append(sql.split()[0])
                t = (re.findall(r"`(eph_\w+)`", sql) or [None])[0]
                self.rows = []
                if sql.startswith("CREATE TABLE"):
                    wh.tables.setdefault(t, {})
                elif sql.startswith("SELECT doc"):
                    self.rows = [(v,) for v in wh.tables[t].values()]
                elif sql.startswith("DELETE"):
                    for k, v in params.items():
                        wh.tables[t].pop(v, None)
                elif sql.startswith("MERGE"):
                    n = len([k for k in params if k.startswith("i")])
                    for j in range(n):
                        wh.tables[t][params[f"i{j}"]] = params[f"v{j}"]

            def fetchall(self): return self.rows

        class Conn:
            def cursor(self): return Cursor()
            def close(self): pass

        return Conn()


def services_on(wh) -> Services:
    svc = make_services()
    svc.store = Store(get_settings(), databricks_connect=wh.connect)
    return svc


def test_run_persists_to_databricks_and_reloads():
    wh = FakeWarehouse()
    svc = services_on(wh)
    assert svc.store.backend == "databricks"
    assert set(wh.tables) >= {"eph_runs", "eph_decisions", "eph_artifacts", "eph_precedents"}

    run_id = create_run(svc, "an ESP32 that waters my plant when the soil is dry")
    assert execute(svc, run_id), svc.store.get_run(run_id)["error"]
    svc.store.mirror.flush()
    assert svc.store.mirror.pending() == 0
    n_dec = svc.store.decisions.count_documents({"run_id": run_id})
    assert len(wh.tables["eph_decisions"]) == n_dec > 10
    assert run_id in wh.tables["eph_runs"]
    assert '"status": "done"' in wh.tables["eph_runs"][run_id]

    # "Restart": a brand-new store reads everything back from the warehouse.
    svc2 = services_on(wh)
    run = svc2.store.get_run(run_id)
    assert run and run["status"] == "done"
    assert svc2.store.decisions.count_documents({"run_id": run_id}) == n_dec
    assert svc2.store.stale(run_id) == []

    # Rebuilds work on reloaded data, and the new versions are written back.
    rebuild(svc2, run_id, "part.oled", "use a 16x2 I2C LCD instead of the OLED")
    svc2.store.mirror.flush()
    assert len(wh.tables["eph_decisions"]) == svc2.store.decisions.count_documents({"run_id": run_id}) > n_dec


def test_deletes_and_failed_writes_retry():
    wh = FakeWarehouse()
    store = Store(get_settings(), databricks_connect=wh.connect)
    _id = store.precedents.insert_one({"source_run_id": "x", "prompt": "p"}).inserted_id
    store.mirror.flush()
    assert str(_id) in wh.tables["eph_precedents"]

    store.precedents.delete_one({"_id": _id})
    store.mirror.flush()
    assert str(_id) not in wh.tables["eph_precedents"]

    # Warehouse down: the write stays queued and goes through on the next flush.
    store.mirror._connect = lambda: (_ for _ in ()).throw(ConnectionError("warehouse asleep"))
    store.mirror._reset()
    store.runs.insert_one({"prompt": "later"})
    store.mirror.flush()
    assert store.mirror.pending() == 1 and "warehouse asleep" in store.mirror.last_error
    store.mirror._connect = wh.connect
    store.mirror.flush()
    assert store.mirror.pending() == 0 and store.mirror.last_error is None
    assert any('"later"' in v for v in wh.tables["eph_runs"].values())
