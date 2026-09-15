# dashboard-tests — Configurable SIL Dashboard

## 1. What this is

A **user-buildable interactive dashboard** for software-in-the-loop (SIL) plant
models. The user drags control and visualisation elements onto a grid, binds each
one to a signal, and drives/observes a running simulation — without writing code.

The dashboard is *model-agnostic*. It learns what signals and parameters exist by
reading a **lexicon** from the Dynamic Configuration Manager (DCM). Swap the plant
model, swap the lexicon, and the same dashboard image serves the new model.

First plant under test: `dc-battery-sim/` — a 2nd-order RC equivalent-circuit
Li-ion battery model (see its own README for the physics).

## 2. Pipeline shape

```
┌─────────────────┐   dashboard-in    ┌──────────────┐   dashboard-out   ┌─────────────────┐
│    Dashboard    │ ────────────────► │  blackbox    │ ────────────────► │    Dashboard    │
│  (control side) │   commands        │  SIL plant   │   telemetry       │   (view side)   │
└─────────────────┘                   └──────────────┘                   └─────────────────┘
        ▲                                     ▲
        │  lexicon (signals + params)         │  live parameter values
        └──────────────── DCM ────────────────┘
```

> **Naming trap — read this before touching topic config.**
> Topic names are from the **pipeline's** point of view, not the dashboard's:
> - `dashboard-in` — what the dashboard **sends**. Dashboard *produces*, SIL *consumes*.
> - `dashboard-out` — what the dashboard **reads**. SIL *produces*, dashboard *consumes*.
>
> On `dc-battery-sim` these are carried by its `input` and `output` env vars,
> set to `dashboard-in` / `dashboard-out` in `quix.yaml`.

The dashboard is a **plug-in service with its own image** — it is a normal Quix
deployment, not a portal add-on. It owns a web UI and speaks only to these topics
plus the DCM.

## 3. The lexicon (lives in DCM)

Single source of truth for *what the dashboard is allowed to show and drive*. Two
collections: `signals` (streamed values) and `parameters` (tunable model constants).

Every entry carries the same descriptor:

| Field | Meaning |
|---|---|
| `name` | Wire name — the JSON key on the topic |
| `label` | Human name shown on the element |
| `description` | Tooltip / help text |
| `datatype` | `bool` \| `uint` \| `int` \| `float` \| `enum` |
| `unit` | Engineering unit (`V`, `A`, `°C`, `%`, …); `null` for dimensionless |
| `default` | Startup value |
| `min` / `max` | Range — drives slider/knob endpoints and input validation |
| `enum` | For `datatype: enum` — `[{value, label}, …]`; `null` otherwise |
| `direction` | `input` (model consumes) \| `output` (model emits). Signals only. |
| `tunable` | Parameters only. `true` = writable live over `dashboard-in`; `false` = fixed at deploy time. |

`direction` is what makes the element picker work — see §4.

**Battery model lexicon** (derive from `dc-battery-sim/README.md`, do not re-invent):
- **Input signals** — `requested_power_w` (float, W), `ambient_temp_c` (float, °C),
  `chiller_setting` (enum 0/1/2), `heater_setting` (enum 0/1/2)
- **Output signals** — `soc_percent`, `q_act_as`, `ocv_v`, `dc_voltage_v`,
  `dc_current_a`, `rc1_voltage_v`, `rc2_voltage_v`, `temperature_c`, `heat_j`,
  `derating_factor` (all float), plus echoed setpoints
- **Fixed parameters** (`tunable: false`) — `SAMPLE_TIME`, `Q_MAX_AH`
- **Tunable parameters** (`tunable: true`) — `A_THERMAL`, `KT0`–`KT2`, `KE`,
  `TAU1`, `TAU2`, `R0`–`R2`, `COOLANT_TEMP`, `MAX_BATTERY_TEMP`

## 4. Dashboard UI contract

The grid has an **Edit mode** (add, move, resize, bind, delete elements) and a
**View mode** (interact only — no accidental re-layout while driving a run). The
binding picker is filtered per element:

| Element | Kind | Bindings | Offers | Valid datatypes |
|---|---|---|---|---|
| Switch | control | 1 | `direction: input` + `tunable` params | `bool`, `enum` |
| Knob | control | 1 | `direction: input` + `tunable` params | `uint`, `int`, `float` (needs `min`/`max`) |
| Type-in field | control | 1 | `direction: input` + `tunable` params | `uint`, `int`, `float` |
| Numeric readout | visualisation | 1 | `direction: output`, **and any parameter (read-only)** | any |
| Chart | visualisation | **1..N** | `direction: output` | `uint`, `int`, `float` |

Rules:
- **Never hardcode a signal name in UI code.** Everything comes from the lexicon.
- An element whose bound entry vanishes from the lexicon renders as *unbound*, it
  does not crash the grid.
- `min`/`max` are enforced client-side before publishing to `dashboard-in`.
- **Fixed parameters are never offered to a control element.** They are offered to a
  numeric readout, read-only, so a user can see a value they cannot change.
- **A chart takes several bindings.** Overlaying `ocv_v` against `dc_voltage_v` is the
  point of a chart; one-series-per-element would make the RC drop invisible. All
  series on one chart must share a `unit`, or the chart renders a second axis.
- **Type-in is numeric only.** An `enum` gets a switch or a select and a `bool` gets a
  switch — a free-text box that accepts `"fast"` for an enum is how Phase 1's C2 crash
  reached the sim in the first place.

## 5. Repo layout

```
dashboard-tests/
├── CLAUDE.md                  ← this file
├── quix.yaml                  ← pipeline definition (deployments + topics)
├── .pre-commit-config.yaml    ← pinned lint gate (ruff v0.6.3)
├── .env                       ← Quix creds (gitignored; .env.example is the template)
├── mongodb/                   ← backing store for the DCM
├── dc-battery-sim/            ← the blackbox SIL plant
│   ├── lexicon.json           ← its signal/parameter lexicon
│   └── tests/                 ← pytest suite
└── dev-planning/<feature>/    ← spec.md, architecture.md, reports
```

## 6. Environment

Quix context `testrig` (`https://portal-api.testrig.dev.quix.io`), org `testrigorg`,
project **quixstreams tests** (`e02ed12d-24ab-4a8f-bb69-3b5dd2fe0751`).

| Environment | Workspace ID | Git branch |
|---|---|---|
| Dashboard Test *(active — `.env` + this branch)* | `testrigorg-quixstreamstests-dashboardtest` | `devDB` |
| QuixStream Pipeline | `testrigorg-quixstreamstests-quixstr-df57e12b` | `dev` |

Dashboard work lives on **`devDB`** — that is the branch the Dashboard Test
environment tracks, and it is what `.env` points at. Committing dashboard work to
`dev` would deploy it to QuixStream Pipeline instead.

## 7. Decisions

### D1 — Parameter tuning is event-based over `dashboard-in` (2026-09-15)

Every parameter is either **fixed** or **tunable**:

- **Fixed** — set once in the deployment (`app.yaml` variables → env vars), read at
  startup, never changes at runtime. Anything whose mid-run change would break
  simulation continuity belongs here: `SAMPLE_TIME` (loop rate *and* both RC
  filter coefficients) and `Q_MAX_AH` (`SOC = q_act / Q_max`, so the reading jumps).
- **Tunable** — carried as events on `dashboard-in` with **partial-update**
  semantics: the env var supplies the startup default, and only the fields present
  in a message are rewritten. This is exactly the pattern `handle_command` already
  uses for signals (`dc-battery-sim/main.py:221-231`).

A restart returns tunable parameters to their deployment baseline. That is
intentional — the deployment defines the known-good starting point for a run.

**No DCM on the value path.** DCM holds the lexicon only.

Implementation constraints for any tunable parameter:

- **Recompute derived constants.** `ALPHA1`/`ALPHA2` `= exp(−SAMPLE_TIME / TAU)`
  (`main.py:35-36`) are computed once at import. A message setting `TAU1` or `TAU2`
  must recompute them — storing the raw value alone is a silent no-op, and it is
  the single most likely way this ships broken.
- **Snapshot under the lock.** The producer loop already snapshots commands under
  `cmd_lock` (`main.py:125`). Parameters join that same snapshot, so no tick ever
  reads half-updated values.
- **Validate against the lexicon `min`/`max`** before applying. Reject out-of-range
  values; never clamp silently.
- **Rebase `heat` when `A_THERMAL` changes.** `temperature = A_THERMAL × heat`
  (`main.py:173`), so a naive write jumps the temperature reading exactly the way a
  `Q_MAX_AH` change would jump SOC. On write, recompute `heat = temperature / A_new`
  so temperature stays continuous and the heat state absorbs the step — the idiom
  `main.py:179` already uses after saturation.

### D2 — `R1`/`R2` defaults corrected to the README's derived values (2026-09-15)

`main.py:31-32` and `app.yaml` shipped `R1 = R2 = 0.0`, which makes the whole
second-order RC circuit inert — `dc_voltage_v == ocv_v` and both RC voltages stay
flat at 0. `README.md:152-153` and its derivation section say `0.1 Ω` / `0.05 Ω` and
justify them physically, and `README.md:64` claims RC dynamics are active out of the
box. The code drifted from its spec; **the README is the intent**. `main.py`,
`app.yaml` and the lexicon move to `0.1` / `0.05` together.

`R0` stays `0.0` — README agrees, and non-zero `R0` engages the quadratic solver.

### D3 — The dashboard reads the lexicon from DCM's REST API (2026-09-15)

Resolves the former OQ-1. The dashboard backend fetches
`GET /api/v1/configurations/{id}/content` at boot and on demand, then serves the
lexicon to its own frontend. It does **not** use `join_lookup` /
`QuixConfigurationService`: that resolves config per record by message key, which
fits an enricher, not an element picker that needs a plain request/response read.
No RocksDB round-trip either — the lexicon is small, read-mostly, and already
durable in DCM, so the `quix-rocksdb-state-api` pattern buys nothing here.

`dc-battery-sim` keeps reading its own `lexicon.json` from disk; it never calls DCM.

### D4 — The dashboard frontend reuses TestManager's stack (2026-09-15)

Reference implementation: `C:\repos\TestManager\Quix.TestManager\frontend`.
Next.js 14 (App Router) + React 18 + TypeScript, shadcn/ui over Radix primitives,
Tailwind, TanStack Table, Playwright for e2e. Reuse its `components/ui` primitives
and `components/layout` patterns rather than re-deriving them; its `app/config-manager`
route is the closest existing analogue to a lexicon editor.

Satisfies the global rule against hand-rolled breakpoints. Two additions Phase 2 needs
that TestManager does not have: a grid layout engine (`react-grid-layout`) and a
streaming-capable chart library — prefer `uPlot` over Recharts at 10 Hz.

### D5 — Grid layouts persist in DCM (2026-09-15)

A layout is versioned JSON config, which is exactly what DCM stores. It survives a
browser change, is shareable between users, and gets version history for free.
Browser storage is per-device and loses work; a dedicated topic reinvents DCM.
Layouts live under their own DCM config type, separate from the lexicon.

### D6 — Chart history is a backend rolling window, not the lakehouse (2026-09-15)

The dashboard backend keeps a short in-memory rolling window of `dashboard-out`
(target 60 s at 10 Hz ≈ 600 samples per signal) and ships it to each client on
connect, so a chart is populated the moment it renders. Live samples stream in after.
No lakehouse read and no topic replay in Phase 2 — revisit only if someone needs
history older than the window.

### D7 — §4 amendments from the Phase 2 design pass (2026-09-15)

Designing the dashboard concretely exposed four contradictions in §4 as originally
written. §4 above is now the corrected version; the changes were: charts take 1..N
bindings, numeric readouts may display parameters read-only, type-in is numeric only,
and the grid gains explicit Edit/View modes.

**Lexicon v1.1** also gains `model.instance_key` — the sim keys its `dashboard-out`
messages `"battery-sim"`, which is not its `model.name` (`"dc-battery-sim"`). Without
this the dashboard needs a hardcoded key per model, which breaks the
model-agnosticism in §1. Until v1.1 ships it is a required env var.

## 8. Open questions — resolve before building, do not guess

*None open. All Phase 1 and Phase 2 design questions are resolved in §7.*

## 9. Working agreements

Global rules in `~/.claude/CLAUDE.md` apply in full. Project-specific emphasis:

- **QuixStreams-first.** Every topic, state store, join and serialisation uses a
  native QS primitive. Load `quixstreams-idioms` before writing QS code. No
  hand-rolled consumer loops, JSON serdes, or config caches.
- **Skills that fire here:** `quix-dcm-join-lookup` (lexicon/config enrichment),
  `quix-rocksdb-state-api` (HTTP-over-State for the dashboard backend),
  `quix-service-create` / `quix-service-update` (any new or changed deployment),
  `quixstreams-idioms` (all QS code). If unsure a skill applies — **ask**.
- **Spec before code.** Buddy writes `dev-planning/<feature>/spec.md`; ArchDev
  implements from it; Tester runs the pre-commit + smoke gate. That gate is never
  skipped.
- **Frontend uses a real responsive framework** (Tailwind / component library) — no
  hand-rolled breakpoints. The grid must survive phone through 4K.
- **Do not edit `dc-battery-sim/` casually.** It is the reference plant and its
  numbers are physically derived. Any change gets a spec and a note here.
