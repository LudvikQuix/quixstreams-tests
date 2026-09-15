# Open points — dashboard-service

Raised by ArchDev during the M1 build. Each needs a decision from Buddy (spec) or the user
before M2 closes it.

---

## OP-1 — `layout-schema.json` is at 1.0 and cannot express a multi-series chart — **CLOSED 2026-09-15**

**Root cause layer:** `spec`

**What happened.** The brief makes D1–D7 binding, and CLAUDE.md §4 as amended by **D7** says a
chart takes **1..N bindings** ("Overlaying `ocv_v` against `dc_voltage_v` is the point of a
chart; one-series-per-element would make the RC drop invisible"). The spec's §6.5 and the
normative `layout-schema.json` (`$id …/dashboard-layout/1.0.json`) model exactly **one**
`binding` per element, with `additionalProperties: false` on `element`; the spec's OQ-1
explicitly quotes the **pre-D7** wording of §4 as its reason for keeping one. The two binding
inputs disagree, and the disagreement is not resolvable by reading harder.

**What was built.** The charter won, via the forward-compatible seam OQ-1 itself recommends:

- a chart element carries `bindings: Binding[]`;
- `binding` is retained as the singular alias and always mirrors `bindings[0]`, so a
  schema-1.0 consumer still sees a valid single-series chart;
- a stored 1.0 document (including `example-layout.json`) loads unchanged;
- documents written by this build declare `layout_version: "1.1"` — still major 1.

**What is needed.** Buddy to bump `dev-planning/dashboard-service/layout-schema.json` to
`dashboard-layout/1.1.json`: add `bindings` to `element` (array of `binding`, permitted for
`type: "chart"`, and only there), keep `binding` as the singular alias, and record the
amendment against OQ-1. **Until that lands, M1's localStorage documents are schema-invalid
against the committed 1.0 schema** — which matters the moment M2 turns on server-side
validation at `PUT /api/layouts/{id}`, because that validation would reject every chart the
user built in M1.

**Resolution (fix round 1).** Done by ArchDev rather than Buddy, on the instruction that came
with Tester's Bug 1.3. `layout-schema.json` is now `dashboard-layout/1.1.json` with an optional,
chart-only `bindings` array; `example-layout.json` declares `1.1` and carries a real two-series
chart (`ocv_v` + `dc_voltage_v`). The bump is additive, so every 1.0 document still validates.
See architecture.md §5.4 and §11.

---

## OP-2 — task 0 cannot be completed without a push

**Root cause layer:** `unclear`

The spec makes "prove the WebSocket upgrade on a **deployed build**" the first task of M1, but
deploying requires commit + push to `devDB` + `POST /workspaces/{ws}/pull` + sync, and the
standing rules forbid pushing without an explicit instruction. The build therefore closed the
question on documentary evidence (Quix's own WebSocket-server connector deploys with
`PublicAccess: true` and documents `wss://` against the public URL) and shipped `GET /ws/echo`
so the empirical check is a one-liner on first deploy.

**What is needed.** Either an explicit "push and sync" instruction so the check can be run, or
acceptance that Tester runs it as the first smoke step after the user deploys. If the echo
fails, the SSE fallback must be taken **before** anything else is added to the hub.

---

## OP-3 — no `package-lock.json`, so the image build uses `npm install` — **CLOSED 2026-09-15**

**Root cause layer:** `code`

The spec's dockerfile uses `npm ci`, which requires a committed lockfile. Generating one means
running `npm install` locally, which is a build step this work does not run. The dockerfile
therefore uses `npm install` and says so in a comment.

**What is needed.** One `npm install` in `dashboard/frontend/`, commit the resulting
`package-lock.json`, and switch the dockerfile line back to `npm ci`. Until then an unrelated
upstream release can silently change what the image ships.

**Resolution (fix round 1).** Tester ran `npm install` during verification; the resulting
`dashboard/frontend/package-lock.json` (lockfileVersion 3) is staged, and `dashboard/dockerfile`
is back on `npm ci` with a non-optional `COPY` of the lockfile. The two must be committed
together or the image build fails outright.

---

## OP-4 — the spec's fail-fast boot policy made an un-seeded DCM undeployable

**Root cause layer:** `spec`

**What happened.** Spec §6.2 forbids a bundled copy of the lexicon and requires the service to
exit non-zero when it cannot load one; §6.11 requires `GET /healthz` to answer 503 until it
has. Both were built exactly as written (M1). But the spec also defers the `dcm-seed-lexicon`
Job to M2 and leaves seeding as a manual `curl` in `architecture.md` §9 — so on the first real
deployment the DCM was empty, `load_at_boot()` could never succeed, the pod crash-looped
before uvicorn ever bound a port, and the Quix ingress answered **503 to every route**,
including the health check. The two requirements are individually reasonable and jointly make
the first deploy of any new environment impossible.

**What was built (hotfix round 2, on the user's brief).**

- The service never exits over a lexicon. It boots degraded and keeps retrying (30 s).
- `dashboard/seed/lexicon.json` ships in the image and is POSTed to the DCM **only on a 404**,
  with no `replace` key, so it creates or is declined — it can never overwrite a stored
  document. `LEXICON_SEED_ENABLED=false` disables it. The bundle is never *served*; the DCM
  remains the only source a lexicon is read from, which is what §6.2 was actually protecting.
- `/healthz` (and the new `/api/healthz` alias) answers 200 with `status: "ok" | "degraded"`
  and the reason in the body. `GET /api/lexicon` keeps its 503, now with an explanatory body.
- D8 is respected: no seeder Job, no second deployment, no sidecar.

**What is needed.** Buddy to amend spec §6.2 (boot policy, the seed-if-absent rule and the two
new variables) and §6.11 (the `/healthz` contract, the `/api/healthz` alias, the 503 body on
`/api/lexicon`), and to drop the `dcm-seed-lexicon` Job from the M2 backlog — it is cancelled,
not deferred. Until that lands, `architecture.md` §5.3, §7 (D-9, D-10) and §12 are the
normative description and the spec is stale on these two points.
