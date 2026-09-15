# Architecture — `dc-battery-sim` parameter contract (Phase 1)

**Implements:** `dev-planning/parameter-contract/spec.md` §6.5–§6.7, CLAUDE.md §7 D1 and D2
(including the resolutions of OQ-2 and OQ-3), plus the 2026-09-15 follow-up that made the
chiller/heater power map tunable (§2, "The chiller/heater power map is built per tick").
That follow-up **supersedes** spec §6.5's edit table row `spec.md:587`, which had
`CHILLER_POWERS` / `HEATER_POWERS` staying module constants.
**Code:** `dc-battery-sim/main.py` (521 lines, ~287 of code), `dc-battery-sim/lexicon.py`,
`dc-battery-sim/signals.json`, `dc-battery-sim/parameters.json`, `dc-battery-sim/app.yaml`,
`dc-battery-sim/README.md`.
**Branch:** `devDB`.

---

## 1. What the code does

`dc-battery-sim` publishes the state of a second-order RC battery model on `dashboard-out`
every `SAMPLE_TIME` seconds and accepts live writes on `dashboard-in`. Before this change
only four setpoints were writable, through a hand-written `if`-ladder that coerced with
`float()`/`int()` and crashed the service on anything unexpected. Now both **signals** (the
four setpoints) and **parameters** (eighteen model constants, sixteen of them tunable) are
writable through one generic path that validates every field against the lexicon, rejects
what does not fit without ever raising, recomputes the derived filter coefficients, and
echoes the effective state back on the output topic so a dashboard can recover it after a
reload. The chiller and heater settings choose a stage; four further tunable parameters decide
what each stage is worth in watts, so the dashboard can change what a setting *delivers* and
not only which setting is active.

---

## 2. Why this shape

### One lock, two dicts, one snapshot per tick

The mutable state is `cmd` (four input signals) and `params` (eighteen parameters plus the
private derived keys `_alpha1`/`_alpha2`), both keyed by **lexicon name**, both covered by a
single `state_lock`.

One lock rather than two, because a tick that took the setpoint lock and the parameter lock
separately could observe a torn pair — a new `TAU2` next to the `α₂` computed from the old
one. The simulation loop takes the lock once, copies both dicts, and releases it
(`main.py:336-340`); everything after that reads the copies. The rule that makes this hold is
mechanical: **no module-level tunable is read inside the loop**. `solve_dc_current` therefore
takes `r0` as an argument (`main.py:281`) instead of reading a global, because the function is
called from inside the loop and a global read there would sit outside the snapshot.

Two dicts rather than one, because the wire envelope has two namespaces and they have
different rules (`tunable: false` applies only to parameters, `direction: input` only to
signals). Both are served by the same `apply_updates(target, spec_map, updates, kind)` —
keying both on lexicon name is what makes one generic function possible and deletes the
`if`-ladder.

### What stayed a module constant

| Stays a constant | Why |
|---|---|
| `Q_MAX`, `SAMPLE_TIME` | The two `tunable: false` parameters. Nothing can rewrite them at runtime, so hoisting them out of `params` is safe — and `SAMPLE_TIME` in particular is read by `_recompute_derived`, which runs while the lock is held. |
| `DERATING_LUT` | Not in the lexicon, therefore not addressable from the wire. |
| `RESERVED_KEYS`, `_ENV_FOR` | Wiring, not state. |

`Q_MAX_AH` and `SAMPLE_TIME` are *also* present in `params`, so the `applied` echo is a
straight copy of the dict and the dashboard can display fixed parameters read-only. They are
simply never writable.

### The chiller/heater power map is built per tick, not at import (2026-09-15)

`CHILLER_POWERS` / `HEATER_POWERS` used to be module constants — spec §6.5's edit table
(`spec.md:587`) said "stay constants — not in the lexicon", and that is what shipped. Driving
the live dashboard exposed it as a gap rather than a decision: `chiller_setting` was a knob,
but what the knob *delivered* was not reachable from anywhere, while `COOLANT_TEMP` right next
to it already was. **This change supersedes that row of the spec table.**

Four parameters now carry the watts — `CHILLER_POWER_LOW` / `CHILLER_POWER_HIGH` /
`HEATER_POWER_LOW` / `HEATER_POWER_HIGH`, defaults `2500` / `5000` / `2500` / `5000`, range
`0 – 20 000 W`. There is deliberately **no** parameter for setting `0`: 0 W is the definition
of "off", and a tunable "off power" would be a fifth way to say something the enum already
says.

They are `tunable: true` on the D1 criterion, and they clear it more cleanly than most: no
derived constant hangs off them (unlike `TAU1`/`TAU2` → `α`), and no state is expressed in
them (unlike `A_THERMAL` → `heat`, or `Q_MAX_AH` → SOC). They are pure per-tick coefficients,
so a mid-run write changes the next tick's heat balance and nothing else — there is no
discontinuity to absorb and nothing to rebase.

The implementation is a two-line helper, `power_map(low, high) -> {0: 0.0, 1: low, 2: high}`
(`main.py:250`), called twice inside the loop *after* the snapshot:

```python
chiller_powers = power_map(p["CHILLER_POWER_LOW"], p["CHILLER_POWER_HIGH"])
power_chiller  = chiller_powers.get(chiller_setting, 0.0)
```

The values come from `p`, the tick's copy, never from `params` — the same rule that makes
`solve_dc_current` take `r0` as an argument. Rebuilding two three-entry dicts at 10 Hz is
free, and it is the form that keeps the C1 backstop (`.get`, not `[...]`) intact and visibly
unchanged.

One behavioural consequence is worth knowing before it surprises someone: because the
`COOLANT_TEMP` lower clamp keys off `power_chiller > 0.0`, setting `CHILLER_POWER_LOW = 0`
makes chiller stage 1 a total no-op — no heat removed *and* no coolant clamp. That is the
correct reading of "this stage delivers nothing", but it means the clamp is now indirectly
tunable.

### Derived constants are recomputed unconditionally

`ALPHA1`/`ALPHA2` used to be import-time module constants. They now live in `params` under
`_alpha1`/`_alpha2` and are rewritten by `_recompute_derived()` (`main.py:107-116`) after
**every** parameter apply, inside the lock, with no check for whether a `TAU` was among the
fields written. Two `exp()` calls per message at human-clicking rates cost nothing, and the
unconditional form deletes the whole class of "did I remember to check the flag" bug — which
is what makes a `TAU2` write actually change `rc2_voltage_v` instead of being a silent no-op.

The `tau > 0.0` guard is carried over unchanged: τ = 0 disables the lag, and a very small
non-zero τ underflows `exp()` to 0.0 without raising, which is the same thing.

### The thermal integration carries `SAMPLE_TIME` (2026-09-15, bug thermal-dt 1.1)

The thermal block used to be `heat += (kt2·I² + … + P_heater − P_chiller)` with no time
factor, while the Coulomb counting eleven lines above it had one. Every term of that sum is a
**power in watts** and `heat` is in **joules**, so the accumulation was dimensionally wrong:
it advanced one second's worth of heat per *tick*, making the entire thermal model run
`1 / SAMPLE_TIME` = **10× too fast** at the default 0.1 s.

The fix is the missing factor and nothing else. The sum now lands in a named local,
`heat_flow_w`, and the accumulation reads `heat += heat_flow_w * SAMPLE_TIME` — the name
carries the unit so the W × s = J conversion is visible at the call site instead of being
derived from a six-line expression.

**No constant changed, and none should have.** `KT2 = 0.0278` and `KE = 1.6` were derived in
README's "Derived thermal parameter values" from explicitly per-*second* constraints (300 A →
+0.5 °C/s; τ ≈ 52 min to ambient). They were always correct *for a correctly integrated
model*; the integration was what disagreed with them. Re-deriving the constants to match the
buggy rate would have made every documented number in that section wrong instead. The same
reasoning covers the lexicon ranges: `spec.md:270` justifies `KE`'s max of 16 as "10× default
→ τ ≈ 5.2 min instead of 52 min", which is exactly the behaviour the pre-fix code produced at
the *default* KE — the range derivation was written against the correct physics all along.

`SAMPLE_TIME` is read from the module constant, not from `p`, because it is `tunable: false`
and hoisted at import (`main.py:91`) — same as the Coulomb counting line.

**Visible consequence:** wall-clock thermal response is now 10× slower. A live deployment that
closed its gap to ambient with τ ≈ 5.2 min now takes ≈ 52 min, and the pack no longer visibly
races to `MAX_BATTERY_TEMP` under load. That is the documented behaviour, not a regression.

The other three integrations in the loop were checked at the same time and are correct:
`q_act += dc_current * SAMPLE_TIME` already had the factor; both RC branches use ZOH-exact
coefficients `α_i = exp(−SAMPLE_TIME / τ_i)` computed in `_recompute_derived()`
(`main.py:114-116`), so `Δt` is baked into α and a second factor would double-count it.

### `A_THERMAL` rebase (OQ-3)

`temperature = A_THERMAL × heat`, so writing `A_THERMAL` mid-run would step the temperature
reading by the ratio of old to new — the exact property that made `Q_MAX_AH` fixed. OQ-3
resolved this in favour of keeping `A_THERMAL` tunable and rebasing the heat state instead.

The rebase lives in the loop, immediately after the snapshot (`main.py:345-351`), because
`heat` and `temperature` are locals of `run_simulation` and are not reachable from
`handle_command`. One extra loop local, `a_thermal_prev`, records which `A_THERMAL` the
current `heat` is expressed in. On a change, `heat = temperature / A_new` runs *before* the
tick's thermal integration, so the published `temperature_c` is continuous and `heat_j`
satisfies `T = A_new × Heat` on the very next message. It is a three-line block with its own
comment; deleting it reverts to the stepping behaviour.

The `SAMPLE_TIME` fix above does not disturb this. Both rebases — the OQ-3 one at the top of
the tick and the post-saturation `heat = temperature / p["A_THERMAL"]` at the bottom — are
pure algebra on `temperature`, with no time dimension of their own; the fix only scales the
*increment* between them. The end-of-tick invariant `heat == temperature / A_THERMAL` (what
`test_thermal_rebase.py` asserts) therefore still holds exactly, and continuity across an
`A_THERMAL` write is if anything tighter, because a smaller per-tick increment means less
drift inside the measurement window.

### Rejection: field-level, log-only, never raises

`handle_command` runs inside `sdf.update`. An exception there takes down `consumer_app.run()`
and the whole service, which is precisely how `{"requested_power_w": "fast"}` used to kill the
deployment. So there is **no `try`/`except` anywhere on this path**. Two mechanisms replace it:

1. `is_command` (`main.py:201`) is a `filter` placed *before* the update step. It rejects any
   non-dict payload — integers, `null`, and also lists and strings, which never raised but
   silently no-opped. Drops are counted and logged on the first and every thousandth, so a
   misconfigured producer is visible without flooding the log at 10 Hz.
2. `coerce` (`main.py:141`) and `apply_updates` (`main.py:167`) use explicit checks only and
   return `(ok, value)` / `bool` instead of raising.

Granularity is field-level and best-effort: a bad field is dropped with a `WARNING` naming the
value, the datatype and the allowed set; every other field in the same message still applies.
Per-message atomicity was rejected for Phase 1 — with no ack channel, dropping a whole message
because of one bad field is invisible to the user.

Three datatype rules are load-bearing and easy to get wrong:

- **`bool` is checked before any numeric test.** `isinstance(True, int)` is `True` in Python,
  so without the early rejection `{"KT2": true}` would store `1.0` in a physics coefficient.
- **`int` is accepted where `float` is expected**, stored as `float(raw)`. The README's own
  documented payload sends `"ambient_temp_c": 15`. The reverse is rejected.
- **No string coercion at all.** `"-8000"` is a rejection. This is a deliberate narrowing of
  the old `float(value[...])` behaviour: silent string coercion is how a stray character in a
  type-in field becomes a setpoint, and refusing it is also what makes the `try`/`except`-free
  implementation possible.

Out-of-range values are rejected, **never clamped** (D1). A clamped write looks accepted, so
the control and the plant would disagree permanently with nothing to signal it.

### Silent death of the producer thread

The most damaging of the three crash paths was not the one that killed the app — it was
`CHILLER_POWERS[5]` raising `KeyError` inside the daemon thread (the power maps were module
constants then; they are built per tick from `params` now). The thread died,
`consumer_app.run()` kept the main thread alive, and the deployment sat green publishing
nothing. QuixStreams cannot surface that for us, which is exactly the criterion the
`quixstreams-idioms` skill sets for earning a guard.

The fix has three layers, and all three are needed:

1. **Validation at the write path.** `coerce` checks enum membership, so `5` never enters
   `cmd` from the wire in the first place.
2. **A total lookup in the loop.** `chiller_powers.get(setting, 0.0)` (`main.py:365`) — an
   out-of-range setting can no longer kill the thread regardless of how it got into `cmd`.
   The lookup stayed `.get` when the map became per-tick and parameter-driven; the map's key
   set is still `{0, 1, 2}` and still comes from a literal, so nothing a wire write can do
   removes a key. The `COOLANT_TEMP` lower clamp keys off `power_chiller > 0.0` rather than
   `chiller_setting > 0`, which now covers a second case as well as the missing key: a stage
   tuned to 0 W must not engage a clamp for a chiller that is removing no heat.
3. **A supervisor around the thread.** `supervise_simulation` (`main.py:456`) is the thread
   target; `run_simulation` itself is untouched and still has no `try`/`except`, so tests and
   harnesses see real exceptions. On any exception the supervisor logs `CRITICAL` with a
   traceback, sets `_sim_failed`, and calls `consumer_app.stop(fail=True)` — the QuixStreams
   primitive documented for exactly this ("only necessary when manually managing the lifecycle
   of the Application, likely through some sort of threading"). `fail=True` also suppresses the
   checkpoint commit. `__main__` then raises `SystemExit(1)` so the platform restarts the
   service rather than leaving it green.

### Lexicon on disk, as two documents (2026-09-15)

`signals.json` and `parameters.json` ship inside the service and are loaded at import by
`lexicon.py`. The alternative — an inline `PARAM_RANGES` dict — has fewer moving parts but
duplicates eighteen ranges that the dashboard also holds, and the two copies drift the first
time someone widens a knob. `SIGNALS_PATH` / `PARAMETERS_PATH` are the seam for sourcing the
documents from DCM later (D3); a relative value resolves against `main.py`'s directory rather
than the working directory, so the container and pytest behave the same.

**Two files, not one, since D9.** The dashboard publishes the lexicon to DCM as two
independently versioned configurations — `sil-signals` and `sil-parameters` — because
parameters are tuned constantly and signals change only when the plant's interface does. The
on-disk source of truth matches that shape exactly, so no transform sits between the repo and
the DCM. The sim itself does not care about the split: `load_lexicon` merges the two into the
same combined `LEXICON` dict the module always read, and `SIGNAL_SPEC` / `PARAM_SPEC` are
built from it unchanged.

The one thing the merge does add is a **pair check**. Both documents carry the same `model`
block, and `load_lexicon` raises `ValueError` at import if the two `model.name` values
disagree. Both files ship in the same image, so a disagreement is a build error — somebody
updated one document and not the other — and merging it would hand the simulation a parameter
set belonging to a different plant with nothing downstream able to tell.

`lexicon.py` is a separate module rather than ten lines in `main.py` because `main.py` is at
the ~500-line ceiling and the loading now carries a responsibility (pair coherence) that has
nothing to do with the simulation. It is also the *only* seam that is safe to take here:
it holds no state and reads no environment at import, whereas the `params.py` seam named
below would. `tests/conftest.py`'s `fresh_main` fixture pops only `"main"` from `sys.modules`,
so a second module caching env-derived state would survive the re-import and silently defeat
every `monkeypatch.setenv` in the suite.

### Logging

`print(payload)` wrote ten lines per second forever, which made the deployment log useless for
spotting exactly the rejection warnings this change adds. The payload moved to
`logger.debug`, gated by `LOG_LEVEL`; rejections are `WARNING`, accepted writes are `INFO`, and
a `[STARTUP]` block prints both lexicon paths, the model name, and the effective value of all
18 parameters plus the 4 setpoints, so the log answers "which config actually loaded".

---

## 3. Data flows

### Import path (once, at module load)

```
SIGNALS_PATH     ── signals.json     ─┐
                   {lexicon_version, model, signals[16]}
                                      ├─► lexicon.load_lexicon()
PARAMETERS_PATH  ── parameters.json  ─┘     │
                   {lexicon_version, model, parameters[18]}
                                            │
                        model.name disagree? ── ValueError, service does not start
                                            │
                                            ▼
                     LEXICON = {lexicon_version, model, signals[16], parameters[18]}
                                            │
                       ┌────────────────────┴────────────────────┐
                       ▼                                         ▼
     SIGNAL_SPEC = 4 entries                    PARAM_SPEC = 18 entries
     (direction == "input")                     (every parameter, fixed included)
```

`SIGNAL_SPEC` and `PARAM_SPEC` are byte-identical to what the single-file load
produced: the same comprehensions run over the same entries in the same order, and
the merge preserves both arrays verbatim.

### Write path

```
dashboard-in ─► Topic(JSON) ─► sdf.filter(is_command)      non-dict → dropped + counted WARNING
                                      │
                                      ▼
                              sdf.update(handle_command)
                                      │
                    envelope split: {"signals":…, "parameters":…}
                    (neither present → legacy flat form, treated as signals)
                                      │
                            ┌─── with state_lock ───────────────────────┐
                            │  apply_updates(cmd,    SIGNAL_SPEC, …)    │
                            │  apply_updates(params, PARAM_SPEC,  …)    │
                            │      └─ coerce(spec, raw) per field       │
                            │           unknown / fixed / wrong type /  │
                            │           out of range → WARNING, skip    │
                            │  _recompute_derived()   (unconditional)   │
                            │  _echo_due = True       (if anything took) │
                            └───────────────────────────────────────────┘
```

### Tick path

```
run_simulation loop, once per SAMPLE_TIME:

  with state_lock: setpoints = dict(cmd); p = dict(params); echo_due = _echo_due; _echo_due = False
        │
        ├─ echo_due |= (APPLIED_ECHO_PERIOD_S elapsed)
        ├─ if p["A_THERMAL"] changed: heat = temperature / A_new      ← OQ-3 rebase
        ├─ power maps rebuilt from p: power_map(LOW, HIGH).get(setting, 0.0), chiller + heater
        ├─ ocv_lookup(q_act)
        ├─ solve_dc_current(power, ocv, v_rc1, v_rc2, p["R0"])
        ├─ charge/derating saturation → dc_voltage → Coulomb counting
        ├─ v_rc{1,2} = p["_alpha{i}"]·v_rc{i} + p["R{i}"]·(1-α)·I
        ├─ heat += heat_flow_w · SAMPLE_TIME  (W→J) → temperature = p["A_THERMAL"]·heat
        ├─ clamps → heat = temperature / p["A_THERMAL"]
        └─ payload (13 fields) [+ "applied" if echo_due] ─► producer.produce ─► dashboard-out
```

`applied` is emitted on the first tick, on the first tick after an accepted write, and every
`APPLIED_ECHO_PERIOD_S` as a heartbeat — roughly 2 messages per minute out of 600 at steady
state. It carries `signals` (the four setpoints) and `parameters` (all 18, with the private
`_alpha*` keys filtered out). Because a rejected write changes nothing, the echo returns the
old value: that is the implicit NACK that lets Phase 1 ship without an ack topic.

---

## 4. File inventory

| File | Change | Why |
|---|---|---|
| `dc-battery-sim/signals.json` | **new** (D9 split of `lexicon.json`) | 16 signals, plus `lexicon_version` and the shared `model` block. Published to DCM as type `sil-signals`, target_key `dc-battery-sim`. The `model` block is duplicated into the parameter document on purpose — it is the only way to detect a mismatched pair (D9). |
| `dc-battery-sim/parameters.json` | **new** (D9 split of `lexicon.json`) | 18 parameters, 16 tunable, same header. Published as type `sil-parameters`. |
| `dc-battery-sim/lexicon.py` | **new**, 66 lines | `lexicon_path()` + `load_lexicon()`: resolve two paths, read both documents, refuse a `model.name` mismatch, return the merged dict. Stateless and env-free at import, which is what makes it safe next to `fresh_main` (§2). |
| `dc-battery-sim/lexicon.json` | **deleted** (was: new, then +4 parameters) | Originally copied verbatim from `dev-planning/parameter-contract/lexicon-battery.json`; now 16 signals + 18 parameters (34 entries), 16 of the parameters tunable. The four `*_POWER_*` entries and the reworded chiller/heater enum labels are **only** here — the planning copy still shows the 14-parameter version, and the rule that the service copy wins on divergence is what makes that survivable. `model.version` 1.0.0 → 1.1.0; `lexicon_version` stays `1.0` because the document *shape* did not change (CLAUDE.md D7 reserves `1.1` for `model.instance_key`). Shipped by the dockerfile's `COPY . .` (line 22) — there is no `.dockerignore`. |
| `dc-battery-sim/main.py` | rewritten in place, 233 → 496 lines; then 496 → 508 for the power map; 508 → 515 for the D9 split; 515 → 521 for the thermal `SAMPLE_TIME` fix | Everything in §2. The thermal fix is six lines in the tick loop and nothing else: the watts-valued sum moved into a named `heat_flow_w` local, the accumulation became `heat += heat_flow_w * SAMPLE_TIME`, and the block's comment was rewritten to state the W × s = J conversion rather than describing the old unit-less form. Deliberately still one file at 521 lines, ~287 of them code — 21 over the ~500 soft ceiling, and splitting to buy back 21 lines would cost an import cycle and a second place to look. **The seam, when it is genuinely needed:** `params.py` taking the lexicon load, `coerce`, `apply_updates` and `_recompute_derived` (~90 lines, no dependency on the loop) and nothing else. Do not split the simulation loop out — it is one coherent tick. |
| `dc-battery-sim/app.yaml` | 3 + 4 variables added, then `LEXICON_PATH` → `SIGNALS_PATH` + `PARAMETERS_PATH`; 18 descriptions rewritten, 3 defaults changed | `LEXICON_PATH`, `LOG_LEVEL`, `APPLIED_ECHO_PERIOD_S` added. `R1` `0.0`→`0.1`, `R2` `0.0`→`0.05` (D2). `input`/`output` defaults moved to `dashboard-in`/`dashboard-out`. Fixed parameters prefixed "FIXED — deploy-time only"; the other sixteen suffixed "Startup default; tunable live over `dashboard-in`" so the Portal tells the truth about which knobs are live. The four `*_POWER_*` variables were added last, after `MAX_BATTERY_TEMP`. |
| `dc-battery-sim/README.md` | parameter table, RC prose, thermal section, power map, Data Flow, Architecture | The table gains Tunable and Range columns; the input section is replaced by the nested envelope plus a rejection table; the output section gains `applied`; the architecture section describes `cmd` + `params` under `state_lock` and the thread supervisor. The "Chiller / Heater power map" table no longer presents 2 500 / 5 000 W as fixed — it names the parameter behind each stage and justifies the 20 kW cap. The "Thermal Model" equation block was `Heat = Heat_prev + HeatDiff [J]` — the missing-`SAMPLE_TIME` bug written down. It now reads `Heat = Heat_prev + HeatFlow × sample_time`, annotates the unit of every term, and says in prose why the derived values below it are per-second rates. |
| `.env`, `.env.example` | 2 lines each | `input=${dashboard_in}` / `output=${dashboard_out}`. Both files already declared `dashboard_in`/`dashboard_out`, which nothing read — `main.py` reads `input`/`output`. Uses the dotenv interpolation already proven there for the token. |
| `dc-battery-sim/requirements.txt` | **no change** | `json`, `logging`, `pathlib` are stdlib. |
| `dc-battery-sim/dockerfile` | **no change** | Confirmed, not assumed: `COPY . .` on line 22, no `.dockerignore` anywhere in the repo. |
| `quix.yaml` | 4 `value:` entries in the `DC Battery Sim` block, then `LEXICON_PATH` replaced by `SIGNALS_PATH` / `PARAMETERS_PATH` | A deployment variable with no `value:` never reaches the container, so the four new parameters would silently fall back to their lexicon defaults. Hand-edited: `quix pipeline update` strips the comments and descriptions this file carries. |

---

## 5. Integration points

- **Producer side of `dashboard-in`** — does not exist yet. The envelope in §6.3 of the spec
  and the rejection table in the README are the contract a dashboard must meet. The keying
  requirement (`"battery-sim"`) is on the producer and cannot be enforced from here.
- **Consumers of `dashboard-out`** — the 13 flat fields are unchanged, no field renamed,
  removed or re-typed. The one visible change is the optional `applied` key on ~2 messages a
  minute; any consumer that assumes a fixed key set must tolerate it. Nothing consumes the
  topic today.
- **DCM (D3)** — out of scope here by design. The service never calls DCM; it reads its own
  two files. `SIGNALS_PATH` / `PARAMETERS_PATH` are where DCM-sourced documents would plug in,
  and nothing else would change. The on-disk shape is now exactly the DCM shape (D9), so that
  plug-in is a swap of `load_lexicon`'s two `read_text` calls for two GETs and nothing more.
- **Root `quix.yaml`** — the `DC Battery Sim` block carries a `value:` for every parameter,
  including the four `*_POWER_*` additions. A variable without one never reaches the container.
- **The dashboard's bundled copies** (`dashboard/seed/signals.json`, `dashboard/seed/parameters.json`)
  are separate files, re-synced by hand — the dashboard image's build context is `dashboard/`
  and a `COPY ../dc-battery-sim/...` is impossible. They were split from `lexicon.json` in the
  same change and are currently byte-identical to the two files here. Drift only matters on a
  *first* boot against an empty DCM, but on that boot it is what gets written.

---

## 6. Known gaps

- **Cross-parameter constraints have no home.** `MAX_BATTERY_TEMP > COOLANT_TEMP` must hold for
  the two clamps not to cross, and the lexicon expresses only per-field ranges. Spec §8 R4
  recommends a `WARNING` when a snapshot violates it; that is **not implemented** — it is
  outside the §6.5 edit plan, and rejecting on a cross-field rule would be wrong anyway, since
  two legitimate partial messages can pass through a transiently invalid pair. The mitigation
  that *is* in place is the range caps (`COOLANT_TEMP` ≤ 40, `MAX_BATTERY_TEMP` ≥ 25).
  `CHILLER_POWER_LOW ≤ CHILLER_POWER_HIGH` (and the heater pair) joins the same list: nothing
  enforces it, and inverting the pair is merely confusing — stage 1 becomes the stronger one —
  not incorrect, so per-field ranges remain the right level to validate at.
- **`DERATING_LUT` is still hardcoded** (`main.py:95-100`) and is the next-most-likely thing
  someone will want to reach. It is not a coefficient but a **shape**: four (temperature,
  factor) breakpoints, whose meaning is positional and whose entries must stay monotonic in
  temperature. The lexicon descriptor has no vocabulary for that — `min`/`max` on a scalar is
  the whole language — so exposing it means either a new `datatype` (`curve` / `array`) with
  its own validation rules and a dashboard element to edit it, or flattening it into eight
  scalar parameters that can be written into a non-monotonic state one field at a time. Both
  are a spec, not a patch. It is also coupled: `MAX_BATTERY_TEMP`'s cap of 60 °C exists only
  because the LUT returns 0 at 60 °C, so a tunable LUT would need that cap to follow it.
- **No uint/int/bool coverage in the lexicon.** All 18 parameters are `float` and the only
  non-float signals are the two enums, so `coerce`'s `int`/`uint`/`bool` branches are exercised
  only by a synthetic descriptor in the test suite. This is a gap in the lexicon, not in the
  code.
- **No ack channel.** A user sees *that* a write did not take (the `applied` echo snaps the
  control back) but not *why*. The reason is in the deployment log. `meta.request_id` plus an
  ack topic is the Phase 2 closure.
- **`main.R1`-style module attribute access** is served by a PEP 562 `__getattr__` that reads
  `params`. It is a read-only convenience for introspection and tests; assigning to it binds a
  shadowing global and changes nothing. Writes must go through `apply_updates`.
