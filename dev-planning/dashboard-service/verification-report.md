# Verification report — dashboard-service M1

**Spec:** `dev-planning/dashboard-service/spec.md`
**Architecture / checklist:** `dev-planning/dashboard-service/architecture.md` §10
**Commit verified:** `b6ce51a`, branch `devDB`
**Run by:** Tester, 2026-09-15
**Deployed e2e:** NOT run (owned separately, per brief §5)

---

## Round 1

### 1. Lint gate — `pre-commit run --all-files` (ruff v0.6.3, pinned)

```
ruff.....................................................................Failed
- hook id: ruff
- exit code: 1

dashboard\backend\lexicon.py:293:17: F841 Local variable `last` is assigned to but never used
Found 1 error.
No fixes available (1 hidden fix can be enabled with the `--unsafe-fixes` option).

ruff-format..............................................................Failed
- hook id: ruff-format
- files were modified by this hook

1 file reformatted, 13 files left unchanged
```

The `ruff-format` auto-edit to `dashboard/backend/lexicon.py` was **reverted** (`git checkout --`) after the run so production code stays exactly as ArchDev committed it — Tester does not fix code, even a formatter's own edit. See Bug 1.1 and 1.2.

Scope note: this hook covers Python only. `dashboard/frontend/**` is not in `.pre-commit-config.yaml` (confirmed — no frontend hook exists). Checked separately below with `npm run lint` / `type-check` / `build`, run directly since no pinned frontend gate exists yet.

### 2. Backend import / smoke

`import backend.settings|window|lexicon|hub|writer|api` — all five import cleanly, no live broker or DCM contact required. `import main` (module import, not `__main__` execution) also succeeds with dummy, unreachable env values — confirmed no side effect fires before `if __name__ == "__main__":`. `Settings.from_env()` raises `KeyError: 'telemetry_in'` when required env vars are absent, as architecture.md §10.2 claims — not a silent default.

**PASS.**

### 3. Frontend — `npm install`, `npm run build`, `npm run type-check`, `npm run lint`

- `npm install`: succeeded, 458 packages, 2 min. 8 vulnerabilities reported by npm audit (7 high, 1 critical) — inherited from `next@14.2.5` / stale eslint chain, not this build's own code; noted, not filed as a bug (OP-3 already covers the lockfile gap; upgrading Next.js is out of scope for M1 verification).
- **`package-lock.json` generated** and left in place (permitted per brief §5) — `dashboard/frontend/package-lock.json`, untracked, ready for ArchDev/user to commit.
- `npm run type-check` (`tsc --noEmit`): **0 errors**, no output at all.
- `npm run build` (`next build`, `output: 'export'`): **succeeded**. One webpack warning (Tailwind CSS-nesting order in `globals.css`) — cosmetic, not a build failure. Static export verified: `out/index.html`, `out/404.html`, `out/_next/` all present.
- `npm run lint` (`next lint`): **0 warnings, 0 errors** — "No ESLint warnings or errors".

**All four green.**

### 4. Phase 1 regression — `python -m pytest dc-battery-sim/tests/ -v`

```
============================= 27 passed in 2.51s ==============================
```

**27/27, unchanged.** Nothing in this build touched `dc-battery-sim/`.

### 5. Schema check — `example-layout.json` vs `layout-schema.json`

Validated with `jsonschema` (`Draft202012Validator`, draft 2020-12 as declared by `$schema`):

- `example-layout.json` (declares `layout_version: "1.0"`, single `binding` per element) → **0 errors**. Validates cleanly.
- A synthetic document built the way the frontend actually emits a multi-series chart today (`layout_version: "1.1"`, chart element carries `bindings: [...]` alongside `binding`) → **1 error**: `Additional properties are not allowed ('bindings' was unexpected)` at `elements[5]`.

This is exactly OP-1 as ArchDev already described it, now confirmed empirically rather than asserted. **Root cause layer: spec** (schema needs the 1.1 bump; ArchDev's code is doing the documented, forward-compatible thing). Filed as Bug 1.3 for traceability, but it is the same open point as OP-1 — no new work implied for ArchDev, this is Buddy's/the user's schema-bump decision.

### 6. Static review — §4 / D1 / D7 binding rules in the frontend

Reviewed `lib/lexicon/resolve.ts`, `components/binding/binding-picker.tsx`, `lib/types/layout.ts`, `app/page.tsx`.

| Rule | Verdict | Evidence |
|---|---|---|
| Type-in is numeric only | **N/A this milestone** | Type-in is not built in M1 (architecture.md §9 explicitly defers it; `page.tsx` only offers Readout/Chart/Knob add-buttons). Knob's numeric field uses `inputMode="decimal"` (`knob-element.tsx:160`). |
| Readouts offer parameters read-only | **PASS** | `resolve.ts:80` — `candidates()` for a non-control type applies no `tunable` filter to parameters, so both fixed and tunable parameters are offered; `groupOf()` labels the fixed group "Fixed parameters (read-only)". |
| Fixed (`tunable: false`) parameters never offered to a control | **PASS** | `resolve.ts:80` — `entry.tunable === true` filter applies only `if (isControl(type))`; a control's candidate list never contains a fixed parameter row. |
| Chart accepts 1..N bindings | **PASS** | `binding-picker.tsx:64,157-169` — `multi = elementType === "chart"` enables multi-select with an "Apply N series" commit button; `layout.ts` `bindingsOf()`/`withBindings()` carry the array; confirmed round-trippable in memory (see §5 for the schema gap on persistence). |
| No signal or parameter name hardcoded in UI | **PASS** | `rg -n -w '<30 lexicon names + fixed-param names>' dashboard/ --glob '!**/*.test.*' --glob '!**/e2e/**' --glob '!**/node_modules/**' --glob '!**/package-lock.json' --glob '!**/out/**'` → **zero matches**. |

---

## Additional findings beyond the brief's checklist

While exercising `backend/api.py` with a FastAPI `TestClient` to corroborate checklist item 10.2 ("`/api/control` is 422 on a bad field and 202 otherwise"), found an uncaught-exception path: see Bug 1.4.

Also hand-verified, since they were cheap and load-bearing for the checklist's specific claims:
- `RollingWindow`: time-bound eviction relative to the newest sample, hard cap at `max_samples`, `snapshot()` returns equal-length `ts`/series columns with `null` for a missing name — all confirmed by direct unit exercise.
- `hub.Connection.enqueue`: overflow discards the oldest queued `frames` message first; a non-droppable message (`applied`) makes room by discarding a frame rather than being dropped itself — confirmed by direct exercise.
- `lexicon.config_id("sil-lexicon", "dc-battery-sim")` == `sha1("sil-lexicon-dc-battery-sim")` == the seeding curl's id — confirmed byte-for-byte.
- `validate_document()` accepts the real `dc-battery-sim/lexicon.json` with zero problems; indexes to 16 signals + 14 parameters = 30 descriptors, matching checklist step 3's expectation.
- `writer.validate()` rejects `SAMPLE_TIME` and `Q_MAX_AH` by name (D1) with a "fixed at deploy time, not tunable" message; `coerce()` rejects `True` for a float field, rejects `0.5` for an int field, accepts an int for a float field and stores it as `float`, canonicalises an enum value, and returns `ok=False` (blocked, not clamped) for an out-of-range float — `validate()` never reads the second tuple element when `ok` is `False`, so the raw (non-`None`) value returned in that branch is inert, not a functional bug.
- `docker build -f dashboard/dockerfile dashboard/` — **succeeds** end to end (both stages), confirming the two-stage build and static export ArchDev described. Image run with an unreachable `CONFIG_API_URL` and a 3 s boot timeout exits non-zero with a `CRITICAL` log and no silent fallback, exactly as designed. Image removed after the check.

### OP-1 / OP-3 / the 400 ms settle window — Tester's verdict

- **OP-1**: confirmed reproducible (see §5). Root cause is the spec/schema disagreement ArchDev already named, not the code. No action for ArchDev; this is a Buddy/schema decision.
- **OP-3**: confirmed. `npm install` (not `npm ci`) succeeded and a `package-lock.json` now exists at `dashboard/frontend/package-lock.json`, untracked. ArchDev (or the user) can commit it and flip the dockerfile's `RUN npm install` back to `RUN npm ci` — Tester does not make that production-code edit.
- **400 ms settle window** (`SETTLE_MS` in `lib/store/telemetry.ts:45,163`): it is a heuristic, not a guarantee. It only compares the echo's *arrival* time against the write's *send* time; it has no correlation id, so it cannot distinguish "this echo reflects my write" from "this echo is a coincidentally-timed heartbeat that still predates my write reaching the plant." Under normal conditions (round trip ~250 ms, 400 ms margin) this is fine. Under load (broker lag, GC pause, slow consumer) a stale echo landing just past 400 ms would still be misjudged as a rejection, exactly the failure mode the window is meant to prevent — it narrows the race window, it does not close it. This is not a new bug: the architecture doc already names the missing correlation id as a deliberate Phase 1 §6.7 deferral, so I am not filing it as a bug, only confirming the doc's own caveat is accurate and not hidden.

---

## Bugs filed this round

### Bug 1.1: `lexicon.py:293` unused variable fails the pinned ruff hook

**Test:** lint gate — `pre-commit run --all-files`, hook `ruff`
**Spec reference:** CLAUDE.md "Linting & CI" — "Lint via pinned versions... reproduce CI exactly before pushing"; this is the CI-equivalent gate and it is currently red.
**Expected:** `pre-commit run --all-files` passes clean on committed code.
**Actual:** `dashboard/backend/lexicon.py:293:17: F841 Local variable 'last' is assigned to but never used`.
**Reproduction:** `pre-commit run --all-files` from repo root on commit `b6ce51a`.
**Root cause layer:** code
**Suspected root cause:** `load_at_boot()` assigns `last = exc` in the retry loop's except clause (line 293) but never reads `last` anywhere — dead code, likely a leftover from an earlier version of the retry-diagnostics.
**Suggested fix:** either use `last` in the final `raise LexiconError(...) from exc` message, or delete the assignment (the `from exc` chain already preserves the real exception, so it may be genuinely unneeded).

### Bug 1.2: `dashboard/backend/lexicon.py` fails `ruff-format`

**Test:** lint gate — `pre-commit run --all-files`, hook `ruff-format`
**Spec reference:** same as 1.1 — the pinned CI-equivalent gate.
**Expected:** `ruff-format` reports no changes needed.
**Actual:** `1 file reformatted, 13 files left unchanged` — `dashboard/backend/lexicon.py` is not in ruff-format's canonical shape. (The change was reverted by Tester after inspection so committed code is untouched; ArchDev should run `ruff format` locally or via `pre-commit` and commit the result.)
**Reproduction:** `pre-commit run --all-files` from repo root on commit `b6ce51a`.
**Root cause layer:** code
**Suspected root cause:** architecture.md §10.1 itself predicts this: "the code was written to Black/ruff-format shape by hand and never formatted by the tool." Confirmed.
**Suggested fix:** run `ruff format dashboard/backend/lexicon.py` (or the whole `pre-commit` hook) and commit.

### Bug 1.3: `layout-schema.json` (1.0) rejects the multi-series chart document the frontend actually produces

**Test:** schema check — `jsonschema.Draft202012Validator` against `dev-planning/dashboard-service/layout-schema.json`
**Spec reference:** CLAUDE.md §4 as amended by D7 — "A chart takes several bindings"; `dev-planning/dashboard-service/open-points.md` OP-1.
**Expected:** A layout document containing a chart with `bindings: [a, b]` (as `lib/types/layout.ts` and `binding-picker.tsx` actually build) validates against the committed schema.
**Actual:** `Additional properties are not allowed ('bindings' was unexpected)` at `elements[5]` — `additionalProperties: false` on the `element` def in schema 1.0 has no `bindings` key.
**Reproduction:**
```python
import json, jsonschema
schema = json.load(open("dev-planning/dashboard-service/layout-schema.json"))
example = json.load(open("dev-planning/dashboard-service/example-layout.json"))
multi = json.loads(json.dumps(example))
multi["layout_version"] = "1.1"
multi["elements"][5]["bindings"] = [multi["elements"][5]["binding"], multi["elements"][6]["binding"]]
list(jsonschema.Draft202012Validator(schema).iter_errors(multi))  # 1 error
```
**Root cause layer:** spec
**Suspected root cause:** none — this is the exact, already-diagnosed OP-1 (ArchDev's own open-points.md), now reproduced empirically rather than asserted. `example-layout.json` itself (single-binding, 1.0) validates with 0 errors — only a genuinely multi-series document is affected.
**Suggested fix:** as OP-1 already states — Buddy/the user bumps `layout-schema.json` to `dashboard-layout/1.1.json` adding `bindings` (array of `binding`, chart-only). No ArchDev code change implied; flagging so it is not lost as "only a Tester assertion."

### Bug 1.4: `POST /api/control` returns an unhandled 500 (not a graceful error) if the lexicon is not yet loaded

**Test:** manual smoke via `fastapi.testclient.TestClient` (extending architecture.md §10.3 step 6-7, which assumes the lexicon is already loaded)
**Spec reference:** architecture.md §10.2 — "`/api/control` is 422 on a bad field and 202 otherwise" (no third case is named, but every other lexicon-dependent route — `/api/lexicon`, `/api/lexicon/refresh` — explicitly catches `LexiconError` and returns a clean 503/502).
**Expected:** A predictable HTTP error (e.g. 503, matching `/healthz`'s convention for "lexicon not loaded") when `POST /api/control` is called before the lexicon has loaded.
**Actual:** An uncaught `backend.lexicon.LexiconError: lexicon not loaded` propagates out of `writer.submit()` → `ControlWriter.submit()` → `lexicon.require()`, past `post_control()`, and becomes a bare `500 Internal Server Error` with no JSON body (`Internal Server Error` plain text) — the only handler in `api.py` that would catch it, `refresh_lexicon()`'s `except LexiconError`, is not in this call path.
**Reproduction:**
```python
# from dashboard/, with dummy env vars (telemetry_in, control_out, PLANT_KEY,
# CONFIG_API_URL, LEXICON_TARGET_KEY all set), lexicon.load_at_boot() NOT called:
client = TestClient(create_app(settings, lexicon, window, hub, writer), raise_server_exceptions=False)
client.post("/api/control", json={"signals": {"nope": 1}})
# -> 500, "Internal Server Error"
```
**Root cause layer:** code
**Suspected root cause:** `post_control()` in `backend/api.py:110-115` calls `writer.submit(...)` with no `try/except LexiconError`, unlike `get_lexicon()` and `refresh_lexicon()` in the same file which both handle it explicitly.
**Suggested fix:** wrap `writer.submit()` in `post_control()` with the same `except LexiconError` pattern used two functions above it, returning a 503 with a body, matching `/healthz`'s semantics for "lexicon not loaded." Note: in the current boot sequence (`main.py` calls `lexicon.load_at_boot()` before starting the HTTP thread) this path should be unreachable in production, so severity is low — filing for defensive-coding consistency, not as a live incident.

---

## Sanity print (verbatim, as requested)

**pre-commit per-hook lines:**
```
ruff.....................................................................Failed
- hook id: ruff
- exit code: 1
Found 1 error.
ruff-format..............................................................Failed
- hook id: ruff-format
- files were modified by this hook
1 file reformatted, 13 files left unchanged
```

**npm run build final status:** `✓ Generating static pages (4/4)` ... build completed, no error exit.

**tsc error count:** 0 (no output at all from `tsc --noEmit`).

**pytest summary line:** `============================= 27 passed in 2.51s ==============================`

**PASS/FAIL per §4/D7 binding rule:**
- Type-in numeric only: N/A (not built in M1)
- Readouts offer parameters read-only: PASS
- Fixed parameters never offered to a control: PASS
- Chart accepts 1..N bindings: PASS
- No hardcoded signal/parameter name in UI: PASS (zero grep matches)
