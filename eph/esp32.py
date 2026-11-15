"""Deterministic ESP32 (WROOM-32 DevKit V1) pin rules.

The LLM proposes the pin map; this module checks it against hard hardware facts, and errors are
fed back to the LLM to fix. Keeping this deterministic means a hallucinated pin can't slip through.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

BOARD = "ESP32 DevKit V1 (ESP32-WROOM-32, 30-pin)"

VALID_GPIO = {0, 1, 2, 3, 4, 5, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 23, 25, 26, 27, 32, 33, 34, 35, 36, 39}
FLASH_PINS = {6, 7, 8, 9, 10, 11}
INPUT_ONLY = {34, 35, 36, 39}
STRAPPING = {0, 2, 5, 12, 15}
UART0 = {1, 3}
ADC1 = {32, 33, 34, 35, 36, 39}
ADC2 = {0, 2, 4, 12, 13, 14, 15, 25, 26, 27}
DAC = {25, 26}
TOUCH = {0, 2, 4, 12, 13, 14, 15, 27, 32, 33}

POWER_ALIASES = {
    "3V3": "3V3", "3.3V": "3V3", "3V": "3V3", "VCC": "3V3",
    "GND": "GND", "G": "GND",
    "VIN": "VIN", "5V": "VIN", "VUSB": "VIN", "EXT": "EXT",
    "EN": "EN",
}

OUTPUT_DIRECTIONS = {"output", "pwm", "bidirectional"}
BUS_SIGNALS = {"i2c_sda", "i2c_scl", "spi_mosi", "spi_miso", "spi_sck", "onewire"}

PIN_GUIDE = f"""Board: {BOARD}.
Usable GPIO: {sorted(VALID_GPIO)}. NEVER use GPIO6-11 (SPI flash).
Input-only (no output, no internal pull-ups): 34, 35, 36(VP), 39(VN).
ADC1 (works with Wi-Fi): 32, 33, 34, 35, 36, 39. ADC2 (UNUSABLE while Wi-Fi is on): 0, 2, 4, 12-15, 25-27.
Strapping pins (avoid, or only use with care): 0, 2, 5, 12, 15. GPIO1/3 are UART0 (USB serial): avoid.
Defaults: I2C SDA=21, SCL=22 (several I2C devices share these). VSPI MOSI=23 MISO=19 SCK=18 CS=5.
DAC: 25, 26. Logic is 3.3 V; GPIO are NOT 5 V tolerant.
Power pins: "3V3", "GND", "VIN" (5 V from USB). An external supply is "EXT" (motors/pumps should use EXT or VIN via a transistor, never a GPIO).
GPIO max ~12 mA: drive motors, pumps, relays and LED strips through a MOSFET/transistor/driver."""


@dataclass
class Issue:
    severity: str  # error | warning
    index: int     # connection index
    pin: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def parse_pin(raw: str) -> int | str | None:
    s = str(raw).strip().upper().replace(" ", "")
    if s in POWER_ALIASES:
        return POWER_ALIASES[s]
    m = re.fullmatch(r"(?:GPIO|IO|D|P)?(\d{1,2})", s)
    if m:
        return int(m.group(1))
    if s in {"VP", "SVP"}:
        return 36
    if s in {"VN", "SVN"}:
        return 39
    return None


def validate(connections: list[dict], wifi: bool, five_volt_parts: set[str] | None = None) -> list[Issue]:
    """connections: [{part_id, part_pin, esp32_pin, direction, signal_type}]"""
    five_volt_parts = five_volt_parts or set()
    issues: list[Issue] = []
    by_gpio: dict[int, list[int]] = {}

    for i, c in enumerate(connections):
        raw = c.get("esp32_pin", "")
        p = parse_pin(raw)
        direction = (c.get("direction") or "").lower()
        sig = (c.get("signal_type") or "").lower()
        if p is None:
            issues.append(Issue("error", i, raw, f"Unknown ESP32 pin '{raw}'"))
            continue
        if isinstance(p, str):
            continue  # power/ground rails
        if p in FLASH_PINS:
            issues.append(Issue("error", i, raw, f"GPIO{p} is wired to the SPI flash and cannot be used"))
            continue
        if p not in VALID_GPIO:
            issues.append(Issue("error", i, raw, f"GPIO{p} is not broken out on {BOARD}"))
            continue
        by_gpio.setdefault(p, []).append(i)
        if p in INPUT_ONLY and direction in OUTPUT_DIRECTIONS:
            issues.append(Issue("error", i, raw, f"GPIO{p} is input-only but used as {direction}"))
        if p in INPUT_ONLY and sig in {"i2c_sda", "i2c_scl", "onewire"}:
            issues.append(Issue("error", i, raw, f"GPIO{p} is input-only; {sig} needs a bidirectional pin"))
        if direction == "analog_in":
            if p not in ADC1 | ADC2:
                issues.append(Issue("error", i, raw, f"GPIO{p} has no ADC"))
            elif p in ADC2 and wifi:
                issues.append(Issue("error", i, raw, f"GPIO{p} is ADC2, which cannot be read while Wi-Fi is on; use ADC1 (32-36, 39)"))
        if direction == "analog_out" and p not in DAC:
            issues.append(Issue("error", i, raw, f"GPIO{p} has no DAC; DAC is only on 25/26 (or use PWM)"))
        if p in STRAPPING:
            issues.append(Issue("warning", i, raw, f"GPIO{p} is a strapping pin; it can break boot if pulled the wrong way"))
        if p in UART0:
            issues.append(Issue("warning", i, raw, f"GPIO{p} is UART0 (USB serial); it will clash with Serial/uploads"))
        if c.get("part_id") in five_volt_parts and direction in {"input", "analog_in", "bidirectional"}:
            issues.append(Issue("warning", i, raw, f"{c.get('part_id')} runs at 5 V logic; add a level shifter / divider before GPIO{p}"))

    for gpio, idxs in by_gpio.items():
        if len(idxs) < 2:
            continue
        sigs = {(connections[j].get("signal_type") or "").lower() for j in idxs}
        if len(sigs) == 1 and sigs <= BUS_SIGNALS:
            continue  # shared bus line, e.g. two I2C devices on SDA=21
        parts = ", ".join(f"{connections[j].get('part_id')}.{connections[j].get('part_pin')}" for j in idxs)
        issues.append(Issue("error", idxs[-1], f"GPIO{gpio}", f"GPIO{gpio} is used by several non-bus signals: {parts}"))
    return issues


def wiring_mermaid(connections: list[dict], parts: dict[str, str], support: list[dict]) -> str:
    """Deterministic Mermaid wiring diagram from the validated pin map."""
    def nid(s: str) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "_", s)

    lines = ["flowchart LR", '  ESP32["ESP32 DevKit V1"]']
    for pid, label in parts.items():
        if pid == "mcu":
            continue
        lines.append(f'  {nid(pid)}["{label.replace(chr(34), "")}"]')
    power: dict[str, list[str]] = {}
    for c in connections:
        pid = c.get("part_id", "")
        if pid == "mcu" or pid not in parts:
            continue
        p = parse_pin(c.get("esp32_pin", ""))
        if p is None:
            continue
        if isinstance(p, str):  # group rails into one dotted edge per part to keep the diagram readable
            power.setdefault(pid, []).append(f"{p}→{c.get('part_pin', '')}")
            continue
        label = f"GPIO{p} → {c.get('part_pin', '')}".replace('"', "")
        lines.append(f'  ESP32 -->|"{label}"| {nid(pid)}')
    for pid, rails in power.items():
        lines.append(f'  ESP32 -.-|"{", ".join(rails)}"| {nid(pid)}'.replace('"', '"'))
    for i, s in enumerate(support):
        lines.append(f'  support{i}(["{(s.get("name", "") + " " + s.get("value", "")).strip().replace(chr(34), "")}"])')
        for pid in s.get("connects", []):
            if pid in parts:
                lines.append(f"  support{i} --- {nid(pid)}")
    return "\n".join(lines)
