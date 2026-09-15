"""Regression tests for OQ-3 (CLAUDE.md §7 D1 / spec §6.5.5, §8 OQ-3):
`temperature = p["A_THERMAL"] * heat` (main.py:406), so a live A_THERMAL write
must rebase `heat = temperature / A_new` to keep temperature_c continuous.

A_THERMAL is a tunable parameter in `params` (§6.5.1), written the same way as
any other parameter. The rebase itself lives in the loop at main.py:344-350,
immediately after the per-tick snapshot, because `heat` and `temperature` are
locals of `run_simulation` and unreachable from `handle_command`. These tests
drive the real, already-importable `run_simulation` for the "before" half via
the `sim_harness` fixture, then write `fresh_main.params["A_THERMAL"]` directly
to trigger the live rebase.
"""

import pytest


def test_a_thermal_live_write_keeps_temperature_continuous(fresh_main, sim_harness):
    """Validates spec §6.5.5 / CLAUDE.md OQ-3: 'recompute heat = temperature / A_new
    so temperature stays continuous ... the heat state absorbs the step.' A 5x jump
    in A_THERMAL mid-run must not step temperature_c by anywhere near that factor."""
    harness = sim_harness(fresh_main)
    before = harness.wait_for_ticks(3)
    temp_before = before[-1]["temperature_c"]

    # Writing the tunable parameter directly triggers the main.py:344-350
    # rebase on the simulation loop's next iteration.
    fresh_main.params["A_THERMAL"] = fresh_main.params["A_THERMAL"] * 5.0

    after = harness.wait_for_ticks(6)
    temp_after_change = after[3]["temperature_c"]

    assert temp_after_change == pytest.approx(temp_before, abs=0.5)


def test_a_thermal_live_write_updates_heat_j_to_preserve_relation(
    fresh_main, sim_harness
):
    """Validates spec §6.5.5: after an A_THERMAL write, heat_j must satisfy
    T = A_THERMAL x Heat under the NEW A_THERMAL (not the old one) on the very next
    published tick — i.e. heat_j is rebased, not left stale."""
    harness = sim_harness(fresh_main)
    harness.wait_for_ticks(3)

    new_a_thermal = fresh_main.params["A_THERMAL"] * 5.0
    fresh_main.params["A_THERMAL"] = new_a_thermal

    after = harness.wait_for_ticks(6)
    tick = after[3]
    assert tick["heat_j"] == pytest.approx(
        tick["temperature_c"] / new_a_thermal, rel=1e-3
    )
