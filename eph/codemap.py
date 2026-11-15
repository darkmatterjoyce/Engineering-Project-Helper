"""Links firmware lines ↔ explanations ↔ flow-diagram nodes, for the interactive Firmware view.

Line numbers are never trusted blindly from the LLM:
  * top-level *blocks* (includes, globals, each function) are found deterministically by brace matching;
  * the LLM only explains blocks by id, and maps each diagram node to a line range on a numbered listing;
  * every range is validated (clamped to the file, start ≤ end) and unknown node ids are dropped.
"""
from __future__ import annotations

import re

FUNC_RE = re.compile(r"^\s*(?:static\s+|inline\s+)*[A-Za-z_][\w:<>\s\*&]*?\b([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:const\s*)?\{?\s*(?://.*)?$")
NODE_RE = re.compile(r"(?:^|[\s;>|&-])([A-Za-z][A-Za-z0-9_]*)\s*(?:\[|\(|\{)")
KEYWORDS = {"if", "for", "while", "switch", "else", "return", "flowchart", "graph", "subgraph", "classDef", "class", "style"}


def numbered(code: str) -> str:
    return "\n".join(f"{i:>3}| {ln}" for i, ln in enumerate(code.splitlines(), 1))


def _strip(line: str, in_block_comment: bool) -> tuple[str, bool]:
    """Remove comments and string/char literals so braces inside them don't count."""
    out, i, n = [], 0, len(line)
    while i < n:
        if in_block_comment:
            j = line.find("*/", i)
            if j < 0:
                return "".join(out), True
            i, in_block_comment = j + 2, False
            continue
        two = line[i:i + 2]
        if two == "//":
            break
        if two == "/*":
            in_block_comment, i = True, i + 2
            continue
        ch = line[i]
        if ch in "\"'":
            j = i + 1
            while j < n and line[j] != ch:
                j += 2 if line[j] == "\\" else 1
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), in_block_comment


def blocks(code: str) -> list[dict]:
    """Top-level blocks: [{id, kind, name, start, end}] with 1-based inclusive line numbers.
    Leading comment lines are attached to the function they describe."""
    lines = code.splitlines()
    out: list[dict] = []
    depth, in_c = 0, False
    cur: dict | None = None
    pending_start: int | None = None  # first line of a comment run just above a function
    loose: list[int] = []             # top-level lines that aren't inside a function

    def flush_loose() -> None:
        if not loose:
            return
        kind = "preamble" if all(lines[i - 1].lstrip().startswith(("#", "//")) or not lines[i - 1].strip()
                                 for i in loose) and not out else "globals"
        if out and out[-1]["kind"] == kind:
            out[-1]["end"] = loose[-1]
        else:
            out.append({"id": f"{kind}{sum(1 for b in out if b['kind'] == kind) + 1}", "kind": kind,
                        "name": "includes" if kind == "preamble" else "constants & globals",
                        "start": loose[0], "end": loose[-1]})
        loose.clear()

    for no, raw in enumerate(lines, 1):
        clean, in_c = _strip(raw, in_c)
        if depth == 0 and cur is None:
            m = FUNC_RE.match(clean) if "(" in clean and not clean.strip().startswith("#") else None
            if m and m.group(1) not in KEYWORDS and ("{" in clean or (no < len(lines) and lines[no].strip().startswith("{"))):
                start = pending_start or no
                if pending_start:  # those comment lines belong to the function, not the globals
                    while loose and loose[-1] >= pending_start:
                        loose.pop()
                flush_loose()
                cur = {"id": f"fn_{m.group(1)}", "kind": "function", "name": f"{m.group(1)}()", "start": start}
            elif raw.strip().startswith("//") and pending_start is None:
                pending_start = no
            elif raw.strip() and not raw.strip().startswith("//"):
                pending_start = None
            elif not raw.strip():
                pending_start = None
        depth += clean.count("{") - clean.count("}")
        if cur is not None:
            if depth <= 0 and "}" in clean:
                cur["end"] = no
                out.append(cur)
                cur, depth, pending_start = None, 0, None
        elif raw.strip():
            loose.append(no)
    if cur is not None:
        cur["end"] = len(lines)
        out.append(cur)
    flush_loose()
    out.sort(key=lambda b: b["start"])
    # split "#include ..." lines off the first globals block so they get their own explanation
    if out and out[0]["kind"] == "globals":
        g = out[0]
        inc = [i for i in range(g["start"], g["end"] + 1) if lines[i - 1].lstrip().startswith("#include")]
        if inc and inc[-1] < g["end"]:
            out.insert(0, {"id": "preamble1", "kind": "preamble", "name": "includes", "start": g["start"], "end": inc[-1]})
            g["start"] = inc[-1] + 1
            while g["start"] < g["end"] and not lines[g["start"] - 1].strip():
                g["start"] += 1
    return out


def node_ids(mermaid: str) -> dict[str, str]:
    """{node_id: label} for every node declared with a shape in the Mermaid source."""
    out: dict[str, str] = {}
    pat = r'([A-Za-z][A-Za-z0-9_]*)\s*(?:\[\[|\[\(|\(\(|\[|\(|\{\{|\{)\s*(?:"([^"]*)"|([^\]\)\}]*))'
    for m in re.finditer(pat, mermaid):
        nid = m.group(1)
        if nid in KEYWORDS or nid in out:
            continue
        out[nid] = (m.group(2) if m.group(2) is not None else m.group(3) or "").strip()
    return out


def clean_steps(steps: list[dict], n_lines: int, valid_nodes: set[str]) -> list[dict]:
    out = []
    for s in steps or []:
        try:
            a, b = int(s.get("start_line", 0)), int(s.get("end_line", 0))
        except (TypeError, ValueError):
            continue
        if s.get("node_id") not in valid_nodes:
            continue
        a, b = max(1, min(a, b)), min(n_lines, max(a, b))
        if a > n_lines or b < 1:
            continue
        out.append({"node_id": s["node_id"], "start": a, "end": b, "explanation": (s.get("explanation") or "").strip()})
    return out


def build_walkthrough(code: str, mermaid: str, block_notes: list[dict], steps: list[dict]) -> dict:
    """Merge LLM notes onto the deterministic blocks and validated steps."""
    bl = blocks(code)
    notes = {b.get("id"): b for b in block_notes or []}
    for b in bl:
        n = notes.get(b["id"], {})
        b["summary"] = (n.get("summary") or "").strip()
        b["explanation"] = (n.get("explanation") or "").strip()
    nodes = node_ids(mermaid)
    return {"blocks": bl, "steps": clean_steps(steps, len(code.splitlines()), set(nodes)), "nodes": nodes}
