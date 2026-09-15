# DC Battery Simulator

A discrete-time second-order RC equivalent circuit battery simulation running as a Quix service. Receives control commands from the UI and publishes battery state to the data pipeline at 100 ms intervals.

---

## Overview

```
ui-data  ──►  DC Battery Sim  ──►  battery-data
```

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

All three resistances default to `0.0 Ω`. Set `R0`, `R1`, `R2` to non-zero values to activate the corresponding voltage drops.

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

The filter coefficients are pre-computed at startup from `SAMPLE_TIME`, `TAU1`, `TAU2`. With `R1 = R2 = 0` the RC voltages remain 0 V; the `.env` defaults ship with `R1 = 0.1 Ω` and `R2 = 0.05 Ω` so RC dynamics are active out of the box.

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

After integration, temperature is saturated:

| Condition | Effect |
|---|---|
| `T_battery > MAX_BATTERY_TEMP` | Clamped to `MAX_BATTERY_TEMP` |
| Chiller running and `T_battery < COOLANT_TEMP` | Clamped to `COOLANT_TEMP` |

`Heat` is back-calculated from the saturated temperature to keep state consistent.

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

All parameters are configurable via Quix environment variables.

| Env Var | Default | Description |
|---|---|---|
| `Q_MAX_AH` | `100` | Maximum charge capacity (Ah). Internally converted to Coulombs: `Q_max = Q_MAX_AH × 3600`. |
| `SAMPLE_TIME` | `0.1` | Simulation sample time (s) |
| `A_THERMAL` | `0.0002` | Heat-to-temperature coefficient: `T = A × Heat` |
| `KT0` | `0.0` | Thermal constant heat term (W) |
| `KT1` | `0.0` | Thermal linear current coefficient (W/A) |
| `KT2` | `0.0278` | Thermal quadratic current coefficient (W/A²) |
| `KE` | `1.6` | Thermal exchange coefficient with ambient (W/°C) |
| `TAU1` | `1.0` | RC1 time constant τ₁ = R1·C1 (s) |
| `TAU2` | `600.0` | RC2 time constant τ₂ = R2·C2 (s) |
| `R0` | `0.0` | Internal series resistance (Ω). Set > 0 to enable R0 voltage drop. |
| `R1` | `0.1` | RC1 branch resistance (Ω). Set 0 to disable RC1 dynamics. |
| `R2` | `0.05` | RC2 branch resistance (Ω). Set 0 to disable RC2 dynamics. |
| `COOLANT_TEMP` | `20.0` | Coolant temperature (°C) — lower bound for battery temperature when chiller is active |
| `MAX_BATTERY_TEMP` | `60.0` | Hard upper saturation for battery temperature (°C) |
| `REQUESTED_POWER` | `-8000` | Initial power request (W) — overridden by `ui-data` topic |
| `AMBIENT_TEMP` | `15` | Initial ambient temperature (°C) — overridden by `ui-data` topic |
| `CHILLER_SETTING` | `0` | Initial chiller state — overridden by `ui-data` topic |
| `HEATER_SETTING` | `0` | Initial heater state — overridden by `ui-data` topic |

### Chiller / Heater power map

| Setting | Power |
|---|---|
| `0` | 0 W (off) |
| `1` | 2 500 W (low) |
| `2` | 5 000 W (high) |

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

> **Note:** the `.env` file must explicitly set `KT2=0.0278` and `KE=1.6`. Any value present in `.env` overrides the Python code defaults (`load_dotenv(override=False)` only protects against already-set shell variables, not missing keys).

---

## Initial Conditions

| State | Value | Notes |
|---|---|---|
| `Q_act` | `Q_max / 2` | 50 % SOC at startup |
| `Heat` | `100 000 J` | Gives `T_battery = 0.0002 × 100 000 = 20 °C` |

---

## Data Flow

### Input — `ui-data` topic

Commands from the UI service. Any subset of fields may be present; missing fields leave the current value unchanged.

```json
{
  "requested_power_w": -8000.0,
  "ambient_temp_c":    15,
  "chiller_setting":   0,
  "heater_setting":    0
}
```

### Output — `battery-data` topic

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

---

## Architecture

Two separate QuixStreams `Application` instances are used to avoid shared internal state between the producer loop and the consumer loop:

- **`producer_app`** — runs in a **background daemon thread**, executes the simulation loop at 100 ms intervals and publishes to `battery-data`.
- **`consumer_app`** — runs `app.run(sdf)` in the **main thread** so that SIGTERM signal handlers are registered correctly. Updates the shared `cmd` dict (protected by a `threading.Lock`) whenever a command arrives from `ui-data`.

```
Main thread:   consumer_app.run()  ──►  handle_command()  ──►  cmd dict
                                                                    │
Background:    run_simulation()  ◄──────────────────────── reads cmd dict
                     │
               producer_app  ──►  battery-data topic
```

---

## Running Locally

```bash
pip install -r requirements.txt

# .env file (minimum)
# input=ui-data
# output=battery-data
# Quix__Broker__Address=localhost:19092

python main.py
```

The simulator will start immediately using the env-var defaults and update its inputs as soon as the first message arrives on `ui-data`.

---

## Dependencies

| Package | Purpose |
|---|---|
| `quixstreams` | Kafka consumer / producer via the Quix SDK |
| `python-dotenv` | Local `.env` file support |
