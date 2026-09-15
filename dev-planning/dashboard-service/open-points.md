# Open points — dashboard-service

Raised by ArchDev during the M1 build. Each needs a decision from Buddy (spec) or the user
before M2 closes it.

---

## OP-1 — `layout-schema.json` is at 1.0 and cannot express a multi-series chart

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

## OP-3 — no `package-lock.json`, so the image build uses `npm install`

**Root cause layer:** `code`

The spec's dockerfile uses `npm ci`, which requires a committed lockfile. Generating one means
running `npm install` locally, which is a build step this work does not run. The dockerfile
therefore uses `npm install` and says so in a comment.

**What is needed.** One `npm install` in `dashboard/frontend/`, commit the resulting
`package-lock.json`, and switch the dockerfile line back to `npm ci`. Until then an unrelated
upstream release can silently change what the image ships.
