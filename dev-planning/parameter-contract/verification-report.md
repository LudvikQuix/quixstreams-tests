# Verification report — `dc-battery-sim` parameter contract (Phase 1, Round 1)

**Spec:** `C:\repos\dashboard-tests\dev-planning\parameter-contract\spec.md`
**Architecture:** `C:\repos\dashboard-tests\dev-planning\parameter-contract\architecture.md`
**Test suite:** `C:\repos\dashboard-tests\dc-battery-sim\tests\`
**Date:** 2026-09-15

## 1. Test edits applied (the only production-adjacent changes this round)

`C:\repos\dashboard-tests\dc-battery-sim\tests\test_command_validation.py`:

- `test_c2_requested_power_non_numeric_string_is_ignored_not_fatal` — `cmd["requested_power"]`
  → `cmd["requested_power_w"]` (spec §6.5.1 rename).
- `test_c3_non_dict_int_payload_is_ignored_not_fatal` — retargeted from `handle_command(42)`
  to `is_command(42) is False` and `is_command([1, 2]) is False` (spec §6.5.6: shape gate is a
  `filter` before `handle_command`, so `handle_command` legitimately assumes a dict). Also
  asserts `cmd["requested_power_w"]` unchanged.
- `test_c3_none_payload_is_ignored_not_fatal` — retargeted to `is_command(None) is False`,
  same rationale, `cmd` unchanged.

No other test file was touched. No production file (`main.py`, `app.yaml`, `README.md`,
`lexicon.json`) was edited.

## 2. New artifact

`C:\repos\dashboard-tests\.pre-commit-config.yaml` — created (did not exist). Pins
`astral-sh/ruff-pre-commit` at `v0.6.3`, hooks `ruff` (lint) and `ruff-format`, repo-wide, no
`mypy` (no type-checking baseline in this repo yet, per brief §4.1).

## 3. Lint gate — `pre-commit run --all-files`

```
ruff.....................................................................Passed
ruff-format..............................................................Passed
```

Both hooks green under the pinned `v0.6.3` versions (not local `ruff 0.15.12`). ArchDev's
`except BaseException:` in `supervise_simulation` and the three `global` statements
(`is_command`, `handle_command`, `run_simulation`) are clean under ruff's **default**
ruleset — `BLE001` (blind except) and `PLW0603` (global statement) are not in the default
rule set (they live in `B`/`PL` extras which are opt-in), so nothing flags them. Confirmed by
the green run above, not inferred.

## 4. Full suite — `python -m pytest tests/ -v`

```
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.0.3, pluggy-1.6.0
collected 27 items

tests/test_command_validation.py::test_c1_chiller_setting_out_of_range_does_not_crash_the_service PASSED
tests/test_command_validation.py::test_c1_chiller_setting_negative_does_not_crash_the_service PASSED
tests/test_command_validation.py::test_c1_heater_setting_out_of_range_does_not_crash_the_service PASSED
tests/test_command_validation.py::test_c1_heater_setting_negative_does_not_crash_the_service PASSED
tests/test_command_validation.py::test_c2_requested_power_non_numeric_string_is_ignored_not_fatal PASSED
tests/test_command_validation.py::test_c3_non_dict_int_payload_is_ignored_not_fatal PASSED
tests/test_command_validation.py::test_c3_none_payload_is_ignored_not_fatal PASSED
tests/test_parameter_updates.py::test_out_of_range_parameter_write_is_rejected_and_not_clamped PASSED
tests/test_parameter_updates.py::test_unknown_parameter_name_is_ignored PASSED
tests/test_parameter_updates.py::test_fixed_parameter_write_is_rejected PASSED
tests/test_parameter_updates.py::test_string_value_is_rejected_no_coercion PASSED
tests/test_parameter_updates.py::test_none_value_is_rejected PASSED
tests/test_parameter_updates.py::test_list_value_is_rejected PASSED
tests/test_parameter_updates.py::test_bool_value_rejected_for_float_parameter PASSED
tests/test_parameter_updates.py::test_int_value_accepted_for_float_parameter PASSED
tests/test_parameter_updates.py::test_coerce_rejects_float_for_int_datatype PASSED
tests/test_parameter_updates.py::test_enum_value_outside_allowed_set_is_rejected PASSED
tests/test_parameter_updates.py::test_enum_value_is_canonicalized_to_int PASSED
tests/test_parameter_updates.py::test_partial_update_leaves_other_parameters_untouched PASSED
tests/test_parameter_updates.py::test_tau2_write_recomputes_alpha2 PASSED
tests/test_rc_circuit.py::test_default_r1_matches_readme_value PASSED
tests/test_rc_circuit.py::test_default_r2_matches_readme_value PASSED
tests/test_rc_circuit.py::test_rc1_voltage_becomes_nonzero_under_sustained_current PASSED
tests/test_rc_circuit.py::test_rc2_voltage_becomes_nonzero_under_sustained_current PASSED
tests/test_rc_circuit.py::test_dc_voltage_diverges_from_ocv_under_sustained_current PASSED
tests/test_thermal_rebase.py::test_a_thermal_live_write_keeps_temperature_continuous PASSED
tests/test_thermal_rebase.py::test_a_thermal_live_write_updates_heat_j_to_preserve_relation PASSED

============================= 27 passed in 2.34s ==============================
```

**27 passed / 0 failed.** Re-run twice to rule out thread-timing flake (the harness threads
are daemons and not joined, per conftest.py's documented shutdown-noise caveat) — both runs
identical, 27/27.

## 5. Smoke checks (1–7, 9 per ArchDev's checklist; 8 skipped — no broker running)

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Import `main` cleanly, no broker/network at import time | PASS | `python -c "import main"` — no exception, no network call (all `Application()` construction stays inside `if __name__ == "__main__":`). |
| 2 | `main.R1 == 0.1`, `main.R2 == 0.05` | PASS | `R1 0.1 R2 0.05` |
| 3 | Alpha values computed from TAU1/TAU2 at import | PASS | `alpha1 0.9048374180359595 alpha2 0.9998333472214507` — matches `exp(-0.1/1.0)` and `exp(-0.1/600.0)`. |
| 4 | Spec counts: 14 parameters, 4 input signals, 16 total signals | PASS | `PARAM_SPEC count 14 SIGNAL_SPEC count 4`, `LEXICON signals 16 params 14`. |
| 5 | `cmd` keyed by lexicon wire names | PASS | `cmd keys ['ambient_temp_c', 'chiller_setting', 'heater_setting', 'requested_power_w']`. |
| 6 | `is_command` gate + drop WARNING fires once, not per-message | PASS | 5× `is_command(42)` produced exactly **one** WARNING log line (at drop count 1); `_dropped_messages` incremented to 5 silently thereafter, confirming the "first and every 1000th" throttle, not per-message spam. |
| 7 | Nested envelope write with `TAU2` recomputes `_alpha2` | PASS | `handle_command({"parameters": {"TAU2": 60.0, "R2": 0.05}})` → `params["TAU2"] == 60.0`, `params["_alpha2"]` recomputed to `exp(-0.1/60.0) = 0.9983347214509387`, matched to `1e-9`. Log showed `INFO Applied parameter TAU2 = 60.0` / `Applied parameter R2 = 0.05`. |
| 8 | End-to-end against a live broker | **SKIPPED** | No broker running — per brief, not attempted, not inferred. |
| 9a | Legacy flat form accepted as signals, int-for-float coerced | PASS | `handle_command({"requested_power_w": -8000.0, "ambient_temp_c": 15})` → both applied; `ambient_temp_c` stored as `15.0` (`<class 'float'>`), confirming int→float coercion on the legacy path too. |
| 9b | Out-of-range `KE` rejected, not clamped | PASS | `apply_updates(params, PARAM_SPEC, {"KE": 150.0}, "parameter")` → `changed is False`, `params["KE"]` stayed at `1.6` (not clamped to `16.0`). Log: `WARNING Rejected parameter 'KE'=150.0: expected float in [0.0, 16.0].` |
| 9c | `LOG_LEVEL` behaviour | PASS | `LOG_LEVEL=DEBUG` in the environment before import → `logging.getLogger("dc-battery-sim").getEffectiveLevel() == 10` (`DEBUG`). Default (`LOG_LEVEL` unset) resolves to `INFO` as observed in every other run above (no `DEBUG` payload lines emitted). |

## 6. `__getattr__` verdict (main.py:124)

**Keep.** Verified directly:

- `main.NONEXISTENT` still raises `AttributeError: module 'main' has no attribute 'NONEXISTENT'`
  — the hook only intercepts names in `PARAM_SPEC`, so a typo is not masked.
- It does not shadow real module attributes: `main.SAMPLE_TIME` resolves to the real bound
  global (`SAMPLE_TIME` is present in `main.__dict__`), because Python only calls a module's
  `__getattr__` when ordinary attribute lookup fails — PEP 562 semantics, confirmed empirically.
- `main.KE` resolves via the hook to `params["KE"]` and is *not* added to `main.__dict__`
  (confirmed: `'KE' in main.__dict__` is `False`), matching its "read-only convenience" framing.
- Assigning `main.R1 = 999.0` does bind a shadowing module global exactly as documented, and
  leaves `params["R1"]` untouched (`0.1`) — the write-must-go-through-`apply_updates` contract
  holds; nothing silently double-writes.

No change recommended to the two `test_rc_circuit.py` tests that read `main.R1`/`main.R2` —
they exercise real, working behaviour.

## 7. Bug list

**None.** All 27 tests pass, both pre-commit hooks pass, and every runnable smoke check (1–7,
9) passes. Check 8 was not run (no broker) and is not counted as a failure.

## 8. Spec/test items not testable this round

None outstanding — the two known gaps documented in ArchDev's architecture doc §6 (no
cross-parameter `MAX_BATTERY_TEMP > COOLANT_TEMP` warning, no `uint`/`int`/`bool` lexicon
coverage) are pre-existing, spec-acknowledged gaps (§8 R4, and the `red-test-report.md`
"coverage gap" note), not new findings — no bug filed for either, consistent with last round.

---

## Round 2 — 2026-09-15

**Scope:** the four new chiller/heater power parameters (`CHILLER_POWER_LOW`,
`CHILLER_POWER_HIGH`, `HEATER_POWER_LOW`, `HEATER_POWER_HIGH`), per architecture.md §2
("The chiller/heater power map is built per tick, not at import (2026-09-15)"), which
supersedes spec.md §6.5 edit-table row `spec.md:587`.

### New artifact

`C:\repos\dashboard-tests\dc-battery-sim\tests\test_power_map.py` — 18 tests:
- 4× parametrized writable (one per power parameter, all four are `tunable: true`).
- 4× parametrized above-max rejection (`20001.0` > lexicon max `20000.0`), value not clamped.
- 4× parametrized below-min rejection (`-1.0` < lexicon min `0.0`).
- `test_heater_power_change_alters_thermal_integration` — heater (not chiller, see rationale
  below) power raised mid-run, asserts the post-change per-tick ΔT exceeds 3× the pre-change
  ΔT.
- `test_zero_chiller_power_disables_coolant_clamp` / `test_nonzero_chiller_power_engages_coolant_clamp`
  — the inverse pair from the brief, confirming `power_chiller > 0.0` (not `chiller_setting > 0`)
  gates the `COOLANT_TEMP` lower clamp.
- `test_power_parameter_change_takes_effect_on_next_tick` — confirms the map is genuinely
  rebuilt per tick from the snapshot, not cached.
- `test_lexicon_signal_and_parameter_counts`, `test_every_parameter_has_all_eleven_descriptor_fields`
  — spec §6.1 / architecture.md §4 file-inventory counts (16 signals, 18 parameters, 16
  tunable, all 11 descriptor keys present on every parameter).

### Docstring fixes (own files, per CLAUDE.md "comments die with the code they describe")

`tests/test_command_validation.py` — lines 24, 32, 45, 55 (and, found in the same sweep, the
C2 section header near the old line 72) rewritten: all cited `CHILLER_POWERS[n]` /
`HEATER_POWERS[n]` as module-constant dicts at `main.py:131-132`/`:38`/`:224`, none of which
exist any more. Now cite the real per-tick `power_map()` helper (`main.py:249`) and its two
call sites / `.get(setting, 0.0)` backstops (`main.py:364-365`), and `coerce`/`apply_updates`
(`main.py:140`/`:166`) for the C2 datatype-rejection case.

`tests/test_parameter_updates.py` — lines 117, 127 rewritten on the same basis.

**Scope note:** `conftest.py`, `test_rc_circuit.py` and `test_thermal_rebase.py` also carry
`main.py:NN` line-number citations that no longer line up exactly with the current 508-line
file (e.g. `main.py:173`, `:19`, `:221`, `:151/161/162`, `:31-32`). These were **not** touched
— out of the brief's explicit scope (§4 "Fix this" named only the two files above) — but they
are the same class of drift and worth a cleanup pass if/when those files are next touched.

### Lint gate — `pre-commit run --all-files`

```
ruff.....................................................................Passed
ruff-format..............................................................Passed
```

`git status --short dc-battery-sim/main.py` shows no diff after the run — ruff-format did not
reflow anything, confirming the file's longest new line (88 chars, not 87 as estimated in the
brief) sits inside ruff's default 88-column limit.

### Full suite — `python -m pytest tests/ -v`

```
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.0.3, pluggy-1.6.0
collected 45 items

tests/test_command_validation.py::test_c1_chiller_setting_out_of_range_does_not_crash_the_service PASSED
tests/test_command_validation.py::test_c1_chiller_setting_negative_does_not_crash_the_service PASSED
tests/test_command_validation.py::test_c1_heater_setting_out_of_range_does_not_crash_the_service PASSED
tests/test_command_validation.py::test_c1_heater_setting_negative_does_not_crash_the_service PASSED
tests/test_command_validation.py::test_c2_requested_power_non_numeric_string_is_ignored_not_fatal PASSED
tests/test_command_validation.py::test_c3_non_dict_int_payload_is_ignored_not_fatal PASSED
tests/test_command_validation.py::test_c3_none_payload_is_ignored_not_fatal PASSED
tests/test_parameter_updates.py::test_out_of_range_parameter_write_is_rejected_and_not_clamped PASSED
tests/test_parameter_updates.py::test_unknown_parameter_name_is_ignored PASSED
tests/test_parameter_updates.py::test_fixed_parameter_write_is_rejected PASSED
tests/test_parameter_updates.py::test_string_value_is_rejected_no_coercion PASSED
tests/test_parameter_updates.py::test_none_value_is_rejected PASSED
tests/test_parameter_updates.py::test_list_value_is_rejected PASSED
tests/test_parameter_updates.py::test_bool_value_rejected_for_float_parameter PASSED
tests/test_parameter_updates.py::test_int_value_accepted_for_float_parameter PASSED
tests/test_parameter_updates.py::test_coerce_rejects_float_for_int_datatype PASSED
tests/test_parameter_updates.py::test_enum_value_outside_allowed_set_is_rejected PASSED
tests/test_parameter_updates.py::test_enum_value_is_canonicalized_to_int PASSED
tests/test_parameter_updates.py::test_partial_update_leaves_other_parameters_untouched PASSED
tests/test_parameter_updates.py::test_tau2_write_recomputes_alpha2 PASSED
tests/test_power_map.py::test_power_parameter_is_writable[CHILLER_POWER_LOW] PASSED
tests/test_power_map.py::test_power_parameter_is_writable[CHILLER_POWER_HIGH] PASSED
tests/test_power_map.py::test_power_parameter_is_writable[HEATER_POWER_LOW] PASSED
tests/test_power_map.py::test_power_parameter_is_writable[HEATER_POWER_HIGH] PASSED
tests/test_power_map.py::test_power_parameter_above_max_is_rejected_not_clamped[CHILLER_POWER_LOW] PASSED
tests/test_power_map.py::test_power_parameter_above_max_is_rejected_not_clamped[CHILLER_POWER_HIGH] PASSED
tests/test_power_map.py::test_power_parameter_above_max_is_rejected_not_clamped[HEATER_POWER_LOW] PASSED
tests/test_power_map.py::test_power_parameter_above_max_is_rejected_not_clamped[HEATER_POWER_HIGH] PASSED
tests/test_power_map.py::test_power_parameter_below_min_is_rejected[CHILLER_POWER_LOW] PASSED
tests/test_power_map.py::test_power_parameter_below_min_is_rejected[CHILLER_POWER_HIGH] PASSED
tests/test_power_map.py::test_power_parameter_below_min_is_rejected[HEATER_POWER_LOW] PASSED
tests/test_power_map.py::test_power_parameter_below_min_is_rejected[HEATER_POWER_HIGH] PASSED
tests/test_power_map.py::test_heater_power_change_alters_thermal_integration PASSED
tests/test_power_map.py::test_zero_chiller_power_disables_coolant_clamp PASSED
tests/test_power_map.py::test_nonzero_chiller_power_engages_coolant_clamp PASSED
tests/test_power_map.py::test_power_parameter_change_takes_effect_on_next_tick PASSED
tests/test_power_map.py::test_lexicon_signal_and_parameter_counts PASSED
tests/test_power_map.py::test_every_parameter_has_all_eleven_descriptor_fields PASSED
tests/test_rc_circuit.py::test_default_r1_matches_readme_value PASSED
tests/test_rc_circuit.py::test_default_r2_matches_readme_value PASSED
tests/test_rc_circuit.py::test_rc1_voltage_becomes_nonzero_under_sustained_current PASSED
tests/test_rc_circuit.py::test_rc2_voltage_becomes_nonzero_under_sustained_current PASSED
tests/test_rc_circuit.py::test_dc_voltage_diverges_from_ocv_under_sustained_current PASSED
tests/test_thermal_rebase.py::test_a_thermal_live_write_keeps_temperature_continuous PASSED
tests/test_thermal_rebase.py::test_a_thermal_live_write_updates_heat_j_to_preserve_relation PASSED

============================= 45 passed in 3.48s ==============================
```

Re-run 5× consecutively (`test_power_map.py` alone, given its thread-timing dependence) — all
5 runs identical, 18/18 passed each time, ~1.1 s per run. No flake observed.

### Thermal assertion — actual measured numbers (test 4)

Instrumented run outside pytest to inspect the real deltas, with `heater_setting = 1` at the
default `HEATER_POWER_LOW = 2500.0`:

| Tick | `temperature_c` |
|---|---|
| 0 | 20.499 |
| 1 | 20.998 |
| 2 | 21.497 |

`delta_before` (tick 2 − tick 1) = **0.4987 °C**, matching the brief's ≈0.5 °C estimate for
2500 W × `A_THERMAL` 0.0002 almost exactly. After `HEATER_POWER_LOW` is raised to `10000.0`
mid-run:

| Tick | `temperature_c` |
|---|---|
| 3 | 23.495 |
| 4 | 25.493 |
| 5 | 27.490 |
| 6 | 29.487 |

`delta_after` (tick 6 − tick 5) = **1.9966 °C**, a **4.00×** ratio over `delta_before` — clear
of the brief's 3× threshold with margin, no threshold adjustment needed. Tick 3, the first one
produced after the write, already shows the jump (23.495, up from 21.497 the tick before —
Δ ≈ 2.0, not the old ≈0.5), which is also the direct evidence for the next-tick-latency
finding below.

### Test 5 — coolant clamp

Both directions of the clamp test hold exactly as specified, no adjustment: with
`CHILLER_POWER_LOW = 0.0` and `COOLANT_TEMP = 40.0`, `temperature_c` stays at ≈20 °C across 3
ticks (clamp never engages, since it keys off `power_chiller > 0.0`); with `CHILLER_POWER_LOW`
restored to `2500.0`, `temperature_c` is pinned to exactly `40.0` on the very first tick.

### Next-tick latency

**Confirmed: yes, the very next tick.** `power_map()` is called fresh every loop iteration
from `p = dict(params)`, the tick's own snapshot (`main.py:335-339`, `:362-363`) — there is no
one-cycle lag. Verified two ways: the instrumented run above (tick 3, the first tick after the
`HEATER_POWER_LOW` write, immediately shows the ≈4× ΔT) and the dedicated
`test_power_parameter_change_takes_effect_on_next_tick`, which passed on every one of 5
consecutive runs.

### C1 regression check

The four pre-existing `test_c1_*` tests in `test_command_validation.py` (chiller/heater
setting out-of-range and negative) still pass unmodified against the new per-tick
`power_map()`/`.get(setting, 0.0)` code path — confirmed by the full-suite run above. No
regression.

### Bugs filed

None. All 45 tests green, both lint hooks green, all smoke and YAML checks pass.

### Smoke checks

| # | Check | Result |
|---|---|---|
| 1 | `python -c "import main"` shows 18 `[STARTUP]` parameter lines, 4 marked `(tunable)` for the new power params (16 tunable total, 2 FIXED) | PASS |
| 2 | `len(main.PARAM_SPEC) == 18` | PASS |
| 3 | `HEATER_POWER_HIGH=7000 python -c "import main; print(main.HEATER_POWER_HIGH)"` → `7000.0` | PASS |
| 4 | `yaml.safe_load` on `dc-battery-sim/app.yaml` | PASS — parses, all 4 vars present |
| 5 | `yaml.safe_load` on root `quix.yaml` | PASS — parses, all 4 vars present |

### Surprises vs. ArchDev's report

None. Every claim in architecture.md §2 held up exactly as described under test: the map is
genuinely rebuilt per tick (not cached), the C1 `.get(setting, 0.0)` backstop is unchanged and
intact, and the `COOLANT_TEMP` clamp's `power_chiller > 0.0` gating behaves exactly as
documented in both directions. No threshold in the test plan needed adjustment — the 3×
margin in the brief turned out conservative against the actual 4.00× measured ratio.
