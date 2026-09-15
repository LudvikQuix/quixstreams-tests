"""Red-first reproductions of the three confirmed crash paths (spec §8 R1;
CLAUDE.md D1 rationale; spec §6.7 "the rule that governs all of it: nothing in
this path may raise"). All three are reachable today from a single malformed
`dashboard-in` (currently `ui-data`) message.

Each test asserts the DESIRED post-fix behaviour from spec §6.7 (§4 user story
5: "A message arrives with ... the service logs and continues"): the bad field
is rejected and the service keeps running. On today's code the assertion is
never reached — the real, unmodified exception from main.py fires first and
propagates out of the test uncaught, which is what makes these tests fail for
the right reason (class (a): no exception is faked or reimplemented anywhere
in this file).

C1 fires inside `run_simulation` (module-level, real import — driven here via
the `sim_harness` fixture so the exception is bounded by a timeout instead of
hanging forever on run_simulation's `while True:`). C2/C3 fire inside
`handle_command`, which main.py defines only inside
`if __name__ == "__main__":` — reached via the `main_dunder_globals` fixture
(conftest.py), which executes the literal source with QuixStreams stubbed
out. Nothing here reimplements main.py logic.
"""


# --- C1: chiller_powers[n] / heater_powers[n] KeyError, per-tick power_map()
# lookups built at main.py:362-365 (was CHILLER_POWERS[n] / HEATER_POWERS[n]
# module constants when this test was first written; the C1 backstop
# `.get(setting, 0.0)` at main.py:364-365 is what these tests now exercise) ---


def test_c1_chiller_setting_out_of_range_does_not_crash_the_service(
    fresh_main, sim_harness
):
    """Validates spec §6.7 (enum value not in the allowed set -> field ignored,
    service keeps running) for §8 R1.1: {"chiller_setting": 5} must not kill the
    producer thread. The per-tick `chiller_powers.get(setting, 0.0)` lookup
    (main.py:364) is the C1 backstop that keeps this from raising an uncaught
    KeyError inside the daemon thread."""
    fresh_main.cmd["chiller_setting"] = 5
    harness = sim_harness(fresh_main)
    ticks = harness.wait_for_ticks(1)
    assert len(ticks) == 1


def test_c1_chiller_setting_negative_does_not_crash_the_service(
    fresh_main, sim_harness
):
    """Validates spec §6.7, negative variant of §8 R1.1: {"chiller_setting": -1} has
    no entry in the power map built by `power_map()` (main.py:249: keys are only
    0, 1, 2) either."""
    fresh_main.cmd["chiller_setting"] = -1
    harness = sim_harness(fresh_main)
    ticks = harness.wait_for_ticks(1)
    assert len(ticks) == 1


def test_c1_heater_setting_out_of_range_does_not_crash_the_service(
    fresh_main, sim_harness
):
    """Validates spec §6.7 for §8 R1.1: {"heater_setting": 5} -> the heater power
    map's `.get(setting, 0.0)` lookup (main.py:365) must not raise a KeyError.
    chiller_setting is left at its valid default (0) so the chiller lookup on the
    preceding line does not mask this one."""
    fresh_main.cmd["heater_setting"] = 5
    harness = sim_harness(fresh_main)
    ticks = harness.wait_for_ticks(1)
    assert len(ticks) == 1


def test_c1_heater_setting_negative_does_not_crash_the_service(fresh_main, sim_harness):
    """Validates spec §6.7, negative variant, for heater_setting."""
    fresh_main.cmd["heater_setting"] = -1
    harness = sim_harness(fresh_main)
    ticks = harness.wait_for_ticks(1)
    assert len(ticks) == 1


# --- C2: wrong-datatype rejection, now handled by coerce() (main.py:140) /
# apply_updates() (main.py:166) instead of the old hand-written float(...)
# ValueError path ---


def test_c2_requested_power_non_numeric_string_is_ignored_not_fatal(
    main_dunder_globals,
):
    """Validates spec §6.7 (wrong datatype -> field ignored, old value kept) for
    §8 R1.2: {"requested_power_w": "fast"} must not crash the app. `cmd` is keyed
    by lexicon wire name per §6.5.1 (requested_power -> requested_power_w)."""
    handle_command = main_dunder_globals["handle_command"]
    cmd = main_dunder_globals["cmd"]
    original_power = cmd["requested_power_w"]

    handle_command({"requested_power_w": "fast"})

    assert cmd["requested_power_w"] == original_power


# --- C3: shape gate is `is_command`, a filter placed BEFORE handle_command
# (spec §6.5.6: "the shape check is a filter before the step ... never
# try/except it"). handle_command itself is therefore entitled to assume a
# dict and must not grow a redundant isinstance guard; these tests retarget
# the real gate instead of calling handle_command with a non-dict directly.
#
# NOTE (surprise, see report): the spec's C3 description lists "a JSON array or
# scalar payload" as reproducing a TypeError via `"requested_power_w" in value`
# inside the old if-ladder. That held for genuinely non-iterable, non-container
# scalars (int, float, bool, None) but NOT for a string or a list/dict —
# `"k" in "some string"` and `"k" in [1, 2, 3]` are both valid Python
# (substring / membership checks) and returned False without raising even
# before this change. `is_command` now rejects all of them uniformly by shape
# (not dict), including the list case that never crashed but silently no-opped.


def test_c3_non_dict_int_payload_is_ignored_not_fatal(main_dunder_globals):
    """Validates spec §6.5.6 (is_command is a shape-gate filter placed before
    handle_command): a bare int payload, and a list payload (the surprise finding
    that a list never crashed but silently no-opped — spec §6.7 "message is not a
    JSON object -> dropped"), must both be rejected by is_command without ever
    reaching handle_command, and cmd must be left untouched."""
    is_command = main_dunder_globals["is_command"]
    cmd = main_dunder_globals["cmd"]
    original_power = cmd["requested_power_w"]

    assert is_command(42) is False
    assert is_command([1, 2]) is False

    assert cmd["requested_power_w"] == original_power


def test_c3_none_payload_is_ignored_not_fatal(main_dunder_globals):
    """Validates spec §6.5.6 for §8 R1.3 (scalar case): a `null` payload must be
    rejected by the is_command shape gate before handle_command ever sees it."""
    is_command = main_dunder_globals["is_command"]
    cmd = main_dunder_globals["cmd"]
    original_power = cmd["requested_power_w"]

    assert is_command(None) is False

    assert cmd["requested_power_w"] == original_power
