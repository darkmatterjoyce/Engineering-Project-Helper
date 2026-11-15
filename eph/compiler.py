"""Compile firmware with arduino-cli (ESP32 core). Falls back to a static check if arduino-cli or the
ESP32 core isn't installed, so the pipeline never hard-fails on a laptop without the toolchain."""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings

_installed_libs: set[str] = set()


@dataclass
class CompileResult:
    status: str                     # ok | failed | skipped
    log: str = ""
    warnings: list[str] = field(default_factory=list)
    size: str | None = None


def static_check(sketch: str, gpios: list[int]) -> list[str]:
    problems = []
    if not re.search(r"\bvoid\s+setup\s*\(", sketch):
        problems.append("sketch has no setup()")
    if not re.search(r"\bvoid\s+loop\s*\(", sketch):
        problems.append("sketch has no loop()")
    for g in sorted(set(gpios)):
        if not re.search(rf"(?<![0-9]){g}(?![0-9])", sketch):
            problems.append(f"GPIO{g} from the pin map never appears in the sketch")
    if sketch.count("{") != sketch.count("}"):
        problems.append("unbalanced braces")
    return problems


_CLI_CACHE: dict[str, str | None] = {}


def find_cli(configured: str = "arduino-cli") -> str | None:
    """arduino-cli from .env / PATH, or from the places Windows installs it (the copy bundled inside the
    Arduino IDE, winget, Program Files), so no PATH or .env editing is needed."""
    if configured in _CLI_CACHE:
        return _CLI_CACHE[configured]
    cand = [str(configured).strip().strip('"').strip("'")]
    found = shutil.which(cand[0]) or (cand[0] if os.path.isfile(cand[0]) else None)
    if not found:
        found = shutil.which("arduino-cli")
    if not found and os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        pf = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)")]
        paths = [os.path.join(local, "Programs", "Arduino IDE", "resources", "app", "lib", "backend", "resources",
                              "arduino-cli.exe"),
                 os.path.join(local, "Microsoft", "WinGet", "Links", "arduino-cli.exe"),
                 *[os.path.join(x, "Arduino CLI", "arduino-cli.exe") for x in pf if x],
                 *[os.path.join(x, "Arduino IDE", "resources", "app", "lib", "backend", "resources", "arduino-cli.exe")
                   for x in pf if x],
                 *glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "ArduinoSA.CLI*", "**",
                                         "arduino-cli.exe"), recursive=True)]
        found = next((x for x in paths if x and os.path.isfile(x)), None)
    _CLI_CACHE[configured] = found
    return found


def toolchain_status(settings: Settings) -> str:
    cli = find_cli(settings.arduino_cli)
    if not cli:
        return "arduino-cli not found"
    try:
        out = subprocess.run([cli, "core", "list"], capture_output=True, text=True, timeout=30).stdout
    except Exception as e:  # noqa: BLE001
        return f"arduino-cli error: {e}"
    return "ready" if "esp32:esp32" in out else \
        "esp32 core not installed (Windows: python scripts\\setup_arduino_windows.py; Mac/Linux: scripts/setup_arduino.sh)"


def compile_sketch(sketch: str, filename: str, libraries: list[str], settings: Settings) -> CompileResult:
    cli = find_cli(settings.arduino_cli)
    if not cli or toolchain_status(settings) != "ready":
        return CompileResult(status="skipped", log=f"Compile skipped: {toolchain_status(settings)}")

    log_parts = []
    for lib in libraries:
        if lib in _installed_libs:
            continue
        r = subprocess.run([cli, "lib", "install", lib], capture_output=True, text=True, timeout=180)
        log_parts.append(f"$ arduino-cli lib install \"{lib}\"\n{(r.stdout + r.stderr).strip()}")
        if r.returncode == 0:
            _installed_libs.add(lib)

    name = Path(filename).stem or "sketch"
    with tempfile.TemporaryDirectory() as tmp:
        sk_dir = Path(tmp) / name
        sk_dir.mkdir()
        (sk_dir / f"{name}.ino").write_text(sketch)
        r = subprocess.run(
            [cli, "compile", "--fqbn", settings.fqbn, "--warnings", "default", *settings.arduino_extra_args, str(sk_dir)],
            capture_output=True, text=True, timeout=600,
        )
    out = (r.stdout + "\n" + r.stderr).strip()
    log_parts.append(f"$ arduino-cli compile --fqbn {settings.fqbn}\n{out}")
    size = None
    m = re.search(r"Sketch uses (\d+) bytes \((\d+)%\)", out)
    if m:
        size = f"{m.group(1)} bytes ({m.group(2)}% of flash)"
    return CompileResult(status="ok" if r.returncode == 0 else "failed", log="\n\n".join(log_parts), size=size)


def trim_errors(log: str, limit: int = 4000) -> str:
    """Keep the lines the LLM needs to fix the build."""
    keep = [ln for ln in log.splitlines() if re.search(r"error|Error|undefined|not declared|no matching|expected", ln)]
    text = "\n".join(keep) or log
    return text[-limit:]
