# Bug Log: thermal-dt

**Spec:** README.md (dc-battery-sim/README.md) — "Thermal Model", "Derived thermal
parameter values", "Initial Conditions", "Parameters" sections. Corroborated by
dev-planning/parameter-contract/spec.md:269-270 (KT2/KE range-derivation comments,
which assume per-second integration).
**Test suite:** dc-battery-sim/tests/test_thermal_dt.py

## Round 1 — 2026-09-15

### Bug 1.1: Thermal integration is missing its `SAMPLE_TIME` factor — runs 10x too fast

**Test:** `test_kt2_matches_documented_ohmic_heating_rate`,
`test_ke_matches_documented_time_constant`,
`test_heater_delivers_rated_watts_per_second`,
`test_thermal_rate_scales_with_sample_time_not_tick_count` — all in
`dc-battery-sim/tests/test_thermal_dt.py`

**Spec reference:** README.md "Derived thermal parameter values": `"|I| = 300 A" ->
"dT/dt = +0.5 °C/s (= 1 °C per 2 s)"` gives `KT2 = 0.0278`, and `"No current ->
battery reaches T_ambient in ≈ 3 hours (τ ≈ 52 min)"` confirms `KE = 1.6`. Both are
explicitly per-*second* rates. README.md "Thermal Model" documents every summed
term (`KT2·I²`, `KT1·I`, `KT0`, `-KE·(T-T_ambient)`, `P_heater`, `P_chiller`) in
watts, and `Heat` in joules — watts integrated into joules requires multiplying by
elapsed time. Compare README's own charge-counting equation on the same page,
`Q_act = Q_prev + I_dc × sample_time`, which does carry the time factor.

**Expected:** At the defaults (`SAMPLE_TIME=0.1`, `KT2=0.0278`, `KE=1.6`,
`A_THERMAL=0.0002`), one simulated second of `|I|=300 A` (KE/RC zeroed to isolate
the term) raises the pack by `0.5 °C`; one simulated second of a 50 °C ambient
offset (zero current) closes it by `A_THERMAL·KE·50 = 0.016 °C`; one simulated
second of `HEATER_POWER_LOW=2500 W` (current/KE zeroed) raises the pack by
`2500·A_THERMAL·1.0 = 0.5 °C`. Critically, the same *simulated duration* must
produce the same temperature change regardless of how many ticks it is divided
into (`SAMPLE_TIME` is a physical duration per README's "Parameters" table: "it
sets the loop rate").

**Actual:** `main.py`'s thermal block —

```python
heat += (
    p["KT2"] * dc_current**2
    + p["KT1"] * dc_current
    + p["KT0"]
    - p["KE"] * (temperature - ambient_temp)
    + power_heater
    - power_chiller
)
temperature = p["A_THERMAL"] * heat
```

— adds the whole watts-valued sum into `heat` once per TICK, never multiplied by
`SAMPLE_TIME`. At the default `SAMPLE_TIME=0.1 s` this makes the accumulator
advance 10x per simulated second (10 ticks/s, each contributing a full "1 second's
worth" of heat). Measured:

| Test | Measured | Expected | Ratio |
|---|---|---|---|
| `test_kt2_matches_documented_ohmic_heating_rate` | 5.0034 °C/s | 0.5 °C/s | 10.007 |
| `test_ke_matches_documented_time_constant` | −0.15980 °C/s | −0.01600 °C/s | 9.988 |
| `test_heater_delivers_rated_watts_per_second` | 5.0000 °C | 0.5 °C | 10.000 |
| `test_thermal_rate_scales_with_sample_time_not_tick_count` | 5.0000 °C (10 ticks @ 0.1s) vs 1.0000 °C (2 ticks @ 0.5s) for the *same* 1 s simulated duration | equal | 5.0 (tracks tick-count ratio 10:2, not the equal duration) |

This also matches live deployment telemetry (zero current, ambient 23.5 °C, pack
22.46 °C closing the 1.04 °C gap at ≈0.0033 °C/s, i.e. τ≈5.2 min) against README's
own derivation of τ≈52 min at the default KE=1.6 — exactly the 10x-too-fast
signature, and exactly what `dev-planning/parameter-contract/spec.md:270`
describes as the behaviour of `KE=16` (10x default) under *correctly* integrated
per-second physics: `"16 = 10x default -> τ ≈ 5.2 min instead of 52 min"`. The
current code reproduces that 5.2 min behaviour at the *default* KE because of the
missing `SAMPLE_TIME` factor, not because KE is actually mistuned.

**Reproduction:** `cd dc-battery-sim && python -m pytest tests/test_thermal_dt.py -v -s`

**Root cause layer:** code

**Suspected root cause:** `heat += (...)` in `run_simulation` (main.py, the block
immediately following the RC branch voltage update) omits `* SAMPLE_TIME`, unlike
the structurally identical charge-counting line two blocks above it
(`q_act += dc_current * SAMPLE_TIME`).

**Suggested fix:** Multiply the entire parenthesized sum by `SAMPLE_TIME` before
adding to `heat` — i.e. `heat += (...) * SAMPLE_TIME`, mirroring the charge-counting
pattern already in the same loop. No constant (`KT2`, `KE`, `A_THERMAL`, the four
`*_POWER_*` parameters) needs to change; they are already calibrated in per-second
units per README's derivation section.

---

## Existing test suite: no changes required

All 45 pre-existing tests in `dc-battery-sim/tests/` were audited for thermal-rate
assumptions and re-run after adding the four new tests above; **all 45 still pass,
unchanged, both before and after this round** (see Sanity print below). None needs
a value update once ArchDev applies the fix. Specifically:

- **`test_power_map.py::test_heater_power_change_alters_thermal_integration`** and
  **`test_power_map.py::test_power_parameter_change_takes_effect_on_next_tick`**
  measure `delta_before ≈ 0.4987 °C` per tick at the default `HEATER_POWER_LOW=2500`
  W (confirmed by manual calculation and consistent with the bug's 10x-too-fast
  rate) and `delta_after` at `HEATER_POWER_LOW=10000` W, but assert only the RATIO
  `delta_after > 3 * delta_before`. Since the missing `SAMPLE_TIME` factor scales
  every term in the sum uniformly, `delta_before` and `delta_after` are both scaled
  by the same missing factor and the ratio between them is dt-invariant: under the
  fix, `delta_before` becomes ≈0.0499 °C/tick and `delta_after` ≈0.1999 °C/tick,
  ratio ≈4.0 either way. No change needed.
- **`test_power_map.py::test_zero_chiller_power_disables_coolant_clamp`** asserts
  `temperature_c < 25.0` after 3 ticks of residual KT2 self-heating with no active
  cooling. The fix makes the pack heat *more slowly*, which only makes this bound
  easier to satisfy. No change needed.
- **`test_power_map.py::test_nonzero_chiller_power_engages_coolant_clamp`** asserts
  `temperature_c == approx(40.0)` on tick 1 — this is the unconditional
  `max(temperature, COOLANT_TEMP)` saturation clamp, which fires regardless of the
  thermal integration rate. No change needed.
- **`test_thermal_rebase.py`** (both tests) check A_THERMAL-rebase continuity
  (`temp_after_change == approx(temp_before, abs=0.5)`) and same-tick
  `heat_j == temperature_c / A_THERMAL` self-consistency. The fix *reduces* drift
  over the 3-tick window (tighter physics), so the continuity bound is easier to
  satisfy, and the self-consistency check is dt-independent by construction. No
  change needed.
- **`test_command_validation.py`**'s four `test_c1_*_does_not_crash_the_service`
  tests only assert `len(ticks) == 1` (the service didn't crash) — no thermal
  magnitude involved. No change needed.
- **`test_rc_circuit.py`** and the KE/KT2 rejection tests in
  `test_command_validation.py` exercise RC-branch voltages and parameter
  validation, neither of which touches the thermal accumulator. No change needed.

## Sanity print

```
tests/test_thermal_dt.py::test_kt2_matches_documented_ohmic_heating_rate FAILED
  [KT2 rate] measured=5.0034 C/s expected=0.5 C/s ratio=10.007
tests/test_thermal_dt.py::test_ke_matches_documented_time_constant FAILED
  [KE rate] measured=-0.15980 C/s expected=-0.01600 C/s ratio=9.988
tests/test_thermal_dt.py::test_heater_delivers_rated_watts_per_second FAILED
  [Heater rise] measured=5.0000 C expected=0.5 C ratio=10.000
tests/test_thermal_dt.py::test_thermal_rate_scales_with_sample_time_not_tick_count FAILED
  [dt invariance] SAMPLE_TIME=0.1 (10 ticks) delta=5.0000 C; SAMPLE_TIME=0.5 (2 ticks) delta=1.0000 C; ratio=5.000

======================== 4 failed, 45 passed in 7.76s =========================
```

Lint gate: `pre-commit run --files dc-battery-sim/tests/test_thermal_dt.py` — both
`ruff` and `ruff-format` hooks passed (v0.6.3 pinned).
