"""Check every service in .env with one tiny request each, and say exactly what's wrong.

    python scripts/check_setup.py

Uses 1 LLM call per model (builder + reviewer) and 1 Linkup search. Safe to run repeatedly."""
import shutil
import sys
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eph.config import REPO_ROOT, get_settings  # noqa: E402

OK, BAD, SKIP = "  OK  ", " FAIL ", " ---- "
results = []


def line(status, name, msg):
    results.append(status)
    print(f"[{status}] {name:<14} {msg}")


s = get_settings()
print(f".env: {'found' if (REPO_ROOT / '.env').exists() else 'NOT FOUND (copy .env.example to .env)'} in {REPO_ROOT}")
if not (REPO_ROOT / '.env').exists():
    near = sorted(x.name for x in REPO_ROOT.iterdir() if x.name.lower().startswith(".env") or x.name.lower().startswith("env"))
    if near:
        print(f"      files that look like it: {near} -> rename the one with your keys to exactly .env")
def _mask(v):
    return f"set ({v[:4]}...{v[-2:]}, {len(v)} chars)" if v else "EMPTY"
for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "LINKUP_API_KEY", "DATABRICKS_TOKEN", "EPH_MOCK"):
    v = os.environ.get(k, "")
    print(f"      {k:<18} {v if k == 'EPH_MOCK' else _mask(v)}")
print()

if s.mock:
    line(SKIP, "offline mode", "EPH_MOCK=1: the app uses canned answers and ignores the keys below. "
                                "Set EPH_MOCK=0 in .env (and run `set EPH_MOCK=` in this window) for real runs.")

# ---- LLMs: one ping per AI provider that has a key (these appear in the Engineer / Bar Raiser pickers)
from eph import models  # noqa: E402

schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
          "required": ["ok", "word"]}
real = s.__class__(**{**s.__dict__, "mock": False})
rows = [r for r in models.listing(real) if r["provider"] != "mock"]
tested = set()
for r in rows:
    if r["provider"] in tested:
        continue
    tested.add(r["provider"])
    if not r["available"]:
        line(SKIP, r["group"][:14], r["reason"])
        continue
    try:
        t = time.time()
        out = models.build(real, r["id"]).call_tool(
            system="You are a connectivity test.", prompt="Reply with ok=true and word='ready'.",
            tool_name="ping", tool_description="Answer the ping.", schema=schema, max_tokens=200)
        line(OK, r["group"][:14], f"{r['label']} answered in {time.time() - t:.1f}s: {out}")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        hint = (" -> key wrong or not activated" if "401" in msg or "403" in msg or "API key" in msg else
                " -> model name not on your account: set EPH_MODEL in .env to one from your console" if "404" in msg else
                " -> free-tier quota used up for now; wait, or use another key/project" if "429" in msg else "")
        line(BAD, r["group"][:14], f"{r['label']}: {msg[:220]}{hint}")
d = models.defaults(real)
print(f"               default picks: Engineer = {d['engineer']}, Bar Raiser = {d['bar_raiser']}")

# ---- Database: MongoDB if MONGODB_URI is set, otherwise Databricks if configured, otherwise in-memory
db_parts = [s.databricks_host, s.databricks_http_path, s.databricks_token]
if not s.mongodb_uri and any(db_parts) and not all(db_parts):
    missing = [n for n, v in zip(["DATABRICKS_HOST", "DATABRICKS_HTTP_PATH", "DATABRICKS_TOKEN"], db_parts) if not v]
    line(BAD, "Databricks", f"missing {', '.join(missing)} in .env")
elif s.use_databricks:
    try:
        from databricks import sql as dbsql

        with dbsql.connect(server_hostname=s.databricks_host, http_path=s.databricks_http_path,
                           access_token=s.databricks_token, _socket_timeout=90,
                           _retry_stop_after_attempts_count=3, _retry_stop_after_attempts_duration=100) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_catalog(), current_user()")
            cat, user = cur.fetchone()
            target = f"`{s.databricks_catalog}`.`{s.databricks_schema}`"
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {target}")
            cur.execute(f"SHOW TABLES IN {target}")
            n = len([r for r in cur.fetchall() if str(r[1]).startswith("eph_")])
        line(OK, "Databricks", f"connected as {user}; tables go in {s.databricks_catalog}.{s.databricks_schema} "
             f"({n} eph_ table(s) so far; the app creates the rest)")
    except ImportError:
        line(BAD, "Databricks", "connector not installed -> pip install -r requirements.txt")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        low = msg.lower()
        hint = (" -> can't reach the host: check DATABRICKS_HOST (just the host, e.g. dbc-1234.cloud.databricks.com) and your internet" if "max retries" in low or "getaddrinfo" in low or "name or service" in low or "proxy" in low else
                " -> token wrong or expired: make a new one in Settings -> Developer -> Access tokens" if "401" in msg or "403" in msg or "invalid access token" in low or "unauthorized" in low else
                " -> catalog not found: set DATABRICKS_CATALOG (Free Edition uses 'workspace')" if "catalog" in low and "not" in low else
                " -> check DATABRICKS_HOST (just the host, e.g. dbc-1234.cloud.databricks.com) and DATABRICKS_HTTP_PATH (/sql/1.0/warehouses/...)" if "404" in msg or "name or service" in low or "getaddrinfo" in low else
                " -> the SQL warehouse may be starting up (takes ~1 min); run this again" if "timed out" in low or "timeout" in low else "")
        line(BAD, "Databricks", f"{type(e).__name__}: {msg[:200]}{hint}")
elif not s.mongodb_uri:
    line(SKIP, "Database", "no MONGODB_URI or DATABRICKS_* set: using the in-memory database (data is lost on restart)")
elif "USER:PASS" in s.mongodb_uri or "<db_password>" in s.mongodb_uri:
    line(BAD, "MongoDB", "MONGODB_URI still has the placeholder text: paste your real connection string")
else:
    try:
        from pymongo import MongoClient

        c = MongoClient(s.mongodb_uri, serverSelectionTimeoutMS=8000)
        c.admin.command("ping")
        n = c[s.mongodb_db]["runs"].count_documents({})
        line(OK, "MongoDB", f"connected; database '{s.mongodb_db}' has {n} run(s)")
        try:
            names = [ix["name"] for ix in c[s.mongodb_db]["precedents"].list_search_indexes()]
            vec = [x for x in names if x.startswith("precedent_vectors")]
            line(OK if vec else SKIP, "Vector index", f"found {vec}" if vec else
                 "not created yet: run `python scripts/init_atlas.py` (memory still works without it)")
        except Exception as e:  # noqa: BLE001
            line(SKIP, "Vector index", f"couldn't list search indexes ({type(e).__name__}); run scripts/init_atlas.py")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        hint = (" -> wrong username/password in the URI" if "auth" in msg.lower() else
                " -> add your IP in Atlas -> Network Access (or 0.0.0.0/0)" if "timed out" in msg.lower()
                or "ServerSelection" in type(e).__name__ else "")
        line(BAD, "MongoDB", f"{type(e).__name__}: {msg[:200]}{hint}")

# ---- Search
if s.linkup_api_key:
    try:
        from eph.search import LinkupSearch

        r = LinkupSearch(s.linkup_api_key, "standard").search("SSD1306 OLED module pinout operating voltage")
        line(OK, "Linkup", f"{len(r.sources)} source(s), e.g. {r.sources[0].url if r.sources else '-'}")
    except Exception as e:  # noqa: BLE001
        line(BAD, "Linkup", f"{str(e)[:220]}")
else:
    line(SKIP, "Linkup", "LINKUP_API_KEY empty: parts won't be verified against the web"
         + (" (Tavily will be used)" if s.tavily_api_key else ""))
if s.tavily_api_key and not s.linkup_api_key:
    try:
        from eph.search import TavilySearch

        r = TavilySearch(s.tavily_api_key).search("SSD1306 OLED module pinout")
        line(OK, "Tavily", f"{len(r.sources)} source(s)")
    except Exception as e:  # noqa: BLE001
        line(BAD, "Tavily", str(e)[:220])

# ---- Embeddings (optional)
if s.voyage_api_key:
    try:
        from eph.precedents import VoyageEmbedder

        v = VoyageEmbedder(s.voyage_api_key, s.voyage_model, s.voyage_dims).embed(["test"], "query")[0]
        line(OK, "Voyage", f"{len(v)}-dim embedding")
    except Exception as e:  # noqa: BLE001
        line(BAD, "Voyage", str(e)[:220])
else:
    line(SKIP, "Voyage", "optional: memory uses the offline embedding")

# ---- Compiler
from eph.compiler import find_cli, toolchain_status  # noqa: E402

st = toolchain_status(s)
line(OK if st == "ready" else SKIP, "arduino-cli",
     f"ESP32 core installed ({find_cli(s.arduino_cli)})" if st == "ready" else f"{st} (firmware is checked but not compiled)")
if st != "ready" and sys.platform.startswith("win"):
    print("               Windows: run `python scripts\\setup_arduino_windows.py` (installs everything), then restart the server.")

print(f"\n{results.count(OK)} OK, {results.count(BAD)} failing, {results.count(SKIP)} not configured / skipped")
sys.exit(1 if BAD in results else 0)
