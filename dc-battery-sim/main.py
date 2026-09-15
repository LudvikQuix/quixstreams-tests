import os
import time
import math
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv
from quixstreams import Application

load_dotenv(override=False)

output_topic_name = os.getenv("output", "battery-data")
input_topic_name  = os.getenv("input",  "ui-data")
consumer_group    = os.getenv("Quix__Deployment__Id", "battery-sim")

# --- Parameters (overridable via env vars) ---
Q_MAX       = float(os.getenv("Q_MAX_AH",   "100"))   * 3600.0  # Ah → Coulombs
SAMPLE_TIME = float(os.getenv("SAMPLE_TIME", "0.1"))             # seconds
A_THERMAL   = float(os.getenv("A_THERMAL",   "0.0002"))

KT0 = float(os.getenv("KT0", "0.0"))
KT1 = float(os.getenv("KT1", "0.0"))
KT2 = float(os.getenv("KT2", "0.0278"))  # derived: 300 A → +1 °C/2 s
KE  = float(os.getenv("KE",  "1.6"))     # derived: equilibrium at |20A|/13°C; τ≈52 min

TAU1 = float(os.getenv("TAU1", "1.0"))    # RC1 time constant (s)
TAU2 = float(os.getenv("TAU2", "600.0"))  # RC2 time constant (s)

# Resistance parameters for the second-order RC equivalent circuit (Ohm)
R0 = float(os.getenv("R0", "0.0"))  # Internal series resistance
R1 = float(os.getenv("R1", "0.0"))  # RC1 branch resistance
R2 = float(os.getenv("R2", "0.0"))  # RC2 branch resistance

# Pre-compute discrete-time filter coefficients (ZOH exact)
ALPHA1 = math.exp(-SAMPLE_TIME / TAU1) if TAU1 > 0.0 else 0.0
ALPHA2 = math.exp(-SAMPLE_TIME / TAU2) if TAU2 > 0.0 else 0.0

CHILLER_POWERS = {0: 0.0, 1: 2500.0, 2: 5000.0}  # W per setting
HEATER_POWERS  = {0: 0.0, 1: 2500.0, 2: 5000.0}  # W per setting

COOLANT_TEMP     = float(os.getenv("COOLANT_TEMP",     "20.0"))  # °C — chiller low-limit
MAX_BATTERY_TEMP = float(os.getenv("MAX_BATTERY_TEMP", "60.0"))  # °C — hard upper saturation

# Derating LUT: (temperature_°C, derating_factor 0–1)
DERATING_LUT = [
    (-30.0, 0.0),
    (-20.0, 1.0),
    ( 50.0, 1.0),
    ( 60.0, 0.0),
]

# --- Mutable command state (defaults from env vars, updated live from ui-data topic) ---
cmd = {
    "requested_power": float(os.getenv("REQUESTED_POWER", "-8000.0")),
    "ambient_temp":    float(os.getenv("AMBIENT_TEMP",    "15.0")),
    "chiller_setting": int(os.getenv("CHILLER_SETTING",   "0")),
    "heater_setting":  int(os.getenv("HEATER_SETTING",    "0")),
}
cmd_lock = threading.Lock()


def derating_lookup(temp: float) -> float:
    """Linear interpolation of derating factor [0, 1] from DERATING_LUT."""
    temps   = [p[0] for p in DERATING_LUT]
    factors = [p[1] for p in DERATING_LUT]
    if temp <= temps[0]:
        return factors[0]
    if temp >= temps[-1]:
        return factors[-1]
    for i in range(len(temps) - 1):
        if temps[i] <= temp <= temps[i + 1]:
            ratio = (temp - temps[i]) / (temps[i + 1] - temps[i])
            return factors[i] + ratio * (factors[i + 1] - factors[i])
    return 1.0


def ocv_lookup(q_act: float) -> float:
    """Linear OCV look-up: 720 V at 0 % SOC → 840 V at 100 % SOC."""
    soc = q_act / Q_MAX
    return 720.0 + 120.0 * soc


def solve_dc_current(power: float, ocv: float, v_rc1: float, v_rc2: float) -> float:
    """
    Solve I = P / V_terminal  where  V_terminal = OCV - R0·I - V_RC1 - V_RC2.

    Rearranges to the quadratic:  R0·I² - (OCV - V_RC_sum)·I + P = 0
    For R0 = 0 this reduces to the simple division.
    """
    v_oc_eff = ocv - v_rc1 - v_rc2
    if v_oc_eff == 0.0:
        return 0.0
    if R0 == 0.0:
        return power / v_oc_eff
    # Quadratic: R0·I² - v_oc_eff·I + P = 0
    disc = v_oc_eff ** 2 - 4.0 * R0 * power
    if disc < 0.0:
        return 0.0
    sqrt_disc = math.sqrt(disc)
    i1 = (v_oc_eff + sqrt_disc) / (2.0 * R0)
    i2 = (v_oc_eff - sqrt_disc) / (2.0 * R0)
    # Pick the root closest to the R0=0 approximation
    i_approx = power / v_oc_eff
    return i1 if abs(i1 - i_approx) < abs(i2 - i_approx) else i2


def run_simulation(producer_app, out_topic):
    """
    Discrete-time second-order RC battery simulation at SAMPLE_TIME intervals.

    State variables:
      q_act  – stored charge (Coulombs)
      v_rc1  – voltage across RC1 branch (V)
      v_rc2  – voltage across RC2 branch (V)
      heat   – accumulated heat energy (J)
    """
    q_act       = Q_MAX / 2.0
    v_rc1       = 0.0
    v_rc2       = 0.0
    heat        = 100_000.0
    temperature = A_THERMAL * heat

    with producer_app.get_producer() as producer:
        while True:
            with cmd_lock:
                requested_power = cmd["requested_power"]
                ambient_temp    = cmd["ambient_temp"]
                chiller_setting = cmd["chiller_setting"]
                heater_setting  = cmd["heater_setting"]

            power_chiller = CHILLER_POWERS[chiller_setting]
            power_heater  = HEATER_POWERS[heater_setting]

            # OCV from charge state
            ocv = ocv_lookup(q_act)

            # DC current — implicit loop resolved analytically (quadratic for R0>0)
            dc_current = solve_dc_current(requested_power, ocv, v_rc1, v_rc2)

            # Saturation: empty battery cannot discharge; full battery cannot charge
            if q_act <= 0.0 and dc_current < 0.0:
                dc_current = 0.0
            if q_act >= Q_MAX and dc_current > 0.0:
                dc_current = 0.0

            # Temperature derating: scale current by [0, 1] factor from LUT
            derating_factor = derating_lookup(temperature)
            dc_current *= derating_factor

            # Terminal voltage including all drops
            dc_voltage = ocv - R0 * dc_current - v_rc1 - v_rc2

            # Update stored charge (Coulomb counting)
            q_act += dc_current * SAMPLE_TIME
            q_act  = max(0.0, min(Q_MAX, q_act))

            soc_percent = q_act / Q_MAX * 100.0

            # RC branch voltages — first-order IIR (ZOH exact low-pass filter)
            #   V_RCi[k] = α_i · V_RCi[k-1] + R_i · (1-α_i) · I[k]
            v_rc1 = ALPHA1 * v_rc1 + R1 * (1.0 - ALPHA1) * dc_current
            v_rc2 = ALPHA2 * v_rc2 + R2 * (1.0 - ALPHA2) * dc_current

            # Thermal model
            heat += (
                KT2 * dc_current ** 2
                + KT1 * dc_current
                + KT0
                - KE * (temperature - ambient_temp)
                + power_heater
                - power_chiller
            )
            temperature = A_THERMAL * heat

            # Temperature saturation
            temperature = min(temperature, MAX_BATTERY_TEMP)
            if chiller_setting > 0:
                temperature = max(temperature, COOLANT_TEMP)
            heat = temperature / A_THERMAL  # keep heat state consistent

            payload = {
                "timestamp":         datetime.now(timezone.utc).isoformat(),
                "soc_percent":       round(soc_percent,  4),
                "q_act_as":          round(q_act,         4),
                "ocv_v":             round(ocv,            4),
                "dc_voltage_v":      round(dc_voltage,     4),
                "dc_current_a":      round(dc_current,     4),
                "rc1_voltage_v":     round(v_rc1,          6),
                "rc2_voltage_v":     round(v_rc2,          6),
                "temperature_c":     round(temperature,    4),
                "heat_j":            round(heat,           4),
                "derating_factor":   round(derating_factor, 4),
                "requested_power_w": requested_power,
                "ambient_temp_c":    ambient_temp,
            }

            msg = out_topic.serialize(key="battery-sim", value=payload)
            producer.produce(topic=out_topic.name, value=msg.value, key=msg.key)

            print(payload)
            time.sleep(SAMPLE_TIME)


if __name__ == "__main__":
    # Separate Application instances: producer in background, consumer in main thread
    # so that QuixStreams signal handlers (SIGTERM) are registered correctly.
    producer_app = Application(consumer_group=f"{consumer_group}-prod")
    out_topic    = producer_app.topic(output_topic_name)

    consumer_app = Application(consumer_group=consumer_group)
    in_topic     = consumer_app.topic(input_topic_name)

    threading.Thread(
        target=run_simulation,
        args=(producer_app, out_topic),
        daemon=True,
    ).start()

    sdf = consumer_app.dataframe(in_topic)

    def handle_command(value):
        with cmd_lock:
            if "requested_power_w" in value:
                cmd["requested_power"] = float(value["requested_power_w"])
            if "ambient_temp_c" in value:
                cmd["ambient_temp"] = float(value["ambient_temp_c"])
            if "chiller_setting" in value:
                cmd["chiller_setting"] = int(value["chiller_setting"])
            if "heater_setting" in value:
                cmd["heater_setting"] = int(value["heater_setting"])

    sdf = sdf.update(handle_command)
    consumer_app.run(sdf)
