"""Red-first reproductions of spec §6.7 (error / rejection semantics) and the
§6.5.1-6.5.2 tunable-parameter machinery it depends on.

Class (b) / API-absent for every test in this file: `params`, `PARAM_SPEC`,
`SIGNAL_SPEC`, `coerce`, `apply_updates` and `_recompute_derived` are all
named in spec §6.5 but do not exist on current main.py — parameters are not
writable at all today, only the four legacy `cmd` fields are (via the
non-importable `handle_command`, covered separately by
test_command_validation.py). Each test below calls the future API the way
§6.5.6/§6.7 specify it will be used; on current main.py this fails with
AttributeError the moment it touches `main.params`, `main.PARAM_SPEC`,
`main.SIGNAL_SPEC`, `main.coerce` or `main.apply_updates`.
"""

import math

import pytest


def test_out_of_range_parameter_write_is_rejected_and_not_clamped(fresh_main):
    """Validates spec §6.7: 'Out of [min, max] -> field ignored, old value kept.
    Never clamped (D1).' KE's lexicon max is 16.0 (lexicon-battery.json); 150.0 must
    be rejected and KE must remain at its default 1.6, not clamped to 16.0."""
    changed = fresh_main.apply_updates(
        fresh_main.params, fresh_main.PARAM_SPEC, {"KE": 150.0}, "parameter"
    )
    assert changed is False
    assert fresh_main.params["KE"] == pytest.approx(1.6)


def test_unknown_parameter_name_is_ignored(fresh_main):
    """Validates spec §6.7: 'Unknown name in signals or parameters -> field ignored,
    rest of message applies.'"""
    changed = fresh_main.apply_updates(
        fresh_main.params, fresh_main.PARAM_SPEC, {"NOT_A_PARAM": 1.0}, "parameter"
    )
    assert changed is False
    assert "NOT_A_PARAM" not in fresh_main.params


def test_fixed_parameter_write_is_rejected(fresh_main):
    """Validates spec §6.7: 'Parameter with tunable: false (SAMPLE_TIME, Q_MAX_AH) ->
    field ignored ... "fixed at deploy time".'"""
    changed = fresh_main.apply_updates(
        fresh_main.params, fresh_main.PARAM_SPEC, {"SAMPLE_TIME": 0.5}, "parameter"
    )
    assert changed is False
    assert fresh_main.params["SAMPLE_TIME"] == pytest.approx(0.1)


def test_string_value_is_rejected_no_coercion(fresh_main):
    """Validates spec §6.7: 'No string coercion ... a string is a rejection.'"""
    changed = fresh_main.apply_updates(
        fresh_main.params, fresh_main.PARAM_SPEC, {"KE": "6.0"}, "parameter"
    )
    assert changed is False
    assert fresh_main.params["KE"] == pytest.approx(1.6)


def test_none_value_is_rejected(fresh_main):
    """Validates spec §6.7: 'Wrong datatype ("fast", null, a list, an object) ->
    field ignored, old value kept.'"""
    changed = fresh_main.apply_updates(
        fresh_main.params, fresh_main.PARAM_SPEC, {"KE": None}, "parameter"
    )
    assert changed is False
    assert fresh_main.params["KE"] == pytest.approx(1.6)


def test_list_value_is_rejected(fresh_main):
    """Validates spec §6.7: a list payload for a float parameter is a wrong-datatype
    rejection, same as the None/string cases."""
    changed = fresh_main.apply_updates(
        fresh_main.params, fresh_main.PARAM_SPEC, {"KE": [1, 2]}, "parameter"
    )
    assert changed is False
    assert fresh_main.params["KE"] == pytest.approx(1.6)


def test_bool_value_rejected_for_float_parameter(fresh_main):
    """Validates spec §6.7 coerce(): 'bool is an int subclass in Python ... Reject
    bool explicitly before any numeric check.' True must not sail through as 1.0 for
    KT2 (a float parameter, default 0.0278)."""
    changed = fresh_main.apply_updates(
        fresh_main.params, fresh_main.PARAM_SPEC, {"KT2": True}, "parameter"
    )
    assert changed is False
    assert fresh_main.params["KT2"] == pytest.approx(0.0278)


def test_int_value_accepted_for_float_parameter(fresh_main):
    """Validates spec §6.7 coerce(): 'An int is acceptable for a float ... Accept int
    where float is expected and store float(raw).'"""
    changed = fresh_main.apply_updates(
        fresh_main.params, fresh_main.PARAM_SPEC, {"KT0": 100}, "parameter"
    )
    assert changed is True
    assert fresh_main.params["KT0"] == pytest.approx(100.0)
    assert isinstance(fresh_main.params["KT0"], float)


def test_coerce_rejects_float_for_int_datatype(fresh_main):
    """Validates spec §6.7 coerce(): 'The reverse is not true: 0.5 for an int/uint is
    a rejection.' No uint/int-typed field exists in the current 30-entry lexicon (all
    14 parameters are float) — see red-test-report.md coverage gap — so this exercises
    `coerce` directly with a synthetic uint descriptor, matching its documented generic
    (spec, raw) -> (ok, value) contract."""
    spec = {"datatype": "uint", "min": 0, "max": 10, "enum": None}
    ok, value = fresh_main.coerce(spec, 0.5)
    assert ok is False


def test_enum_value_outside_allowed_set_is_rejected(fresh_main):
    """Validates spec §6.7 coerce(): enum membership is checked against the
    descriptor's `enum` list; 5 is not in chiller_setting's {0, 1, 2}. This is also
    the fix for crash path C1 (§8 R1.1): once apply_updates/coerce sit in front of
    `cmd`, an out-of-range chiller_setting never reaches the per-tick
    `chiller_powers.get(...)` lookup built by `power_map()` (main.py:249,
    called at main.py:362)."""
    changed = fresh_main.apply_updates(
        fresh_main.cmd, fresh_main.SIGNAL_SPEC, {"chiller_setting": 5}, "signal"
    )
    assert changed is False
    assert fresh_main.cmd["chiller_setting"] == 0


def test_enum_value_is_canonicalized_to_int(fresh_main):
    """Validates spec §6.7: 'Enum values are canonicalised ... a wire 1.0 is stored
    as 1. This matters: the dicts returned by `power_map()` (main.py:249) are
    keyed by int.'"""
    changed = fresh_main.apply_updates(
        fresh_main.cmd, fresh_main.SIGNAL_SPEC, {"chiller_setting": 1.0}, "signal"
    )
    assert changed is True
    assert fresh_main.cmd["chiller_setting"] == 1
    assert isinstance(fresh_main.cmd["chiller_setting"], int)


def test_partial_update_leaves_other_parameters_untouched(fresh_main):
    """Validates spec §6.3.1: 'inner key absent means that entry is untouched.'
    Writing KE alone must not touch KT2."""
    original_kt2 = fresh_main.params["KT2"]
    changed = fresh_main.apply_updates(
        fresh_main.params, fresh_main.PARAM_SPEC, {"KE": 6.0}, "parameter"
    )
    assert changed is True
    assert fresh_main.params["KT2"] == pytest.approx(original_kt2)
    assert fresh_main.params["KE"] == pytest.approx(6.0)


def test_tau2_write_recomputes_alpha2(fresh_main):
    """Validates spec §6.5.2 / CLAUDE.md D1: '_recompute_derived() ... runs after
    EVERY parameter write.' Writing TAU2 must update params['_alpha2'] =
    exp(-SAMPLE_TIME / TAU2) — storing the raw TAU2 alone (main.py's current
    ALPHA1/ALPHA2-at-import-only behaviour) is the 'silent no-op' spec §4 story 3
    warns about."""
    fresh_main.apply_updates(
        fresh_main.params, fresh_main.PARAM_SPEC, {"TAU2": 60.0}, "parameter"
    )
    fresh_main._recompute_derived()
    expected_alpha2 = math.exp(-fresh_main.SAMPLE_TIME / 60.0)
    assert fresh_main.params["_alpha2"] == pytest.approx(expected_alpha2)
