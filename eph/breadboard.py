"""Breadboard view: draws the validated wiring as a picture of a real breadboard (SVG).

Deterministic, no LLM: the picture is generated from the same pin decisions the firmware uses, so it
can't disagree with the pin table.

Layout conventions (top to bottom):
  modules that need 5 V     cards above the board, header pins plugged into row j
  top rails                 red + = 5 V (VIN), blue − = GND
  rows j..f                 top half (each column is one connected strip)
  ── centre gap ──          the ESP32 DevKit straddles it: top header in row i, bottom header in row b
  rows e..a                 bottom half
  bottom rails              blue − = GND, red + = 3V3
  3.3 V modules             cards below the board, header pins plugged into row a
Parts with no ESP32 pin of their own (pumps, motors) sit beside the board, wired in.
Transistors (MOSFET/BJT) plug into the board: three legs in three columns of the bottom half.
2-terminal parts (resistors, diodes, capacitors) are laid out the way you would build it: straight along a
row between two columns, or straight from a column into a rail. When one end is an ESP32 pin or an
off-board part, a jumper first brings that net into a free column next to the component.
Cables (USB, jumpers) are not drawn as parts.
"""
from __future__ import annotations

import math
import re
from html import escape

from .esp32 import parse_pin

P = 20                 # hole pitch (px)
ROWS_TOP = ["j", "i", "h", "g", "f"]
ROWS_BOT = ["e", "d", "c", "b", "a"]
ESP_COL0 = 2           # ESP32 occupies columns 2..16
ESP_TOP = ["VIN", "GND", "D13", "D12", "D14", "D27", "D26", "D25", "D33", "D32", "D35", "D34", "VN", "VP", "EN"]
ESP_BOT = ["3V3", "GND", "D15", "D2", "D4", "RX2", "TX2", "D5", "D18", "D19", "D21", "RX0", "TX0", "D22", "D23"]
LABEL_GPIO = {"VP": 36, "VN": 39, "RX2": 16, "TX2": 17, "RX0": 3, "TX0": 1}
FIRST_MODULE_COL = ESP_COL0 + len(ESP_TOP) + 2

NET_COLORS = {
    "VIN": "#dc2626", "3V3": "#f97316", "GND": "#1f2937", "EXT": "#be123c",
    "i2c_sda": "#2563eb", "i2c_scl": "#eab308", "analog": "#16a34a", "spi_mosi": "#db2777",
    "spi_miso": "#9333ea", "spi_sck": "#0d9488", "spi_cs": "#a16207", "uart_tx": "#0284c7", "uart_rx": "#65a30d",
    "onewire": "#c026d3",
}
SIGNAL_CYCLE = ["#0891b2", "#c2410c", "#7c3aed", "#65a30d", "#4f46e5", "#be185d", "#0f766e", "#a16207"]
NET_LABELS = {"VIN": "5 V (VIN)", "3V3": "3.3 V", "GND": "GND", "EXT": "external supply", "i2c_sda": "I2C SDA",
              "i2c_scl": "I2C SCL", "analog": "analog signal"}


def _gpio_of_label(label: str) -> int | None:
    if label in LABEL_GPIO:
        return LABEL_GPIO[label]
    if label.startswith("D") and label[1:].isdigit():
        return int(label[1:])
    return None


def _norm(pin: str) -> str:
    return str(pin).strip().upper()


_CABLE = re.compile(r"\b(usb|cable|jumper|dupont|breadboard|power bank|charger|lead)\b", re.I)
_TRANSISTOR = re.compile(r"mosfet|transistor|\bbjt\b|\bfet\b|2n2222|2n3904|bc547|irlz|irf\d|ao3400|tip12|\bnpn\b|\bpnp\b", re.I)
_PASSIVE = {"resistor": re.compile(r"resistor|\bohms?\b|Ω|\d\s*k\b", re.I),
            "capacitor": re.compile(r"capacitor|\d\s*[uµn]f\b", re.I),
            "diode": re.compile(r"\bdiode\b|1n4\d{3}|1n58\d{2}", re.I)}
ESP_REFS = {"MCU", "ESP32", "ESP", "DEVKIT"}


def _passive_kind(part: dict) -> str | None:
    if len(part.get("pins") or []) > 2:
        return None
    text = f"{part.get('name', '')} {part.get('model', '')}"
    for kind, rx in _PASSIVE.items():
        if rx.search(text):
            return kind
    return "resistor" if part.get("role") == "passive" else None


def _digits(text: str) -> str:
    m = re.search(r"\d+(?:\.\d+)?", str(text))
    return m.group(0) if m else ""


class Board:
    def __init__(self, cols: int, top_space: int):
        self.cols = cols
        self.x0 = 40
        self.by = top_space
        self.y: dict[str, float] = {}
        y = self.by + 22
        self.y["T+"], self.y["T-"] = y, y + P
        y += 2 * P + 14
        for r in ROWS_TOP:
            self.y[r] = y
            y += P
        y += 18  # centre gap
        for r in ROWS_BOT:
            self.y[r] = y
            y += P
        y += 14
        self.y["B-"], self.y["B+"] = y, y + P
        self.bottom = y + P + 22
        self.right = self.x(cols) + 34
        self.used: set[tuple[str, int]] = set()

    def x(self, col: int) -> float:
        return self.x0 + 34 + (col - 1) * P

    def pt(self, row: str, col: int) -> tuple[float, float]:
        return (self.x(col), self.y[row])

    @staticmethod
    def rail_col_ok(col: int) -> bool:
        return col % 6 != 0  # rails come in groups of 5 holes

    def take(self, row: str, col: int) -> tuple[float, float]:
        self.used.add((row, col))
        return self.pt(row, col)

    def free_in_strip(self, half: str, col: int, prefer: list[str]) -> tuple[float, float] | None:
        for r in prefer:
            if (r, col) not in self.used:
                return self.take(r, col)
        return None

    def free_rows(self, half: str, col: int, order: list[str] | None = None) -> list[str]:
        rows = order or (ROWS_BOT if half == "bot" else list(reversed(ROWS_TOP)))
        return [r for r in rows if (r, col) not in self.used]

    def rail_hole(self, rail: str, near: int, avoid: set[int]) -> tuple[float, float]:
        best = None
        for d in range(0, self.cols + 1):
            for c in (near - d, near + d):
                if 1 <= c <= self.cols and self.rail_col_ok(c) and c not in avoid and (rail, c) not in self.used:
                    best = c
                    break
            if best:
                break
        best = best or near
        return self.take(rail, best)


class Renderer:
    def __init__(self, connections: list[dict], parts: dict[str, dict], support: list[dict]):
        self.connections = [c for c in connections if c.get("part_id") != "mcu"]
        self.parts = {k: v for k, v in parts.items() if k != "mcu"}
        self.support = [dict(s) for s in (support or [])]
        self.wires: list[dict] = []        # {a, b, color, net}
        self.components: list[dict] = []   # {kind, a, b, label}
        self.nets_used: dict[str, str] = {}
        self.notes: list[str] = []
        self.cable_notes: list[str] = []
        self._sig_colors: dict[str, str] = {}
        self._normalise()
        self._plan()

    # ------------------------------------------------------------- clean up what the planner listed
    def _normalise(self) -> None:
        """Cables are not parts on a breadboard, and a resistor/diode/capacitor listed as a 'part' is a
        component, not a module card. Their pins collapse into the net they connect to."""
        alias: dict[str, str] = {}   # "pid.PIN" -> endpoint ref it is really connected to
        drop: set[str] = set()
        for pid, part in list(self.parts.items()):
            text = f"{part.get('name', '')} {part.get('model', '')}"
            if _CABLE.search(text) and part.get("role") not in ("sensor", "display", "actuator"):
                drop.add(pid)
                self.cable_notes.append(part.get("name", pid))
                continue
            kind = _passive_kind(part)
            if not kind:
                continue
            drop.add(pid)
            conns = [c for c in self.connections if c["part_id"] == pid]
            ends = {}
            for c in conns:
                p = parse_pin(c["esp32_pin"])
                ends[_norm(c["part_pin"])] = f"GPIO{p}" if isinstance(p, int) else str(p)
            referenced = any(e.split(".", 1)[0] == pid for s in self.support for e in s.get("ends", []) if "." in e)
            for pin in part.get("pins") or []:
                if _norm(pin) in ends:
                    alias[f"{pid}.{_norm(pin)}"] = ends[_norm(pin)]
            fallback = next(iter(ends.values()), None)
            for s in self.support:   # support ends that name this part's pins -> the net that pin is on
                s["ends"] = [alias.get(f"{e.split('.', 1)[0]}.{_norm(e.split('.', 1)[1])}", fallback or e)
                             if "." in e and e.split(".", 1)[0] == pid else e for e in s.get("ends", [])]
            same = any(s.get("kind") == kind and _digits(s.get("value", "")) and
                       _digits(s.get("value", "")) == _digits(part.get("name", "")) for s in self.support)
            if not referenced and not same and len(ends) == 2:
                self.support.append({"kind": kind, "name": part.get("name", pid), "value": _digits(part.get("name", "")),
                                     "ends": list(ends.values())})
        self.parts = {k: v for k, v in self.parts.items() if k not in drop}
        self.connections = [c for c in self.connections if c["part_id"] not in drop]

    # ------------------------------------------------------------- planning
    def _plan(self) -> None:
        wired = {c["part_id"] for c in self.connections}
        ends_parts = {e.split(".", 1)[0] for s in self.support for e in s.get("ends", []) if "." in e}
        self.chips = [p for p in self.parts if _TRANSISTOR.search(f"{self.parts[p].get('name', '')} "
                                                                   f"{self.parts[p].get('model', '')}")
                      and (p in wired or p in ends_parts)]
        self.onboard = [p for p in self.parts if (p in wired or p in self.chips)]
        self.offboard = [p for p in self.parts if p not in self.onboard and p in ends_parts]

        def pins_of(pid: str) -> list[str]:
            seen, out = set(), []
            for pn in (self.parts[pid].get("pins") or []):
                if _norm(pn) not in seen and _norm(pn) not in ("GPIO",):
                    seen.add(_norm(pn)); out.append(str(pn))
            for c in self.connections:
                if c["part_id"] == pid and _norm(c["part_pin"]) not in seen:
                    seen.add(_norm(c["part_pin"])); out.append(c["part_pin"])
            for s in self.support:
                for e in s.get("ends", []):
                    if e.startswith(pid + ".") and _norm(e.split(".", 1)[1]) not in seen:
                        seen.add(_norm(e.split(".", 1)[1])); out.append(e.split(".", 1)[1])
            return out

        self.mod_pins = {p: pins_of(p) for p in self.onboard + self.offboard}
        # 5 V modules on top (near the 5 V rail), everything else at the bottom (3.3 V rail)
        self.half = {}
        for p in self.onboard:
            needs5 = any(parse_pin(c["esp32_pin"]) == "VIN" for c in self.connections if c["part_id"] == p)
            self.half[p] = "bot" if p in self.chips else "top" if needs5 else "bot"
        cursor = {"top": FIRST_MODULE_COL, "bot": FIRST_MODULE_COL}
        self.mod_cols: dict[str, list[int]] = {}
        for p in self.onboard:
            h = self.half[p]
            n = len(self.mod_pins[p])
            width = n if p in self.chips else max(n, 4)
            start = cursor[h] + (width - n) // 2
            self.mod_cols[p] = list(range(start, start + n))
            self.mod_span = getattr(self, "mod_span", {})
            self.mod_span[p] = (cursor[h], cursor[h] + width - 1)
            cursor[h] += width + 2
        n_comp = sum(1 for s in self.support if s.get("kind") != "wire")
        cols = max(30, cursor["top"] + 2, cursor["bot"] + 2) + 3 * n_comp
        top_space = 120 if any(h == "top" for h in self.half.values()) else 34
        self.b = Board(cols, top_space)
        # off-board parts stacked to the right of the board
        self.off_pos: dict[str, dict] = {}
        y = self.b.by + 30
        for p in self.offboard:
            n = len(self.mod_pins[p])
            h = 34 + 24 * n
            x = self.b.right + 70
            self.off_pos[p] = {"x": x, "y": y, "w": 150, "h": h,
                               "pins": {_norm(pn): (x, y + 30 + 24 * i) for i, pn in enumerate(self.mod_pins[p])}}
            y += h + 24
        self.width = self.b.right + (260 if self.offboard else 20)
        self.has_ext = any("EXT" == _norm(e) for s in self.support for e in s.get("ends", [])) or \
            any(parse_pin(c["esp32_pin"]) == "EXT" for c in self.connections)

        # mark module header holes and ESP header holes as used
        b = self.b
        for p in self.onboard:
            if p in self.chips:   # legs in row d, body over row e
                for col in self.mod_cols[p]:
                    b.used.update({("d", col), ("e", col)})
                continue
            row = "j" if self.half[p] == "top" else "a"
            for col in self.mod_cols[p]:
                b.used.add((row, col))
        for i in range(len(ESP_TOP)):
            col = ESP_COL0 + i
            for r in ("i", "h", "g", "f", "e", "d", "c", "b"):
                b.used.add((r, col))  # header rows + under the ESP32 body

        self.claimed: set[int] = set(range(1, FIRST_MODULE_COL))   # ESP32 area + its power columns
        for p in self.onboard:
            lo, hi = self.mod_span[p]
            self.claimed.update(range(lo, hi + 1))
        self.rail_avoid = {"top": set(), "bot": set()}
        for p in self.onboard:
            if p in self.chips:
                continue
            lo, hi = self.mod_span[p]
            self.rail_avoid[self.half[p]].update(range(lo, hi + 1))
        self.rails_used: set[str] = set()
        self._route()

    def _color(self, net: str, sig: str = "") -> str:
        key = net if net in NET_COLORS else sig if sig in NET_COLORS else None
        if key:
            self.nets_used[key] = NET_COLORS[key]
            return NET_COLORS[key]
        if net not in self._sig_colors:   # a colour no other wire on this board uses yet
            taken = set(self.nets_used.values()) | set(self._sig_colors.values()) | set(NET_COLORS.values())
            free = [c for c in SIGNAL_CYCLE if c not in taken]
            self._sig_colors[net] = free[0] if free else SIGNAL_CYCLE[len(self._sig_colors) % len(SIGNAL_CYCLE)]
        self.nets_used[net] = self._sig_colors[net]
        return self._sig_colors[net]

    def _module_hole(self, pid: str, pin: str) -> tuple[float, float] | None:
        pins = [_norm(x) for x in self.mod_pins[pid]]
        if _norm(pin) not in pins:
            return None
        col = self.mod_cols[pid][pins.index(_norm(pin))]
        prefer = (["c", "b", "a"] if pid in self.chips else
                  ["i", "h", "g", "f"] if self.half[pid] == "top" else ["b", "c", "d", "e"])
        return self.b.free_in_strip(self.half[pid], col, prefer)

    def _module_col(self, pid: str, pin: str) -> int:
        pins = [_norm(x) for x in self.mod_pins[pid]]
        return self.mod_cols[pid][pins.index(_norm(pin))] if _norm(pin) in pins else FIRST_MODULE_COL

    def _rail(self, net: str, near: int, half_hint: str) -> tuple[float, float]:
        rail = {"VIN": "T+", "3V3": "B+"}.get(net)
        if net == "GND":
            rail = "T-" if half_hint == "top" else "B-"
        self.rails_used.add(rail)
        half = "top" if rail.startswith("T") else "bot"
        return self.b.rail_hole(rail, near, self.rail_avoid[half])

    def _esp_hole(self, gpio: int) -> tuple[float, float] | None:
        for i, lab in enumerate(ESP_TOP):
            if _gpio_of_label(lab) == gpio:
                return self.b.free_in_strip("top", ESP_COL0 + i, ["j"])
        for i, lab in enumerate(ESP_BOT):
            if _gpio_of_label(lab) == gpio:
                return self.b.free_in_strip("bot", ESP_COL0 + i, ["a"])
        return None

    @staticmethod
    def _esp_gpio(ref: str) -> int | None:
        r = str(ref).strip()
        if "." in r and r.split(".", 1)[0].upper() in ESP_REFS:
            r = r.split(".", 1)[1]
        if "." in r:
            return None
        p = parse_pin(r)
        return p if isinstance(p, int) else None

    def _endpoint(self, ref: str, near: int, half_hint: str):
        r = _norm(ref)
        if r in ("VIN", "5V", "3V3", "3.3V", "GND"):
            net = {"5V": "VIN", "3.3V": "3V3"}.get(r, r)
            return self._rail(net, near, half_hint), net
        if r == "EXT":
            return self.ext_terminal, "EXT"
        g = self._esp_gpio(ref)
        if g is not None:
            return self._esp_hole(g), f"GPIO{g}"
        if "." not in ref:
            return None, ""
        pid, pin = ref.split(".", 1)
        if pid in self.off_pos:
            return self.off_pos[pid]["pins"].get(_norm(pin)), f"{pid}.{pin}"
        if pid in self.mod_cols:
            return self._module_hole(pid, pin), f"{pid}.{pin}"
        return None, ""

    def _route(self) -> None:
        b = self.b
        self.ext_terminal = (b.right + 70, b.bottom + 30) if self.has_ext else None
        bus_last: dict[int, tuple[float, float]] = {}  # gpio -> last module hole on a shared bus (daisy chain)
        for c in self.connections:
            pid, pin = c["part_id"], c["part_pin"]
            if pid not in self.mod_cols:
                continue
            p = parse_pin(c["esp32_pin"])
            col = self._module_col(pid, pin)
            hole = self._module_hole(pid, pin)
            if hole is None:
                self.notes.append(f"no free hole for {pid}.{pin}")
                continue
            if isinstance(p, int):
                src = bus_last.get(p) or self._esp_hole(p)
                if src is None:
                    self.notes.append(f"GPIO{p} is not on the 30-pin DevKit header")
                    continue
                net = f"GPIO{p}"
                self.wires.append({"a": src, "b": hole, "color": self._color(net, c.get("signal_type", "")),
                                   "label": f"GPIO{p} → {pid}.{pin}"})
                bus_last[p] = hole
            elif isinstance(p, str) and p in ("VIN", "3V3", "GND"):
                rail_pt = self._rail(p, col, self.half[pid])
                self.wires.append({"a": rail_pt, "b": hole, "color": self._color(p), "label": f"{p} → {pid}.{pin}"})
            elif p == "EXT" and self.ext_terminal:
                self.wires.append({"a": self.ext_terminal, "b": hole, "color": self._color("EXT"), "label": f"EXT → {pid}.{pin}"})

        self._hops: dict[str, int] = {}   # net brought into a free column by a jumper -> that column
        for sup in self.support:
            ends = sup.get("ends") or []
            if len(ends) != 2:
                continue
            try:
                ok = self._place_support(sup, ends)
            except Exception as e:  # noqa: BLE001  a drawing problem must never break the wiring stage
                ok, _ = False, e
            if not ok:
                self.notes.append(f"couldn't place {sup.get('name')} ({' ↔ '.join(ends)})")

        # ESP32 power pins feed the rails that are in use
        feeds = {"T+": ("top", "VIN", 0), "T-": ("top", "GND", 1), "B+": ("bot", "3V3", 0), "B-": ("bot", "GND", 1)}
        for rail in sorted(self.rails_used):
            half, net, idx = feeds[rail]
            col = ESP_COL0 + idx
            src = b.free_in_strip(half, col, ["j"] if half == "top" else ["a"])
            if src:
                dst = b.rail_hole(rail, col, set())
                self.wires.append({"a": src, "b": dst, "color": self._color(net), "label": f"ESP32 {net} → rail"})

    # ------------------------------------------------------------- support parts, laid out straight
    def _anchor(self, ref: str) -> dict | None:
        """Where a support end attaches: a rail, a column strip on the board, or a point off the board."""
        r = _norm(ref)
        if r in ("VIN", "5V", "3V3", "3.3V", "GND"):
            return {"t": "rail", "net": {"5V": "VIN", "3.3V": "3V3"}.get(r, r)}
        if r == "EXT":
            return {"t": "pt", "key": "EXT", "pt": self.ext_terminal, "net": "EXT"} if self.ext_terminal else None
        g = self._esp_gpio(ref)
        if g is not None:
            for hdr, half in ((ESP_TOP, "top"), (ESP_BOT, "bot")):
                for i, lab in enumerate(hdr):
                    if _gpio_of_label(lab) == g:
                        return {"t": "esp", "key": f"GPIO{g}", "col": ESP_COL0 + i, "half": half, "net": f"GPIO{g}"}
            return None
        if "." not in ref:
            return None
        pid, pin = ref.split(".", 1)
        if pid in self.off_pos:
            pt = self.off_pos[pid]["pins"].get(_norm(pin))
            return {"t": "pt", "key": f"{pid}.{_norm(pin)}", "pt": pt, "net": f"{pid}.{pin}"} if pt else None
        if pid in self.mod_cols and _norm(pin) in [_norm(x) for x in self.mod_pins[pid]]:
            return {"t": "col", "col": self._module_col(pid, pin), "half": self.half[pid], "net": f"{pid}.{pin}"}
        return None

    def _fresh_col(self, half: str, near: int, avoid: set[int] = frozenset(), side: int = 1) -> int | None:
        """A column right of the ESP32 that nothing uses yet (all five holes of that half free), searching
        outwards from `near`, preferring `side` (+1 right, -1 left)."""
        b = self.b
        rows = ROWS_BOT if half == "bot" else ROWS_TOP
        for d in range(0, b.cols + 1):
            for c in (near + side * d, near - side * d):
                if (FIRST_MODULE_COL <= c <= b.cols and c not in self.claimed and c not in avoid
                        and all((r, c) not in b.used for r in rows)):
                    self.claimed.add(c)
                    return c
        return None

    def _hole(self, half: str, col: int, prefer_far_from_rail: bool = False) -> tuple[float, float] | None:
        order = (ROWS_BOT if half == "bot" else list(reversed(ROWS_TOP)))
        if not prefer_far_from_rail:
            order = list(reversed(order))   # bottom half: a first (next to the rails); top half: j first
        rows = self.b.free_rows(half, col, order)
        return self.b.take(rows[0], col) if rows else None

    def _to_column(self, a: dict, half: str, near: int, net_color: str) -> dict | None:
        """Bring an ESP32 pin or an off-board pin into a free column with one jumper (reused per net)."""
        if a["key"] in self._hops:
            col = self._hops[a["key"]]
            return {"t": "col", "col": col, "half": self._hop_half[col], "net": a["net"]}
        col = self._fresh_col(half, near + 2 * self._side(near), avoid={near - 1, near, near + 1}, side=self._side(near))
        if col is None:
            return None
        if a["t"] == "esp":
            src = self.b.free_in_strip(a["half"], a["col"], ["j"] if a["half"] == "top" else ["a"]) \
                or self.b.free_in_strip(a["half"], a["col"], ["a", "j"])
        else:
            src = a["pt"]
        dst = self._hole(half, col)
        if src is None or dst is None:
            return None
        self.wires.append({"a": src, "b": dst, "color": net_color, "label": f"{a['net']} → column {col}"})
        self._hops[a["key"]] = col
        self._hop_half = getattr(self, "_hop_half", {})
        self._hop_half[col] = half
        return {"t": "col", "col": col, "half": half, "net": a["net"]}

    def _side(self, col: int) -> int:
        """Which way to put a helper column: away from a neighbouring pin of the same part (e.g. the other
        legs of a transistor), so a component next to it doesn't lie across them."""
        right_busy, left_busy = (col + 1) in self.claimed, (col - 1) in self.claimed
        if right_busy and not left_busy:
            return -1
        return 1

    def _net_color(self, a: dict) -> str:
        if a["t"] == "rail":
            return self._color(a["net"])
        if a["t"] == "esp":
            sig = next((c.get("signal_type", "") for c in self.connections
                        if parse_pin(c["esp32_pin"]) == int(a["key"][4:])), "")
            return self._color(a["key"], sig)
        return self._color(a["net"]) if a["net"] in NET_COLORS else "#64748b"

    def _place_support(self, sup: dict, ends: list[str]) -> bool:
        b = self.b
        A, B = self._anchor(ends[0]), self._anchor(ends[1])
        if A is None or B is None:
            return False
        kind = sup.get("kind", "other")
        label, name = sup.get("value", ""), sup.get("name", "")

        if A["t"] == "rail" and B["t"] == "rail":   # e.g. bulk capacitor: across two adjacent rails
            nets = [A["net"], B["net"]]
            half = "top" if "VIN" in nets else "bot"
            plus, minus = ("T+", "T-") if half == "top" else ("B+", "B-")
            col = b.cols - 1
            while col > 1 and (not b.rail_col_ok(col) or (plus, col) in b.used or (minus, col) in b.used):
                col -= 1
            pa = b.take(plus if nets[0] != "GND" else minus, col)
            pb = b.take(minus if nets[0] != "GND" else plus, col)
            self.rails_used.update({plus, minus})
            self.components.append({"kind": kind, "a": pa, "b": pb, "label": label, "name": name})
            for n in nets:
                self._color(n)
            return True

        if kind == "wire":   # plain jumper between the two ends
            near_col = next((x["col"] for x in (A, B) if x["t"] in ("col", "esp")), b.cols - 2)
            hint = next((x["half"] for x in (A, B) if x["t"] in ("col", "esp")), "bot")
            (pa, na), (pb, nb) = self._endpoint(ends[0], near_col, hint), self._endpoint(ends[1], near_col, hint)
            if pa is None or pb is None:
                return False
            net = next((n for n in (na, nb) if n in NET_COLORS), None) or name or na
            self.wires.append({"a": pa, "b": pb, "color": self._color(net), "label": name})
            return True

        # work in the half of whichever end is already a column on the board
        cols = [x for x in (A, B) if x["t"] == "col"]
        mid_y = (b.y["f"] + b.y["e"]) / 2
        pts = [x["pt"][1] for x in (A, B) if x["t"] == "pt" and x.get("pt")]
        half = (cols[0]["half"] if cols else
                ("top" if all(y < mid_y for y in pts) else "bot") if pts else
                ("top" if "VIN" in (A.get("net"), B.get("net")) else "bot"))
        near = cols[0]["col"] if cols else (b.cols - 4 if any(x["t"] == "pt" for x in (A, B)) else FIRST_MODULE_COL)
        A = self._to_column(A, half, near, self._net_color(A)) if A["t"] in ("esp", "pt") else A
        if A is not None and A["t"] == "col":
            near = A["col"]
        B = self._to_column(B, half, near, self._net_color(B)) if B and B["t"] in ("esp", "pt") else B
        if A is None or B is None:
            return False
        if A["t"] == "rail":
            A, B = B, A
            ends = list(reversed(ends))
            flipped = True
        else:
            flipped = False

        if B["t"] == "rail":   # column → rail
            net = B["net"]
            rail = {"VIN": "T+", "3V3": "B+"}.get(net) or ("T-" if A["half"] == "top" else "B-")
            rail_half = "top" if rail.startswith("T") else "bot"
            if rail_half != A["half"] or A["col"] in self.rail_avoid[rail_half]:
                # rail on the other side, or this column sits under a module's header pins:
                # lie flat along a row and jumper the rail into the far end
                sd = self._side(A["col"])
                hop = self._fresh_col(A["half"], A["col"] + 2 * sd, avoid={A["col"] - 1, A["col"], A["col"] + 1}, side=sd)
                if hop is None:
                    return False
                dst = self._hole(A["half"], hop)
                self.wires.append({"a": self._rail(net, hop, rail_half), "b": dst, "color": self._color(net),
                                   "label": f"{net} → column {hop}"})
                B = {"t": "col", "col": hop, "half": A["half"], "net": net}
            else:
                pa = self._hole(A["half"], A["col"], prefer_far_from_rail=True)
                if pa is None:
                    return False
                self.rails_used.add(rail)
                pb = b.rail_hole(rail, A["col"], self.rail_avoid[rail_half])
                self._color(net)
                a_pt, b_pt = (pb, pa) if flipped else (pa, pb)
                self.components.append({"kind": kind, "a": a_pt, "b": b_pt, "label": label, "name": name})
                return True

        # column ↔ column: lie flat along one row
        if A["half"] != B["half"] or abs(A["col"] - B["col"]) < 2:
            sd = self._side(A["col"])
            hop = self._fresh_col(A["half"], A["col"] + 2 * sd, avoid={A["col"] - 1, A["col"], A["col"] + 1}, side=sd)
            if hop is None:
                return False
            src, dst = self._hole(B["half"], B["col"]), self._hole(A["half"], hop)
            if src is None or dst is None:
                return False
            self.wires.append({"a": src, "b": dst, "color": "#64748b", "label": f"{B['net']} → column {hop}"})
            B = {"t": "col", "col": hop, "half": A["half"], "net": B["net"]}
        order = ["c", "b", "d", "a", "e"] if A["half"] == "bot" else ["h", "i", "g", "j", "f"]
        lo, hi = sorted((A["col"], B["col"]))
        spans = getattr(self, "_row_spans", {})
        self._row_spans = spans

        def clear(r):   # both holes free, and no other flat component in this row overlaps the span
            return ((r, A["col"]) not in b.used and (r, B["col"]) not in b.used
                    and all(h < lo or l > hi for l, h in spans.get(r, [])))
        row = next((r for r in order if clear(r)), None)
        if row is None:
            return False
        spans.setdefault(row, []).append((lo, hi))
        pa, pb = b.take(row, A["col"]), b.take(row, B["col"])
        a_pt, b_pt = (pb, pa) if flipped else (pa, pb)
        self.components.append({"kind": kind, "a": a_pt, "b": b_pt, "label": label, "name": name})
        return True

    # ------------------------------------------------------------- drawing
    def svg(self) -> str:
        b = self.b
        legend_y = max(b.bottom + (130 if any(h == "bot" for p, h in self.half.items() if p not in self.chips) else 30),
                       max((o["y"] + o["h"] + 20 for o in self.off_pos.values()), default=0))
        height = legend_y + 40
        out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.width} {height}" '
               f'width="{self.width}" height="{height}" font-family="Helvetica, Arial, sans-serif">',
               '<rect class="bb-bg" width="100%" height="100%" fill="#ffffff"/>']
        out.append("<style>.bbw:hover .bbw-core{stroke-width:6.5}.bbw:hover .bbw-edge{stroke-width:9.5}"
                   ".bbw{cursor:pointer}</style>")
        out += self._draw_board()
        out += self._draw_esp()
        out += self._draw_modules()
        out += self._draw_offboard()
        for comp in self.components:
            out += self._draw_component(comp)
        self._route_wires()
        for i, w in enumerate(self.wires):
            out += self._draw_wire(w, i)
        out += self._draw_legend(legend_y)
        out.append("</svg>")
        return "\n".join(out)

    def _draw_board(self) -> list[str]:
        b = self.b
        o = [f'<rect x="{b.x0}" y="{b.by}" width="{b.right - b.x0}" height="{b.bottom - b.by}" rx="10" '
             f'fill="#f4f4ef" stroke="#cfcfc6" stroke-width="2" class="bb-board"/>']
        gap_y = b.y["f"] + P / 2 + 1
        o.append(f'<rect x="{b.x0 + 4}" y="{gap_y}" width="{b.right - b.x0 - 8}" height="{b.y["e"] - b.y["f"] - P - 2}" fill="#e2e2da"/>')
        for rail, color, sign in (("T+", "#dc2626", "+"), ("T-", "#2563eb", "−"), ("B-", "#2563eb", "−"), ("B+", "#dc2626", "+")):
            y = b.y[rail] + (-P / 2 + 3 if rail in ("T+", "B-") else P / 2 - 3)
            o.append(f'<line x1="{b.x0 + 26}" y1="{y}" x2="{b.right - 26}" y2="{y}" stroke="{color}" stroke-width="1.6"/>')
            for xx in (b.x0 + 13, b.right - 13):
                o.append(f'<text x="{xx}" y="{b.y[rail] + 5}" font-size="15" font-weight="700" fill="{color}" text-anchor="middle">{sign}</text>')
        rail_names = {"T+": "5V", "T-": "GND", "B-": "GND", "B+": "3V3"}
        for rail, name in rail_names.items():
            o.append(f'<text x="{b.x0 - 4}" y="{b.y[rail] + 4}" font-size="9" fill="#6b7280" class="bb-ink-muted" text-anchor="end">{name}</text>')
        for col in range(1, b.cols + 1):
            x = b.x(col)
            for rail in ("T+", "T-", "B-", "B+"):
                if b.rail_col_ok(col):
                    o.append(f'<rect x="{x - 3.5}" y="{b.y[rail] - 3.5}" width="7" height="7" rx="1.5" fill="#55554f"/>')
            for r in ROWS_TOP + ROWS_BOT:
                o.append(f'<rect x="{x - 3.5}" y="{b.y[r] - 3.5}" width="7" height="7" rx="1.5" fill="#55554f"/>')
            if col == 1 or col % 5 == 0:
                o.append(f'<text x="{x}" y="{b.y["j"] - 11}" font-size="8" fill="#8a8a80" text-anchor="middle">{col}</text>')
                o.append(f'<text x="{x}" y="{b.y["a"] + 17}" font-size="8" fill="#8a8a80" text-anchor="middle">{col}</text>')
        for r in ROWS_TOP + ROWS_BOT:
            for xx in (b.x0 + 18, b.right - 18):
                o.append(f'<text x="{xx}" y="{b.y[r] + 3}" font-size="9" fill="#8a8a80" text-anchor="middle" font-style="italic">{r}</text>')
        return o

    def _draw_esp(self) -> list[str]:
        b = self.b
        x1, x2 = b.x(ESP_COL0) - P / 2 + 1, b.x(ESP_COL0 + len(ESP_TOP) - 1) + P / 2 - 1
        y1, y2 = b.y["i"] - 10, b.y["b"] + 10
        used_gpio = {parse_pin(c["esp32_pin"]) for c in self.connections}
        o = [f'<rect x="{x1 - 16}" y="{(y1 + y2) / 2 - 12}" width="22" height="24" rx="2" fill="#c0c4cc" stroke="#8b9099"/>',  # USB
             f'<text x="{x1 - 13}" y="{(y1 + y2) / 2 - 16}" font-size="8" font-weight="700" fill="#475569" text-anchor="middle">USB</text>',
             f'<rect x="{x1}" y="{y1}" width="{x2 - x1}" height="{y2 - y1}" rx="5" fill="#1e293b" stroke="#0f172a" stroke-width="1.5"/>',
             f'<rect x="{x2 - 92}" y="{(y1 + y2) / 2 - 30}" width="84" height="60" rx="3" fill="#cbd5e1" stroke="#94a3b8"/>',
             f'<text x="{x2 - 50}" y="{(y1 + y2) / 2 + 3}" font-size="9" fill="#334155" text-anchor="middle">ESP-WROOM-32</text>',
             f'<text x="{(x1 + x2 - 92) / 2}" y="{(y1 + y2) / 2 + 4}" font-size="12" font-weight="700" fill="#f8fafc" text-anchor="middle">ESP32 DevKit V1</text>']
        for header, row, ty in ((ESP_TOP, "i", b.y["i"] + 15), (ESP_BOT, "b", b.y["b"] - 9)):
            for i, lab in enumerate(header):
                x, y = b.pt(row, ESP_COL0 + i)
                g = _gpio_of_label(lab)
                hot = (g is not None and g in used_gpio) or (lab in ("VIN", "3V3", "GND"))
                o.append(f'<circle cx="{x}" cy="{y}" r="4" fill="#d4a017" stroke="#7c5c00" stroke-width="1"/>')
                o.append(f'<text x="{x}" y="{ty}" font-size="7" fill="{"#fde047" if hot else "#cbd5e1"}" '
                         f'font-weight="{"700" if hot else "400"}" text-anchor="middle">{escape(lab)}</text>')
        return o

    def _draw_modules(self) -> list[str]:
        b, o = self.b, []
        for pid in self.onboard:
            if pid in self.chips:
                o += self._draw_chip(pid)
                continue
            lo, hi = self.mod_span[pid]
            x1, x2 = b.x(lo) - P / 2 + 2, b.x(hi) + P / 2 - 2
            top = self.half[pid] == "top"
            h = 78
            y1 = b.by - 12 - h if top else b.bottom + 12
            row = "j" if top else "a"
            name = self.parts[pid].get("name", pid)
            for col, pin in zip(self.mod_cols[pid], self.mod_pins[pid]):
                x, y = b.pt(row, col)
                ly = y1 + h if top else y1
                o.append(f'<line x1="{x}" y1="{ly}" x2="{x}" y2="{y}" stroke="#9ca3af" stroke-width="2.5"/>')
                o.append(f'<circle cx="{x}" cy="{y}" r="4" fill="#d4a017" stroke="#7c5c00"/>')
            o.append(f'<rect x="{x1}" y="{y1}" width="{x2 - x1}" height="{h}" rx="5" fill="#0f766e" stroke="#115e59" stroke-width="1.5"/>')
            role = self.parts[pid].get("role", "")
            if role == "display":
                o.append(f'<rect x="{x1 + 6}" y="{y1 + (6 if top else 22)}" width="{x2 - x1 - 12}" height="{h - 30}" rx="2" fill="#0b1220"/>')
            lines = _wrap(name, max(8, int((x2 - x1) / 5.6)))[:4]
            ty = y1 + (h - 30) / 2 + (6 if top else 22) + 4 - 5.5 * (len(lines) - 1)
            for k, ln in enumerate(lines):
                o.append(f'<text x="{(x1 + x2) / 2}" y="{ty + 11 * k}" font-size="9.5" font-weight="600" '
                         f'fill="#ecfeff" text-anchor="middle">{escape(ln)}</text>')
            for col, pin in zip(self.mod_cols[pid], self.mod_pins[pid]):
                py = y1 + h - 7 if top else y1 + 13
                o.append(f'<text x="{b.x(col)}" y="{py}" font-size="7" fill="#fef9c3" text-anchor="middle">{escape(str(pin)[:5])}</text>')
        return o

    def _draw_chip(self, pid: str) -> list[str]:
        """A TO-220 transistor seen from above: legs in row d, body over row e, metal tab in the centre gap."""
        b, o = self.b, []
        cols = self.mod_cols[pid]
        x1, x2 = b.x(cols[0]) - P / 2 + 3, b.x(cols[-1]) + P / 2 - 3
        yd, ye = b.y["d"], b.y["e"]
        tab_y = b.y["f"] + P / 2 + 2
        o.append(f'<rect x="{x1 + 2}" y="{tab_y}" width="{x2 - x1 - 4}" height="{ye - tab_y - 6}" rx="2" '
                 f'fill="#c3c8d0" stroke="#8b9099"/>')
        o.append(f'<circle cx="{(x1 + x2) / 2}" cy="{tab_y + (ye - tab_y - 6) / 2}" r="3.2" fill="#8b9099"/>')
        body_y = ye - 8
        o.append(f'<rect x="{x1}" y="{body_y}" width="{x2 - x1}" height="{yd - body_y - 2}" rx="2" fill="#111827" stroke="#000"/>')
        for col, pin in zip(cols, self.mod_pins[pid]):
            x = b.x(col)
            o.append(f'<line x1="{x}" y1="{yd - 3}" x2="{x}" y2="{yd}" stroke="#9ca3af" stroke-width="3"/>')
            o.append(f'<circle cx="{x}" cy="{yd}" r="3.5" fill="#d4a017" stroke="#7c5c00"/>')
            o.append(f'<text x="{x}" y="{body_y + 9}" font-size="8" font-weight="700" fill="#f9fafb" '
                     f'text-anchor="middle">{escape(str(pin)[:3])}</text>')
        part = self.parts[pid]
        name = part.get("model") or part.get("name", pid)
        o.append(f'<text x="{x2 + 5}" y="{tab_y + 9}" font-size="9" font-weight="700" fill="#1f2937" stroke="#ffffff" '
                 f'stroke-width="3" paint-order="stroke">{escape(str(name)[:22])}</text>')
        return o

    def _draw_offboard(self) -> list[str]:
        o = []
        for pid, pos in self.off_pos.items():
            x, y, w, h = pos["x"], pos["y"], pos["w"], pos["h"]
            o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="#e0e7ff" stroke="#6366f1" stroke-width="1.5"/>')
            lines = _wrap(self.parts[pid].get("name", pid), 20)
            for k, ln in enumerate(lines[:2]):
                o.append(f'<text x="{x + w / 2 + 10}" y="{y + 18 + 12 * k}" font-size="10" font-weight="600" fill="#312e81" text-anchor="middle">{escape(ln)}</text>')
            for pin, (px, py) in pos["pins"].items():
                o.append(f'<circle cx="{px}" cy="{py}" r="5" fill="#c7d2fe" stroke="#4338ca" stroke-width="1.5"/>')
                o.append(f'<text x="{px + 10}" y="{py + 4}" font-size="10" fill="#312e81">{escape(pin)}</text>')
        if self.ext_terminal:
            x, y = self.ext_terminal
            o.append(f'<rect x="{x}" y="{y - 16}" width="120" height="32" rx="6" fill="#ffe4e6" stroke="#be123c"/>')
            o.append(f'<text x="{x + 64}" y="{y + 4}" font-size="10" fill="#881337" text-anchor="middle">External supply +</text>')
            o.append(f'<circle cx="{x}" cy="{y}" r="5" fill="#fecdd3" stroke="#be123c" stroke-width="1.5"/>')
        return o

    # ------------------------------------------------------------- tidy wire routing
    def _tracks(self) -> list[float]:
        """Horizontal lanes a wire can run along: in every gap between rows/rails, at least 6 px clear of the
        holes, 8 px apart (one lane between two rows, several in wider gaps)."""
        b = self.b
        ys = sorted(set(b.y.values()))
        bands = [(b.by + 2, ys[0])] + list(zip(ys, ys[1:])) + [(ys[-1], b.bottom - 2)]
        out = []
        for y1, y2 in bands:
            lo, hi = y1 + 6, y2 - 6
            if hi < lo:
                continue
            n = int((hi - lo) // 8) + 1
            step = (hi - lo) / (n - 1) if n > 1 else 0
            out += [lo + k * step if n > 1 else (lo + hi) / 2 for k in range(n)]
        return out

    def _route_wires(self) -> None:
        b = self.b
        # things a wire must not run across vertically: the ESP32 body and every transistor body
        obstacles = [(b.x(ESP_COL0) - P / 2 - 2, b.x(ESP_COL0 + len(ESP_TOP) - 1) + P / 2 + 2,
                      b.y["i"] - 12, b.y["b"] + 12)]
        for pid in self.chips:
            cols = self.mod_cols[pid]
            obstacles.append((b.x(cols[0]) - P / 2, b.x(cols[-1]) + P / 2, b.y["f"] + P / 2, b.y["d"] + 4))

        def hits_esp(x, ya, yb):
            return any(x1 <= x <= x2 and max(ya, yb) > y1 and min(ya, yb) < y2 for x1, x2, y1, y2 in obstacles)

        def clear_x(x_from: float, x_to: float) -> list[float]:
            """Vertical runs between columns that miss every obstacle, nearest to x_to first."""
            xs = [b.x(c) + P / 2 for c in range(1, b.cols)]
            ok = [x for x in xs if not any(x1 <= x <= x2 for x1, x2, _, _ in obstacles)]
            return sorted(ok, key=lambda x: abs(x - x_to) + 0.3 * abs(x - x_from))

        tracks = self._tracks()
        busy: dict[float, list[tuple[float, float]]] = {}
        right_channel = [b.right + 14]
        order = sorted(range(len(self.wires)), key=lambda k: abs(self.wires[k]["a"][0] - self.wires[k]["b"][0]))
        for k in order:
            w = self.wires[k]
            (xa, ya), (xb, yb) = w["a"], w["b"]
            if xa > xb:   # route left to right
                (xa, ya), (xb, yb) = (xb, yb), (xa, ya)
            off = xb > b.right + 2   # ends on a part beside the board
            if not off and (abs(xa - xb) < 3 or abs(ya - yb) < 3) and not (hits_esp(xa, ya, yb) and abs(xa - xb) < 3):
                w["path"] = [(xa, ya), (xb, yb)]
                continue
            xturn = xb
            if off:
                xturn = right_channel[0]
                right_channel[0] += 8
            best = None
            for t in tracks:
                if hits_esp(xa, ya, t) or (not off and hits_esp(xturn, t, yb)):
                    continue
                lo, hi = min(xa, xturn) - 5, max(xa, xturn) + 5
                free = all(h < lo or l > hi for l, h in busy.get(t, []))
                cost = abs(ya - t) + abs(yb - t) + (0 if free else 4000)
                if best is None or cost < best[0]:
                    best = (cost, t, lo, hi)
            if best is None and not off:
                # no single lane works (e.g. from the top header to a transistor pin below its body):
                # lane 1 out of the source, down a clear gap between columns, lane 2 into the destination
                for xv in clear_x(xa, xb)[:12]:
                    t1 = min((t for t in tracks if not hits_esp(xa, ya, t) and not hits_esp(xv, t, yb)),
                             key=lambda t: abs(ya - t), default=None)
                    t2 = min((t for t in tracks if not hits_esp(xb, t, yb) and not hits_esp(xv, t1, t)),
                             key=lambda t: abs(yb - t), default=None) if t1 is not None else None
                    if t1 is not None and t2 is not None:
                        pts = [(xa, ya), (xa, t1), (xv, t1), (xv, t2), (xb, t2), (xb, yb)]
                        busy.setdefault(t1, []).append((min(xa, xv) - 5, max(xa, xv) + 5))
                        busy.setdefault(t2, []).append((min(xv, xb) - 5, max(xv, xb) + 5))
                        w["path"] = [pts[0]] + [p for i, p in enumerate(pts[1:], 1) if p != pts[i - 1]]
                        break
                else:
                    w["path"] = [(xa, ya), (xb, yb)]
                continue
            if best is None:
                w["path"] = [(xa, ya), (xb, yb)]
                continue
            _, t, lo, hi = best
            busy.setdefault(t, []).append((lo, hi))
            pts = [(xa, ya), (xa, t), (xturn, t), (xturn, yb)]
            if off:
                pts.append((xb, yb))
            w["path"] = [pts[0]] + [p for i, p in enumerate(pts[1:], 1) if p != pts[i - 1]]

    @staticmethod
    def _rounded(pts: list[tuple[float, float]], r: float = 6.0) -> str:
        if len(pts) == 2:
            return f"M{pts[0][0]},{pts[0][1]} L{pts[1][0]},{pts[1][1]}"
        d = f"M{pts[0][0]},{pts[0][1]}"
        for i in range(1, len(pts) - 1):
            (x0, y0), (x1, y1), (x2, y2) = pts[i - 1], pts[i], pts[i + 1]
            l1, l2 = math.hypot(x1 - x0, y1 - y0) or 1, math.hypot(x2 - x1, y2 - y1) or 1
            rr = min(r, l1 / 2, l2 / 2)
            ax, ay = x1 - (x1 - x0) / l1 * rr, y1 - (y1 - y0) / l1 * rr
            bx, by = x1 + (x2 - x1) / l2 * rr, y1 + (y2 - y1) / l2 * rr
            d += f" L{ax:.1f},{ay:.1f} Q{x1},{y1} {bx:.1f},{by:.1f}"
        return d + f" L{pts[-1][0]},{pts[-1][1]}"

    def _draw_wire(self, w: dict, i: int) -> list[str]:
        (x1, y1), (x2, y2) = w["a"], w["b"]
        path = self._rounded(w.get("path") or [(x1, y1), (x2, y2)])
        title = f"<title>{escape(w.get('label', ''))}</title>"
        return [f'<g class="bbw">{title}<path d="{path}" fill="none" stroke="rgba(0,0,0,0.28)" class="bb-wire-edge bbw-edge" '
                f'stroke-width="6.5" stroke-linecap="round" stroke-linejoin="round"/>',
                f'<path d="{path}" fill="none" stroke="{w["color"]}" class="bbw-core" stroke-width="4.2" '
                f'stroke-linecap="round" stroke-linejoin="round"/>',
                f'<rect x="{x1 - 3}" y="{y1 - 3}" width="6" height="6" fill="#111"/>',
                f'<rect x="{x2 - 3}" y="{y2 - 3}" width="6" height="6" fill="#111"/></g>']

    def _draw_component(self, c: dict) -> list[str]:
        (x1, y1), (x2, y2) = c["a"], c["b"]
        d = math.hypot(x2 - x1, y2 - y1) or 1
        ux, uy = (x2 - x1) / d, (y2 - y1) / d
        L = 32.0 if c["kind"] != "capacitor" else 26.0
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        if d < L + 4:  # too short to sit between the points: lift it beside them
            mx, my = mx - uy * 30 if uy else mx - 30, my + ux * 30
        e1 = (mx - ux * L / 2, my - uy * L / 2)
        e2 = (mx + ux * L / 2, my + uy * L / 2)
        ang = math.degrees(math.atan2(uy, ux))
        o = [f'<g><title>{escape(c.get("name", ""))} {escape(c.get("label", ""))}</title>',
             f'<line x1="{x1}" y1="{y1}" x2="{e1[0]}" y2="{e1[1]}" stroke="#9ca3af" stroke-width="2"/>',
             f'<line x1="{x2}" y1="{y2}" x2="{e2[0]}" y2="{e2[1]}" stroke="#9ca3af" stroke-width="2"/>',
             f'<g transform="translate({mx},{my}) rotate({ang})">']
        k = c["kind"]
        if k == "resistor":
            o += [f'<rect x="{-L / 2}" y="-5" width="{L}" height="10" rx="4" fill="#e7c89a" stroke="#a8875a"/>',
                  '<rect x="-10" y="-5" width="3" height="10" fill="#7c2d12"/><rect x="-4" y="-5" width="3" height="10" fill="#111"/>',
                  '<rect x="2" y="-5" width="3" height="10" fill="#ea580c"/><rect x="10" y="-5" width="2" height="10" fill="#ca8a04"/>']
        elif k == "diode":  # ends[0] = cathode → stripe at the start of the body
            o += [f'<rect x="{-L / 2}" y="-5" width="{L}" height="10" rx="2" fill="#1f2937"/>',
                  f'<rect x="{-L / 2 + 3}" y="-5" width="4" height="10" fill="#d1d5db"/>']
        elif k == "capacitor":  # electrolytic, ends[0] = positive; stripe on the negative end
            o += [f'<rect x="{-L / 2}" y="-8" width="{L}" height="16" rx="5" fill="#1d4ed8" stroke="#1e3a8a"/>',
                  f'<rect x="{L / 2 - 8}" y="-8" width="5" height="16" fill="#bfdbfe"/>']
        elif k == "led":
            o += ['<circle r="8" fill="#ef4444" stroke="#991b1b"/>']
        else:
            o += [f'<rect x="{-L / 2}" y="-6" width="{L}" height="12" rx="2" fill="#9ca3af"/>']
        o.append("</g>")
        o.append(f'<text x="{mx + uy * 15}" y="{my - ux * 15 + 3}" font-size="9" font-weight="600" fill="#1f2937" '
                 f'stroke="#ffffff" stroke-width="3" paint-order="stroke" text-anchor="middle">{escape(c.get("label", ""))}</text></g>')
        return o

    def _draw_legend(self, y: float) -> list[str]:
        o, x = [], self.b.x0
        o.append(f'<text x="{x}" y="{y}" font-size="11" font-weight="700" fill="#374151" class="bb-ink">Wires:</text>')
        x += 52
        for net, color in self.nets_used.items():
            label = NET_LABELS.get(net) or re.sub(r"^w[_ ]+", "", str(net), flags=re.I).replace("_", " ")
            o.append(f'<line x1="{x}" y1="{y - 4}" x2="{x + 22}" y2="{y - 4}" stroke="rgba(0,0,0,0.28)" class="bb-wire-edge" stroke-width="7" stroke-linecap="round"/>')
            o.append(f'<line x1="{x}" y1="{y - 4}" x2="{x + 22}" y2="{y - 4}" stroke="{color}" stroke-width="4.5" stroke-linecap="round"/>')
            o.append(f'<text x="{x + 28}" y="{y}" font-size="11" fill="#374151" class="bb-ink">{escape(label)}</text>')
            x += 40 + 6.5 * len(label)
            if x > self.width - 120:
                x, y = self.b.x0 + 52, y + 20
        return o


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def render(connections: list[dict], parts: dict[str, dict], support: list[dict]) -> tuple[str, list[str]]:
    """Returns (svg, notes about anything that couldn't be placed)."""
    r = Renderer(connections, parts, support)
    return r.svg(), r.notes
