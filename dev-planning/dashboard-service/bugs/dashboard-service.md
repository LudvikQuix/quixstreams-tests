# Bug Log: dashboard-service

**Spec:** dev-planning/dashboard-service/spec.md
**Architecture / checklist:** dev-planning/dashboard-service/architecture.md §10
**Test suite:** no dashboard-specific pytest/vitest suite exists yet (M1 verification gate only — pre-commit, backend import smoke, frontend build/type-check/lint, Phase 1 regression, schema validation, static review)
**Full detail:** dev-planning/dashboard-service/verification-report.md (this log is the index; the report has reproductions and evidence)

---

## Round 1 — 2026-09-15

### Bug 1.1: `lexicon.py:293` unused variable fails the pinned ruff hook

**Test:** lint gate — `pre-commit run --all-files` (hook `ruff`)
**Spec reference:** CLAUDE.md "Linting & CI" — pinned pre-commit is the CI-equivalent gate.
**Expected:** Gate passes clean.
**Actual:** `dashboard/backend/lexicon.py:293:17: F841 Local variable 'last' is assigned to but never used`.
**Reproduction:** `pre-commit run --all-files` from repo root at commit `b6ce51a`.
**Root cause layer:** code
**Suspected root cause:** `load_at_boot()`'s retry loop assigns `last = exc` but never reads it.
**Suggested fix:** use `last` in the final raised message, or delete the dead assignment.

### Bug 1.2: `dashboard/backend/lexicon.py` fails `ruff-format`

**Test:** lint gate — `pre-commit run --all-files` (hook `ruff-format`)
**Spec reference:** same as 1.1.
**Expected:** No reformatting needed.
**Actual:** `1 file reformatted, 13 files left unchanged`. Reverted by Tester after inspection (not fixed) so committed code is untouched.
**Reproduction:** `pre-commit run --all-files` from repo root at commit `b6ce51a`.
**Root cause layer:** code
**Suspected root cause:** written by hand to Black/ruff-format shape but never run through the tool, as architecture.md §10.1 itself predicted.
**Suggested fix:** run `ruff format` (or the `pre-commit` hook) on the file and commit.

### Bug 1.3: `layout-schema.json` (1.0) rejects the multi-series chart document the frontend actually produces

**Test:** schema check — `jsonschema.Draft202012Validator` against `layout-schema.json`
**Spec reference:** CLAUDE.md §4 as amended by D7 ("a chart takes several bindings"); `open-points.md` OP-1.
**Expected:** A chart element with `bindings: [a, b]` (as `lib/types/layout.ts`/`binding-picker.tsx` build) validates.
**Actual:** `Additional properties are not allowed ('bindings' was unexpected)`. `example-layout.json` (single-binding, 1.0) itself validates with 0 errors.
**Reproduction:** see verification-report.md §5 for the exact script.
**Root cause layer:** spec
**Suspected root cause:** none beyond the already-diagnosed OP-1 — confirmed empirically, not a new finding.
**Suggested fix:** Buddy/user bumps the schema to 1.1 per OP-1. No ArchDev code change implied.

### Bug 1.4: `POST /api/control` returns an unhandled 500 if the lexicon is not yet loaded

**Test:** manual smoke via `fastapi.testclient.TestClient`
**Spec reference:** architecture.md §10.2 ("422 on a bad field and 202 otherwise" — no third case named, but sibling routes `/api/lexicon` and `/api/lexicon/refresh` both catch `LexiconError` explicitly).
**Expected:** A clean error status (e.g. 503, matching `/healthz`'s convention) when the lexicon has not loaded.
**Actual:** Uncaught `LexiconError` propagates out of `writer.submit()` → bare `500 Internal Server Error`, no JSON body.
**Reproduction:** see verification-report.md Bug 1.4 for the exact script.
**Root cause layer:** code
**Suspected root cause:** `post_control()` in `backend/api.py` has no `try/except LexiconError`, unlike its two neighbours in the same file.
**Suggested fix:** wrap `writer.submit()` the same way `get_lexicon()`/`refresh_lexicon()` do. Low severity — `main.py`'s boot order (lexicon loads before the HTTP thread starts) likely makes this unreachable in production; filed for defensive-coding consistency.

---

No other rounds yet. Re-run the full gate after ArchDev's fixes and append Round 2.
