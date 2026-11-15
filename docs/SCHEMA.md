# Decision log schema

The decision log is the backbone of the agent. Every stage reads its inputs **from the log**
(not from in-memory state) and writes its outputs **to the log**. That gives us three things
for free:

1. **Explainability**: every part, pin and line of firmware traces back to a rationale, rejected
   alternatives and cited evidence.
2. **Partial rebuilds**: change one decision and only its downstream decisions need redoing.
3. **Review hooks**: the Bar Raiser comments on decisions, not on prose.

## Collections (MongoDB)

| Collection | One doc per | Key indexes |
|---|---|---|
| `runs` | prompt → run | `created_at` |
| `decisions` | decision *version* | unique `(run_id, key, version)`; `(run_id, status, stage)`; `(run_id, depends_on.key)` |
| `artifacts` | rendered output version | unique `(run_id, kind, version)` |
| `search_cache` | Linkup/Tavily query | unique `qhash`, TTL 7 days on `created_at` |
| `reviews` | Bar Raiser review of one decision version | `(run_id, round)` |
| `precedents` | Bar-Raiser-approved run | unique `source_run_id`; Atlas Vector Search index `precedent_vectors_<dims>` on `embedding` (+ filters `embedding_model`, `review_status`) |

## `decisions` document

```jsonc
{
  "_id": ObjectId,
  "run_id": "6736...",               // runs._id as string
  "key": "pin.soil_sensor.aout",     // stable slug, unique per run (see namespaces)
  "version": 2,                      // 1, 2, 3... allocated by the store
  "stage": "wiring",                 // requirements | parts | wiring | firmware | diagram | review
  "kind": "pin",                     // feature | power | connectivity | part | pin | support_components | firmware | diagram
  "title": "Soil sensor AOUT → GPIO34",
  "choice": { ... },                 // stage-specific payload (examples below)
  "rationale": "GPIO34 is ADC1, so analog reads still work while Wi-Fi is on",
  "alternatives": [ { "option": "GPIO4", "reason_rejected": "ADC2 is unusable with Wi-Fi" } ],
  "depends_on": [                    // exact upstream versions this was built from
    { "key": "part.soil_sensor", "version": 1 },
    { "key": "part.mcu", "version": 1 },
    { "key": "req.connectivity", "version": 1 }
  ],
  "evidence": [                      // citations (Linkup), compiler logs, validator output
    { "source": "linkup", "url": "https://...", "title": "...", "snippet": "...", "retrieved_at": ISODate }
  ],
  "confidence": 0.85,                // 0..1; lowered for unverified parts / validator warnings
  "status": "active",                // active | superseded | invalidated | rejected | retired
  "author": "agent",                 // agent | bar_raiser | user | precedent (who asked for this version)
  "review": {                        // Bar Raiser's review of THIS version (null until reviewed)
    "verdict": "challenge", "severity": "high", "notes": "...", "suggested_fix": "...",
    "reviewer": "bar_raiser", "model": "claude-opus-5-5", "round": 1, "at": ISODate },
  "trigger": {                       // set on versions produced by a rebuild
    "source": "bar_raiser", "key": "wiring.support", "note": "pump inrush can brown out the ESP32..." },
  "precedent_ids": ["..."],          // precedents shown to the model when it made this decision
  "created_at": ISODate
}
```

### Key namespaces

| Prefix | Stage | Example | Depends on |
|---|---|---|---|
| `req.summary` | requirements | project name + summary | – |
| `req.feature.<id>` | requirements | "measure soil moisture" | – |
| `req.power`, `req.connectivity`, `req.assumptions` | requirements | | – |
| `part.<id>` | parts | `part.soil_sensor` | the `req.feature.*` it satisfies, `req.power` |
| `pin.<part>.<pin>` | wiring | `pin.oled.sda` | `part.<part>`, `part.mcu`, (`req.connectivity` for analog) |
| `wiring.support` | wiring | MOSFET, flyback diode, pull-ups | the parts involved |
| `fw.sketch` | firmware | compiled sketch metadata | every `pin.*`, `wiring.support`, `part.*`, `req.feature.*` |
| `diag.flow` | diagram | Mermaid code-flow diagram | `fw.sketch` |

### Versioning and staleness rules

* Decisions are **immutable**. Re-deciding a key inserts `version + 1` and flips the old one to
  `superseded`. The only in-place update is attaching the Bar Raiser `review` to the version it reviewed.
* **No-op dedupe**: re-deciding the same `choice` (ignoring volatile fields such as compile `attempts`) from
  the same dependency versions keeps the existing version. This stops a rebuild from cascading through
  decisions that didn't change.
* A stage re-run in a rebuild **retires** any of its decisions it no longer produces (status `retired`).
* `depends_on` stores **versions**, not just keys. A decision is **stale** when any dependency's
  current active version differs from the one recorded (`Store.stale()`).
* `Store.downstream(key)` returns the transitive set of dependents, which is the exact set a partial
  rebuild must redo. `Store.invalidate(keys)` marks them `invalidated` until rebuilt.

## `artifacts` document

```jsonc
{ "run_id": "...", "kind": "wiring", "version": 1,
  "content": { "connections": [...], "support_components": [...], "issues": [...], "mermaid": "flowchart LR ..." },
  "built_from": [ { "key": "pin.oled.sda", "version": 1 }, ... ],
  "created_at": ISODate }
```

Kinds: `parts_list`, `wiring`, `firmware`, `compile_log`, `flow_diagram`.

## `runs` document

```jsonc
{ "prompt": "...", "status": "running", "current_stage": "wiring",
  "review_status": "approved_with_notes",  // pending | approved | approved_with_notes | unresolved | off
  "review_rounds": [ { "round": 1, "group": "hardware", "reviewed": 10, "challenges": 2, "risk_level": "high", "summary": "..." } ],
  "rebuilds": [ { "at": ISODate, "source": "user", "targets": [ { "key": "part.oled", "note": "use an LCD" } ],
                  "stages": ["parts", "wiring", "firmware", "diagram"], "changed": ["part.lcd", "..."], "retired": ["part.oled", "..."] } ],
  "model": "claude-sonnet-5-5", "providers": { "llm": "anthropic", "search": "linkup", "db": "mongodb" },
  "events": [ { "at": ISODate, "stage": "parts", "level": "info", "message": "Linkup: 4 parts verified" } ],
  "error": null, "created_at": ISODate, "finished_at": ISODate }
```

## Example `choice` payloads

```jsonc
// part.soil_sensor
{ "name": "Capacitive soil moisture sensor", "model": "Capacitive Soil Moisture Sensor v1.2",
  "qty": 1, "role": "sensor", "interface": "analog", "supply_voltage": "3.3-5V", "logic_voltage": "3.3V",
  "pins": ["VCC", "GND", "AOUT"], "library": null, "verified": true, "verification_notes": "..." }

// pin.soil_sensor.aout
{ "part_id": "soil_sensor", "part_pin": "AOUT", "esp32_pin": "GPIO34", "gpio": 34,
  "direction": "analog_in", "signal_type": "analog", "bus": null }

// fw.sketch
{ "filename": "plant_waterer.ino", "libraries": [ { "name": "Adafruit SSD1306" } ],
  "functions": [ { "name": "readMoisture", "purpose": "..." } ],
  "compile_status": "ok", "attempts": 1, "sha256": "..." }
```

## `precedents` document

```jsonc
{ "source_run_id": "...", "prompt": "an ESP32 that waters my plant when the soil is dry",
  "project_name": "Smart Plant Waterer", "summary": "...", "features": ["Measure soil moisture", "..."],
  "parts": [ { "id": "oled", "name": "0.96\" OLED display", "model": "SSD1306 128x64 I2C", "interface": "i2c", "library": "Adafruit SSD1306" } ],
  "pin_map": [ { "part_id": "oled", "part_pin": "SDA", "esp32_pin": "GPIO21" } ],
  "support_components": ["Bulk capacitor 470µF 16V", "..."],
  "challenges": [ { "key": "wiring.support", "severity": "high", "notes": "...", "suggested_fix": "...", "resolution": "fixed" } ],
  "review_status": "approved_with_notes", "compile_status": "ok", "rebuilds": 1,
  "text": "prompt | name | summary | features",      // what was embedded
  "embedding": [0.012, ...], "embedding_model": "voyage-4-lite-1024",
  "uses": 3, "created_at": ISODate, "updated_at": ISODate }
```

Runs record what they recalled: `runs.precedents = [{id, prompt, project_name, score}]` and
`runs.precedent_search = "atlas $vectorSearch" | "local cosine"`.

### Vector index (Atlas)

```json
{ "fields": [
  { "type": "vector", "path": "embedding", "numDimensions": 1024, "similarity": "cosine" },
  { "type": "filter", "path": "embedding_model" },
  { "type": "filter", "path": "review_status" } ] }
```

## Wiring `support_components`

Each entry is one 2-terminal component or wire, with machine-readable `ends` so the breadboard view can
draw it: `{ name, value, kind: resistor|capacitor|diode|led|wire|other, ends: ["mosfet.G", "GND"], purpose,
wiring, connects }`. Ends are `part_id.PIN` or a rail (`3V3`, `VIN`, `GND`, `EXT`). Polarity: diode
`ends[0]` = cathode (stripe); capacitor/LED `ends[0]` = positive.

## `flow_diagram` artifact: walkthrough

```jsonc
{ "mermaid": "flowchart TD ...", "explanation": "...", "code_sha": "<sha256 of the sketch>",
  "walkthrough": {
    "blocks": [ { "id": "fn_loop", "kind": "function", "name": "loop()", "start": 78, "end": 92,
                  "summary": "Runs forever: ...", "explanation": "Never blocks: ..." } ],   // kinds: preamble | globals | function
    "steps":  [ { "node_id": "F", "start": 89, "end": 90, "explanation": "..." } ],     // diagram node → code lines
    "nodes":  { "F": "Pump OFF, start 60 s cooldown" } } }
```

## `explanations` collection

On-demand "explain these lines" answers: `{ run_id, code_sha, start, end, explanation, hardware: [{pin, what}],
watch_out, model, created_at }`, indexed on `(run_id, code_sha, start, end)`. Keyed by the code hash, so a
rebuilt sketch never shows stale explanations.
