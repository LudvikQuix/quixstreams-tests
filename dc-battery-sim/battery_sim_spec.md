# Battery Simulation Specification

## Overview

A discrete-time battery simulation model based on a second-order RC equivalent circuit, designed for deployment in the Quix environment.

---

## Inputs

| Parameter | Description |
|---|---|
| `requested_power` | Power demand (W). Negative = discharge, positive = charge |
| `ambient_temperature` | Ambient temperature (°C) |
| `chiller_setting` | Chiller state: `0` = off, `1` = low power, `2` = high power |
| `heater_setting` | Heater state: `0` = off, `1` = low power, `2` = high power |

---

## Outputs

All internal states are published as JSON payload to the `battery-data` topic in Quix.

---

## Internal States

| State | Description |
|---|---|
| `SOC` / `SOE` | State of Charge / State of Energy |
| `OCV` | Open circuit terminal voltage |
| `V_RC1`, `V_RC2` | Voltage across RC1 and RC2 |
| `V_terminal` | DC voltage on terminal output |
| `I_dc` | DC current |
| `T_battery` | Battery temperature |

---

## Parameters

| Parameter | Description | Default Value |
|---|---|---|
| `Q_max` | Maximum charge capacity | `100 Ah` |
| `OCV_table` | Open circuit voltage look-up table | Linear: `720 V` at 0% → `840 V` at 100% SOC |
| `Tau1` | RC1 time constant | `1 s` |
| `Tau2` | RC2 time constant | `600 s` |
| `kt0`, `kt1`, `kt2` | Thermal polynomial coefficients | `0`, `0`, `0` |
| `ke` | Thermal exchange coefficient | `0` |
| `A` | Heat-to-temperature coefficient | `0.0002` |
| `P_chiller` | Chiller power | Per chiller setting |
| `P_heater` | Heater power | Per heater setting |
| `sample_time` | Simulation sample time | `0.1 s` (100 ms) |

---

## Model Principles

### Circuit Model

The battery is modelled as a **second-order RC equivalent circuit** consisting of:
- An internal resistance
- Two RC branches (RC1 and RC2) in series

> **Note:** RC1 and RC2 dynamics are ignored in this iteration. OCV is used directly as the terminal voltage.

---

## Simulation Equations

### 1. DC Current

```
I_dc = P_requested / V_terminal
```

- Negative `P_requested` → discharging
- Positive `P_requested` → charging

### 2. State of Charge (SOC)

```
Q_act = Q_prev + I_dc × sample_time
```

`Q_act` is saturated:
- **Lower bound:** `Q_act = 0` — battery cannot discharge further (no negative current)
- **Upper bound:** `Q_act = Q_max` — battery cannot charge further (no positive current)

OCV is derived from the look-up table using `Q_act` as the input.

### 3. Thermal Model

```
HeatDiff = kt2×I_dc² + kt1×I_dc + kt0 - ke×(T_battery - T_ambient) + P_heater - P_chiller

Heat = Heat_prev + HeatDiff

T_battery = A × Heat
```

---

## Initial Conditions

| State | Initial Value |
|---|---|
| `Q_act` | `Q_max / 2` (50% SOC) |
| `Heat` | `100,000 J` |

---

## Stub Input Values

| Input | Value |
|---|---|
| `chiller_setting` | `0` (0 W) |
| `heater_setting` | `0` (0 W) |
| `requested_power` | `-8000 W` (discharging) |
| `ambient_temperature` | `15 °C` |

---

## Output

All internal states are published each sample step as a JSON payload to the **`battery-data`** topic in Quix.
