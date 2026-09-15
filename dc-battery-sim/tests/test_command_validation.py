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


# --- C1: CHILLER_POWERS[n] / HEATER_POWERS[n] KeyError (main.py:131-132) ---


def test_c1_chiller_setting_out_of_range_does_not_crash_the_service(
    fresh_main, sim_harness
):
    """Validates spec §6.7 (enum value not in the allowed set -> field ignored,
    service keeps running) for §8 R1.1: {"chiller_setting": 5} must not kill the
    producer thread. Today CHILLER_POWERS[5] raises an uncaught KeyError
    (main.py:131) inside the daemon thread, so this test fails with that KeyError
    instead of reaching the assertion."""
    fresh_main.cmd["chiller_setting"] = 5
    harness = sim_harness(fresh_main)
    ticks = harness.wait_for_ticks(1)
    assert len(ticks) == 1


def test_c1_chiller_setting_negative_does_not_crash_the_service(
    fresh_main, sim_harness
):
    """Validates spec §6.7, negative variant of §8 R1.1: {"chiller_setting": -1} has
    no entry in CHILLER_POWERS (main.py:38: keys are only 0, 1, 2) either."""
    fresh_main.cmd["chiller_setting"] = -1
    harness = sim_harness(fresh_main)
    ticks = harness.wait_for_ticks(1)
    assert len(ticks) == 1


def test_c1_heater_setting_out_of_range_does_not_crash_the_service(
    fresh_main, sim_harness
):
    """Validates spec §6.7 for §8 R1.1: {"heater_setting": 5} -> HEATER_POWERS[5]
    KeyError (main.py:132). chiller_setting is left at its valid default (0) so the
    chiller lookup on the preceding line does not mask this one."""
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


# --- C2: float("fast") ValueError (main.py:224) ---


def test_c2_requested_power_non_numeric_string_is_ignored_not_fatal(
    main_dunder_globals,
):
    """Validates spec §6.7 (wrong datatype -> field ignored, old value kept) for
    §8 R1.2: {"requested_power_w": "fast"} must not crash the app. Today
    float("fast") (main.py:224) raises an uncaught ValueError inside sdf.update,
    which takes down consumer_app.run() and the whole service — so this test fails
    with that ValueError before it can check that requested_power was left alone."""
    handle_command = main_dunder_globals["handle_command"]
    cmd = main_dunder_globals["cmd"]
    original_power = cmd["requested_power"]

    handle_command({"requested_power_w": "fast"})

    assert cmd["requested_power"] == original_power


# --- C3: "requested_power_w" in value TypeError (main.py:223) ---
#
# NOTE (surprise, see report): the spec's C3 description lists "a JSON array or
# scalar payload" as reproducing this TypeError via `"requested_power_w" in value`.
# That holds for genuinely non-iterable, non-container scalars (int, float, bool,
# None) but NOT for a string or a list/dict — `"k" in "some string"` and
# `"k" in [1, 2, 3]` are both valid Python (substring / membership checks) and
# return False without raising. A JSON array payload does NOT crash main.py today;
# it silently no-ops. Only int/float/bool/None payloads reproduce C3 as described.
#
# Also a surprise: the exact TypeError message is Python-version-dependent —
# "argument of type 'int' is not iterable" on this environment's Python 3.12.10,
# vs. "... is not a container or iterable" quoted in some other CPython versions.
# These tests therefore assert on behaviour, not on exception message text.


def test_c3_non_dict_int_payload_is_ignored_not_fatal(main_dunder_globals):
    """Validates spec §6.7 (message is not a JSON object -> dropped, service keeps
    running) for §8 R1.3 (scalar case): a bare int payload. Today there is no
    is_command filter, so it reaches `"requested_power_w" in value` (main.py:223)
    and raises an uncaught TypeError instead of being safely ignored."""
    handle_command = main_dunder_globals["handle_command"]
    cmd = main_dunder_globals["cmd"]
    original_power = cmd["requested_power"]

    handle_command(42)

    assert cmd["requested_power"] == original_power


def test_c3_none_payload_is_ignored_not_fatal(main_dunder_globals):
    """Validates spec §6.7 for §8 R1.3 (scalar case): a `null` payload must not
    crash the app either."""
    handle_command = main_dunder_globals["handle_command"]
    cmd = main_dunder_globals["cmd"]
    original_power = cmd["requested_power"]

    handle_command(None)

    assert cmd["requested_power"] == original_power
