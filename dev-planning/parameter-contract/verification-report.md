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
