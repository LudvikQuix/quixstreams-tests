"""Tests for the four chiller/heater power parameters (CHILLER_POWER_LOW/HIGH,
HEATER_POWER_LOW/HIGH) and the per-tick `power_map` helper that consumes them.

Validates dev-planning/parameter-contract/architecture.md §2 "The chiller/heater
power map is built per tick, not at import (2026-09-15)", which supersedes
spec.md:587 (that row said `CHILLER_POWERS` / `HEATER_POWERS` stay module
constants — overridden by the 2026-09-15 follow-up making them tunable
parameters instead).
"""

import pytest

POWER_PARAM_NAMES = (
    "CHILLER_POWER_LOW",
    "CHILLER_POWER_HIGH",
    "HEATER_POWER_LOW",
    "HEATER_POWER_HIGH",
)

POWER_PARAM_DEFAULTS = {
    "CHILLER_POWER_LOW": 2500.0,
    "CHILLER_POWER_HIGH": 5000.0,
    "HEATER_POWER_LOW": 2500.0,
    "HEATER_POWER_HIGH": 5000.0,
}


@pytest.mark.parametrize("name", POWER_PARAM_NAMES)
def test_power_parameter_is_writable(fresh_main, name):
    """Validates architecture.md §2: the four power parameters are `tunable: true`.
    A valid in-range write is applied."""
    changed = fresh_main.apply_updates(
        fresh_main.params, fresh_main.PARAM_SPEC, {name: 3000.0}, "parameter"
    )
    assert changed is True
    assert fresh_main.params[name] == pytest.approx(3000.0)


@pytest.mark.parametrize("name", POWER_PARAM_NAMES)
def test_power_parameter_above_max_is_rejected_not_clamped(fresh_main, name):
    """Validates spec §6.7: 'Out of [min, max] -> field ignored, old value kept.
    Never clamped (D1).' Lexicon max for all four is 20000.0 W."""
    changed = fresh_main.apply_updates(
        fresh_main.params, fresh_main.PARAM_SPEC, {name: 20001.0}, "parameter"
    )
    assert changed is False
    assert fresh_main.params[name] == pytest.approx(POWER_PARAM_DEFAULTS[name])


@pytest.mark.parametrize("name", POWER_PARAM_NAMES)
def test_power_parameter_below_min_is_rejected(fresh_main, name):
    """Validates spec §6.7 out-of-range rejection: lexicon min for all four is
    0.0 W, so a negative watt value must be rejected."""
    changed = fresh_main.apply_updates(
        fresh_main.params, fresh_main.PARAM_SPEC, {name: -1.0}, "parameter"
    )
    assert changed is False
    assert fresh_main.params[name] == pytest.approx(POWER_PARAM_DEFAULTS[name])


def test_heater_power_change_alters_thermal_integration(fresh_main, sim_harness):
    """Validates architecture.md §2: a `*_POWER_*` write 'changes the next tick's
    heat balance and nothing else.' Uses the heater, not the chiller: the pack
    starts at exactly 20 C, the default COOLANT_TEMP, so the chiller's lower
    clamp is flat at the starting temperature and a chiller-based version of
    this test would pass vacuously regardless of CHILLER_POWER_LOW/HIGH."""
    fresh_main.cmd["heater_setting"] = 1
    harness = sim_harness(fresh_main)

    before = harness.wait_for_ticks(3)
    delta_before = before[2]["temperature_c"] - before[1]["temperature_c"]

    fresh_main.params["HEATER_POWER_LOW"] = 10000.0

    after = harness.wait_for_ticks(7)
    delta_after = after[6]["temperature_c"] - after[5]["temperature_c"]

    assert delta_after > 3 * delta_before


def test_zero_chiller_power_disables_coolant_clamp(fresh_main, sim_harness):
    """Validates architecture.md §2: 'setting CHILLER_POWER_LOW = 0 makes chiller
    stage 1 a total no-op — no heat removed *and* no coolant clamp.' With
    COOLANT_TEMP raised to 40 (above the pack's starting 20 C) and
    CHILLER_POWER_LOW at 0, the lower clamp (which keys off power_chiller > 0.0)
    must never engage: temperature_c stays near its 20 C start, not 40."""
    fresh_main.cmd["chiller_setting"] = 1
    fresh_main.params["COOLANT_TEMP"] = 40.0
    fresh_main.params["CHILLER_POWER_LOW"] = 0.0

    harness = sim_harness(fresh_main)
    ticks = harness.wait_for_ticks(3)

    for tick in ticks:
        assert tick["temperature_c"] < 25.0


def test_nonzero_chiller_power_engages_coolant_clamp(fresh_main, sim_harness):
    """Validates the inverse of the previous test: with CHILLER_POWER_LOW restored
    to a nonzero value, power_chiller > 0.0 and the COOLANT_TEMP lower clamp
    engages on the very first tick, pinning temperature_c to 40."""
    fresh_main.cmd["chiller_setting"] = 1
    fresh_main.params["COOLANT_TEMP"] = 40.0
    fresh_main.params["CHILLER_POWER_LOW"] = 2500.0

    harness = sim_harness(fresh_main)
    ticks = harness.wait_for_ticks(1)

    assert ticks[0]["temperature_c"] == pytest.approx(40.0)


def test_power_parameter_change_takes_effect_on_next_tick(fresh_main, sim_harness):
    """Validates architecture.md §2: 'Both maps are rebuilt every tick from the
    snapshot' (main.py:357-358) — a mid-run parameter write must be visible on
    the very next published tick, not delayed by an extra cycle."""
    fresh_main.cmd["heater_setting"] = 1
    harness = sim_harness(fresh_main)

    before = harness.wait_for_ticks(2)
    delta_baseline = before[1]["temperature_c"] - before[0]["temperature_c"]

    fresh_main.params["HEATER_POWER_LOW"] = 10000.0

    after = harness.wait_for_ticks(3)
    delta_immediate = after[2]["temperature_c"] - after[1]["temperature_c"]

    assert delta_immediate > 3 * delta_baseline


def test_lexicon_signal_and_parameter_counts(fresh_main):
    """Validates architecture.md §4 file inventory: 'now 16 signals + 18
    parameters (34 entries), 16 of the parameters tunable.'"""
    lexicon = fresh_main.LEXICON
    assert len(lexicon["signals"]) == 16
    assert len(lexicon["parameters"]) == 18
    tunable_count = sum(1 for p in lexicon["parameters"] if p["tunable"])
    assert tunable_count == 16


def test_every_parameter_has_all_eleven_descriptor_fields(fresh_main):
    """Validates spec §6.1: 'Every descriptor carries all eleven keys, always.
    Inapplicable keys are explicitly null, never absent.'"""
    expected_fields = {
        "name",
        "label",
        "description",
        "datatype",
        "unit",
        "default",
        "min",
        "max",
        "enum",
        "direction",
        "tunable",
    }
    for spec in fresh_main.LEXICON["parameters"]:
        assert set(spec.keys()) == expected_fields
