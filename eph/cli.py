"""Command line: python -m eph.cli "an ESP32 that waters my plant when the soil is dry"

Writes the outputs to ./out/<run_id>/ as well as MongoDB."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import clean
from .pipeline import create_run, execute, make_services


def export(svc, run_id: str, out_dir: Path) -> Path:
    d = out_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    arts = svc.store.latest_artifacts(run_id)
    if "firmware" in arts:
        fw = arts["firmware"]["content"]
        (d / fw["filename"]).write_text(fw["code"])
    if "parts_list" in arts:
        (d / "parts.json").write_text(json.dumps(arts["parts_list"]["content"], indent=2))
    if "wiring" in arts:
        (d / "wiring.json").write_text(json.dumps(arts["wiring"]["content"], indent=2))
        (d / "wiring.mmd").write_text(arts["wiring"]["content"]["mermaid"])
        if arts["wiring"]["content"].get("breadboard_svg"):
            (d / "breadboard.svg").write_text(arts["wiring"]["content"]["breadboard_svg"], encoding="utf-8")
    if "flow_diagram" in arts:
        (d / "code_flow.mmd").write_text(arts["flow_diagram"]["content"]["mermaid"])
    if "compile_log" in arts:
        (d / "compile.log").write_text(arts["compile_log"]["content"]["log"])
    (d / "decision_log.json").write_text(json.dumps(clean(svc.store.active(run_id)), indent=2))
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description="Engineering Project Helper: one sentence → ESP32 project")
    ap.add_argument("prompt", help="one-sentence project idea")
    ap.add_argument("--out", default="out", help="export directory (default ./out)")
    args = ap.parse_args()

    svc = make_services()
    print(f"providers: {svc.providers}")
    run_id = create_run(svc, args.prompt)
    print(f"run {run_id}")
    ok = execute(svc, run_id)
    path = export(svc, run_id, Path(args.out))
    stale = svc.store.stale(run_id)
    print(f"\n{'DONE' if ok else 'FAILED'}: {len(svc.store.active(run_id))} active decisions, "
          f"{len(stale)} stale. Files in {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
