"""Red-first regression tests for the missing `SAMPLE_TIME` factor in the thermal
integration (main.py, `run_simulation`, the `heat += (...)` block).

Every term summed into `heat` is in watts (README.md "Thermal Model" and "Derived
thermal parameter values"), so integrating them into a joule accumulator once per
tick REQUIRES a `* SAMPLE_TIME` factor -- exactly as charge counting has one
(`q_act += dc_current * SAMPLE_TIME`, main.py). The thermal block has no such
factor, so at the default `SAMPLE_TIME = 0.1 s` the whole thermal model runs
1 / SAMPLE_TIME = 10x too fast: what should take one second of wall-clock time
happens in one 100 ms tick.

These tests assert the PHYSICALLY CORRECT, per-second behaviour documented in
README.md's "Derived thermal parameter values" and "Initial Conditions" sections,
so they are expected to be RED against the current main.py (measuring ~10x the
asserted value) and GREEN once ArchDev adds the missing `* SAMPLE_TIME` factor.

Do not weaken these assertions to match the buggy 10x-too-fast behaviour -- see
dev-planning/parameter-contract/bugs/thermal-dt.md for the full bug report.
"""

import importlib
import sys

import pytest

# README.md "Initial Conditions": Heat = 100_000 J, A_THERMAL = 0.0002 -> 20.0 C.
INITIAL_TEMPERATURE_C = 20.0

# README.md "OCV Look-up Table": 50 % SOC (Q_act starts at Q_max / 2) -> 780 V.
OCV_AT_50_PCT_SOC_V = 780.0


def _isolate_ohmic_term(module):
    """Zero every thermal contributor except KT2 * I**2, and remove RC-branch
    voltage drop so a constant requested power yields a constant current for the
    whole measurement window. KT0/KT1 are already 0.0 by default."""
    module.params["KE"] = 0.0
    module.params["R1"] = 0.0
    module.params["R2"] = 0.0


def _isolate_ambient_exchange_term(module, ambient_offset_c):
    """Zero current (so KT2/KT1/KT0 contribute nothing) and set a fixed ambient
    offset from the 20.0 C starting temperature, isolating -KE * (T - T_ambient)."""
    module.cmd["requested_power_w"] = 0.0
    module.cmd["ambient_temp_c"] = INITIAL_TEMPERATURE_C - ambient_offset_c


def _isolate_heater_term(module):
    """Zero current and ambient exchange, isolating +power_heater."""
    module.cmd["requested_power_w"] = 0.0
    module.params["KE"] = 0.0
    module.cmd["heater_setting"] = 1


def test_kt2_matches_documented_ohmic_heating_rate(fresh_main, sim_harness):
    """Validates README.md "Derived thermal parameter values": '|I| = 300 A ->
    dT/dt = +0.5 C/s (= 1 C per 2 s)' is the constraint KT2 = 0.0278 was derived
    from. Drives ~300 A for one full simulated second (10 ticks at the default
    SAMPLE_TIME = 0.1 s) with every other thermal term zeroed, and asserts the
    measured rate is the documented PER-SECOND rate.

    Bug: `heat += p["KT2"] * dc_current**2 + ...` has no `* SAMPLE_TIME`, so this
    measures ~5.0 C/s (10x too fast) against the current main.py.
    """
    _isolate_ohmic_term(fresh_main)
    fresh_main.cmd["requested_power_w"] = 300.0 * OCV_AT_50_PCT_SOC_V
    fresh_main.cmd["ambient_temp_c"] = INITIAL_TEMPERATURE_C

    harness = sim_harness(fresh_main)
    ticks = harness.wait_for_ticks(10)  # 10 * 0.1 s = 1 simulated second

    measured_rate_c_per_s = ticks[-1]["temperature_c"] - INITIAL_TEMPERATURE_C
    expected_rate_c_per_s = 0.5
    ratio = measured_rate_c_per_s / expected_rate_c_per_s

    print(
        f"[KT2 rate] measured={measured_rate_c_per_s:.4f} C/s "
        f"expected={expected_rate_c_per_s} C/s ratio={ratio:.3f}"
    )
    assert measured_rate_c_per_s == pytest.approx(expected_rate_c_per_s, rel=0.05)


def test_ke_matches_documented_time_constant(fresh_main, sim_harness):
    """Validates README.md "Derived thermal parameter values": 'No current ->
    battery reaches T_ambient in ~3 hours (tau ~ 52 min)' confirms KE = 1.6.
    tau = 1 / (A_THERMAL * KE) = 1 / (0.0002 * 1.6) ~= 3125 s ~= 52.1 min.

    Rather than running a simulated hour, this measures the INITIAL per-second
    rate for a 50 C ambient offset: dT/dt(0) = -A_THERMAL * KE * offset =
    -0.0002 * 1.6 * 50 = -0.016 C/s, over one simulated second (10 ticks at the
    default SAMPLE_TIME = 0.1 s).

    Bug: with the missing `* SAMPLE_TIME`, this measures ~-0.16 C/s (10x too
    fast -- the effective tau collapses to ~5.2 min, matching the live telemetry
    evidence in dev-planning/parameter-contract/bugs/thermal-dt.md).
    """
    ambient_offset_c = 50.0
    _isolate_ambient_exchange_term(fresh_main, ambient_offset_c)

    harness = sim_harness(fresh_main)
    ticks = harness.wait_for_ticks(10)  # 10 * 0.1 s = 1 simulated second

    measured_rate_c_per_s = ticks[-1]["temperature_c"] - INITIAL_TEMPERATURE_C
    a_thermal = fresh_main.params["A_THERMAL"]
    ke = fresh_main.params["KE"]
    expected_rate_c_per_s = -a_thermal * ke * ambient_offset_c
    ratio = measured_rate_c_per_s / expected_rate_c_per_s

    print(
        f"[KE rate] measured={measured_rate_c_per_s:.5f} C/s "
        f"expected={expected_rate_c_per_s:.5f} C/s ratio={ratio:.3f}"
    )
    assert measured_rate_c_per_s == pytest.approx(expected_rate_c_per_s, rel=0.05)


def test_heater_delivers_rated_watts_per_second(fresh_main, sim_harness):
    """Validates README.md "Derived thermal parameter values" units: every
    thermal term, including `power_heater`, is in watts, and T = A_THERMAL *
    Heat (README "Thermal Model"). 2500 W (HEATER_POWER_LOW default) for one
    simulated second must raise the pack by
    2500 W * A_THERMAL * 1.0 s = 2500 * 0.0002 = 0.5 C -- i.e. 0.05 C per tick
    at the default SAMPLE_TIME = 0.1 s, not 0.5 C per tick.

    Bug: with the missing `* SAMPLE_TIME`, 10 ticks accumulate 2500 W of heat
    EACH (not 2500 W * 0.1 s each), so the measured rise is ~5.0 C, 10x the
    documented 0.5 C.
    """
    _isolate_heater_term(fresh_main)

    harness = sim_harness(fresh_main)
    ticks = harness.wait_for_ticks(10)  # 10 * 0.1 s = 1 simulated second

    measured_rise_c = ticks[-1]["temperature_c"] - INITIAL_TEMPERATURE_C
    expected_rise_c = 0.5
    ratio = measured_rise_c / expected_rise_c

    print(
        f"[Heater rise] measured={measured_rise_c:.4f} C "
        f"expected={expected_rise_c} C ratio={ratio:.3f}"
    )
    assert measured_rise_c == pytest.approx(expected_rise_c, rel=0.05)


def test_thermal_rate_scales_with_sample_time_not_tick_count(monkeypatch):
    """Validates the invariant that actually pins the bug: `SAMPLE_TIME` is a
    physical duration (README.md "Parameters": 'it sets the loop rate'), so the
    temperature change over a fixed simulated DURATION must be the same whether
    that duration is covered by many short ticks or few long ones.

    Runs the heater-only scenario (see test_heater_delivers_rated_watts_per_second)
    for one simulated second under two different SAMPLE_TIME values: 10 ticks at
    0.1 s/tick, and 2 ticks at 0.5 s/tick. Both must land at ~+0.5 C.

    SAMPLE_TIME is a fixed (non-tunable) parameter read once at import time into
    a module constant (main.py: `SAMPLE_TIME = params["SAMPLE_TIME"]`), so each
    value under test requires its own from-scratch import via the `SAMPLE_TIME`
    env var -- reimporting `main` under a different module identity for each,
    mirroring the `fresh_main` fixture in conftest.py.

    Bug: `SAMPLE_TIME` never multiplies any term in the `heat += (...)` block,
    so the thermal accumulator advances once per TICK regardless of how long
    that tick represents -- the 10-tick run measures ~5.0 C and the 2-tick run
    measures ~1.0 C, a 5x mismatch tracking the 5x tick-count ratio, not the
    equal 1-second duration.
    """
    # Import the local SimulationHarness the same way conftest.py's `sim_harness`
    # fixture does, since this test needs two independently-configured module
    # instances rather than the single one `fresh_main` provides.
    from conftest import SimulationHarness

    def _import_main_with_sample_time(sample_time_s):
        monkeypatch.setenv("SAMPLE_TIME", str(sample_time_s))
        sys.modules.pop("main", None)
        module = importlib.import_module("main")
        _isolate_heater_term(module)
        return module

    module_fast = _import_main_with_sample_time(0.1)
    ticks_fast = SimulationHarness(module_fast).wait_for_ticks(10)  # 10 * 0.1 = 1 s
    delta_fast = ticks_fast[-1]["temperature_c"] - INITIAL_TEMPERATURE_C

    module_slow = _import_main_with_sample_time(0.5)
    ticks_slow = SimulationHarness(module_slow).wait_for_ticks(2)  # 2 * 0.5 = 1 s
    delta_slow = ticks_slow[-1]["temperature_c"] - INITIAL_TEMPERATURE_C

    sys.modules.pop("main", None)

    ratio = delta_fast / delta_slow
    print(
        f"[dt invariance] SAMPLE_TIME=0.1 (10 ticks) delta={delta_fast:.4f} C; "
        f"SAMPLE_TIME=0.5 (2 ticks) delta={delta_slow:.4f} C; ratio={ratio:.3f}"
    )
    assert delta_fast == pytest.approx(delta_slow, rel=0.05)
