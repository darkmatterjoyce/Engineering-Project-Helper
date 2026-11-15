# GUI redesign

The front end has been redesigned around a graphite engineering workspace with strict section/subsection hierarchy. The only modified original file is `web/index.html`. No backend code, API contract, model behavior, engineering validation or existing test was changed. No new production dependency or build step was added.

## Install

Replace your existing `web/index.html` with the one in this archive, or use the complete project. Keep your existing `.env` and credentials. Start the server using your normal command, then hard-refresh the browser (`Ctrl+Shift+R` on Windows).

```sh
EPH_MOCK=1 uvicorn eph.api:app --reload
```

Windows Command Prompt:

```bat
set EPH_MOCK=1
uvicorn eph.api:app --reload
```

## Design

- Graphite canvas and charcoal surfaces, restrained blue actions, system fonts and subtle motion.
- A concise opening title and project input. Decorative illustration and promotional blocks are not displayed.
- A collapsible creation panel; model selectors under Engineering team, samples under Need an idea?, and project history under Your projects.
- A minimal project heading. Cost, acceptance, compilation and review information stay inside their respective sections; no global metric strip.
- A vertical workspace navigator on desktop; scrollable horizontal navigation on mobile.
- Preserved engineering content organized into initially collapsed subsections. Build guide owns the brief, instructions, connection checklist, tests, safety and troubleshooting; Review & refine owns acceptance and review. Expansion state is retained across refreshes.
- Dark code canvas within the light workspace. Existing selection, explanation, copy and download actions are retained.
- Keyboard-operable navigation buttons, labeled form inputs, visible focus and reduced-motion support.

## Where to find existing functionality

| Previous location | New location |
| --- | --- |
| Develop project | Create something useful → Develop project; also New project in the header |
| Improve project picker | Create something useful → Improve project |
| Recent runs sidebar | Your projects, expandable above the workspace |
| Overview | Build guide |
| BoM / Cart | Parts & budget |
| Wiring | Connections |
| Firmware / Code flow | Code & logic |
| For improvement / Bar Raiser / Acceptance gaps | Review & refine |
| Other | Behind the build: Decision log, Agent activity, Dependency graph, Memory |
| Provider and compiler status chips | System status in the header |
| Acceptance criteria | Review & refine → Acceptance criteria |
| Stage progression | Behind the build → Agent activity → Build progress |
| Model selection | Create something useful → Engineering team |

## Verification

- Existing Python suite: **55 passed**.
- JavaScript syntax check: passed.
- DOM interaction checks connected to the actual Python offline pipeline: creation and clarifications; all six sections; checkbox persistence; firmware line selection and copying; decision/activity/graph/memory navigation; review verdict with OLED removal; LCD substitution in an independent project; project selection and improvement mode. Both test scenarios passed.
- The DOM checks stub Mermaid's renderer, while retaining the actual firmware and flow-map data. They do not prove rendered diagram appearance or pointer-to-SVG behavior.
- A live browser could not reach the execution environment, and local-file preview was blocked. Actual browser screenshots, rendered responsive layouts and visual diagram QA have **not** been verified. Responsive rules are implemented; inspect them in your local browser before the presentation.
- Real paid models, live shopping, actual Arduino compilation and hardware were not exercised.

## Existing offline scenario limitation (not changed)

In the existing canned backend, LCD feedback always initially targets `part.oled`. If a previous user verdict has already removed that part, feedback falls back to rebuilding requirements and may not add an LCD. Accordingly, OLED removal and LCD substitution were verified in separate fresh scenarios. This is existing mock behavior, not a GUI change; the backend was deliberately left untouched.
