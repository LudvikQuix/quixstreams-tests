"""Regression tests for D2 / OQ-2 (CLAUDE.md §7 D2; spec §6.2, §8 OQ-2), which
found R1 = R2 = 0.0 in both main.py and app.yaml, making the second-order RC
circuit inert out of the box and contradicting README's documented 0.1 Ω /
0.05 Ω defaults and its "RC dynamics are active out of the box" claim. Fixed:
`lexicon.json` and `app.yaml` now default R1 = 0.1 / R2 = 0.05.

These tests drive real code: R0/R1/R2 are tunable parameters read from the
lexicon-sourced `params` dict (built at main.py:82-84) — accessed here via the
PEP 562 `__getattr__` module view (main.py:121-130, e.g. `fresh_main.R1`) —
and the real `run_simulation` loop (via the `run_ticks` / SimulationHarness
fixtures in conftest.py), with no env var overrides — i.e. exactly the values
a freshly deployed service would start with today.
"""

import pytest


def test_default_r1_matches_readme_value(fresh_main):
    """Validates spec §6.2 / CLAUDE.md D2: R1's startup default should be 0.1 Ω
    (README's parameter table and derivation section), not the pre-fix 0.0."""
    assert fresh_main.R1 == pytest.approx(0.1)


def test_default_r2_matches_readme_value(fresh_main):
    """Validates spec §6.2 / CLAUDE.md D2: R2's startup default should be 0.05 Ω
    (README's parameter table and derivation section), not the pre-fix 0.0."""
    assert fresh_main.R2 == pytest.approx(0.05)


def test_rc1_voltage_becomes_nonzero_under_sustained_current(fresh_main, run_ticks):
    """Validates spec §6.2 / CLAUDE.md D2: with R1 at its corrected default and a
    sustained discharge (the -8000 W default requested_power_w), rc1_voltage_v
    should move off 0 V. Exercises the RC1 IIR term
    `p["R1"] * (1.0 - p["_alpha1"]) * dc_current` (main.py:394) — with the
    pre-fix R1 = 0.0 this term is always exactly 0.0."""
    ticks = run_ticks(fresh_main, count=5)
    assert ticks[-1]["rc1_voltage_v"] != 0.0


def test_rc2_voltage_becomes_nonzero_under_sustained_current(fresh_main, run_ticks):
    """Validates spec §6.2 / CLAUDE.md D2: same as RC1, for rc2_voltage_v
    (main.py:395), which depends on R2."""
    ticks = run_ticks(fresh_main, count=5)
    assert ticks[-1]["rc2_voltage_v"] != 0.0


def test_dc_voltage_diverges_from_ocv_under_sustained_current(fresh_main, run_ticks):
    """Validates spec §6.2 / CLAUDE.md D2: dc_voltage_v = ocv_v - R0*I - V_RC1 - V_RC2
    (main.py:384) should diverge from ocv_v once the RC branches are active. With
    the pre-fix defaults R0 = R1 = R2 = 0.0, dc_voltage_v was identically equal
    to ocv_v every tick."""
    ticks = run_ticks(fresh_main, count=5)
    last = ticks[-1]
    assert last["dc_voltage_v"] != last["ocv_v"]
