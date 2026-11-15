"""Stage: procurement. Turns the parts list into a shopping cart with buy links and a budget check.

No LLM call (cheap and deterministic): for each part it runs one web search ("buy <model> UK price"),
takes the first source as the buy link and a £ price from the answer when one is stated; otherwise it
keeps the parts stage's estimate and marks the line `estimate`. The user always does the final checkout.

Decisions: `cart.<part_id>` (depends on `part.<part_id>`), `cart.support` (small parts from
`wiring.support`) and `cart.total` (depends on every line + `req.budget`)."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from ..schema import Decision
from . import Ctx

STAGE = "procurement"
PRICE_RE = re.compile(r"£\s?(\d{1,4}(?:\.\d{1,2})?)")
SMALL_PART_GBP = {"resistor": 0.1, "diode": 0.1, "capacitor": 0.3, "wire": 0.0}
LIVE_PROVIDERS = {"linkup", "tavily"}


def _price(answer: str) -> float | None:
    m = PRICE_RE.search(answer or "")
    return float(m.group(1)) if m else None


def run(ctx: Ctx) -> None:
    order = {r: i for i, r in enumerate(["mcu", "sensor", "input", "actuator", "display", "comms", "driver",
                                         "power", "passive", "mechanical"])}
    parts = sorted(ctx.store.active(ctx.run_id, prefix="part."),
                   key=lambda d: (order.get(d["choice"].get("role"), 5), d["key"]))
    live_search = ctx.search.name in LIVE_PROVIDERS
    results = {}
    if ctx.search.name != "none":
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = {d["key"]: pool.submit(ctx.search.search, f"buy {d['choice']['model']} UK price in stock")
                    for d in parts}
            for k, f in futs.items():
                try:
                    results[k] = f.result()
                except Exception as e:  # noqa: BLE001
                    ctx.emit(STAGE, f"shop search failed for {k}: {e}", "warn")

    lines, refs = [], []
    for d in parts:
        c = d["choice"]
        r = results.get(d["key"])
        live_price = _price(r.answer) if (r and live_search) else None
        est = float(c.get("est_price_gbp") or 0) or (0.1 if c.get("role") == "passive" else 0.0)
        unit = live_price if live_price is not None else est
        url = r.sources[0].url if (r and r.sources) else None
        line = {"part_id": d["key"].split(".", 1)[1], "name": c["name"], "model": c["model"],
                "qty": int(c.get("qty") or 1), "unit_price_gbp": round(unit, 2),
                "price_source": "live" if live_price is not None else "estimate",
                "url": url, "sources": [s.url for s in (r.sources if r else [])[:3] if s.url]}
        rec = ctx.store.record(Decision(
            run_id=ctx.run_id, key=f"cart.{line['part_id']}", stage=STAGE, kind="cart_line",
            title=f"{line['qty']}× {c['name']}: £{unit * line['qty']:.2f} ({line['price_source']})",
            choice=line, rationale="Cheapest listing found" if live_price is not None else
            "No live price found; using the parts-stage estimate",
            depends_on=ctx.store.refs(ctx.run_id, [d["key"]]),
            evidence=(r.sources[:2] if r else []), confidence=0.8 if live_price is not None else 0.5))
        lines.append(line)
        refs.append(rec["key"])

    support = ctx.store.get_active(ctx.run_id, "wiring.support")
    in_cart = {(c["choice"].get("model") or "").lower() for c in parts} | {(c["choice"].get("name") or "").lower() for c in parts}

    def already_listed(s: dict) -> bool:
        """A support item that is also a part (e.g. the flyback diode) must not be bought twice."""
        text = f"{s.get('name', '')} {s.get('value', '')}".lower()
        return any(m and (m in text or text.strip() == m) for m in in_cart)

    items = [s for s in (support["choice"] or []) if s.get("kind") != "wire" and not already_listed(s)] if support else []
    if items:
        cost = sum(SMALL_PART_GBP.get(s.get("kind", ""), 0.2) for s in items)
        line = {"part_id": "support", "name": "Small parts (resistors, diodes, capacitors)",
                "model": "; ".join(f"{s.get('name')} {s.get('value', '')}".strip() for s in items),
                "qty": 1, "unit_price_gbp": round(cost, 2), "price_source": "estimate", "url": None, "sources": []}
        ctx.store.record(Decision(
            run_id=ctx.run_id, key="cart.support", stage=STAGE, kind="cart_line",
            title=f"Small parts: £{cost:.2f} (estimate)", choice=line, rationale="Standard values, any kit has them.",
            depends_on=ctx.store.refs(ctx.run_id, ["wiring.support"]), confidence=0.7))
        lines.append(line)
        refs.append("cart.support")

    total = round(sum(l["unit_price_gbp"] * l["qty"] for l in lines), 2)
    budget_d = ctx.store.get_active(ctx.run_id, "req.budget")
    budget = budget_d["choice"]["gbp"] if budget_d else None
    over = budget is not None and total > budget
    n_est = sum(1 for l in lines if l["price_source"] == "estimate")
    ctx.store.record(Decision(
        run_id=ctx.run_id, key="cart.total", stage=STAGE, kind="cart_total",
        title=f"Cart total £{total:.2f}" + (f" / budget £{budget:.2f}" if budget else "") + (" OVER BUDGET" if over else ""),
        choice={"total_gbp": total, "budget_gbp": budget, "over_budget": over, "lines": len(lines),
                "estimated_lines": n_est},
        rationale=f"{len(lines) - n_est} live price(s), {n_est} estimate(s). The user does the final checkout.",
        depends_on=ctx.store.refs(ctx.run_id, refs + (["req.budget"] if budget_d else [])),
        confidence=0.6 if n_est else 0.85))
    ctx.store.save_artifact(ctx.run_id, "cart", {"lines": lines, "total_gbp": total, "budget_gbp": budget,
                                                 "over_budget": over}, ctx.store.refs(ctx.run_id, refs))
    ctx.emit(STAGE, f"Cart: {len(lines)} lines, £{total:.2f}" + (f" of £{budget:.2f} budget" if budget else "")
             + f"; {n_est} estimated price(s)", "warn" if over else "info")
