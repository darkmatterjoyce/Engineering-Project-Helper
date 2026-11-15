"""One-step firmware compiler setup for Windows.

    python scripts\\setup_arduino_windows.py

1. Finds arduino-cli, or installs it with winget.
2. Installs the ESP32 board package and the libraries the agent usually picks.
3. Writes ARDUINO_CLI=<full path> into .env, so the app finds it even before Windows updates PATH.

Safe to run again: everything already installed is skipped by arduino-cli.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ESP32_INDEX = "https://espressif.github.io/arduino-esp32/package_esp32_index.json"
LIBS = ["Adafruit SSD1306", "Adafruit GFX Library", "Adafruit BusIO", "DHT sensor library",
        "Adafruit Unified Sensor", "Adafruit NeoPixel", "PubSubClient", "ArduinoJson", "LiquidCrystal I2C"]


def find_cli() -> str | None:
    found = shutil.which("arduino-cli")
    if found:
        return found
    local, pf = os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates = [
        os.path.join(local, "Microsoft", "WinGet", "Links", "arduino-cli.exe"),
        os.path.join(pf, "Arduino CLI", "arduino-cli.exe"),
        os.path.join(pf, "arduino-cli", "arduino-cli.exe"),
        *glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "ArduinoSA.CLI*", "**", "arduino-cli.exe"),
                   recursive=True),
        *glob.glob(os.path.join(local, "Programs", "**", "arduino-cli.exe"), recursive=True),
    ]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def run(cli: str, *args: str, check: bool = True) -> int:
    print(f"\n> arduino-cli {' '.join(args)}", flush=True)
    rc = subprocess.call([cli, *args])
    if check and rc != 0:
        print(f"  (exit code {rc})")
    return rc


def set_env_value(key: str, value: str) -> None:
    env = REPO / ".env"
    lines = env.read_text(encoding="utf-8-sig").splitlines() if env.exists() else []
    out, done = [], False
    for line in lines:
        if line.strip().startswith(f"{key}=") and not done:
            out.append(f"{key}={value}")
            done = True
        elif line.strip().startswith(f"{key}="):
            continue                      # drop duplicates so there is one clear value
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    env.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    cli = find_cli()
    if not cli:
        if not shutil.which("winget"):
            print("winget isn't available. Download arduino-cli for Windows (64-bit .zip) from\n"
                  "https://arduino.github.io/arduino-cli/latest/installation/ , unzip arduino-cli.exe\n"
                  "somewhere, then run this script again with its folder on PATH, or set\n"
                  "ARDUINO_CLI=C:\\full\\path\\arduino-cli.exe in .env.")
            return 1
        print("Installing arduino-cli with winget (accept any Windows prompt)...", flush=True)
        subprocess.call(["winget", "install", "-e", "--id", "ArduinoSA.CLI",
                         "--accept-source-agreements", "--accept-package-agreements"])
        cli = find_cli()
        if not cli:
            print("\narduino-cli was installed but this window can't see it yet.\n"
                  "Close this Command Prompt, open a new one in the project folder, activate the venv\n"
                  "and run this script again.")
            return 1
    print(f"Using arduino-cli at: {cli}")

    run(cli, "config", "init", "--overwrite", check=False)
    run(cli, "config", "add", "board_manager.additional_urls", ESP32_INDEX, check=False)
    run(cli, "core", "update-index")
    print("\nInstalling the ESP32 board package. This is a large download (several hundred MB) and can take"
          " a few minutes...", flush=True)
    if run(cli, "core", "install", "esp32:esp32") != 0:
        print("ESP32 core install failed. Check your internet connection and run this script again.")
        return 1
    run(cli, "lib", "update-index", check=False)
    run(cli, "lib", "install", *LIBS, check=False)

    set_env_value("ARDUINO_CLI", cli)
    print(f"\nWrote ARDUINO_CLI={cli} to .env")
    print("\nDone. Restart the server (Ctrl+C, then uvicorn ...) and new runs will compile the firmware.\n"
          "Tip: the first ESP32 compile takes 1-3 minutes; later ones are much faster.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
