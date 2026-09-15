# Parameter & signal contract — `dashboard-in` / `dashboard-out`

**Status:** Draft
**Project:** dashboard-tests
**Branch:** `devDB`
**Created:** 2026-09-15
**Planned with:** Buddy
**Phase:** 1 — contract + SIL tuning path only. The dashboard web UI is a later spec.

---

## 1. Summary

The dashboard is model-agnostic: it learns what a plant exposes by reading a **lexicon**.
This spec formalises that lexicon into a concrete JSON document, populates it for
`dc-battery-sim`, defines the wire envelope that carries signal *and* parameter writes
over `dashboard-in`, defines what comes back on `dashboard-out`, and lays out the exact
changes `dc-battery-sim/main.py` needs so that the 12 tunable parameters become
live-writable without breaking simulation continuity.

It implements CLAUDE.md §7 **D1** (event-based tuning, partial update, no DCM on the value
path) and stops there. It deliberately does not resolve §8 **Q1** (how the *dashboard*
reads the lexicon) — §6.1 leaves a clean seam for it.

## 2. Goals

- One JSON document shape that any SIL plant can publish to describe itself.
- A populated, valid lexicon for `dc-battery-sim`: 16 signal entries, 14 parameters.
- A `dashboard-in` envelope where signals and parameters coexist with partial-update
  semantics at every level, and where a partial write is unambiguous.
- `dc-battery-sim` applies tunable parameters live, recomputes every derived constant,
  and never lets a tick read half-updated state.
- Out-of-range / malformed writes are rejected, logged, and **never crash the service** —
  today several of them do.
- The dashboard can recover the *effective* value of every signal and parameter after a
  page reload, without a dedicated request/response channel.

## 3. Non-goals

- The dashboard service itself — grid, element rendering, binding picker, frontend stack.
- How the dashboard obtains the lexicon (CLAUDE.md §8 Q1). Sim-side, the lexicon is a
  local file; the dashboard's read path stays open.
- Making `SAMPLE_TIME` or `Q_MAX_AH` tunable, or any rescale path for them.
- Any change to the physics: no default value, LUT anchor, equation or derivation moves.
- A per-message ack / error topic (see §6.7 — deferred, with a rationale).
- Layout persistence (§8 Q2) and chart history depth (§8 Q3).

## 4. User stories

1. **Drive a signal.** User drags a knob, binds it to `requested_power_w`, drags to
   −20 kW. Dashboard publishes `{"signals": {"requested_power_w": -20000.0}}`. Within one
   sample period `dc_current_a` on the chart moves. Nothing else changes.
2. **Tune a parameter.** User binds a knob to `KE`, raises it 1.6 → 6.0. Dashboard
   publishes `{"parameters": {"KE": 6.0}}`. The pack's thermal time constant drops from
   ~52 min to ~14 min and `temperature_c` visibly bends toward ambient faster.
3. **Tune a parameter with a derived constant.** User sets `TAU2` 600 → 60.
   `rc2_voltage_v` starts tracking current ten times faster. (If `ALPHA2` were not
   recomputed this would be a silent no-op — see §6.5.2.)
4. **Reject an out-of-range write.** A hand-crafted message sets `MAX_BATTERY_TEMP` to
   150. The sim logs a warning, keeps 60.0, and keeps running. The value is **not**
   clamped to 60 silently — the write simply did not happen.
5. **Survive a bad message.** A message arrives with `"chiller_setting": 5`, or
   `"requested_power_w": "fast"`, or is a JSON array. The service logs and continues.
   *Today every one of these kills a thread or the whole app (§8 R1).*
6. **Reload the page.** User refreshes the browser mid-run. Within 5 s every control
   element re-renders at the plant's actual current value, not the lexicon default.
7. **Restart the deployment.** Tunable parameters return to their `app.yaml` baseline.
   Intentional, per D1.

## 5. Proposed design

Four artifacts, one code change:

- **`lexicon.json`** ships *inside* `dc-battery-sim/`. It is the single source of truth for
  names, datatypes, units and ranges. The sim loads it at startup and validates every
  incoming write against it; the same file is what gets uploaded to DCM for the dashboard
  to read. One file, no duplicated range tables, and the dashboard's read path stays an
  open question rather than a coupling.
- **Nested `dashboard-in` envelope** — `{"signals": {…}, "parameters": {…}}`. Explicit
  namespaces beat a flat object because the dashboard is generic: with a flat object an
  unknown key is ambiguous (bad signal or bad parameter?) and two plants with a colliding
  name are unfixable. A legacy flat message is still accepted as signals-only (§6.3.3).
- **`dashboard-out` keeps its 13 flat fields** and gains one optional `applied` block,
  emitted only after a change and as a 5 s heartbeat. This closes user story 6 and gives
  rejection an implicit NACK without building an ack topic.
- **`main.py` keeps one `handle_command` on the existing SDF.** A `params` dict joins
  `cmd` under one lock, both keyed by lexicon `name`, both fed by one generic
  `apply_updates`. No second consumer, no config cache, no `try`/`except` in the pipeline.

## 6. Work breakdown

### 6.1 Lexicon JSON schema

**Owner:** ArchDev (as a checked-in file) · **Depends on:** nothing

A lexicon is one JSON object with four top-level keys.

| Key | Type | Meaning |
|---|---|---|
| `lexicon_version` | string `"<major>.<minor>"` | Schema version of *this document shape*. The dashboard refuses an unknown **major**. |
| `model` | object | `{name, label, description, version}` — identifies the plant and its own build. |
| `signals` | array of descriptor | Streamed values. |
| `parameters` | array of descriptor | Model constants. |

**Arrays, not maps — and this is load-bearing.** `requested_power_w` and `ambient_temp_c`
exist as *both* an input signal and an output echo. A map keyed by `name` cannot hold
both. So:

- `signals` is unique on the composite key **(`name`, `direction`)**.
- `parameters` is unique on **`name`**.
- A `name` must not appear in both collections.
- An element binding therefore stores `{collection, name, direction}`, not just `name`.

This costs the dashboard one extra field per binding and changes no wire bytes. The §4
picker rule is unaffected — filtering by `direction` still produces exactly the right
candidate list, and a name legitimately appearing in both lists means "you may drive it
*and* chart it".

**Every descriptor carries all eleven keys, always.** Inapplicable keys are explicitly
`null`, never absent — absent-vs-null ambiguity is a reliable source of UI bugs.

| Field | Type | Signals | Parameters |
|---|---|---|---|
| `name` | string, `^[A-Za-z_][A-Za-z0-9_]*$` | required | required |
| `label` | string, non-empty | required | required |
| `description` | string | required | required |
| `datatype` | `bool`\|`uint`\|`int`\|`float`\|`enum` | required | required |
| `unit` | string \| `null` | `null` = dimensionless | `null` = dimensionless |
| `default` | number \| bool \| string | startup value | deployment baseline |
| `min` / `max` | number \| `null` | see R1–R3 | see R1–R3 |
| `enum` | array of `{value, label}` \| `null` | see R1 | see R1 |
| `direction` | `input` \| `output` | **required** | always `null` |
| `tunable` | boolean | always `null` | **required** |

**Semantics of `default`.** For an **input signal** and a **parameter** it is the value the
plant starts with — i.e. it must equal the `app.yaml` `defaultValue` of the corresponding
env var (D1: the env var supplies the startup default). For an **output signal** it is the
value at the documented initial condition, and serves only as a render placeholder before
the first message arrives.

**Semantics of `min`/`max`.** On an input signal or tunable parameter they are a
**validation contract**: enforced client-side by the dashboard (§4) and again server-side
by the plant (§6.7). On an output signal they are a **display hint** only — a chart axis
range. A plant may legitimately emit outside them (e.g. during a transient); the dashboard
must not treat that as an error.

**Load-time rules (not expressible in the schema block below; the loader enforces them):**

- **R1** `datatype: enum` ⇒ `enum` is a non-empty array, `min` and `max` are `null`.
- **R2** `datatype` in {`uint`, `int`, `float`} ⇒ `min` and `max` are numbers, `min ≤ max`,
  `enum` is `null`.
- **R3** `datatype: bool` ⇒ `min`, `max`, `enum` all `null`.
- **R4** `default` satisfies `min ≤ default ≤ max`, or is a member value of `enum`.
- **R5** `uint` ⇒ `min ≥ 0`.
- **R6** uniqueness as above.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://quix.io/schemas/sil-lexicon/1.0.json",
  "title": "SIL plant lexicon",
  "type": "object",
  "required": ["lexicon_version", "model", "signals", "parameters"],
  "additionalProperties": false,
  "properties": {
    "lexicon_version": { "type": "string", "pattern": "^\\d+\\.\\d+$" },
    "model": {
      "type": "object",
      "required": ["name", "label", "description", "version"],
      "additionalProperties": false,
      "properties": {
        "name":        { "type": "string" },
        "label":       { "type": "string" },
        "description": { "type": "string" },
        "version":     { "type": "string" }
      }
    },
    "signals":    { "type": "array", "items": { "$ref": "#/$defs/signal" } },
    "parameters": { "type": "array", "items": { "$ref": "#/$defs/parameter" } }
  },
  "$defs": {
    "enumMember": {
      "type": "object",
      "required": ["value", "label"],
      "additionalProperties": false,
      "properties": {
        "value": { "type": ["integer", "string", "boolean"] },
        "label": { "type": "string" }
      }
    },
    "descriptor": {
      "type": "object",
      "required": ["name", "label", "description", "datatype", "unit",
                   "default", "min", "max", "enum", "direction", "tunable"],
      "additionalProperties": false,
      "properties": {
        "name":        { "type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$" },
        "label":       { "type": "string", "minLength": 1 },
        "description": { "type": "string" },
        "datatype":    { "enum": ["bool", "uint", "int", "float", "enum"] },
        "unit":        { "type": ["string", "null"] },
        "default":     { "type": ["number", "boolean", "string"] },
        "min":         { "type": ["number", "null"] },
        "max":         { "type": ["number", "null"] },
        "enum":        { "type": ["array", "null"],
                         "minItems": 1,
                         "items": { "$ref": "#/$defs/enumMember" } },
        "direction":   { "enum": ["input", "output", null] },
        "tunable":     { "type": ["boolean", "null"] }
      }
    },
    "signal": {
      "allOf": [
        { "$ref": "#/$defs/descriptor" },
        { "properties": { "direction": { "enum": ["input", "output"] },
                          "tunable":   { "type": "null" } } }
      ]
    },
    "parameter": {
      "allOf": [
        { "$ref": "#/$defs/descriptor" },
        { "properties": { "direction": { "type": "null" },
                          "tunable":   { "type": "boolean" } } }
      ]
    }
  }
}
```

> **Seam for §8 Q1.** Nothing above says *where* the document comes from. The sim reads a
> local file (`LEXICON_PATH`, §6.6). The dashboard's read path — DCM REST, or the
> `quix-rocksdb-state-api` pattern over the config topic — plugs in here and changes
> nothing else. Keep it that way.

### 6.2 The battery model's lexicon instance

**Owner:** ArchDev · **Depends on:** 6.1

The populated document is **`dev-planning/parameter-contract/lexicon-battery.json`**
(valid, complete, 16 signals + 14 parameters). ArchDev copies it to
**`dc-battery-sim/lexicon.json`** — that is the file the service ships and loads. The
spec copy is the review artifact; the service copy is the live one. If they ever diverge,
the service copy wins and the spec copy gets updated.

`timestamp` is **not** a lexicon entry. It is message metadata, not a bindable signal.

Everything is sourced from `dc-battery-sim/README.md`. Fields README does not give, and
the justification for each:

| Entry | Field | Chosen | Justification |
|---|---|---|---|
| `requested_power_w` | `min`/`max` | ±250 000 W | The KT2 derivation's reference point is \|I\| = 300 A; at the 780 V nominal that is ±234 kW. Rounded up to a clean ±250 kW. |
| `ambient_temp_c` | `min`/`max` | −40 / 60 °C | −40 clears the derating LUT's fully-derated cold anchor (−30 °C) with margin; +60 equals `MAX_BATTERY_TEMP` so ambient can never drive the pack past its own saturation. |
| `chiller_setting`, `heater_setting` | `enum` labels | Off / Low (2.5 kW) / High (5 kW) | README "Chiller / Heater power map". |
| `q_act_as` | `max` | 360 000 A·s | `Q_MAX_AH` default 100 × 3600. Exact. |
| `ocv_v` | `min`/`max` | 720 / 840 V | The OCV LUT endpoints. Exact. |
| `dc_voltage_v` | `min`/`max` | 600 / 900 V | OCV band ±120 V, headroom for R0 and RC drops at extreme current. Display hint only. |
| `dc_current_a` | `min`/`max` | ±400 A | ±250 kW at the 720 V low end ≈ ±347 A, rounded up. |
| `rc1_voltage_v` | `min`/`max` | ±50 V | 400 A × R1's documented 0.1 Ω = 40 V, rounded up. |
| `rc2_voltage_v` | `min`/`max` | ±25 V | 400 A × R2's documented 0.05 Ω = 20 V, rounded up. |
| `temperature_c` | `min`/`max` | −40 / 60 °C | Lower = ambient minimum; upper = `MAX_BATTERY_TEMP` maximum. |
| `heat_j` | `min`/`max` | −200 000 / 300 000 J | `Heat = T / A_THERMAL`: 60/0.0002 and −40/0.0002. Exact. |
| output `default`s | all | README's example payload | The documented first sample: SOC 50 %, q 180 000, OCV 780, I −10.26, T 20, Heat 100 000, d 1.0. Not invented. |
| `Q_MAX_AH` | `min`/`max` | 1 / 1000 Ah | Informational — it is fixed, so no control ever uses these. Spans plausible pack capacities. |
| `SAMPLE_TIME` | `min`/`max` | 0.001 / 1.0 s | Informational — fixed. 1 kHz to 1 Hz. |
| `A_THERMAL` | `min` | 0.00002 °C/J | **Must be strictly positive.** `main.py:179` divides by it. Zero raises `ZeroDivisionError` in the daemon thread and the sim dies silently. `max` 0.002 = 10× default. |
| `KT0` | `min`/`max` | ±5000 W | Same order as the 5 kW heater/chiller, so it can plausibly offset one. |
| `KT1` | `min`/`max` | ±10 W/A | At the 300 A reference that is ±3 kW — the same order as the KT2 term there (0.0278 × 300² = 2502 W). |
| `KT2` | `min`/`max` | 0 / 0.28 W/A² | Negative is unphysical (current cooling the pack). 0.28 = 10× default → 300 A gives +5 °C/s, past any real pack. |
| `KE` | `min`/`max` | 0 / 16 W/°C | 0 = perfectly insulated. 16 = 10× default → τ ≈ 5.2 min instead of 52 min. |
| `TAU1` | `min`/`max` | 0 / 60 s | 0 disables the lag (`main.py:35` already handles τ ≤ 0 → α = 0). 60 s is where "fast charge-transfer" stops being distinguishable from RC2. |
| `TAU2` | `min`/`max` | 0 / 3600 s | 0 disables. 1 h is the upper end of solid-state diffusion time scales. |
| `R0`, `R1`, `R2` | `min`/`max` | 0 / 0.5 Ω | Negative resistance is unphysical. 0.5 Ω at 300 A is a 150 V drop (~20 % of a 780 V pack); beyond that the quadratic solver's root selection in `solve_dc_current` gets marginal. |
| `COOLANT_TEMP` | `min`/`max` | −20 / 40 °C | −20 is the LUT's full-power cold anchor. Above ~40 °C it would clamp the pack hotter than nominal ambient. |
| `MAX_BATTERY_TEMP` | `min`/`max` | 25 / 60 °C | **Upper bound is 60 for a real reason:** `DERATING_LUT` (`main.py:45-50`) returns 0.0 at and above 60 °C and is *not* in the lexicon, so any clamp above 60 leaves the pack permanently derated to zero current. Lower bound 25 keeps it clear of `COOLANT_TEMP`'s 20 °C default so the two clamps cannot cross. |

**`R0`/`R1`/`R2` `default` = 0.0 — flagged, see §8 OQ-2.** The README parameter table and
its "Derived RC parameter values" section both say R1 = 0.1 Ω and R2 = 0.05 Ω, but
`main.py:31-32` and `app.yaml` both default them to 0.0. The lexicon mirrors
**`app.yaml`**, because D1 defines the env var as the startup default and a lexicon that
disagrees with the running deployment makes every knob lie. Resolving the divergence is a
one-line `app.yaml` change and a decision for the user, not for this spec.

### 6.3 `dashboard-in` message envelope

**Owner:** ArchDev (sim side) + a later dashboard spec (producer side) · **Depends on:** 6.1

#### 6.3.1 Shape — nested

```json
{
  "signals":    { "<input signal name>": <value>, ... },
  "parameters": { "<tunable parameter name>": <value>, ... },
  "meta":       { }
}
```

`signals`, `parameters` and `meta` are **reserved top-level names**; a plant may not have
a signal or parameter called any of them.

**Partial update holds at every level:**

| Level | Absent means |
|---|---|
| top-level key (`signals` / `parameters`) | that entire collection is untouched |
| inner key | that entry is untouched |

There is no "unset" and no null semantics: `{"parameters": {"KE": null}}` is a rejected
value (§6.7), not a reset. A restart is the only way back to the baseline (D1).

**Why nested over flat.** A flat object works *today* only because the battery's signal
names are lowercase and its parameter names uppercase — an accident of this one model, not
a contract. Flat also makes rejection reporting ambiguous: an unknown key could be a
misspelled signal or a misspelled parameter, and the sim cannot say which. Nested costs
one extra level of JSON and makes the message self-describing for a generic dashboard.
`meta` is reserved now (request ids, correlation) and unused in Phase 1.

#### 6.3.2 Keying — not optional

**`dashboard-in` messages MUST be produced with a fixed key identifying the plant
instance.** Phase 1 uses the literal `"battery-sim"`, matching the key the sim already
produces with (`main.py:197`). Partial update is last-write-wins, and last-write-wins
requires a total order; unkeyed messages round-robin across partitions and a
`parameters` write can overtake a `signals` write. This is the kind of bug that shows up
once a week and is never reproduced.

#### 6.3.3 Legacy flat form

The README's documented input payload is flat. The sim accepts it: **if a message contains
no `signals` object and no `parameters` object, the whole message — minus the three
reserved keys — is treated as `signals`.** Three lines of code; keeps the README's
contract and any existing `ui-data` producer working. Deprecate in Phase 2 once the
dashboard is the only producer.

#### 6.3.4 Worked examples

Signal only:
```json
{ "signals": { "requested_power_w": -20000.0 } }
```

Parameter only (two at once; `ALPHA2` is recomputed from `TAU2`):
```json
{ "parameters": { "TAU2": 60.0, "R2": 0.05 } }
```

Mixed:
```json
{
  "signals":    { "chiller_setting": 1, "ambient_temp_c": 30.0 },
  "parameters": { "KE": 6.0 }
}
```

Legacy flat (still accepted, treated as signals):
```json
{ "requested_power_w": -8000.0, "ambient_temp_c": 15 }
```

Note the last example sends an **integer** for a `float` signal. That is valid — see §6.7.

### 6.4 `dashboard-out` payload

**Owner:** ArchDev · **Depends on:** 6.2

**The existing 13 flat fields are unchanged and sufficient for charting.** No field is
renamed, removed or re-typed.

They are **not** sufficient for control-element state, in two specific ways:

1. `requested_power_w` and `ambient_temp_c` are echoed but `chiller_setting` and
   `heater_setting` are not. A switch bound to the chiller has no source of truth.
2. **No parameter value is ever published.** A knob bound to `KE` can only show the
   lexicon `default`, which is stale the moment anyone tunes it — including for the user
   who tuned it, after a reload.

#### 6.4.1 Addition: one optional `applied` block

`dashboard-out` messages gain **one** optional top-level key:

```json
{
  "timestamp": "...", "soc_percent": 50.0, "...": "...13 fields as today...",
  "applied": {
    "signals":    { "requested_power_w": -20000.0, "ambient_temp_c": 30.0,
                    "chiller_setting": 1, "heater_setting": 0 },
    "parameters": { "Q_MAX_AH": 100.0, "SAMPLE_TIME": 0.1, "A_THERMAL": 0.0002,
                    "KT0": 0.0, "KT1": 0.0, "KT2": 0.0278, "KE": 6.0,
                    "TAU1": 1.0, "TAU2": 60.0, "R0": 0.0, "R1": 0.0, "R2": 0.05,
                    "COOLANT_TEMP": 20.0, "MAX_BATTERY_TEMP": 60.0 }
  }
}
```

Emission rule — `applied` is present on a tick if **any** of:
- it is the first tick after startup;
- a `dashboard-in` write was accepted since the last emission;
- `APPLIED_ECHO_PERIOD_S` (default 5.0 s) has elapsed since the last emission.

Otherwise the key is **absent**. Steady-state cost is 2 messages per minute out of 600.

Why each rule:
- *first tick* — a dashboard attached at start gets the baseline without asking.
- *after a change* — closes the loop. Crucially, **a rejected write produces no change,
  so the echoed value stays put**: the dashboard sees its optimistic knob position snap
  back. That is an implicit NACK, which is why §6.7 can defer a real ack channel.
- *5 s heartbeat* — bounds staleness for a dashboard that attaches late or reloads
  (user story 6) without any request/response machinery.

`applied` adds **no lexicon entries**. Every name in it is already described in
`lexicon.json` — the block carries *values*, and `Q_MAX_AH` / `SAMPLE_TIME` appear there
read-only so the dashboard can display fixed parameters (§4 permits that; it forbids only
*binding a control* to them).

### 6.5 `dc-battery-sim` changes

**Owner:** ArchDev · **Depends on:** 6.2, 6.3, 6.4 · **Verified by:** Tester

All in `dc-battery-sim/main.py`. **Stay in one file** — per `quixstreams-idioms` §0 a
service splits only past 300 lines of *code* with a real seam. Projection: ~233 → ~330
total lines, ~255 code lines. If ArchDev's actual result passes 300 code lines, the seam
is `params.py` (lexicon load + `coerce` + `apply_updates` + `_recompute_derived`), and
nothing else.

#### 6.5.1 `params` alongside `cmd`, under **one** lock

Rename `cmd_lock` → `state_lock` and let it cover both dicts. **One lock, not two** — a
tick must snapshot setpoints and parameters together or it can read a torn pair (e.g. new
`TAU2` with old `ALPHA2`).

Also **re-key `cmd` to lexicon wire names**: `requested_power` → `requested_power_w`,
`ambient_temp` → `ambient_temp_c`. Both dicts then key on lexicon `name`, and one generic
`apply_updates` serves both instead of a hand-written `if`-ladder.

```python
LEXICON_PATH = os.getenv("LEXICON_PATH", str(Path(__file__).with_name("lexicon.json")))
LEXICON      = json.loads(Path(LEXICON_PATH).read_text(encoding="utf-8"))
PARAM_SPEC   = {p["name"]: p for p in LEXICON["parameters"]}
SIGNAL_SPEC  = {s["name"]: s for s in LEXICON["signals"] if s["direction"] == "input"}

state_lock = threading.Lock()

cmd = {name: _typed_env(SIGNAL_SPEC[name], os.getenv(_ENV_FOR[name]))
       for name in SIGNAL_SPEC}
params = {name: float(os.getenv(name, str(spec["default"])))
          for name, spec in PARAM_SPEC.items()}
```

`_ENV_FOR` maps the four input signals to their existing env var names
(`requested_power_w` → `REQUESTED_POWER`, `ambient_temp_c` → `AMBIENT_TEMP`,
`chiller_setting` → `CHILLER_SETTING`, `heater_setting` → `HEATER_SETTING`). Those names
are *not* the wire names and renaming them would be a deployment change for no gain —
keep the map, it is four lines.

Note `params` holds **all 14** parameters including the two fixed ones, so the §6.4
`applied` block is a straight copy. Fixed ones are simply never writable (§6.7).

#### 6.5.2 Derived-constant recompute — the one that ships broken

`ALPHA1`/`ALPHA2` (`main.py:35-36`) stop being module constants and live in `params`
under private keys:

```python
def _recompute_derived():
    """ALPHA_i = exp(-Δt / τ_i). TAU is tunable, so this runs after EVERY parameter write —
    unconditionally, because 'did I remember to check the TAU flag' is the bug D1 predicts."""
    for i in (1, 2):
        tau = params[f"TAU{i}"]
        params[f"_alpha{i}"] = math.exp(-SAMPLE_TIME / tau) if tau > 0.0 else 0.0
```

Called once at import and at the end of every parameter apply, **inside the lock**,
unconditionally. Two `exp()` per message at human-clicking rates is free, and it deletes
an entire class of bug. `SAMPLE_TIME` is fixed, so it is safe to read as a module constant
here.

`tau > 0.0` preserves the existing guard exactly. A very small non-zero τ underflows
`exp()` to 0.0 without raising, which is the correct behaviour anyway.

#### 6.5.3 Per-tick snapshot

Replace `main.py:125-129` with a snapshot of *both* dicts:

```python
with state_lock:
    setpoints = dict(cmd)
    p         = dict(params)
    echo_due  = _take_echo_flag()      # see 6.5.6
```

Then every parameter read inside the loop body becomes `p["NAME"]` — `p["KT2"]`,
`p["KE"]`, `p["R1"]`, `p["_alpha1"]`, `p["COOLANT_TEMP"]`, `p["MAX_BATTERY_TEMP"]`, and
so on. **No module-level tunable may be read inside the loop after this change.**

`Q_MAX` and `SAMPLE_TIME` stay module constants and are still read directly. That is the
point of them being fixed.

#### 6.5.4 `solve_dc_current` must take `r0`

`solve_dc_current` (`main.py:83-104`) reads the module global `R0` on lines 93 and 96–101.
Once `R0` is tunable that is a read **outside** the snapshot — the function could see a
different `R0` than the tick that called it. Change the signature to
`solve_dc_current(power, ocv, v_rc1, v_rc2, r0)` and pass `p["R0"]`. Its docstring names
R0 explicitly; rewrite the docstring in the same edit.

`ocv_lookup` reads `Q_MAX`, which is fixed — leave it alone.
`derating_lookup` reads `DERATING_LUT`, which is not in the lexicon — leave it alone.

#### 6.5.5 `A_THERMAL` continuity — needs a decision

`T = A_THERMAL × Heat`, and `main.py:179` re-derives `heat = temperature / A_THERMAL`
every tick. So changing `A_THERMAL` between ticks **steps `temperature_c` by the ratio of
old to new**. That is exactly the property D1 uses to justify making `Q_MAX_AH` fixed, yet
`A_THERMAL` is listed as tunable (§8 R2).

Recommended rule — rebase `heat` so the observable does not jump:

```python
# A_THERMAL is tunable and T = A·Heat. Rebase Heat on change so the temperature
# reading is continuous; without this a knob nudge teleports the pack's temperature.
if p["A_THERMAL"] != a_thermal_prev:
    heat           = temperature / p["A_THERMAL"]
    a_thermal_prev = p["A_THERMAL"]
```

`heat` and `temperature` are locals of `run_simulation`, so this must live in the loop
(right after the snapshot), not in `handle_command`. `a_thermal_prev` is one new local
initialised alongside them.

This changes no default and no equation — it only defines what happens on an event that
did not previously exist. **It still needs user sign-off (§8 OQ-3).** The alternative is
to do nothing and accept the step, which is defensible if `A_THERMAL` is understood as
"re-scale the thermal mass" rather than "adjust a coefficient". ArchDev must not choose:
if OQ-3 is unresolved, implement the rebase and leave the rule in one clearly-commented
block so removing it is a three-line revert.

#### 6.5.6 `handle_command` rewrite

The existing four-field `if`-ladder (`main.py:221-231`) is **deleted and replaced**. Its
behaviour is preserved for the legacy flat form and extended to parameters.

```python
def is_command(value):
    """Shape gate. A non-dict payload would raise inside handle_command, and an exception
    in sdf.update takes the whole application down — filter it, never try/except it."""
    return isinstance(value, dict)


def handle_command(value):
    signals    = value.get("signals")
    parameters = value.get("parameters")
    if not isinstance(signals, dict) and not isinstance(parameters, dict):
        # Legacy flat form, as documented in README "Input — ui-data".
        signals = {k: v for k, v in value.items()
                   if k not in ("signals", "parameters", "meta")}
        parameters = None

    with state_lock:
        changed = False
        if isinstance(signals, dict):
            changed |= apply_updates(cmd, SIGNAL_SPEC, signals, "signal")
        if isinstance(parameters, dict):
            changed |= apply_updates(params, PARAM_SPEC, parameters, "parameter")
            _recompute_derived()
        if changed:
            _set_echo_flag()          # makes the next tick emit "applied" (§6.4.1)


sdf = sdf.filter(is_command).update(handle_command)
```

`_set_echo_flag` / `_take_echo_flag` are a single module-level boolean read and cleared
under `state_lock` — no extra primitive.

#### 6.5.7 Globals: what moves, what stays

| Global | `main.py` | Becomes |
|---|---|---|
| `Q_MAX` | `:17` | **stays a constant** — fixed, derived from `Q_MAX_AH` at import |
| `SAMPLE_TIME` | `:18` | **stays a constant** — fixed |
| `A_THERMAL` | `:19` | → `params["A_THERMAL"]` |
| `KT0 KT1 KT2 KE` | `:21-24` | → `params[...]` |
| `TAU1 TAU2` | `:26-27` | → `params[...]` |
| `R0 R1 R2` | `:30-32` | → `params[...]` |
| `ALPHA1 ALPHA2` | `:35-36` | → `params["_alpha1"]` / `params["_alpha2"]`, **derived, never written directly** |
| `COOLANT_TEMP MAX_BATTERY_TEMP` | `:41-42` | → `params[...]` |
| `CHILLER_POWERS HEATER_POWERS` | `:38-39` | **stay constants** — not in the lexicon |
| `DERATING_LUT` | `:45-50` | **stays a constant** — not in the lexicon (see §8 R3) |
| `cmd` / `cmd_lock` | `:53-59` | re-keyed to wire names; lock renamed `state_lock` |

#### 6.5.8 Logging

Replace the bare `print(payload)` at `main.py:200`. At `SAMPLE_TIME = 0.1` it writes ten
lines per second forever, which makes the deployment log useless for spotting the
rejection warnings this spec adds. Use `logging` with `LOG_LEVEL` (§6.6):

- `logger.debug(payload)` for the per-tick payload.
- A `[STARTUP]` `logger.info` block listing the effective value of all 14 parameters and
  the lexicon path — per the `quix-service-update` skill, so the deployment log shows
  which config actually loaded.
- `logger.warning` for every rejection (§6.7).

#### 6.5.9 Comment / doc hygiene

Blocks whose comments must be rewritten **in the same edit**, not left behind:

- `main.py:16` `# --- Parameters (overridable via env vars) ---` → env vars are now the
  *startup baseline*; 12 of the 14 are also live-tunable.
- `main.py:34` `# Pre-compute discrete-time filter coefficients (ZOH exact)` → no longer
  pre-computed at import only.
- `main.py:52` `# --- Mutable command state ... updated live from ui-data topic ---` →
  now two dicts, one lock, topic is `dashboard-in`.
- `solve_dc_current` docstring (`:84-89`) → signature gained `r0`.
- `run_simulation` docstring (`:108-116`) → state variable list is unchanged, but
  `a_thermal_prev` joins it if §6.5.5 lands.
- `dc-battery-sim/README.md`: the **Parameters** table gains a "Tunable" column; the
  **Input — `ui-data`** section is replaced by the §6.3 envelope; the **Output** section
  gains `applied`; the **Architecture** diagram says "cmd dict (protected by a
  `threading.Lock`)" and must say `cmd` + `params` under `state_lock`.

### 6.6 `app.yaml` / deployment changes

**Owner:** ArchDev · **Depends on:** 6.5 · Walk the `quix-service-update` checklist.

| Layer | Change |
|---|---|
| `dc-battery-sim/main.py` | §6.5. Reads two new env vars. |
| `dc-battery-sim/lexicon.json` | **new file**, copied from §6.2. |
| `dc-battery-sim/requirements.txt` | **no change** — `json`, `logging`, `pathlib` are stdlib; `quixstreams==3.23.1` stays. |
| `dc-battery-sim/dockerfile` | **no change** — `COPY . .` (line 22) already ships `lexicon.json`, and there is no `.dockerignore`. ArchDev should confirm, not assume. |
| `dc-battery-sim/app.yaml` | below. |
| root `quix.yaml` | **does not exist yet** (CLAUDE.md §5). When created, its `dc-battery-sim` deployment block must carry `value:` entries for every variable below, or the running deployment never receives them. |
| `.env` / `.env.example` | below. |
| Portal | after push, rebuild + redeploy (code changed, not just values). |

**`app.yaml` variable changes — no variable is removed:**

1. **Add `LEXICON_PATH`** — `FreeText`, default `lexicon.json`, not required.
   "Path to the signal/parameter lexicon. Also the seam for sourcing it from DCM later."
2. **Add `LOG_LEVEL`** — `FreeText`, default `INFO`, not required.
   "`DEBUG` emits the full payload every tick (10 lines/s)."
3. **Add `APPLIED_ECHO_PERIOD_S`** — `FreeText`, default `5`, not required.
   "How often the full applied signal/parameter set is echoed on the output topic."
4. **Rewrite the `description` of all 14 parameter variables** so the Portal tells the
   truth about which are live:
   - `Q_MAX_AH`, `SAMPLE_TIME`: prefix **"FIXED — deploy-time only, not tunable at
     runtime."**
   - the other 12: suffix **"Startup default; tunable live over `dashboard-in`."**
5. **Point the topics at the dashboard pipeline.** `input`/`output` still default to
   `ui-data`/`battery-data`; per CLAUDE.md §2 this deployment's values must be
   `dashboard-in` and `dashboard-out`. Set them as the `defaultValue` in `app.yaml` and as
   `value:` in `quix.yaml` when it is created.
6. **`.env` has a dangling pair.** It declares `dashboard_in` / `dashboard_out`, which
   *nothing reads* — `main.py:12-13` reads `output` and `input`. Add to `.env` and
   `.env.example`, using the interpolation already proven there for the token:
   ```
   input=${dashboard_in}
   output=${dashboard_out}
   ```
7. **Variable names stay alphanumeric + `_`.** All proposed names comply; the topic
   *values* keep their hyphens, which is fine.

**Do not add env vars for the `applied` block contents, the enum power maps, or the
derating LUT.** They are not deployment config.

### 6.7 Error and rejection semantics

**Owner:** ArchDev · **Verified by:** Tester (these are the red-first test cases)

**Granularity: field-level, best-effort.** A bad field is dropped; every other field in the
same message still applies. Partial update is field-granular by definition, the dashboard
already enforces `min`/`max` client-side so rejections are rare or adversarial, and
all-or-nothing without an ack channel means a silently discarded message — strictly worse
for the user. Per-message atomicity is the Phase 2 upgrade, once acks exist.

**The rule that governs all of it: nothing in this path may raise.** `handle_command` runs
inside `sdf.update`; an exception there takes down `consumer_app.run()` and the whole
service. Per `quixstreams-idioms` §"No try/except in the pipeline", the shape check is a
`filter` *before* the step (`is_command`, §6.5.6) and everything inside uses explicit
checks — **no `try`/`except` anywhere in `handle_command`, `coerce` or `apply_updates`.**

| Case | Sim behaviour | Log | To dashboard |
|---|---|---|---|
| Message is not a JSON object | dropped by `is_command` filter | `WARNING` once per 1000 drops | nothing |
| Unknown name in `signals` or `parameters` | field ignored, rest of message applies | `WARNING` "unknown name" | nothing |
| Name is an **output** signal | field ignored — `SIGNAL_SPEC` only holds `direction: input` | `WARNING` "not writable" | nothing |
| Parameter with `tunable: false` (`SAMPLE_TIME`, `Q_MAX_AH`) | field ignored | `WARNING` "fixed at deploy time" | nothing |
| Wrong datatype (`"fast"`, `null`, a list, an object) | field ignored, old value kept | `WARNING` with received value and expected datatype | nothing |
| Out of `[min, max]` | field ignored, old value kept. **Never clamped** (D1). | `WARNING` with the value and the bounds | nothing |
| Enum value not in the allowed set | field ignored, old value kept | `WARNING` with the allowed set | nothing |
| Valid | applied | `INFO`, one line per accepted field | `applied` block on the next tick |

**Datatype rules — the non-obvious ones:**

- **`bool` is an `int` subclass in Python.** `isinstance(True, int)` is `True`, so `True`
  would sail through a numeric check and end up as `1` in `KT2`. Reject `bool` explicitly
  *before* any numeric check, for every non-`bool` datatype.
- **An `int` is acceptable for a `float`.** The README's own example sends
  `"ambient_temp_c": 15`. Accept `int` where `float` is expected and store `float(raw)`.
  The reverse is not true: `0.5` for an `int`/`uint` is a rejection.
- **No string coercion.** Today `float(value["requested_power_w"])` accepts `"−8000"`.
  After this change a string is a rejection. This is a deliberate narrowing: the dashboard
  sends typed JSON, and silent string coercion is how a type-in field's stray character
  becomes a physics value. It is also what makes "no `try`/`except`" possible.
- **Enum values are canonicalised.** Membership is checked against the descriptor's
  `enum`, and the stored value is the member's `value` — so a wire `1.0` is stored as `1`.
  This matters: `CHILLER_POWERS` / `HEATER_POWERS` are dicts keyed by `int`.

```python
def coerce(spec, raw):
    """Return (ok, value). Must never raise — it runs inside sdf.update."""
    if spec["datatype"] == "bool":
        return (isinstance(raw, bool), raw)
    if isinstance(raw, bool):                      # bool subclasses int — reject first
        return (False, None)
    if spec["datatype"] == "enum":
        for member in spec["enum"]:
            if raw == member["value"]:
                return (True, member["value"])     # canonical form, not the wire form
        return (False, None)
    if not isinstance(raw, (int, float)):
        return (False, None)
    if spec["datatype"] in ("int", "uint") and not isinstance(raw, int):
        return (False, None)
    value = float(raw) if spec["datatype"] == "float" else int(raw)
    return (spec["min"] <= value <= spec["max"], value)
```

**Feedback channel: not in Phase 1, and here is why that is acceptable.** A dedicated ack
topic or error stream would need a correlation id, a dashboard-side subscription and a
toast/undo UX — a real feature, and the dashboard does not exist yet. The `applied` block
(§6.4.1) substitutes for it: a rejected write produces no change, so the echo shows the
old value and the dashboard's optimistic knob snaps back within one sample period. The
user sees *that* the write did not take; they do not see *why* — that is the gap, and it
is what `meta.request_id` plus an ack topic would close in Phase 2. Until then the reason
is in the deployment log.

## 7. Data & interface contracts — summary

| Contract | Defined in | Artifact |
|---|---|---|
| Lexicon document shape | §6.1 | JSON Schema block, `$id` `sil-lexicon/1.0` |
| Battery lexicon instance | §6.2 | `dev-planning/parameter-contract/lexicon-battery.json` → `dc-battery-sim/lexicon.json` |
| `dashboard-in` envelope | §6.3 | nested `{signals, parameters, meta}`, keyed `"battery-sim"` |
| `dashboard-out` payload | §6.4 | 13 flat fields (unchanged) + optional `applied` |
| New env vars | §6.6 | `LEXICON_PATH`, `LOG_LEVEL`, `APPLIED_ECHO_PERIOD_S` |
| Rejection behaviour | §6.7 | field-level, log-only, never raises |

## 8. Risks, constraints, and open questions

### Risks found in the existing code

- **R1 — three live crash paths that this spec closes, and Tester should red-test first.**
  All are reachable from a single malformed `dashboard-in` message today:
  1. `"chiller_setting": 5` → `CHILLER_POWERS[5]` (`main.py:131`) raises `KeyError` in the
     **daemon producer thread**. The thread dies, `consumer_app.run()` keeps running, the
     deployment stays green — and publishes nothing. This is the silent-failure class that
     `quixstreams-idioms` says is worth a guard.
  2. `"requested_power_w": "fast"` → `float(...)` (`main.py:224`) raises `ValueError`
     inside `sdf.update` → the whole application goes down.
  3. A JSON array or scalar payload → `"requested_power_w" in value` raises `TypeError`,
     same path.
- **R2 — `A_THERMAL` contradicts D1's own criterion.** D1 defines *fixed* as "anything
  whose mid-run change would break simulation continuity", and cites `Q_MAX_AH` because
  SOC jumps. `A_THERMAL` has exactly that property for temperature (`T = A × Heat`), yet
  §3 lists it as tunable. Either the rebase in §6.5.5 lands, or `A_THERMAL` should be
  reclassified fixed. Do not ship it tunable with no rebase. → **OQ-3**.
- **R3 — `MAX_BATTERY_TEMP` is coupled to a LUT that is not tunable.** `DERATING_LUT`
  hard-codes 0.0 derating at 60 °C. Raising `MAX_BATTERY_TEMP` above 60 produces a pack
  that clamps at, say, 80 °C and therefore carries zero current forever — a knob that
  bricks the sim. Mitigated by capping `max` at 60.0 (§6.2). The LUT anchors are not in
  the lexicon and stay that way in Phase 1.
- **R4 — cross-parameter constraints have no home.** `MAX_BATTERY_TEMP > COOLANT_TEMP`
  must hold for the two clamps not to cross, and the lexicon schema expresses only
  per-field ranges. Rejecting on a cross-field rule is wrong here: two separate partial
  messages can legitimately pass through a transiently-invalid pair. **Recommendation:**
  log a `WARNING` when the snapshot violates it, do not reject. A constraints section in
  lexicon v1.1 is the proper fix.
- **R5 — `applied` echo vs. chart consumers.** Any downstream consumer of `dashboard-out`
  that assumes a fixed key set will now see an extra key on ~2 messages per minute.
  Nothing consumes it today; note it before a sink is attached.
- **R6 — unkeyed `dashboard-in` reorders writes.** See §6.3.2. Cheap to get right now,
  expensive to diagnose later.

### Open questions

- **OQ-1 (inherited, CLAUDE.md §8 Q1) — how does the *dashboard* read the lexicon?**
  Deliberately unresolved. The sim reads a local file; nothing in §6.1 constrains the
  dashboard's path. Resolve before the dashboard spec.
- **OQ-2 — RESOLVED 2026-09-15: adopt the README's derived values, `R1 = 0.1`,
  `R2 = 0.05`.** The code drifted from its spec; `main.py:31-32` and `app.yaml` are the
  bug, and RC dynamics are meant to be on out of the box. Update `main.py`, `app.yaml`
  and the lexicon together. Original finding below.
- **OQ-2 (original) — `R1` / `R2` defaults disagree three ways.** README's parameter table and its
  derivation section say 0.1 Ω / 0.05 Ω and justify them physically; `main.py:31-32` and
  `app.yaml` say 0.0 / 0.0; README's RC-dynamics prose claims "the `.env` defaults ship
  with R1 = 0.1 and R2 = 0.05". The lexicon currently mirrors `app.yaml` (0.0), so RC
  dynamics are **off** out of the box despite the README saying they are on. **Needs a
  user decision**, and it is a one-line `app.yaml` change either way — this spec does not
  make it.
- **OQ-3 — RESOLVED 2026-09-15: `A_THERMAL` stays tunable, and `heat` is rebased.**
  On any `A_THERMAL` write, recompute `heat = temperature / A_new` so temperature is
  continuous across the change and the heat state absorbs the step. This is the idiom
  `main.py:179` already uses after thermal saturation. Implement §6.5.5 as specified.
- **OQ-4 — should `applied` be its own topic instead of a key on `dashboard-out`?** A
  separate `dashboard-state` topic would let the dashboard subscribe from `earliest` and
  get the current state instantly rather than waiting up to 5 s. Costs a topic and a
  second subscription. Phase 1 uses the simpler in-band key; revisit if reload latency
  is felt.

## 9. Alternatives considered

- **Flat `dashboard-in` object.** Rejected: works only because this model's signal names
  happen to be lowercase and its parameter names uppercase. Makes unknown-key rejection
  ambiguous and does not survive a second plant model. Kept as a *legacy read path* only
  (§6.3.3).
- **Parameters on a second topic.** Clean separation, but it breaks ordering between a
  signal and a parameter written together, doubles the dashboard's producer wiring, and
  D1 already says `dashboard-in`.
- **DCM on the value path** (`join_lookup` + `QuixConfigurationService`). Explicitly
  forbidden by D1 and wrong-shaped anyway: `join_lookup` resolves config *per record by
  message key*, which is a per-tick enrichment idiom, not a control channel. DCM holds the
  lexicon only.
- **Inline `PARAM_RANGES` dict in `main.py`** instead of loading `lexicon.json`. Fewer
  moving parts, but it duplicates 14 ranges that the dashboard also holds, and the two
  copies drift the first time someone widens a knob. The file is ~15 lines of loader.
- **Clamping out-of-range values instead of rejecting.** Forbidden by D1, and correctly so:
  a clamped write looks accepted, so the dashboard's knob and the plant's state disagree
  permanently with no signal that anything went wrong.
- **Per-message atomic apply.** Considered for §6.7. Rejected for Phase 1 — with no ack
  channel, dropping a whole message on one bad field is invisible to the user.
- **Echoing `applied` on every tick.** ~100 extra bytes at 10 Hz is affordable, but it
  makes the 13-field payload harder to read in logs and in any future sink schema, for no
  benefit over change-driven + heartbeat.
- **Splitting `main.py` into a package.** Rejected: `quixstreams-idioms` §0 sets the
  trigger at 300 lines of code with a real seam; the projection is ~255. Seam named in
  §6.5 for when it is earned.

## 10. References

- `C:\repos\dashboard-tests\CLAUDE.md` — §3 lexicon, §4 UI contract, §7 D1, §8 open questions
- `C:\repos\dashboard-tests\dc-battery-sim\main.py` — anchors cited throughout §6.5
- `C:\repos\dashboard-tests\dc-battery-sim\README.md` — physics, parameter table, derivations, payload schemas
- `C:\repos\dashboard-tests\dc-battery-sim\app.yaml` · `dockerfile` · `requirements.txt`
- `C:\repos\dashboard-tests\.env.example` — topic naming, dotenv interpolation pattern
- `C:\repos\dashboard-tests\dev-planning\parameter-contract\lexicon-battery.json` — the §6.2 artifact
- Skills: `quixstreams-idioms` (§0 service shape, §1 Application/topics, "no try/except in
  the pipeline"), `quix-service-update` (the §6.6 layer checklist)

---

## Sanity print — full battery lexicon

| name | kind | datatype | unit | direction | tunable | default | min | max |
|---|---|---|---|---|---|---|---|---|
| `requested_power_w` | signal | float | W | input | — | -8000.0 | -250000.0 | 250000.0 |
| `ambient_temp_c` | signal | float | °C | input | — | 15.0 | -40.0 | 60.0 |
| `chiller_setting` | signal | enum | — | input | — | 0 | — | — |
| `heater_setting` | signal | enum | — | input | — | 0 | — | — |
| `soc_percent` | signal | float | % | output | — | 50.0 | 0.0 | 100.0 |
| `q_act_as` | signal | float | A·s | output | — | 180000.0 | 0.0 | 360000.0 |
| `ocv_v` | signal | float | V | output | — | 780.0 | 720.0 | 840.0 |
| `dc_voltage_v` | signal | float | V | output | — | 780.0 | 600.0 | 900.0 |
| `dc_current_a` | signal | float | A | output | — | -10.26 | -400.0 | 400.0 |
| `rc1_voltage_v` | signal | float | V | output | — | 0.0 | -50.0 | 50.0 |
| `rc2_voltage_v` | signal | float | V | output | — | 0.0 | -25.0 | 25.0 |
| `temperature_c` | signal | float | °C | output | — | 20.0 | -40.0 | 60.0 |
| `heat_j` | signal | float | J | output | — | 100000.0 | -200000.0 | 300000.0 |
| `derating_factor` | signal | float | — | output | — | 1.0 | 0.0 | 1.0 |
| `requested_power_w` | signal | float | W | output | — | -8000.0 | -250000.0 | 250000.0 |
| `ambient_temp_c` | signal | float | °C | output | — | 15.0 | -40.0 | 60.0 |
| `Q_MAX_AH` | parameter | float | Ah | — | false | 100.0 | 1.0 | 1000.0 |
| `SAMPLE_TIME` | parameter | float | s | — | false | 0.1 | 0.001 | 1.0 |
| `A_THERMAL` | parameter | float | °C/J | — | true | 0.0002 | 0.00002 | 0.002 |
| `KT0` | parameter | float | W | — | true | 0.0 | -5000.0 | 5000.0 |
| `KT1` | parameter | float | W/A | — | true | 0.0 | -10.0 | 10.0 |
| `KT2` | parameter | float | W/A² | — | true | 0.0278 | 0.0 | 0.28 |
| `KE` | parameter | float | W/°C | — | true | 1.6 | 0.0 | 16.0 |
| `TAU1` | parameter | float | s | — | true | 1.0 | 0.0 | 60.0 |
| `TAU2` | parameter | float | s | — | true | 600.0 | 0.0 | 3600.0 |
| `R0` | parameter | float | Ω | — | true | 0.0 | 0.0 | 0.5 |
| `R1` | parameter | float | Ω | — | true | 0.0 † | 0.0 | 0.5 |
| `R2` | parameter | float | Ω | — | true | 0.0 † | 0.0 | 0.5 |
| `COOLANT_TEMP` | parameter | float | °C | — | true | 20.0 | -20.0 | 40.0 |
| `MAX_BATTERY_TEMP` | parameter | float | °C | — | true | 60.0 | 25.0 | 60.0 |

**Counts: 30 rows = 4 input signals + 12 output signals + 14 parameters (2 fixed, 12
tunable).** `requested_power_w` and `ambient_temp_c` each appear twice — once as an input
signal, once as an output echo — which is why `signals` is keyed on (`name`, `direction`)
(§6.1). `—` means the field is `null` in the JSON. `timestamp` is message metadata, not a
lexicon entry.

† `R1`/`R2` defaults mirror `app.yaml` (0.0). README documents 0.1 / 0.05 — unresolved,
see §8 **OQ-2**.
