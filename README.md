# Engineering Project Helper

**Describe an electronics project in one sentence. Get back everything you need to build it.**

> *"An ESP32 that waters my plant when the soil is dry"*

Engineering Project Helper is an autonomous agent that turns that sentence into a buildable ESP32 project:

- a **parts list** where every part is checked against live web sources, with buy links and a budget check
- a **breadboard layout** drawn from the validated pin map, so you can wire it hole by hole
- **firmware that compiles**, with an automatic fix loop when it doesn't
- a **code walkthrough**: click any line to have it explained, linked to a flow diagram
- a **step-by-step build guide** with a connection checklist, tests, safety notes and troubleshooting

Every choice the agent makes is recorded in a **versioned decision log with dependencies**. An independent
**Bar Raiser** (a second AI model from a different company) reviews the design, the engineer accepts or rejects
each challenge, and you get the final say on disagreements. Change anything ("use an LCD instead of the OLED")
and only what depends on it is rebuilt.

Built at the **Imperial AI Society Hackathon** (Autonomous Agents track).

---

## How it works

```mermaid
flowchart LR
    U["One sentence"] --> Q["Up to 2 questions"]
    Q --> C["Bar Raiser writes<br/>acceptance criteria"]
    C --> R[Requirements] --> P["Parts<br/>(checked by Linkup)"] --> W["Wiring<br/>(pin validator)"]
    W --> S["Shopping list<br/>(buy links, budget)"] --> F["Firmware<br/>(arduino-cli compile + fix)"] --> D[Code flow] --> G[Build guide]
    G --> B{"Bar Raiser<br/>review"}
    B -- "challenge accepted" --> X["Partial rebuild<br/>of affected decisions"] --> B
    B -- "engineer disagrees" --> J["You judge"]
    B -- approved --> A["Final acceptance check"] --> M[("Decision log + memory<br/>Databricks")]
```

1. **Clarify.** The agent asks at most two essential questions (budget, power source…).
2. **Set the bar first.** Before any design exists, the Bar Raiser writes acceptance criteria from your request.
   The builder never sees them.
3. **Design.** Requirements → parts → wiring → shopping list → firmware → code flow → build guide. Each stage
   writes decisions to the log, each linked to the decisions it depends on.
4. **Independent review.** The Bar Raiser reviews the results blind, then reads the engineer's reasoning and keeps,
   downgrades or withdraws each challenge. The engineer accepts (→ partial rebuild, re-reviewed) or rejects with
   a reason.
5. **You decide.** Rejected high-severity challenges come to you: Bar Raiser right, engineer right, or neither.
6. **Honest delivery.** A final check scores every criterion on the finished design and lists every gap
   (unmet criteria, estimated prices, firmware not compiled…).
7. **Memory.** Approved projects become precedents. Similar future projects learn from what was built and from
   every mistake the reviewer caught.

## Features

| | |
|---|---|
| **Versioned decision log** | Every part, pin and design choice is a decision with a version, rationale, alternatives, evidence and dependencies. Nothing is overwritten: a change creates a new version. |
| **Partial rebuilds** | Change one decision and only its downstream is rebuilt; everything else keeps its version. The dependency graph shows exactly what a change will touch. |
| **Independent Bar Raiser** | A reviewer from a different AI company, with its own acceptance criteria, a two-step review and a final acceptance check. |
| **Live part verification** | Every non-passive part is checked with Linkup, and the sources are cited on the parts list. |
| **Real compilation** | Firmware is compiled with `arduino-cli` for the ESP32. Errors are fed back to the model until it compiles. |
| **Breadboard view** | A deterministic drawing generated from the validated pin map: transistors on the board, resistors laid straight, wires in tidy lanes. |
| **Interactive code** | Click or drag over lines to get an explanation; the matching step lights up in the flow diagram. |
| **Choose your models** | Pick the Engineer and the Bar Raiser separately: Claude, Gemini, Groq (free tiers work), or the offline demo. |
| **Improve a project** | Describe what you'd like changed; the feedback is mapped onto decisions and only those are rebuilt and re-reviewed. |

## Sponsor integrations

- **Databricks**: the durable store. Runs, every decision version, reviews, outputs and the agent's memory are
  rows in Delta tables, one per kind of record. The app shows live row counts and ready-to-run SQL under
  *Behind the build → Storage*. Writes are batched in the background so the agent never waits on the warehouse.
- **Linkup**: live `sourcedAnswer` search verifies every part's pins, voltages and logic levels, finds buy links,
  and supplies the citations shown on the parts list.
- **MongoDB Atlas** (alternative store): the same app runs on Atlas, with Atlas Vector Search for memory.

## Quick start

You need Python 3.11+. Everything works offline first, with no keys and no cost.

**Windows (Command Prompt)**
```bat
cd engineering-project-helper
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn eph.api:app --port 8001 --no-access-log
```

**macOS / Linux**
```bash
cd engineering-project-helper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn eph.api:app --port 8001 --no-access-log
```

Open **http://localhost:8001** and try an example. With no `.env` it runs the **offline demo**: canned answers,
no API calls.

### Real runs

1. Copy `.env.example` to `.env` and fill in what you have. Each is optional:

   | What | `.env` | Get it |
   |---|---|---|
   | AI models (at least one) | `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY` | Gemini and Groq have free tiers, no card needed |
   | Live part checks | `LINKUP_API_KEY` | [app.linkup.so](https://app.linkup.so) |
   | Databricks storage | `DATABRICKS_HOST`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN` | SQL Warehouses → Connection details; Settings → Developer → Access tokens |
   | Turn off the demo | `EPH_MOCK=0` | |

2. Install the ESP32 compiler (optional, enables real compiles):
   - Windows: `python scripts\setup_arduino_windows.py`. It finds or installs `arduino-cli` and the ESP32 core,
     and reuses the copy inside the Arduino IDE if you have it.
   - macOS / Linux: `./scripts/setup_arduino.sh`

3. Check everything: `python scripts/check_setup.py`. It tests every key and service and tells you how to fix
   anything that fails.

4. Restart the server, open the app, pick an **Engineer** and an **Independent reviewer**, and build.

## Using the app

| Section | What's there |
|---|---|
| **Build guide** | What it is and how it works, numbered build steps, a connection checklist, test & calibrate, safety, troubleshooting |
| **Parts & budget** | Every part with specs, how it was verified (with sources), price and a buy link |
| **Connections** | The breadboard picture, the pin map, support components, the pin validator |
| **Code & logic** | The firmware linked to its flow diagram; click a line for an explanation |
| **Review & refine** | Acceptance criteria, the Bar Raiser's review, disagreements for you to judge, gaps, and the improve box |
| **Behind the build** | Decision log, agent activity, dependency graph, memory, and storage (live Databricks counts + SQL) |

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python, FastAPI | Simple, fast to build; the whole pipeline is plain Python |
| Agents | Anthropic Claude, Google Gemini, Groq via one tool-calling interface | Structured outputs everywhere; builder and reviewer can be different companies |
| Storage | Databricks Delta tables (or MongoDB Atlas, or in-memory) | The decision log is document-shaped and graph-linked; every version is kept |
| Search | Linkup (Tavily fallback) | Sourced answers with citations for part verification and buy links |
| Compiler | arduino-cli + ESP32 core | Proof that the firmware builds, not just that it looks right |
| Frontend | One HTML file, Mermaid for diagrams | No build step; runs anywhere the server runs |

## Project structure

```
eph/
  pipeline.py          the run: stages, review loop, partial rebuilds, final check
  stages/              requirements, parts, wiring, procurement, firmware, diagram, guide
  bar_raiser.py        independent reviewer: criteria, blind review, final acceptance
  engineer.py          accepts / rejects challenges, maps feedback onto decisions
  db.py                the versioned decision log (dependencies, stale detection, downstream)
  databricks_store.py  Databricks backend: write-behind mirror into Delta tables
  precedents.py        memory: similar past projects and their mistakes
  breadboard.py        breadboard drawing from the pin map
  llm.py, models.py    model providers and the model menu
  api.py               HTTP API used by the web app
web/index.html         the app
scripts/               setup checker, Arduino setup, Atlas setup
tests/                 offline tests (pytest)
docs/                  technical notes, decision log schema
```

## Tests

```bash
pytest -q
```

The suite runs fully offline with a mock model, mock search and an in-memory database. It covers the full
pipeline, partial rebuilds, review and verdicts, the Databricks backend against a fake warehouse, provider quirks
(rate limits, timeouts, cut-off answers) and the breadboard layout.

## Limitations

- Targets the ESP32 DevKit V1 (WROOM-32); the pin rules are specific to it.
- Generated designs are for low-voltage hobby circuits. Check current draw and power before connecting motors
  or pumps.
- Firmware is compiled, not run on hardware: test on the bench before relying on it.
- A full real run makes around 25 model calls, so free tiers allow a handful of runs per day.

## More detail

- [Technical notes](docs/TECHNICAL.md): how each stage, the Bar Raiser, partial rebuilds, memory and the
  API work
- [Decision log schema](docs/SCHEMA.md)

---

Built by Joyce and Eric at the Imperial AI Society Hackathon, November 2026.
