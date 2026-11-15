"""Create the MongoDB indexes, including the Atlas Vector Search index for precedents.

    python scripts/init_atlas.py

If your cluster tier can't create search indexes from a driver, this prints the JSON to paste into
Atlas: Database → your cluster → Atlas Search → Create Search Index → Atlas Vector Search → JSON Editor,
database = MONGODB_DB, collection = precedents, name as printed.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eph.config import get_settings  # noqa: E402
from eph.db import Store  # noqa: E402
from eph.precedents import ensure_vector_index, index_definition, index_name, make_embedder  # noqa: E402

s = get_settings()
if not s.mongodb_uri:
    sys.exit("MONGODB_URI is not set in .env")
store = Store(s)
print(f"Connected to {s.mongodb_db}; regular indexes ensured.")
emb = make_embedder(s)
status = ensure_vector_index(store, emb)
print(f"Embeddings: {emb.name}  →  vector search: {status}")
if not status.startswith("atlas"):
    print(f"\nCreate it by hand in the Atlas UI. Index name: {index_name(emb)}  collection: precedents\n")
    print(json.dumps(index_definition(emb), indent=2))
else:
    print("Done. New indexes take a minute to build; until then the app falls back to exact search.")
