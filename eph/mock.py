"""Canned LLM + search responses (EPH_MOCK=1). Lets the whole pipeline, UI and tests run with no API
keys, and is a safety net if the venue Wi-Fi dies mid-demo. The first wiring answer deliberately
puts an analog sensor on an ADC2 pin so you can watch the validator → LLM fix loop work."""
from __future__ import annotations

import json
import re

from .schema import Evidence
from .search import SearchResult

SKETCH = r'''// Smart plant waterer: ESP32 DevKit V1
#include <WiFi.h>
#include <WebServer.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

const int PIN_SOIL = 34;      // capacitive soil sensor AOUT (ADC1, Wi-Fi safe)
const int PIN_PUMP_GATE = 26; // IRLZ44N gate -> pump
const int PIN_SDA = 21;
const int PIN_SCL = 22;

const char* WIFI_SSID = "YOUR_SSID";
const char* WIFI_PASS = "YOUR_PASSWORD";

const int DRY_RAW = 3200;     // calibrate: reading in dry air
const int WET_RAW = 1400;     // calibrate: reading in water
const int WATER_BELOW_PCT = 35;
const unsigned long PUMP_MS = 3000;
const unsigned long SAMPLE_MS = 2000;
const unsigned long COOLDOWN_MS = 60000;

Adafruit_SSD1306 display(128, 64, &Wire, -1);
WebServer server(80);

int moisturePct = 0;
bool pumpOn = false;
unsigned long pumpStarted = 0, lastSample = 0, lastWatered = 0;

// Read the soil sensor and convert to 0-100 %
int readMoisture() {
  long sum = 0;
  for (int i = 0; i < 8; i++) sum += analogRead(PIN_SOIL);
  int raw = sum / 8;
  return constrain(map(raw, DRY_RAW, WET_RAW, 0, 100), 0, 100);
}

// Switch the pump through the MOSFET
void setPump(bool on) {
  pumpOn = on;
  digitalWrite(PIN_PUMP_GATE, on ? HIGH : LOW);
  if (on) pumpStarted = millis();
  Serial.printf("pump %s\n", on ? "ON" : "OFF");
}

// Draw status on the OLED
void drawStatus() {
  display.clearDisplay();
  display.setTextSize(2);
  display.setCursor(0, 0);
  display.printf("Soil %d%%", moisturePct);
  display.setTextSize(1);
  display.setCursor(0, 32);
  display.println(pumpOn ? "Pump: WATERING" : "Pump: idle");
  display.println(WiFi.isConnected() ? WiFi.localIP().toString() : "WiFi: offline");
  display.display();
}

// Serve a tiny status page
void handleRoot() {
  String html = "<h1>Plant waterer</h1><p>Moisture: " + String(moisturePct) + "%</p><p>Pump: " +
                String(pumpOn ? "on" : "off") + "</p>";
  server.send(200, "text/html", html);
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_PUMP_GATE, OUTPUT);
  setPump(false);
  analogReadResolution(12);
  Wire.begin(PIN_SDA, PIN_SCL);
  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) Serial.println("OLED not found");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  server.on("/", handleRoot);
  server.begin();
}

void loop() {
  unsigned long now = millis();
  server.handleClient();
  if (now - lastSample >= SAMPLE_MS) {
    lastSample = now;
    moisturePct = readMoisture();
    Serial.printf("moisture=%d%%\n", moisturePct);
    if (!pumpOn && moisturePct < WATER_BELOW_PCT && now - lastWatered > COOLDOWN_MS) setPump(true);
    drawStatus();
  }
  if (pumpOn && now - pumpStarted >= PUMP_MS) {
    setPump(false);
    lastWatered = now;
  }
}
'''

LCD_SKETCH = (SKETCH
    .replace("#include <Adafruit_GFX.h>\n#include <Adafruit_SSD1306.h>", "#include <LiquidCrystal_I2C.h>")
    .replace("Adafruit_SSD1306 display(128, 64, &Wire, -1);", "LiquidCrystal_I2C lcd(0x27, 16, 2);")
    .replace("""// Draw status on the OLED
void drawStatus() {
  display.clearDisplay();
  display.setTextSize(2);
  display.setCursor(0, 0);
  display.printf("Soil %d%%", moisturePct);
  display.setTextSize(1);
  display.setCursor(0, 32);
  display.println(pumpOn ? "Pump: WATERING" : "Pump: idle");
  display.println(WiFi.isConnected() ? WiFi.localIP().toString() : "WiFi: offline");
  display.display();
}""", """// Show status on the 16x2 LCD
void drawStatus() {
  lcd.setCursor(0, 0);
  lcd.printf("Soil %3d%% %s   ", moisturePct, pumpOn ? "PUMP" : "    ");
  lcd.setCursor(0, 1);
  String ip = WiFi.isConnected() ? WiFi.localIP().toString() : String("WiFi offline");
  lcd.print((ip + "                ").substring(0, 16));
}""")
    .replace("""  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) Serial.println("OLED not found");""",
             """  lcd.init();
  lcd.backlight();"""))

NO_DISPLAY_SKETCH = (SKETCH
    .replace("#include <Wire.h>\n#include <Adafruit_GFX.h>\n#include <Adafruit_SSD1306.h>\n", "")
    .replace("const int PIN_SDA = 21;\nconst int PIN_SCL = 22;\n", "")
    .replace("Adafruit_SSD1306 display(128, 64, &Wire, -1);\n", "")
    .replace("""// Draw status on the OLED
void drawStatus() {
  display.clearDisplay();
  display.setTextSize(2);
  display.setCursor(0, 0);
  display.printf("Soil %d%%", moisturePct);
  display.setTextSize(1);
  display.setCursor(0, 32);
  display.println(pumpOn ? "Pump: WATERING" : "Pump: idle");
  display.println(WiFi.isConnected() ? WiFi.localIP().toString() : "WiFi: offline");
  display.display();
}""", """// Log status to Serial (the web page is the display)
void drawStatus() {
  Serial.printf("soil %d%%, pump %s\\n", moisturePct, pumpOn ? "on" : "off");
}""")
    .replace("""  Wire.begin(PIN_SDA, PIN_SCL);
  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) Serial.println("OLED not found");
""", ""))

LCD_PART = {"id": "lcd", "name": "16x2 I2C LCD", "model": "1602 LCD + PCF8574 I2C backpack", "qty": 1,
            "role": "display", "interface": "i2c", "satisfies": ["show_status"], "library": "LiquidCrystal I2C",
            "search_query": "1602 LCD PCF8574 I2C backpack ESP32 3.3V address 0x27 pinout", "est_price_gbp": 4,
            "rationale": "Requested by the user in place of the OLED; readable, cheap, same I2C bus.",
            "alternatives": [{"option": "SSD1306 OLED", "reason_rejected": "user asked for an LCD"}],
            "confidence": 0.85}

MERMAID = '''flowchart TD
  S["setup()"] --> A["Serial 115200"]
  A --> B["Pump gate GPIO26 = OUTPUT, LOW"]
  B --> C["I2C on GPIO21/22, init SSD1306"]
  C --> D["Connect Wi-Fi, start web server"]
  D --> L{"loop()"}
  L --> W["server.handleClient()"]
  W --> T{"2 s since last sample?"}
  T -- yes --> R["Read soil sensor GPIO34 (avg of 8)"]
  R --> M{"moisture < 35% and cooldown over?"}
  M -- yes --> P["Pump ON via MOSFET"]
  M -- no --> O["Update OLED"]
  P --> O
  T -- no --> Q{"Pump on for 3 s?"}
  O --> Q
  Q -- yes --> F["Pump OFF, start 60 s cooldown"]
  Q -- no --> L
  F --> L'''

PARTS = [
    {"id": "mcu", "name": "ESP32 DevKit V1", "model": "ESP32-WROOM-32 DevKit V1 (30-pin)", "qty": 1, "role": "mcu",
     "interface": "none", "satisfies": ["remote_monitoring"], "library": None,
     "search_query": "ESP32 DevKit V1 30 pin pinout ADC1 ADC2", "est_price_gbp": 6,
     "rationale": "Fixed MCU; built-in Wi-Fi covers remote monitoring.", "alternatives": [], "confidence": 0.95},
    {"id": "soil_sensor", "name": "Capacitive soil moisture sensor", "model": "Capacitive Soil Moisture Sensor v1.2",
     "qty": 1, "role": "sensor", "interface": "analog", "satisfies": ["measure_moisture"], "library": None,
     "search_query": "Capacitive Soil Moisture Sensor v1.2 pinout operating voltage 3.3V output",
     "est_price_gbp": 3, "rationale": "Capacitive probes don't corrode like resistive ones.",
     "alternatives": [{"option": "YL-69 resistive probe", "reason_rejected": "electrodes corrode within weeks"}],
     "confidence": 0.85},
    {"id": "pump", "name": "Mini submersible water pump", "model": "3-5V DC mini submersible pump", "qty": 1,
     "role": "actuator", "interface": "power", "satisfies": ["auto_water"], "library": None,
     "search_query": "3-5V mini submersible water pump current draw", "est_price_gbp": 4,
     "rationale": "Cheap, runs from USB 5 V.", "alternatives": [], "confidence": 0.8},
    {"id": "mosfet", "name": "Logic-level N-MOSFET", "model": "IRLZ44N", "qty": 1, "role": "driver",
     "interface": "digital", "satisfies": ["auto_water"], "library": None,
     "search_query": "IRLZ44N gate threshold 3.3V logic level", "est_price_gbp": 1,
     "rationale": "Switches the pump from a 3.3 V GPIO.",
     "alternatives": [{"option": "IRF520 module", "reason_rejected": "not fully on at 3.3 V gate drive"}],
     "confidence": 0.85},
    {"id": "flyback_diode", "name": "Flyback diode", "model": "1N4007", "qty": 1, "role": "passive",
     "interface": "none", "satisfies": ["auto_water"], "library": None, "search_query": "1N4007 flyback diode",
     "rationale": "Protects the MOSFET from the pump's inductive kick.", "alternatives": [], "confidence": 0.9},
    {"id": "reservoir", "name": "Water reservoir", "model": "1 L plastic container with lid", "qty": 1,
     "role": "mechanical", "interface": "none", "satisfies": ["auto_water"], "library": None,
     "search_query": "1L plastic water container", "est_price_gbp": 2,
     "rationale": "Holds several days of water; the pump sits inside.", "alternatives": [], "confidence": 0.8},
    {"id": "tubing", "name": "Silicone tubing", "model": "Food-grade silicone tube 6 mm, 1 m", "qty": 1,
     "role": "mechanical", "interface": "none", "satisfies": ["auto_water"], "library": None,
     "search_query": "silicone tubing 6mm 1m", "est_price_gbp": 2,
     "rationale": "Carries water from the pump to the pot.", "alternatives": [], "confidence": 0.8},
    {"id": "oled", "name": "0.96\" OLED display", "model": "SSD1306 128x64 I2C", "qty": 1, "role": "display",
     "interface": "i2c", "satisfies": ["show_status"], "library": "Adafruit SSD1306",
     "search_query": "SSD1306 0.96 inch I2C OLED module pinout 3.3V address 0x3C", "est_price_gbp": 4,
     "rationale": "Ubiquitous, 4 wires, well supported.", "alternatives": [], "confidence": 0.9},
]

VERIFY = {
    "mcu": ("3.3V regulator, 5V via USB/VIN", "3.3V", ["3V3", "GND", "VIN", "GPIO"]),
    "soil_sensor": ("3.3-5.5V", "3.3V", ["VCC", "GND", "AOUT"]),
    "pump": ("3-5V, ~100-200 mA", "n/a", ["+", "-"]),
    "mosfet": ("Vgs(th) 1-2V", "3.3V", ["G", "D", "S"]),
    "oled": ("3.3-5V", "3.3V", ["VCC", "GND", "SCL", "SDA"]),
    "lcd": ("5V (backlight); I2C works with 3.3 V pull-ups", "3.3V/5V", ["GND", "VCC", "SDA", "SCL"]),
}

SUPPORT = [
    {"name": "Gate pull-down resistor", "value": "10kΩ", "kind": "resistor", "ends": ["mosfet.G", "GND"],
     "purpose": "keeps pump off during boot", "wiring": "MOSFET G to GND", "connects": ["mosfet"]},
    {"name": "Pump supply", "value": "5V", "kind": "wire", "ends": ["VIN", "pump.+"],
     "purpose": "pump power from the 5 V rail", "wiring": "5 V rail → pump +", "connects": ["pump"]},
    {"name": "Pump return", "value": "", "kind": "wire", "ends": ["pump.-", "mosfet.D"],
     "purpose": "low-side switch through the MOSFET", "wiring": "pump − → MOSFET D", "connects": ["pump", "mosfet"]},
    {"name": "Flyback diode", "value": "1N4007", "kind": "diode", "ends": ["pump.+", "pump.-"],
     "purpose": "clamps inductive spike", "wiring": "across pump, stripe to +", "connects": ["pump", "flyback_diode"]},
]
BULK_CAP = {"name": "Bulk capacitor", "value": "470µF 16V", "kind": "capacitor", "ends": ["VIN", "GND"],
            "purpose": "absorbs pump inrush so the ESP32 doesn't brown out",
            "wiring": "across 5 V and GND next to the pump (stripe to GND)", "connects": ["pump"]}

REVIEW_NOTES = {
    "part.oled": ("challenge", "medium", "The OLED duplicates the Wi-Fi status page and adds £4 and 4 wires; "
                  "for a set-and-forget waterer it is not needed.", "Drop the OLED and rely on the web page."),
    "wiring.support": None,  # computed: challenges until a bulk capacitor is present
    "pin.mosfet.g": ("challenge", "low", "GPIO26 is fine; a ~100 Ω series gate resistor would limit ringing.",
                     "Add 100 Ω between GPIO26 and the gate."),
    "req.assumptions": ("challenge", "low", "'Pump draws < 250 mA' is assumed, not verified.",
                        "Measure or check the pump listing before relying on USB power."),
    "part.lcd": ("challenge", "medium", "1602 backpacks need 5 V for good contrast; make sure its I2C pull-ups go to 3.3 V, not 5 V.",
                 "Power the LCD from VIN and remove/replace the backpack's 5 V pull-ups, or add a level shifter."),
    "fw.sketch": ("approve", "low", "Implements all features on the wired pins; loop is non-blocking.", ""),
}


def _conns(soil_pin: str, lcd: bool = False, oled: bool = True) -> list[dict]:
    c = lambda pid, pp, ep, d, s, r: {"part_id": pid, "part_pin": pp, "esp32_pin": ep, "direction": d,  # noqa: E731
                                      "signal_type": s, "rationale": r, "alternatives": []}
    return [
        c("soil_sensor", "VCC", "3V3", "power", "power", "Sensor works at 3.3 V, keeping AOUT within ADC range."),
        c("soil_sensor", "GND", "GND", "power", "power", "Common ground."),
        c("soil_sensor", "AOUT", soil_pin, "analog_in", "analog", "Analog moisture reading."),
        c("mosfet", "G", "GPIO26", "output", "digital", "Free output pin, not a strapping pin."),
        c("mosfet", "S", "GND", "power", "power", "Low-side switch source to ground."),
    ] + ([
        c("lcd", "VCC", "VIN", "power", "power", "Backlight and contrast need 5 V."),
        c("lcd", "GND", "GND", "power", "power", "Common ground."),
        c("lcd", "SDA", "GPIO21", "bidirectional", "i2c_sda", "Default I2C SDA, shared bus."),
        c("lcd", "SCL", "GPIO22", "output", "i2c_scl", "Default I2C SCL, shared bus."),
    ] if lcd else [
        c("oled", "VCC", "3V3", "power", "power", "3.3 V logic."),
        c("oled", "GND", "GND", "power", "power", "Common ground."),
        c("oled", "SDA", "GPIO21", "bidirectional", "i2c_sda", "Default I2C SDA."),
        c("oled", "SCL", "GPIO22", "output", "i2c_scl", "Default I2C SCL."),
    ] if oled else [])


BLOCK_NOTES = {
    "preamble1": ("Libraries the sketch needs.",
                  "Pulls in Wi-Fi and the tiny web server built into the ESP32 core, plus the I2C (Wire) bus and the "
                  "display driver library. Nothing runs here: these lines just make the functions available."),
    "globals1": ("Pin numbers, tuning values and shared state.",
                 "Every GPIO is named once at the top (soil sensor on GPIO34, MOSFET gate on GPIO26) so the rest of "
                 "the code never uses bare numbers. DRY_RAW/WET_RAW are the sensor's readings in air and in water: "
                 "calibrate them with your probe. The timing constants are in milliseconds."),
    "fn_readMoisture": ("Reads the soil sensor and converts it to 0-100 %.",
                        "Takes 8 analogRead() samples on GPIO34 and averages them to smooth out noise. GPIO34 is on "
                        "ADC1, which keeps working while Wi-Fi is on. map() turns the raw 12-bit value into a "
                        "percentage: capacitive probes read LOWER when wetter, so DRY_RAW maps to 0 and WET_RAW to 100."),
    "fn_setPump": ("Turns the pump on or off through the MOSFET.",
                   "Writing GPIO26 HIGH switches the logic-level MOSFET on, which connects the pump's negative lead "
                   "to ground so it runs from the 5 V rail. It records when the pump started so loop() can stop it "
                   "after PUMP_MS without blocking."),
    "fn_drawStatus": ("Shows the current state on the display.",
                      "Redraws moisture, pump state and the board's IP address. It is called every 2 s from "
                      "loop(), not continuously, so the display bus isn't kept busy."),
    "fn_handleRoot": ("The web status page.",
                      "Runs when a browser opens the ESP32's IP address and sends back a small HTML page with the "
                      "latest moisture and pump state."),
    "fn_setup": ("Runs once at power-up.",
                 "Starts the serial monitor at 115200 baud, makes the pump pin an output and forces it LOW first so "
                 "the pump can't twitch on at boot, sets the ADC to 12 bits, starts the display and Wi-Fi, and "
                 "registers the web page."),
    "fn_loop": ("Runs forever: serve, sample, decide, time the pump.",
                "Never blocks: instead of delay() it compares millis() timestamps. Every 2 s it samples moisture "
                "and, if the soil is drier than 35 % and the 60 s cooldown has passed, starts the pump. A separate "
                "check stops the pump after 3 s. The cooldown lets water soak in before measuring again."),
}

STEP_ANCHORS = {
    "S": ["void setup()"], "A": ["Serial.begin(115200)"], "B": ["pinMode(PIN_PUMP_GATE", "setPump(false);"],
    "C": ["analogReadResolution", "Wire.begin", "display.begin", "lcd.init", "lcd.backlight"],
    "D": ["WiFi.begin", "server.on", "server.begin()"], "L": ["void loop()"], "W": ["server.handleClient()"],
    "T": ["if (now - lastSample >= SAMPLE_MS)", "lastSample = now;"], "R": ["moisturePct = readMoisture()"],
    "M": ["if (!pumpOn && moisturePct < WATER_BELOW_PCT"], "P": ["setPump(true)"], "O": ["    drawStatus();"],
    "Q": ["if (pumpOn && now - pumpStarted >= PUMP_MS)"], "F": ["    setPump(false);", "lastWatered = now;"],
}


def _numbered_lines(prompt: str) -> dict[int, str]:
    return {int(m.group(1)): m.group(2) for m in re.finditer(r"^\s*(\d+)\| ?(.*)$", prompt, flags=re.M)}


def _walkthrough(prompt: str) -> dict:
    lines = _numbered_lines(prompt.split("BLOCKS TO EXPLAIN:")[0])
    ids = re.findall(r"^- (\w+): ", prompt.split("BLOCKS TO EXPLAIN:")[-1], flags=re.M)
    blocks = [{"id": i, "summary": BLOCK_NOTES.get(i, ("Helper code.", ""))[0],
               "explanation": BLOCK_NOTES.get(i, ("", "Supporting code for the features above."))[1]} for i in ids]
    steps = []
    for node, anchors in STEP_ANCHORS.items():
        hits = sorted(n for n, t in lines.items() for a in anchors if a in t)
        if node == "O":  # the call inside loop(), not the definition
            hits = hits[-1:]
        loop_at = max((k for k, t in lines.items() if "void loop()" in t), default=0)
        if node == "F":
            hits = [n for n in hits if n > loop_at]
        if node in ("B", "C", "D"):
            hits = [n for n in hits if n < loop_at]
        if hits:
            steps.append({"node_id": node, "start_line": hits[0], "end_line": hits[-1],
                          "explanation": f"Lines {hits[0]}-{hits[-1]} implement this step."})
    return {"blocks": blocks, "steps": steps}


def _explain(prompt: str) -> dict:
    sel = _numbered_lines(prompt.split("SELECTED LINES")[-1])
    text = " ".join(sel.values())
    pins = [("GPIO34", "soil sensor AOUT (analog input)")] if "PIN_SOIL" in text or "readMoisture" in text else []
    if "PIN_PUMP_GATE" in text or "setPump" in text:
        pins.append(("GPIO26", "MOSFET gate that switches the pump"))
    if "Wire" in text or "display" in text or "lcd" in text:
        pins.append(("GPIO21/22", "I2C SDA/SCL to the display"))
    calls = sorted(set(re.findall(r"([A-Za-z_]\w*)\(", text)) - {"if", "for", "while", "String", "constrain"})
    lo, hi = (min(sel), max(sel)) if sel else (0, 0)
    watch = ("millis() wraps after ~49 days; subtracting timestamps (now - last) handles that correctly."
             if "millis" in text or "now -" in text else
             "analogRead on ADC2 pins fails while Wi-Fi is on; GPIO34 is ADC1 so this is safe." if "analogRead" in text else "")
    return {"explanation": f"(offline explanation) Lines {lo}-{hi} " +
                           (f"call {', '.join(calls[:5])}. " if calls else "check a condition before acting. "
                            if re.search(r"\bif\s*\(", text) else "declare values used elsewhere. ") +
                           "In the full version, Claude explains exactly what these lines do and why, in the context of "
                           "the whole sketch and your wiring.",
            "hardware": [{"pin": p, "what": w} for p, w in pins], "watch_out": watch}


def _toolchain_ready() -> bool:
    from .compiler import toolchain_status
    from .config import get_settings

    return toolchain_status(get_settings()) == "ready"


class MockLLM:
    name = "mock"

    def __init__(self, model: str = "mock-llm"):
        self.model = model

    def call_tool(self, *, system, prompt, tool_name, tool_description, schema, max_tokens=8000):
        if tool_name == "clarifying_questions":
            if "Feedback:" in prompt:
                return {"questions": []}
            low = prompt.lower()
            if any(w in low for w in ("plant", "water", "garden")):
                return {"questions": ["Is the plant indoors or outdoors?", "Do you have a budget in mind (in £)?"]}
            return {"questions": []}
        if tool_name == "acceptance_criteria":
            return {"criteria": [
                {"id": "AC1", "criterion": "Waters the plant automatically when the soil is dry, with no user action",
                 "why": "'waters my plant when the soil is dry'", "priority": "must"},
                {"id": "AC2", "criterion": "Runs reliably: the pump starting must not reset or brown out the ESP32",
                 "why": "an unattended waterer that resets is useless", "priority": "must"},
                {"id": "AC3", "criterion": "Everything needed to build it is in the cart (incl. reservoir and tubing), "
                 "within the stated budget", "why": "the user should be able to order and build", "priority": "must"},
                {"id": "AC4", "criterion": "Firmware is verified to compile for the ESP32", "why": "buildable",
                 "priority": "must"},
                {"id": "AC5", "criterion": "Shows soil moisture and pump state somewhere the user can check",
                 "why": "the user wants to know it is working", "priority": "should"},
            ]}
        if tool_name == "reconsider_after_logbook":
            out = []
            for key in re.findall(r"^YOUR CHALLENGE \[\w+\] on (\S+):", prompt, flags=re.M):
                if key == "req.assumptions":
                    out.append({"key": key, "action": "withdraw",
                                "note": "The rationale makes the assumption explicit and the pump spec supports it."})
                else:
                    out.append({"key": key, "action": "keep",
                                "note": "The rationale does not address the risk; the point stands."})
            return {"decisions": out}
        if tool_name == "respond_to_review":
            resp = []
            for key in re.findall(r"^CHALLENGE \[\w+\] on (\S+):", prompt, flags=re.M):
                if key == "wiring.support":
                    resp.append({"key": key, "response": "accept", "reason": "Correct: add a 470 µF bulk capacitor "
                                 "across 5 V/GND next to the pump."})
                elif key == "part.oled":
                    resp.append({"key": key, "response": "reject", "reason": "A glanceable display at the plant is "
                                 "useful without a phone, and £4 still fits the budget."})
                elif key == "part.lcd":
                    resp.append({"key": key, "response": "reject", "reason": "The user explicitly asked for the LCD; "
                                 "it is powered from VIN and its I2C lines are pulled up to 3.3 V."})
                else:
                    resp.append({"key": key, "response": "reject", "reason": "Low impact for an on/off pump; keeping "
                                 "the simpler wiring."})
            return {"responses": resp}
        if tool_name == "final_acceptance":
            ids = re.findall(r"^- (AC\d+) \[", prompt, flags=re.M)
            skipped = "COMPILE: skipped" in prompt
            out = []
            for i in ids:
                if i == "AC4" and skipped:
                    out.append({"id": i, "status": "unverified", "note": "arduino-cli was not available, so the "
                                "firmware was never compiled."})
                elif i == "AC3" and "over_budget=True" in prompt:
                    out.append({"id": i, "status": "not_met", "note": "The cart is over budget."})
                else:
                    out.append({"id": i, "status": "met", "note": "Satisfied by the final design."})
            return {"assessment": out}
        if tool_name == "record_feedback":
            n = len(re.findall(r"^- AC\d+ ", prompt, flags=re.M)) + 1
            lcd = "lcd" in prompt.lower()
            return {"lesson": "The user prefers a character LCD to an OLED." if lcd else "Note the user's preference.",
                    "missed_by_review": False,
                    "criteria": [{"id": f"AC{n}", "criterion": "Status is shown on a 16x2 LCD, as the user asked" if lcd
                                  else "Meets the user's improvement request", "why": "user feedback after delivery",
                                  "priority": "must"}]}
        if tool_name == "map_feedback":
            low = prompt.split("DECISIONS:")[0].lower()
            if "lcd" in low:
                # if the OLED was already removed (e.g. after a verdict), add the LCD through the parts stage
                key = "part.oled" if "- part.oled:" in prompt else "part.mcu"
                return {"targets": [{"key": key, "note": "Use a 16x2 I2C LCD for status (replace any OLED)."}]}
            return {"targets": [{"key": "req.summary", "note": "Apply the user's feedback."}]}
        if tool_name == "record_requirements":
            return {
                "project_name": "Smart Plant Waterer",
                "summary": "Measures soil moisture and waters the plant automatically with a small pump. "
                           "Shows status on an OLED and a local web page.",
                "features": [
                    {"id": "measure_moisture", "description": "Measure soil moisture", "type": "sense",
                     "rationale": "Core input for deciding when to water.", "confidence": 0.95},
                    {"id": "auto_water", "description": "Run a pump when the soil is dry", "type": "actuate",
                     "rationale": "The 'automatic' in the idea.", "confidence": 0.9},
                    {"id": "show_status", "description": "Show moisture and pump state on a display", "type": "display",
                     "rationale": "Feedback without a phone.", "confidence": 0.75},
                    {"id": "remote_monitoring", "description": "Serve a status page over Wi-Fi", "type": "comms",
                     "rationale": "ESP32 has Wi-Fi built in; near-zero cost.", "confidence": 0.7},
                ],
                "power": {"source": "USB 5 V", "notes": "USB powers the ESP32; pump runs from VIN via MOSFET.",
                          "alternatives": [{"option": "18650 battery", "reason_rejected": "adds charging circuitry"}]},
                "connectivity": {"wifi": True, "ble": False, "notes": "Local web status page."},
                "assumptions": ["Indoor single pot", "Water reservoir next to the plant", "Pump draws < 250 mA"],
                "budget_gbp": (float(m.group(1)) if (m := re.search(r"£\s?(\d+(?:\.\d+)?)", prompt)) else None),
            }
        if tool_name == "select_parts":
            parts = json.loads(json.dumps(PARTS))
            if "CHANGE REQUESTS" in prompt and "lcd" in prompt.lower():
                parts = [p for p in parts if p["id"] != "oled"] + [dict(LCD_PART)]
            elif "CHANGE REQUESTS" in prompt and "drop the oled" in prompt.lower():
                parts = [p for p in parts if p["id"] != "oled"]   # user sided with the Bar Raiser
            elif re.search(r"on part\.oled[^\n]*USER SIDED WITH THE BAR RAISER", prompt):
                parts = [p for p in parts if p["id"] != "oled"]   # learned from a past project's verdict
            elif "CURRENT DECISIONS" in prompt and "part.oled" not in prompt and "part.lcd" not in prompt:
                parts = [p for p in parts if p["id"] != "oled"]   # OLED was dropped earlier; keep it dropped
            return {"parts": parts}
        if tool_name == "verify_parts":
            ids = re.findall(r'PART \{"id": "([^"]+)"', prompt)
            out = []
            for pid in ids:
                sv, lv, pins = VERIFY.get(pid, ("n/a", "n/a", []))
                out.append({"id": pid, "verified": pid in VERIFY, "supply_voltage": sv, "logic_voltage": lv,
                            "pins": pins, "notes": f"Mock verification for {pid}.",
                            "cited_urls": [f"https://example.com/mock/{pid}"]})
            return {"parts": out}
        if tool_name == "assign_pins":
            fixed = "VALIDATOR ERRORS" in prompt or "GPIO34" in prompt
            support = [dict(x) for x in SUPPORT]
            low = prompt.lower()
            learned = "PRECEDENTS" in prompt and "470" in low  # a similar past project was challenged on this
            if ("CHANGE REQUESTS" in prompt and ("capacitor" in low or "470" in low)) or "Bulk capacitor" in prompt or learned:
                support.append(dict(BULK_CAP))
            return {"connections": _conns("GPIO34" if fixed else "GPIO4", lcd='"id": "lcd"' in prompt,
                                          oled='"id": "oled"' in prompt),
                    "support_components": support}
        if tool_name == "write_firmware":
            lcd = "\n- lcd:" in prompt
            no_display = not lcd and "\n- oled:" not in prompt
            sketch = LCD_SKETCH if lcd else NO_DISPLAY_SKETCH if no_display else SKETCH
            if "COMPILER ERRORS" not in prompt and "CURRENT SKETCH" not in prompt and _toolchain_ready():
                # realistic first-attempt mistake: core 2.x PWM API, removed in core 3.x
                sketch = SKETCH.replace("  pinMode(PIN_PUMP_GATE, OUTPUT);",
                                        "  ledcSetup(0, 5000, 8);\n  ledcAttachPin(PIN_PUMP_GATE, 0);")
            return {"filename": "plant_waterer.ino", "sketch": sketch,
                    "libraries": ["LiquidCrystal I2C"] if lcd else [] if no_display else ["Adafruit SSD1306", "Adafruit GFX Library"],
                    "functions": [{"name": "readMoisture", "purpose": "averaged ADC read → %"},
                                  {"name": "setPump", "purpose": "drive MOSFET gate"},
                                  {"name": "drawStatus", "purpose": "LCD status" if lcd else "OLED status"},
                                  {"name": "handleRoot", "purpose": "web status page"}],
                    "feature_map": [{"feature_id": "measure_moisture", "functions": ["readMoisture"]},
                                    {"feature_id": "auto_water", "functions": ["setPump"]},
                                    {"feature_id": "show_status", "functions": ["drawStatus"]},
                                    {"feature_id": "remote_monitoring", "functions": ["handleRoot"]}],
                    "notes": "Calibrate DRY_RAW/WET_RAW with the probe in air and in water."}
        if tool_name == "flow_diagram":
            m = MERMAID
            if "LiquidCrystal_I2C" in prompt:
                m = m.replace("init SSD1306", "init 16x2 LCD").replace("Update OLED", "Update LCD")
            elif "Adafruit_SSD1306" not in prompt:
                m = m.replace("I2C on GPIO21/22, init SSD1306", "Configure ADC").replace("Update OLED", "Log status to Serial")
            return {"mermaid": m, "explanation": "setup() configures the pump pin, OLED and Wi-Fi. loop() "
                    "serves the web page, samples moisture every 2 s, starts the pump below 35 % and stops it after 3 s.",
                    **_walkthrough(prompt)}
        if tool_name == "review_decisions":
            reviews = []
            for key in re.findall(r"^KEY: (\S+)", prompt, flags=re.M):
                if key == "wiring.support":
                    if "470" in prompt:
                        r = ("approve", "low", "Bulk capacitor now absorbs pump inrush; protection is complete.", "")
                    else:
                        r = ("challenge", "high",
                             "The pump's start-up surge (often 0.5-1 A) comes off the same USB 5 V rail as the ESP32. "
                             "Combined with Wi-Fi transmit peaks this can brown out and reset the board every time it waters.",
                             "Add a 470 µF electrolytic across VIN and GND next to the pump, or give the pump its own 5 V supply.")
                else:
                    r = REVIEW_NOTES.get(key) or ("approve", "low", "OK.", "")
                reviews.append({"key": key, "verdict": r[0], "severity": r[1], "notes": r[2], "suggested_fix": r[3]})
            high = any(x["severity"] == "high" and x["verdict"] != "approve" for x in reviews)
            return {"reviews": reviews, "risk_level": "high" if high else "low",
                    "summary": "Power integrity issue around the pump must be fixed before building." if high
                    else "No blocking issues; remaining notes are minor."}
        if tool_name == "build_guide":
            parts_sec = prompt.split("PARTS:", 1)[1].split("VALIDATED PIN MAP:", 1)[0]
            disp = "16x2 I2C LCD" if "16x2 I2C LCD" in parts_sec else '0.96" OLED display' if "OLED" in parts_sec else None
            cap = "470" in prompt
            show = (f"It also shows the moisture level and pump state on the {disp} next to the plant and on a small "
                    "web page you can open from your phone on the same Wi-Fi.") if disp else (
                    "You can check the moisture level and pump state on a small web page from your phone on the same Wi-Fi.")
            steps = [
                {"title": "Order the parts", "body": "Everything is in the BoM tab, including the reservoir and tubing. "
                 "Small parts (10 kΩ resistor, 1N4007 diode" + (", 470 µF capacitor" if cap else "") + ") come in any starter kit. "
                 "You also need a USB cable and jumper wires; no soldering is required."},
                {"title": "Install the software", "body": "Install the Arduino IDE, add the ESP32 boards package (Espressif, "
                 "core 3.x) and select 'ESP32 Dev Module'. Install the libraries listed on the Firmware tab from the Library Manager."},
                {"title": "Place the ESP32 and power rails", "body": "Push the ESP32 DevKit across the centre gap of the breadboard. "
                 "Run VIN (5 V) and GND to the top rails and 3V3 and GND to the bottom rails, as in the Wiring tab picture. "
                 "Never connect 5 V to a 3.3 V pin."},
                {"title": "Connect the soil sensor", "body": "Power the capacitive soil sensor from 3V3 and GND, and connect its "
                 "AOUT pin to GPIO34. GPIO34 is an ADC1 pin, so it still reads correctly while Wi-Fi is on."},
                {"title": "Wire the pump through the MOSFET", "body": "The pump draws far more current than a GPIO can give, so the "
                 "IRLZ44N MOSFET switches it: gate to GPIO26 with the 10 kΩ pull-down to GND, source to GND, drain to the pump's "
                 "minus wire, pump plus to 5 V. Put the 1N4007 across the pump with the striped end on the plus side."
                 + (" Add the 470 µF capacitor across 5 V and GND next to the pump (stripe to GND) so the pump's start-up "
                    "surge cannot reset the ESP32." if cap else "")},
            ]
            if disp:
                steps.append({"title": f"Connect the {disp}", "body": f"The {disp} uses I2C: SDA to GPIO21 and SCL to GPIO22, "
                              "plus power and ground. Follow the connection checklist for which rail powers it."})
            steps += [
                {"title": "Check every wire", "body": "Go through the connection checklist below one line at a time and tick each "
                 "wire off. Most first-build problems are one wire in the wrong row."},
                {"title": "Upload the firmware", "body": "Open the sketch from the Firmware tab, put your Wi-Fi name and password in "
                 "WIFI_SSID and WIFI_PASS, then upload over USB. Open the Serial Monitor at 115200 baud to see the moisture readings."},
                {"title": "Install it at the plant", "body": "Push the sensor into the soil up to its line (keep the electronics on top "
                 "dry), put the pump in the full reservoir and run the tubing to the pot. Keep the breadboard above and away from the water."},
            ]
            return {
                "what_it_is": "This is a self-watering plant monitor. Every few seconds it measures how wet the soil is, and "
                              "when the soil gets too dry it runs a small pump for a few seconds to water the plant from a "
                              "reservoir, then waits before watering again so it never floods the pot.\n\n" + show +
                              " An ESP32 board reads the capacitive soil sensor, decides when to water, and switches the pump "
                              "through a MOSFET, because the pump needs more current than the ESP32 can supply directly.",
                "you_will_need": "A computer with the Arduino IDE, a USB cable for the ESP32 and about an hour. No soldering.",
                "build_steps": steps,
                "test_and_calibrate": [
                    "With the sensor in dry air, note the reading in the Serial Monitor and set DRY_RAW to it.",
                    "Dip the sensor in a glass of water, note the reading and set WET_RAW to it, then upload again.",
                    "Press the probe into dry soil: the pump should run for 3 s, then stay off for at least 60 s.",
                    "Open the IP address shown in the Serial Monitor in your phone's browser to see the status page.",
                ],
                "safety": ["Keep water away from the breadboard and USB power; mount the electronics above the reservoir.",
                           "Unplug USB before changing any wiring."],
                "troubleshooting": [
                    {"symptom": "The ESP32 resets when the pump starts", "fix": "Check the bulk capacitor and that the pump is on 5 V, not 3V3."},
                    {"symptom": "Moisture always reads 0 % or 100 %", "fix": "Recalibrate DRY_RAW/WET_RAW and check AOUT is on GPIO34."},
                    {"symptom": "The pump never runs", "fix": "Check the MOSFET orientation (G, D, S) and the gate wire on GPIO26."},
                ],
            }
        if tool_name == "refresh_summary":
            low = prompt.lower()
            shows = ("a 16x2 LCD" if "16x2" in low or "lcd" in low.split("current parts:")[-1]
                     else "an OLED" if "oled" in low.split("current parts:")[-1] else None)
            return {"summary": "Measures soil moisture and waters the plant automatically with a small pump. "
                               + (f"Shows status on {shows} and a local web page." if shows
                                  else "Shows status on a local web page.")}
        if tool_name == "explain_code":
            return _explain(prompt)
        raise ValueError(f"mock has no response for tool {tool_name}")


class MockSearch:
    name = "mock-search"

    def search(self, query: str) -> SearchResult:
        key = re.sub(r"[^a-z0-9]+", "-", query.lower())[:40]
        return SearchResult(query=query, provider=self.name,
                            answer=f"(mock) Spec summary for: {query}",
                            sources=[Evidence(source="mock", url=f"https://example.com/mock/{key}",
                                              title=f"Mock datasheet: {query[:50]}", snippet="mock snippet")])
