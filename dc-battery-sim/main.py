"""DC battery simulator — second-order RC equivalent circuit with thermal dynamics.

Publishes pack state on the output topic every SAMPLE_TIME seconds and accepts live
signal *and* parameter writes on the input topic. `lexicon.json` is the single source
of truth for names, datatypes and ranges: every incoming field is validated against it
and rejected — never clamped — if it does not fit. Nothing on that path may raise; it
runs inside `sdf.update`, where one exception takes the whole service down.
"""

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from quixstreams import Application

load_dotenv(override=False)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
logger = logging.getLogger("dc-battery-sim")

output_topic_name = os.getenv("output", "battery-data")
input_topic_name = os.getenv("input", "ui-data")
consumer_group = os.getenv("Quix__Deployment__Id", "battery-sim")
APPLIED_ECHO_PERIOD_S = float(os.getenv("APPLIED_ECHO_PERIOD_S", "5"))

# --- Lexicon: the contract every incoming write is validated against ---
# A relative LEXICON_PATH resolves next to this file, not against the working
# directory, so the service behaves the same from the container and from pytest.
_lexicon_path = Path(os.getenv("LEXICON_PATH", "lexicon.json"))
if not _lexicon_path.is_absolute():
    _lexicon_path = Path(__file__).parent / _lexicon_path
LEXICON_PATH = _lexicon_path
LEXICON = json.loads(LEXICON_PATH.read_text(encoding="utf-8"))
PARAM_SPEC = {p["name"]: p for p in LEXICON["parameters"]}
SIGNAL_SPEC = {s["name"]: s for s in LEXICON["signals"] if s["direction"] == "input"}

# Reserved top-level keys of the dashboard-in envelope. A plant may not name a
# signal or a parameter any of these.
RESERVED_KEYS = ("signals", "parameters", "meta")

# The four input signals predate the lexicon and keep their original env var names;
# renaming them would be a deployment change for no gain.
_ENV_FOR = {
    "requested_power_w": "REQUESTED_POWER",
    "ambient_temp_c": "AMBIENT_TEMP",
    "chiller_setting": "CHILLER_SETTING",
    "heater_setting": "HEATER_SETTING",
}


def _startup_value(spec, raw):
    """Typed startup value for one lexicon entry: the env var if set, else the default.

    Env vars are strings, so this is the only place string coercion is allowed — the
    wire path (`coerce`) rejects strings outright. No `bool` entry exists in the
    lexicon; add a branch here if one appears.
    """
    if raw is None:
        return spec["default"]
    if spec["datatype"] in ("enum", "int", "uint"):
        return int(raw)
    return float(raw)


# --- Live state: setpoints and parameters, both keyed by lexicon name ---
# Env vars supply the startup baseline; both dicts are then rewritten field by field
# from the input topic (D1 partial update) and are snapshotted together under
# `state_lock` once per tick, so no tick can read a torn pair.
cmd = {
    name: _startup_value(spec, os.getenv(_ENV_FOR[name]))
    for name, spec in SIGNAL_SPEC.items()
}
params = {
    name: _startup_value(spec, os.getenv(name)) for name, spec in PARAM_SPEC.items()
}
state_lock = threading.Lock()

# Fixed parameters (`tunable: false`) are also the only ones safe to hoist out of
# `params` into module constants — nothing can rewrite them at runtime.
Q_MAX = params["Q_MAX_AH"] * 3600.0  # Ah → Coulombs
SAMPLE_TIME = params["SAMPLE_TIME"]  # seconds

CHILLER_POWERS = {0: 0.0, 1: 2500.0, 2: 5000.0}  # W per setting
HEATER_POWERS = {0: 0.0, 1: 2500.0, 2: 5000.0}  # W per setting

# Derating LUT: (temperature_°C, derating_factor 0–1). Not in the lexicon, so not
# tunable — MAX_BATTERY_TEMP's lexicon max of 60 °C is what keeps the two consistent.
DERATING_LUT = [
    (-30.0, 0.0),
    (-20.0, 1.0),
    (50.0, 1.0),
    (60.0, 0.0),
]

_echo_due = True  # first tick after startup always carries the "applied" block
_dropped_messages = 0
_sim_failed = threading.Event()


def _recompute_derived():
    """ALPHA_i = exp(-Δt / τ_i), stored in `params` under private keys.

    TAU1/TAU2 are tunable, so this runs after EVERY parameter write, unconditionally —
    "did I remember to check the TAU flag" is the silent no-op this design prevents,
    and two exp() per message at human-clicking rates is free.
    """
    for i in (1, 2):
        tau = params[f"TAU{i}"]
        params[f"_alpha{i}"] = math.exp(-SAMPLE_TIME / tau) if tau > 0.0 else 0.0


_recompute_derived()


def __getattr__(name):
    """Read-only module view of the live parameters (`main.R1` → `params["R1"]`).

    PEP 562 hook, consulted only for names not bound at module level, so the real
    constants (SAMPLE_TIME, Q_MAX) are unaffected. Assigning `main.R1` would bind a
    shadowing global and change nothing — write through `apply_updates`.
    """
    if name in PARAM_SPEC:
        return params[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _allowed(spec):
    """The permitted value set of a descriptor, for a rejection warning."""
    if spec["enum"]:
        return [member["value"] for member in spec["enum"]]
    return f"[{spec['min']}, {spec['max']}]"


def coerce(spec, raw):
    """Return (ok, value) for one wire value checked against its lexicon descriptor.

    Must never raise — it runs inside `sdf.update`. Three rules are not obvious:
    `bool` subclasses `int`, so True must be rejected before any numeric test or it
    lands in a float parameter as 1.0; an `int` is acceptable where a `float` is
    expected but not the reverse; and there is no string coercion at all, which is
    both a deliberate narrowing and what makes a try/except-free body possible.
    """
    if spec["datatype"] == "bool":
        return (isinstance(raw, bool), raw)
    if isinstance(raw, bool):
        return (False, None)
    if spec["datatype"] == "enum":
        for member in spec["enum"]:
            if raw == member["value"]:
                return (True, member["value"])  # canonical form, not the wire form
        return (False, None)
    if not isinstance(raw, int | float):
        return (False, None)
    if spec["datatype"] in ("int", "uint") and not isinstance(raw, int):
        return (False, None)
    value = float(raw) if spec["datatype"] == "float" else int(raw)
    return (spec["min"] <= value <= spec["max"], value)


def apply_updates(target, spec_map, updates, kind):
    """Apply one collection of wire values to `target`; return True if any was taken.

    Field-level and best-effort: a bad field is dropped with a WARNING and every other
    field in the same message still applies. Out-of-range values are rejected, never
    clamped — a clamped write looks accepted, so the knob and the plant would then
    disagree forever with nothing to signal it. Caller holds `state_lock`.
    """
    changed = False
    for name, raw in updates.items():
        spec = spec_map.get(name)
        if spec is None:
            logger.warning("Rejected %s %r: unknown name.", kind, name)
            continue
        if spec.get("tunable") is False:
            logger.warning("Rejected %s %r: fixed at deploy time.", kind, name)
            continue
        ok, value = coerce(spec, raw)
        if not ok:
            logger.warning(
                "Rejected %s %r=%r: expected %s in %s.",
                kind,
                name,
                raw,
                spec["datatype"],
                _allowed(spec),
            )
            continue
        target[name] = value
        changed = True
        logger.info("Applied %s %s = %r", kind, name, value)
    return changed


def is_command(value):
    """Shape gate for the input topic.

    A non-dict payload raises inside `handle_command`, and an exception in
    `sdf.update` takes the whole application down — filter it, never try/except it.
    Drops are logged on the first and every thousandth, so a misconfigured producer
    is visible without flooding the log.
    """
    global _dropped_messages
    if isinstance(value, dict):
        return True
    _dropped_messages += 1
    if _dropped_messages % 1000 == 1:
        logger.warning(
            "Dropped %d non-object message(s) on %s; last was %r.",
            _dropped_messages,
            input_topic_name,
            value,
        )
    return False


def handle_command(value):
    """Apply one `dashboard-in` message: {"signals": {...}, "parameters": {...}}.

    A message carrying neither object is read as the legacy flat form documented in
    README "Input": the whole payload minus the reserved keys is treated as signals.
    Absent keys are untouched at every level; there is no unset and no null semantics,
    and a restart is the only way back to the deployment baseline.
    """
    global _echo_due

    signals = value.get("signals")
    parameters = value.get("parameters")
    if not isinstance(signals, dict) and not isinstance(parameters, dict):
        signals = {k: v for k, v in value.items() if k not in RESERVED_KEYS}
        parameters = None

    with state_lock:
        changed = False
        if isinstance(signals, dict):
            changed |= apply_updates(cmd, SIGNAL_SPEC, signals, "signal")
        if isinstance(parameters, dict):
            changed |= apply_updates(params, PARAM_SPEC, parameters, "parameter")
            _recompute_derived()
        if changed:
            _echo_due = True


def derating_lookup(temp: float) -> float:
    """Linear interpolation of derating factor [0, 1] from DERATING_LUT."""
    temps = [p[0] for p in DERATING_LUT]
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


def solve_dc_current(
    power: float, ocv: float, v_rc1: float, v_rc2: float, r0: float
) -> float:
    """
    Solve I = P / V_terminal  where  V_terminal = OCV - r0·I - V_RC1 - V_RC2.

    Rearranges to the quadratic:  r0·I² - (OCV - V_RC_sum)·I + P = 0
    For r0 = 0 this reduces to the simple division.

    `r0` is passed in rather than read from `params`: it is tunable, and a read here
    would sit outside the caller's per-tick snapshot.
    """
    v_oc_eff = ocv - v_rc1 - v_rc2
    if v_oc_eff == 0.0:
        return 0.0
    if r0 == 0.0:
        return power / v_oc_eff
    disc = v_oc_eff**2 - 4.0 * r0 * power
    if disc < 0.0:
        return 0.0
    sqrt_disc = math.sqrt(disc)
    i1 = (v_oc_eff + sqrt_disc) / (2.0 * r0)
    i2 = (v_oc_eff - sqrt_disc) / (2.0 * r0)
    # Pick the root closest to the r0=0 approximation
    i_approx = power / v_oc_eff
    return i1 if abs(i1 - i_approx) < abs(i2 - i_approx) else i2


def run_simulation(producer_app, out_topic):
    """
    Discrete-time second-order RC battery simulation at SAMPLE_TIME intervals.

    Setpoints and parameters are snapshotted together once per tick under
    `state_lock`; no module-level tunable is read inside the loop.

    State variables:
      q_act          – stored charge (Coulombs)
      v_rc1          – voltage across RC1 branch (V)
      v_rc2          – voltage across RC2 branch (V)
      heat           – accumulated heat energy (J)
      a_thermal_prev – the A_THERMAL `heat` is currently expressed in
    """
    global _echo_due

    q_act = Q_MAX / 2.0
    v_rc1 = 0.0
    v_rc2 = 0.0
    heat = 100_000.0
    with state_lock:
        a_thermal_prev = params["A_THERMAL"]
    temperature = a_thermal_prev * heat
    last_echo = 0.0

    with producer_app.get_producer() as producer:
        while True:
            with state_lock:
                setpoints = dict(cmd)
                p = dict(params)
                echo_due = _echo_due
                _echo_due = False

            now = time.monotonic()
            echo_due = echo_due or (now - last_echo >= APPLIED_ECHO_PERIOD_S)

            # A_THERMAL is tunable and T = A·Heat, so a write would step the
            # temperature reading by the ratio of old to new. Rebase Heat instead:
            # temperature stays continuous and the heat state absorbs the step.
            # Deleting these three lines reverts to the stepping behaviour.
            if p["A_THERMAL"] != a_thermal_prev:
                heat = temperature / p["A_THERMAL"]
                a_thermal_prev = p["A_THERMAL"]

            requested_power = setpoints["requested_power_w"]
            ambient_temp = setpoints["ambient_temp_c"]
            chiller_setting = setpoints["chiller_setting"]
            heater_setting = setpoints["heater_setting"]

            # .get, not [...]: an out-of-range setting must never kill this thread.
            # The wire path rejects one long before it gets here (coerce checks enum
            # membership); this is the backstop for any other writer.
            power_chiller = CHILLER_POWERS.get(chiller_setting, 0.0)
            power_heater = HEATER_POWERS.get(heater_setting, 0.0)

            # OCV from charge state
            ocv = ocv_lookup(q_act)

            # DC current — implicit loop resolved analytically (quadratic for R0>0)
            dc_current = solve_dc_current(requested_power, ocv, v_rc1, v_rc2, p["R0"])

            # Saturation: empty battery cannot discharge; full battery cannot charge
            if q_act <= 0.0 and dc_current < 0.0:
                dc_current = 0.0
            if q_act >= Q_MAX and dc_current > 0.0:
                dc_current = 0.0

            # Temperature derating: scale current by [0, 1] factor from LUT
            derating_factor = derating_lookup(temperature)
            dc_current *= derating_factor

            # Terminal voltage including all drops
            dc_voltage = ocv - p["R0"] * dc_current - v_rc1 - v_rc2

            # Update stored charge (Coulomb counting)
            q_act += dc_current * SAMPLE_TIME
            q_act = max(0.0, min(Q_MAX, q_act))

            soc_percent = q_act / Q_MAX * 100.0

            # RC branch voltages — first-order IIR (ZOH exact low-pass filter)
            #   V_RCi[k] = α_i · V_RCi[k-1] + R_i · (1-α_i) · I[k]
            v_rc1 = p["_alpha1"] * v_rc1 + p["R1"] * (1.0 - p["_alpha1"]) * dc_current
            v_rc2 = p["_alpha2"] * v_rc2 + p["R2"] * (1.0 - p["_alpha2"]) * dc_current

            # Thermal model
            heat += (
                p["KT2"] * dc_current**2
                + p["KT1"] * dc_current
                + p["KT0"]
                - p["KE"] * (temperature - ambient_temp)
                + power_heater
                - power_chiller
            )
            temperature = p["A_THERMAL"] * heat

            # Temperature saturation. The lower clamp keys off the chiller's actual
            # power, so a setting with no power map entry cannot engage it.
            temperature = min(temperature, p["MAX_BATTERY_TEMP"])
            if power_chiller > 0.0:
                temperature = max(temperature, p["COOLANT_TEMP"])
            heat = temperature / p["A_THERMAL"]  # keep heat state consistent

            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "soc_percent": round(soc_percent, 4),
                "q_act_as": round(q_act, 4),
                "ocv_v": round(ocv, 4),
                "dc_voltage_v": round(dc_voltage, 4),
                "dc_current_a": round(dc_current, 4),
                "rc1_voltage_v": round(v_rc1, 6),
                "rc2_voltage_v": round(v_rc2, 6),
                "temperature_c": round(temperature, 4),
                "heat_j": round(heat, 4),
                "derating_factor": round(derating_factor, 4),
                "requested_power_w": requested_power,
                "ambient_temp_c": ambient_temp,
            }

            # The "applied" echo is the dashboard's only source of truth for control
            # element state after a reload, and its implicit NACK: a rejected write
            # changes nothing, so the old value comes back and the knob snaps back.
            if echo_due:
                payload["applied"] = {
                    "signals": setpoints,
                    "parameters": {k: v for k, v in p.items() if not k.startswith("_")},
                }
                last_echo = now

            msg = out_topic.serialize(key="battery-sim", value=payload)
            producer.produce(topic=out_topic.name, value=msg.value, key=msg.key)

            logger.debug(payload)
            time.sleep(SAMPLE_TIME)


def supervise_simulation(producer_app, out_topic, consumer_app):
    """Thread entry point for `run_simulation`.

    Earned guard: an uncaught exception in this thread used to kill only the thread.
    `consumer_app.run()` kept the main thread alive, so the deployment stayed green
    while publishing nothing — silent death, the one failure class QuixStreams cannot
    surface for us. Log it CRITICAL, stop the Application through its documented
    cross-thread handle, and let `__main__` exit non-zero so the platform restarts.
    """
    try:
        run_simulation(producer_app, out_topic)
    except BaseException:
        logger.critical("Simulation thread died — stopping the service.", exc_info=True)
        _sim_failed.set()
        consumer_app.stop(fail=True)


def log_startup():
    """Effective configuration, so the deployment log shows what actually loaded."""
    logger.info("[STARTUP] lexicon: %s (v%s)", LEXICON_PATH, LEXICON["lexicon_version"])
    logger.info(
        "[STARTUP] topics: in=%s out=%s consumer_group=%s",
        input_topic_name,
        output_topic_name,
        consumer_group,
    )
    for name, spec in PARAM_SPEC.items():
        logger.info(
            "[STARTUP] %-16s = %-10s %s",
            name,
            params[name],
            "(tunable)" if spec["tunable"] else "(FIXED)",
        )
    for name, value in cmd.items():
        logger.info("[STARTUP] %-16s = %s", name, value)


if __name__ == "__main__":
    # Separate Application instances: producer in background, consumer in main thread
    # so that QuixStreams signal handlers (SIGTERM) are registered correctly.
    producer_app = Application(consumer_group=f"{consumer_group}-prod")
    out_topic = producer_app.topic(output_topic_name)

    consumer_app = Application(consumer_group=consumer_group)
    in_topic = consumer_app.topic(input_topic_name)

    log_startup()

    threading.Thread(
        target=supervise_simulation,
        args=(producer_app, out_topic, consumer_app),
        daemon=True,
    ).start()

    sdf = consumer_app.dataframe(in_topic)
    sdf = sdf.filter(is_command).update(handle_command)
    consumer_app.run(sdf)

    if _sim_failed.is_set():
        raise SystemExit(1)
