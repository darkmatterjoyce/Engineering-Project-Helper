#!/usr/bin/env bash
# Installs arduino-cli + the ESP32 core + common libraries so the agent can compile firmware.
# macOS: uses Homebrew. Linux: official install script into ~/.local/bin.
set -euo pipefail

if ! command -v arduino-cli >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    brew install arduino-cli
  else
    mkdir -p "$HOME/.local/bin"
    curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR="$HOME/.local/bin" sh
    export PATH="$HOME/.local/bin:$PATH"
    echo "Add ~/.local/bin to your PATH"
  fi
fi

arduino-cli config init --overwrite >/dev/null
arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32
arduino-cli lib update-index
# Pre-install libraries the agent commonly picks (it also installs any others on demand)
arduino-cli lib install "Adafruit SSD1306" "Adafruit GFX Library" "Adafruit BusIO" \
  "DHT sensor library" "Adafruit Unified Sensor" "Adafruit NeoPixel" "PubSubClient" "ArduinoJson" || true
arduino-cli core list
echo "ESP32 toolchain ready."
