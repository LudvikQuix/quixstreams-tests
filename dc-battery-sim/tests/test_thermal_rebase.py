"""Red-first reproductions of OQ-3 (CLAUDE.md §7 D1 / spec §6.5.5, §8 OQ-3):
`temperature_c = A_THERMAL x heat` (main.py:173), so a live A_THERMAL write must
rebase `heat = temperature / A_new` to keep temperature_c continuous.

Class (b) / API-absent: today there is no write path for A_THERMAL at all —
it is a module-level constant set once at import (main.py:19), and the
`params` live-parameter dict named throughout spec §6.5 does not exist yet.
These tests drive the real, already-importable `run_simulation` for the
"before" half, then reach for `fresh_main.params` to perform the live write —
which is exactly where they fail today, with an AttributeError, because the
tunable-parameter surface has not been built.
"""

import pytest


def test_a_thermal_live_write_keeps_temperature_continuous(fresh_main, sim_harness):
    """Validates spec §6.5.5 / CLAUDE.md OQ-3: 'recompute heat = temperature / A_new
    so temperature stays continuous ... the heat state absorbs the step.' A 5x jump
    in A_THERMAL mid-run must not step temperature_c by anywhere near that factor."""
    harness = sim_harness(fresh_main)
    before = harness.wait_for_ticks(3)
    temp_before = before[-1]["temperature_c"]

    # `params` does not exist on current main.py (§6.5.1 names it; it is not built
    # yet) — this is the API-absent failure point for this whole test.
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
