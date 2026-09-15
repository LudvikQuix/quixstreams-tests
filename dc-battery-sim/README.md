# DC Battery Simulator

A discrete-time second-order RC equivalent circuit battery simulation running as a Quix service. Receives control commands from the dashboard and publishes battery state to the data pipeline at 100 ms intervals.

---

## Overview

```
dashboard-in  ──►  DC Battery Sim  ──►  dashboard-out
```

Two kinds of write arrive on `dashboard-in`: **signals** (setpoints such as `requested_power_w`) and **parameters** (model constants such as `KE` or `TAU2`). Sixteen of the eighteen parameters are tunable live; the other two are fixed at deploy time. `lexicon.json`, shipped next to `main.py`, describes every name, datatype and range and is what the service validates incoming writes against.

The simulator models:
- **Electrochemical state** — charge counting (Coulomb counting) with OCV look-up
- **Electrical behaviour** — second-order RC equivalent circuit: terminal voltage includes R0 series drop and two RC branch voltages
- **Thermal dynamics** — polynomial heat generation, heat exchange with ambient, heater/chiller influence, with upper and lower temperature saturation
- **Temperature derating** — DC current scaled by a [0, 1] factor from a temperature LUT

---

## Circuit Model

The battery is based on a **second-order RC equivalent circuit**:

```
  OCV ── R0 ──┬── R1/C1 ──┬── R2/C2 ──┬── terminal
              │            │            │
             GND          GND          GND
```

`R1` and `R2` default to their derived values (`0.1 Ω` / `0.05 Ω`), so both RC branches are active out of the box. `R0` defaults to `0.0 Ω`; setting it non-zero engages the quadratic current solver. All three are tunable live.

---

## Simulation Equations

### Terminal Voltage and DC Current

The terminal voltage includes drops across all circuit elements:

```
V_terminal = OCV  −  R0 · I_dc  −  V_RC1  −  V_RC2
```

`I_dc` and `V_terminal` are mutually dependent (implicit loop for R0 > 0). Solved analytically:

```
# Rearranges to quadratic: R0·I² − (OCV − V_RC1 − V_RC2)·I + P = 0
# For R0 = 0: I_dc = P / (OCV − V_RC1 − V_RC2)
```

- Negative `P_requested` → discharge (negative current)
- Positive `P_requested` → charge (positive current)

### RC Branch Dynamics (first-order IIR — ZOH exact)

Each RC branch voltage is a **discrete-time low-pass filter** on DC current:

```
α_i      = exp(−Δt / τ_i)
V_RCi[k] = α_i · V_RCi[k−1]  +  R_i · (1 − α_i) · I_dc[k]
```

The filter coefficients are derived from `SAMPLE_TIME`, `TAU1` and `TAU2` at startup **and again after every parameter write** — `TAU1`/`TAU2` are tunable, so a stale coefficient would make a `TAU` change a silent no-op. With `R1 = R2 = 0` the RC voltages remain 0 V; the shipped defaults are `R1 = 0.1 Ω` and `R2 = 0.05 Ω`, so RC dynamics are active out of the box.

### State of Charge (Coulomb counting)

```
Q_act = Q_prev + I_dc × sample_time
```

Saturation limits:

| Condition | Effect |
|---|---|
| `Q_act ≤ 0` and `I_dc < 0` | `I_dc` clamped to 0 (battery empty, cannot discharge) |
| `Q_act ≥ Q_max` and `I_dc > 0` | `I_dc` clamped to 0 (battery full, cannot charge) |

```
SOC (%) = Q_act / Q_max × 100
```

### OCV Look-up Table

Linear interpolation between two anchor points:

| SOC | OCV |
|---|---|
| 0 % | 720 V |
| 100 % | 840 V |

```
OCV = 720 + (840 - 720) × SOC
```

### Thermal Model

```
HeatDiff  = kt2 × I_dc²  +  kt1 × I_dc  +  kt0
          - ke × (T_battery - T_ambient)
          + P_heater  -  P_chiller

Heat      = Heat_prev + HeatDiff        [J]
T_battery = A × Heat                   [°C]
```

`P_heater` and `P_chiller` are not constants: the `heater_setting` / `chiller_setting` signals pick a stage and the four tunable `*_POWER_*` parameters say what that stage delivers — see [Chiller / Heater power map](#chiller--heater-power-map).

After integration, temperature is saturated:

| Condition | Effect |
|---|---|
| `T_battery > MAX_BATTERY_TEMP` | Clamped to `MAX_BATTERY_TEMP` |
| Chiller drawing power and `T_battery < COOLANT_TEMP` | Clamped to `COOLANT_TEMP` |

`Heat` is back-calculated from the saturated temperature to keep state consistent.

`A_THERMAL` is tunable, and `T = A × Heat`, so a live write would otherwise step the temperature reading by the ratio of old to new. On any change the simulator rebases `Heat = T / A_new` **before** the tick's thermal integration, so `temperature_c` is continuous and the heat state absorbs the step.

### Temperature Derating

A derating factor `d ∈ [0, 1]` is looked up by battery temperature and applied to the DC current before charge counting and thermal integration:

```
I_dc_actual = I_dc_requested × d(T_battery)
```

Default LUT (linear interpolation between anchor points):

| Temperature | Derating factor |
|---|---|
| ≤ −30 °C | 0.0 (fully derated) |
| −20 °C | 1.0 |
| 50 °C | 1.0 |
| 60 °C | 0.0 (fully derated) |
| ≥ 60 °C | 0.0 |

---

## Parameters

Every environment variable below supplies the **startup baseline**. A **tunable** parameter can then be rewritten live over `dashboard-in`; a **fixed** one cannot, because changing it mid-run would break simulation continuity. A restart returns every tunable to its deployment baseline — that is intentional, the deployment defines the known-good starting point.

The `Range` column is the validation contract enforced by `lexicon.json`. A write outside it is **rejected and logged, never clamped**.

| Env Var | Default | Tunable | Range | Description |
|---|---|---|---|---|
| `Q_MAX_AH` | `100` | **no** | 1 – 1000 | Maximum charge capacity (Ah). Internally converted to Coulombs: `Q_max = Q_MAX_AH × 3600`. Fixed: `SOC = q_act / Q_max`, so a mid-run change steps the SOC reading. |
| `SAMPLE_TIME` | `0.1` | **no** | 0.001 – 1.0 | Simulation sample time (s). Fixed: it sets the loop rate *and* both RC filter coefficients. |
| `A_THERMAL` | `0.0002` | yes | 0.00002 – 0.002 | Heat-to-temperature coefficient: `T = A × Heat`. A live change rebases `Heat` (see Thermal Model). |
| `KT0` | `0.0` | yes | −5000 – 5000 | Thermal constant heat term (W) |
| `KT1` | `0.0` | yes | −10 – 10 | Thermal linear current coefficient (W/A) |
| `KT2` | `0.0278` | yes | 0 – 0.28 | Thermal quadratic current coefficient (W/A²) |
| `KE` | `1.6` | yes | 0 – 16 | Thermal exchange coefficient with ambient (W/°C) |
| `TAU1` | `1.0` | yes | 0 – 60 | RC1 time constant τ₁ = R1·C1 (s). A write recomputes α₁. |
| `TAU2` | `600.0` | yes | 0 – 3600 | RC2 time constant τ₂ = R2·C2 (s). A write recomputes α₂. |
| `R0` | `0.0` | yes | 0 – 0.5 | Internal series resistance (Ω). Set > 0 to enable R0 voltage drop. |
| `R1` | `0.1` | yes | 0 – 0.5 | RC1 branch resistance (Ω). Set 0 to disable RC1 dynamics. |
| `R2` | `0.05` | yes | 0 – 0.5 | RC2 branch resistance (Ω). Set 0 to disable RC2 dynamics. |
| `COOLANT_TEMP` | `20.0` | yes | −20 – 40 | Coolant temperature (°C) — lower bound for battery temperature when the chiller is running |
| `MAX_BATTERY_TEMP` | `60.0` | yes | 25 – 60 | Hard upper saturation for battery temperature (°C). Capped at 60 because the derating LUT returns 0 at and above 60 °C. |
| `CHILLER_POWER_LOW` | `2500.0` | yes | 0 – 20 000 | Heat removed (W) while `chiller_setting` is 1. At 0 the stage is a no-op and no longer engages the `COOLANT_TEMP` clamp. |
| `CHILLER_POWER_HIGH` | `5000.0` | yes | 0 – 20 000 | Heat removed (W) while `chiller_setting` is 2 |
| `HEATER_POWER_LOW` | `2500.0` | yes | 0 – 20 000 | Heat added (W) while `heater_setting` is 1 |
| `HEATER_POWER_HIGH` | `5000.0` | yes | 0 – 20 000 | Heat added (W) while `heater_setting` is 2 |
| `REQUESTED_POWER` | `-8000` | — | ±250 000 | Initial power request (W) — signal, written live over `dashboard-in` as `requested_power_w` |
| `AMBIENT_TEMP` | `15` | — | −40 – 60 | Initial ambient temperature (°C) — signal, written live as `ambient_temp_c` |
| `CHILLER_SETTING` | `0` | — | 0 / 1 / 2 | Initial chiller state — signal, written live as `chiller_setting` |
| `HEATER_SETTING` | `0` | — | 0 / 1 / 2 | Initial heater state — signal, written live as `heater_setting` |

### Operational variables

| Env Var | Default | Description |
|---|---|---|
| `input` | `dashboard-in` | Command topic. `app.yaml` default; `main.py`'s own fallback if the variable is unset entirely is still `ui-data`. |
| `output` | `dashboard-out` | Telemetry topic. Same: `main.py`'s bare fallback is `battery-data`. |
| `LEXICON_PATH` | `lexicon.json` | Signal/parameter lexicon. A relative path resolves next to `main.py`. Also the seam for sourcing the document from DCM later. |
| `LOG_LEVEL` | `INFO` | `DEBUG` emits the full payload every tick (10 lines/s). Rejection warnings and accepted-write lines are `WARNING`/`INFO`. |
| `APPLIED_ECHO_PERIOD_S` | `5` | Heartbeat period for the `applied` block on the output topic. |

### Chiller / Heater power map

The `chiller_setting` / `heater_setting` signals choose a **stage**; four tunable parameters decide what each stage is worth. The map is rebuilt from the parameter snapshot on every tick, so a live write to any of them changes the thermal integration from the next tick onwards.

| Setting | Chiller removes | Heater adds |
|---|---|---|
| `0` | 0 W | 0 W |
| `1` | `CHILLER_POWER_LOW` (default 2 500 W) | `HEATER_POWER_LOW` (default 2 500 W) |
| `2` | `CHILLER_POWER_HIGH` (default 5 000 W) | `HEATER_POWER_HIGH` (default 5 000 W) |

Setting `0` is 0 W by definition of "off" and has no parameter. The `0 – 20 000 W` range is roughly four times the shipped high stage and comfortably out-cools the pack's own worst-case ohmic self-heating (`KT2 × I²` ≈ 4.4 kW at the default `KT2` and the ±400 A edge of `dc_current_a`), while keeping the default at an eighth of a slider's travel rather than lost near zero. Production pack chillers and heaters sit in the 3 – 10 kW band, so there is nothing credible above it.

Nothing enforces `LOW ≤ HIGH` — the lexicon validates each field on its own. Inverting them simply makes stage 1 the stronger one; the model does not care.

### Derived RC parameter values

The RC branch resistances and time constants were chosen to represent two distinct electrochemical polarisation processes:

| Branch | R (Ω) | τ (s) | Implied C (F) | Physical meaning |
|---|---|---|---|---|
| RC1 | `0.1` | `1` | 10 | Fast charge-transfer polarisation (~1 s relaxation) |
| RC2 | `0.05` | `600` | 12 000 | Slow diffusion polarisation (~10 min relaxation) |

`τ = R · C` — the implied capacitance is informational only; only `R` and `TAU` are used by the simulation.

RC2 is intentionally tuned to **600 s (10 min)** so that diffusion dynamics build up gradually over many minutes of sustained load, consistent with solid-state diffusion time scales in Li-ion cells.

---

### Derived thermal parameter values

The default `KT2` and `KE` values were derived from three constraints:

| Constraint | Result |
|---|---|
| `\|I\| = 300 A` → `dT/dt = +0.5 °C/s` (= 1 °C per 2 s) | `KT2 = 0.0278 W/A²` |
| Thermal equilibrium at `\|I\| = 20 A`, `T_battery ≈ 20 °C`, `T_ambient = 13 °C` | `KE = 1.6 W/°C` |
| No current → battery reaches `T_ambient` in ≈ 3 hours (τ ≈ 52 min) | confirms `KE = 1.6` |

`KT0 = 0`, `KT1 = 0` — no constant heat term; heating is symmetric for charge and discharge.

> **Note:** an env var, when set, wins over the lexicon default — `load_dotenv(override=False)` only protects already-set shell variables, not the fallback chain. If `.env` or the deployment carries a stale `KT2`/`KE`, that value is what runs. The `[STARTUP]` log block prints the effective value of all 18 parameters for exactly this reason.

---

## Initial Conditions

| State | Value | Notes |
|---|---|---|
| `Q_act` | `Q_max / 2` | 50 % SOC at startup |
| `Heat` | `100 000 J` | Gives `T_battery = 0.0002 × 100 000 = 20 °C` |

---

## Data Flow

### Input — `dashboard-in` topic

Two namespaces, both optional, both partial at every level: an absent `signals`/`parameters` key leaves that whole collection untouched, and an absent inner key leaves that entry untouched. `meta` is reserved and unused.

```json
{
  "signals":    { "requested_power_w": -20000.0, "chiller_setting": 1 },
  "parameters": { "TAU2": 60.0, "R2": 0.05 },
  "meta":       { }
}
```

Messages **must** be produced with a fixed key identifying the plant instance (`"battery-sim"`). Partial update is last-write-wins, and last-write-wins needs a total order; unkeyed messages round-robin across partitions and a `parameters` write can overtake a `signals` write.

A message containing neither `signals` nor `parameters` is read as the **legacy flat form** and treated as signals-only, so the original payload still works:

```json
{ "requested_power_w": -8000.0, "ambient_temp_c": 15 }
```

#### Rejection semantics

Validation is field-level and best-effort: a bad field is dropped and every other field in the same message still applies. Nothing on this path raises — an exception inside the pipeline would take the whole service down.

| Case | Behaviour |
|---|---|
| Payload is not a JSON object | Message dropped; `WARNING` on the first drop and every thousandth |
| Unknown name, or an output-only signal name | Field ignored, `WARNING` |
| Parameter with `tunable: false` (`SAMPLE_TIME`, `Q_MAX_AH`) | Field ignored, `WARNING` "fixed at deploy time" |
| Wrong datatype — string, `null`, list, object, or `true`/`false` for a number | Field ignored, old value kept, `WARNING` |
| Outside the lexicon `[min, max]` | Field ignored, old value kept, `WARNING`. **Never clamped.** |
| Enum value outside the allowed set | Field ignored, old value kept, `WARNING` |
| Valid | Applied; `INFO` per field; `applied` block on the next tick |

An `int` is accepted where a `float` is expected (`"ambient_temp_c": 15` is valid); the reverse is not. Strings are never coerced — `"-8000"` is a rejection, not a setpoint. Enum values are canonicalised, so a wire `1.0` is stored as `1`.

### Output — `dashboard-out` topic

Published every `SAMPLE_TIME` seconds (default 100 ms).

```json
{
  "timestamp":         "2026-03-13T10:00:00.000000+00:00",
  "soc_percent":       50.0,
  "q_act_as":          180000.0,
  "ocv_v":             780.0,
  "dc_voltage_v":      780.0,
  "dc_current_a":      -10.26,
  "rc1_voltage_v":     0.0,
  "rc2_voltage_v":     0.0,
  "temperature_c":     20.0,
  "heat_j":            100000.0,
  "derating_factor":   1.0,
  "requested_power_w": -8000.0,
  "ambient_temp_c":    15.0
}
```

| Field | Unit | Description |
|---|---|---|
| `timestamp` | ISO 8601 UTC | Wall-clock time of the sample |
| `soc_percent` | % | State of charge (0–100) |
| `q_act_as` | A·s (C) | Actual stored charge in Coulombs |
| `ocv_v` | V | Open-circuit voltage from look-up |
| `dc_voltage_v` | V | DC terminal voltage = OCV − R0·I − V_RC1 − V_RC2 |
| `dc_current_a` | A | DC current — negative = discharge, positive = charge |
| `rc1_voltage_v` | V | RC1 branch voltage (0 V when R1 = 0) |
| `rc2_voltage_v` | V | RC2 branch voltage (0 V when R2 = 0) |
| `temperature_c` | °C | Battery temperature (after saturation) |
| `heat_j` | J | Accumulated heat energy (consistent with saturated temperature) |
| `derating_factor` | — | Temperature derating factor applied to `dc_current_a` (0–1) |
| `requested_power_w` | W | Active power setpoint |
| `ambient_temp_c` | °C | Active ambient temperature setpoint |

#### Optional `applied` block

Some messages carry one extra top-level key, `applied`, holding the effective value of every signal and parameter:

```json
{
  "timestamp": "...", "soc_percent": 50.0, "...": "...13 fields as above...",
  "applied": {
    "signals":    { "requested_power_w": -20000.0, "ambient_temp_c": 30.0,
                    "chiller_setting": 1, "heater_setting": 0 },
    "parameters": { "Q_MAX_AH": 100.0, "SAMPLE_TIME": 0.1, "A_THERMAL": 0.0002,
                    "KT0": 0.0, "KT1": 0.0, "KT2": 0.0278, "KE": 1.6,
                    "TAU1": 1.0, "TAU2": 600.0, "R0": 0.0, "R1": 0.1, "R2": 0.05,
                    "COOLANT_TEMP": 20.0, "MAX_BATTERY_TEMP": 60.0,
                    "CHILLER_POWER_LOW": 2500.0, "CHILLER_POWER_HIGH": 5000.0,
                    "HEATER_POWER_LOW": 2500.0, "HEATER_POWER_HIGH": 5000.0 }
  }
}
```

It is present on the first tick after startup, on the first tick after an accepted write, and every `APPLIED_ECHO_PERIOD_S` seconds as a heartbeat. Otherwise the key is **absent** — a consumer must not assume a fixed key set. Steady-state cost is 2 messages per minute out of 600.

The block is also the implicit NACK: a rejected write changes nothing, so the echo carries the old value and a dashboard's optimistic control snaps back within one sample period.

---

## Architecture

Two separate QuixStreams `Application` instances are used to avoid shared internal state between the producer loop and the consumer loop:

- **`producer_app`** — runs in a **background daemon thread**, executes the simulation loop at 100 ms intervals and publishes to `dashboard-out`.
- **`consumer_app`** — runs `app.run(sdf)` in the **main thread** so that SIGTERM signal handlers are registered correctly. Its pipeline is `sdf.filter(is_command).update(handle_command)`: the filter is the shape gate (a non-object payload would raise inside the update step and take the service down), and `handle_command` rewrites the shared state.

Two dicts hold all mutable state, both keyed by lexicon name and both covered by **one** `threading.Lock` (`state_lock`):

- `cmd` — the four input signals.
- `params` — all 18 parameters, plus the private derived keys `_alpha1` / `_alpha2`.

One lock, not two: a tick must snapshot setpoints and parameters together or it can read a torn pair (a new `TAU2` with a stale `α₂`). The simulation loop copies both dicts once per tick and reads nothing else — no module-level tunable is read inside the loop.

```
Main thread:   consumer_app.run()
                 └─ filter(is_command) ─► update(handle_command)
                                            └─ apply_updates ─► cmd + params
                                                                 (state_lock)
                                                                      │
Background:    run_simulation()  ◄─── snapshots both dicts once per tick
                     │
               producer_app  ──►  dashboard-out topic
```

If the simulation thread ever dies, its supervisor logs `CRITICAL`, stops `consumer_app`, and the process exits non-zero. Without that, an exception in the daemon thread left the deployment green while it published nothing.

---

## Running Locally

```bash
pip install -r requirements.txt

# .env file (minimum)
# input=dashboard-in
# output=dashboard-out
# Quix__Broker__Address=localhost:19092

python main.py
```

The simulator starts immediately from its env-var/lexicon baseline — the `[STARTUP]` log block prints the effective value of all 18 parameters and both topic names — and updates its signals and parameters as soon as the first message arrives on `dashboard-in`.

---

## Dependencies

| Package | Purpose |
|---|---|
| `quixstreams` | Kafka consumer / producer via the Quix SDK |
| `python-dotenv` | Local `.env` file support |
