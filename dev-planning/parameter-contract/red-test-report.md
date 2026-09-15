# Red-first test report — `dc-battery-sim` parameter contract (Phase 1)

**Spec:** `C:\repos\dashboard-tests\dev-planning\parameter-contract\spec.md`
**Lexicon:** `C:\repos\dashboard-tests\dev-planning\parameter-contract\lexicon-battery.json`
**Test suite:** `C:\repos\dashboard-tests\dc-battery-sim\tests\`
**Run:** `python -m pytest tests/ -v` from `dc-battery-sim/`
**Result:** `27 failed in 2.12s` — **0 passed.** No test in this suite passes against
current `dc-battery-sim/main.py`. main.py was NOT modified to produce this result.

**Environment note:** `quixstreams==3.23.1` is pinned in `requirements.txt`; the
installed environment resolves `3.23.4+williams.qcs.metadata.bootstrap.filter`.
`main.py` imports cleanly under this version with no network/broker calls at
import time (all `Application()` construction is inside `if __name__ ==
"__main__":`), so the mismatch did not block writing or running these tests.
Flagged as a version drift worth reconciling, not a test blocker.

**Lint note:** no `.pre-commit-config.yaml` exists in this repo yet, so there is
no pinned-version lint gate to run. The test files were checked with locally
installed `ruff 0.15.12` (`ruff check` clean, `ruff format` applied) as a
best-effort substitute — this is **not** a CI-equivalent gate and should be
revisited once pre-commit is configured.

## Class summary

- **(a) bug-reproducing red** (drives real, unmodified current code; fails with
  the actual exception/assertion): **12 tests** — all of `test_command_validation.py`
  (7) and all of `test_rc_circuit.py` (5).
- **(b) API-absent red** (targets a §6.5 function/attribute that does not exist
  yet on `main.py`; fails with `AttributeError`): **15 tests** — all of
  `test_parameter_updates.py` (13) and all of `test_thermal_rebase.py` (2).
- **Tests that unexpectedly passed: none.** All 27 are red, which is the
  correct starting state for this handoff.

## test_command_validation.py — C1 / C2 / C3 (class a)

| Test | Target | Exception observed | Fired at |
|---|---|---|---|
| `test_c1_chiller_setting_out_of_range_does_not_crash_the_service` | C1 | `KeyError: 5` | `main.py:131` |
| `test_c1_chiller_setting_negative_does_not_crash_the_service` | C1 | `KeyError: -1` | `main.py:131` |
| `test_c1_heater_setting_out_of_range_does_not_crash_the_service` | C1 | `KeyError: 5` | `main.py:132` |
| `test_c1_heater_setting_negative_does_not_crash_the_service` | C1 | `KeyError: -1` | `main.py:132` |
| `test_c2_requested_power_non_numeric_string_is_ignored_not_fatal` | C2 | `ValueError: could not convert string to float: 'fast'` | `main.py:224` |
| `test_c3_non_dict_int_payload_is_ignored_not_fatal` | C3 | `TypeError: argument of type 'int' is not iterable` | `main.py:223` |
| `test_c3_none_payload_is_ignored_not_fatal` | C3 | `TypeError: argument of type 'NoneType' is not iterable` | `main.py:223` |

Each test calls the real `run_simulation` (C1, via a background-thread harness
with the real exception re-raised into the test) or the real `handle_command`
(C2/C3, reached via `runpy.run_path(..., run_name="__main__")` with only
QuixStreams's `Application` and the daemon thread's `.start()` stubbed — the
function body itself is untouched). No test wraps the crash in `pytest.raises`:
each asserts the desired post-fix state (old value preserved, tick still
produced), so the real, uncaught exception is what makes it fail today.

## test_rc_circuit.py — D2 / OQ-2 (class a)

| Test | Target | Failure observed | Fired at |
|---|---|---|---|
| `test_default_r1_matches_readme_value` | D2 | `assert 0.0 == pytest.approx(0.1)` fails | `main.py:31` (module constant) |
| `test_default_r2_matches_readme_value` | D2 | `assert 0.0 == pytest.approx(0.05)` fails | `main.py:32` |
| `test_rc1_voltage_becomes_nonzero_under_sustained_current` | D2 | `assert 0.0 != 0.0` | `main.py:161` (RC1 IIR term, driven live) |
| `test_rc2_voltage_becomes_nonzero_under_sustained_current` | D2 | `assert 0.0 != 0.0` | `main.py:162` |
| `test_dc_voltage_diverges_from_ocv_under_sustained_current` | D2 | `assert 779.9986 != 779.9986` | `main.py:151` |

These drive the real `run_simulation` loop (via the `run_ticks`/`SimulationHarness`
fixtures) with **no env var overrides** — exactly today's deployment default —
and observe the published payload directly.

## test_parameter_updates.py — spec §6.7 (class b)

| Test | Target | Exception observed | Fired at (test file:line) |
|---|---|---|---|
| `test_out_of_range_parameter_write_is_rejected_and_not_clamped` | §6.7 out-of-range | `AttributeError: module 'main' has no attribute 'apply_updates'` | `test_parameter_updates.py:24` |
| `test_unknown_parameter_name_is_ignored` | §6.7 unknown name | same | `:34` |
| `test_fixed_parameter_write_is_rejected` | §6.7 tunable:false | same | `:44` |
| `test_string_value_is_rejected_no_coercion` | §6.7 wrong datatype (string) | same | `:53` |
| `test_none_value_is_rejected` | §6.7 wrong datatype (null) | same | `:63` |
| `test_list_value_is_rejected` | §6.7 wrong datatype (list) | same | `:73` |
| `test_bool_value_rejected_for_float_parameter` | §6.7 bool-before-numeric rule | same | `:84` |
| `test_int_value_accepted_for_float_parameter` | §6.7 int-for-float rule | same | `:94` |
| `test_coerce_rejects_float_for_int_datatype` | §6.7 float-for-int rejection | `AttributeError: module 'main' has no attribute 'coerce'` | `:109` |
| `test_enum_value_outside_allowed_set_is_rejected` | §6.7 enum rejection (also closes C1) | `AttributeError: module 'main' has no attribute 'apply_updates'` | `:118` |
| `test_enum_value_is_canonicalized_to_int` | §6.7 enum canonicalisation | same | `:128` |
| `test_partial_update_leaves_other_parameters_untouched` | §6.3.1 partial update | `AttributeError: module 'main' has no attribute 'params'` | `:139` |
| `test_tau2_write_recomputes_alpha2` | §6.5.2 derived-constant recompute | `AttributeError: module 'main' has no attribute 'apply_updates'` | `:154` |

**Coverage gap, noted in code:** `test_coerce_rejects_float_for_int_datatype`
exercises `coerce(spec, raw)` with a **synthetic** `uint` descriptor, not a real
lexicon entry — the current 30-entry lexicon has zero `uint`/`int`-typed
parameters or signals (all 14 parameters are `float`; the only non-float
signals are the two `enum`s). This is a real coverage gap in the *lexicon*,
not something this test suite can close with real data.

## test_thermal_rebase.py — OQ-3 (class b)

| Test | Target | Exception observed | Fired at |
|---|---|---|---|
| `test_a_thermal_live_write_keeps_temperature_continuous` | OQ-3 heat rebase | `AttributeError: module 'main' has no attribute 'params'` | `test_thermal_rebase.py:27` |
| `test_a_thermal_live_write_updates_heat_j_to_preserve_relation` | OQ-3 heat rebase | same | `:44` |

Both tests drive the real `run_simulation` for an initial 3-tick "before"
window (proving the harness itself works against real code), then fail the
moment they reach for `fresh_main.params["A_THERMAL"]` — the live-parameter
surface named in spec §6.5.1 that does not exist yet.

## Surprises / discrepancies vs. the brief

1. **C3's "JSON array or scalar payload" claim is only half right.** Verified
   directly: `"requested_power_w" in value` raises `TypeError` for `int`,
   `float`, `bool`, `None` (non-iterable, non-container), but **not** for a
   string or a list/dict — `"k" in "some string"` and `"k" in [1, 2, 3]` are
   valid Python membership/substring checks that return `False` without
   raising. A JSON array payload does **not** crash `main.py` today; it
   silently no-ops (worse in a different way — it's a silent drop with no log,
   which spec §6.7 also forbids, just not via a crash). Documented in
   `test_command_validation.py` and not asserted as a passing test (that would
   violate the red-first requirement); only int/`None` (and by extension
   `bool`/`float`) reproduce C3 as a crash.
2. **The C3 TypeError message text is Python-version-dependent.** This
   environment (3.12.10) produces `"argument of type 'int' is not iterable"`;
   an earlier probe with a different `python3` on this machine produced
   `"argument of type 'int' is not a container or iterable"`. Tests assert on
   behaviour (old value preserved), not message text, to avoid coupling to a
   CPython version.
3. **A naive `pytest.raises(...)` design is backwards for red-first.** The
   first draft of `test_command_validation.py` wrapped the crash paths in
   `pytest.raises(KeyError)`/`pytest.raises(ValueError)`, which **passed**
   today (5 of 7 tests) because the bug reliably reproduces — but that is
   exactly inverted: it would go **red** once ArchDev fixes the bug (no more
   `KeyError`), the opposite of red-first TDD. Rewritten so each test asserts
   the desired post-fix state and lets the real exception propagate uncaught;
   confirmed via `pytest -v` that all 27 tests now fail and 0 pass.

## Infrastructure notes

- `conftest.py`'s `SimulationHarness` starts a real daemon `threading.Thread`
  per test that uses `sim_harness`/`run_ticks` (5 tests: 4 C1 + none reused
  elsewhere, plus the 5 rc_circuit tests and 2 thermal tests = 9 total
  threads across a full run). Threads are daemons and are not joined; a
  `"comparison failed"` message sometimes appears on stderr during interpreter
  shutdown from a stale thread racing module teardown. It is shutdown noise,
  not a test failure — confirmed the `27 failed` count and per-test outcomes
  are unaffected across repeated runs.
- `main_dunder_globals` (used by C2/C3) patches `quixstreams.Application` and
  `threading.Thread.start` before `runpy.run_path(main.py, run_name="__main__")`,
  so `handle_command` is reached by running the literal file, not by
  reimplementing it.
