# Dashboard service — configurable SIL grid UI

**Status:** Draft
**Project:** dashboard-tests
**Branch:** `devDB`
**Created:** 2026-09-15
**Planned with:** Buddy
**Phase:** 2 — the dashboard service. Phase 1 (`dev-planning/parameter-contract/`) is built, merged and treated as fixed ground.

---

## 1. Summary

One Quix deployment, one image, that serves a user-buildable grid UI, drives any SIL plant
over `dashboard-in`, and visualises `dashboard-out` live at 10 Hz. It learns what the plant
exposes by reading a **lexicon** from the Dynamic Configuration Manager's REST API (D3), and
it persists the user's grid back into DCM under its own configuration type (D5). Chart
history is a backend in-memory rolling window shipped to each client on connect (D6). No
signal name, parameter name, unit or range appears anywhere in dashboard source: swap the
plant, swap the lexicon, redeploy with two changed environment variables, and the same image
serves the new model.

## 2. Goals

- One deployment, one image, one runtime process — a normal Quix service (CLAUDE.md §2).
- The five element types of CLAUDE.md §4, rendered from lexicon descriptors alone.
- A binding picker filtered by `direction` and `tunable`, with D1 enforced: a `tunable: false`
  parameter can never be bound to a control.
- 10 Hz telemetry to the browser with a bounded, observable degradation path — never an
  unbounded server-side buffer, never a silently frozen chart.
- Grid layouts that round-trip losslessly through DCM and survive a lexicon change.
- Controls that publish the Phase 1 §6.3 nested envelope, validated client-side *and*
  server-side, keyed so partial updates cannot reorder.
- Phone-width to 4K without a hand-rolled breakpoint.

## 3. Non-goals

- Any change to `dc-battery-sim/`. Findings that would need one are raised in §8 as open
  questions, not implemented.
- An ack/error channel for rejected writes. Phase 1 §6.7 deferred it deliberately; §6.8 below
  works within that constraint and §8 OQ-5 re-raises it.
- Authentication and per-user layouts. The deployment sits behind the Quix portal's access
  control; layouts are shared workspace-wide.
- Multi-plant dashboards. One deployment drives exactly one plant instance.
- Lakehouse history, topic replay, or any chart window longer than the in-memory one (D6).
- A lexicon editor. The lexicon is authored by the plant and seeded into DCM; the dashboard
  only reads it.
- Multi-series charts. Blocked by the §4 one-element-one-binding rule — see §8 OQ-1.

## 4. User stories

1. **Build a grid.** User opens the dashboard on a fresh deployment, clicks *Edit*, drags a
   Chart onto the grid, picks `temperature_c` from a list of output signals, leaves edit mode,
   and the chart is drawing within one sample period.
2. **Drive the plant.** User binds a Knob to `requested_power_w`, drags from −8 kW to −40 kW.
   `dc_current_a` on the readout moves within ~200 ms. The knob shows *pending* until the next
   `applied` echo confirms it.
3. **Tune a parameter.** User binds a Knob to `KE` and raises it 1.6 → 6.0. The temperature
   chart visibly bends toward ambient faster.
4. **See a rejection.** A Type-in field is used to send `MAX_BATTERY_TEMP = 150`. The client
   blocks it before publishing, because the lexicon says `max: 60`. The user forces it through
   `POST /api/control` instead; the sim drops the field, the next `applied` still reads 60, and
   the UI snaps the control back with a toast naming the field.
5. **Reload mid-run.** User refreshes. The grid comes back from DCM, every chart is
   immediately populated with the last 60 s from the backend's rolling window, and every
   control renders at the plant's actual value from the cached `applied` block — not the
   lexicon default.
6. **Background a tab.** User switches to another tab for ten minutes and comes back. No
   buffer grew; the charts refill instantly from a fresh snapshot with a visible gap for the
   hidden period.
7. **Lose a signal.** The plant ships a new lexicon without `rc2_voltage_v`. The chart bound to
   it renders as *unbound* with a re-bind button. Nothing else on the grid changes, and the
   stored layout is not rewritten.
8. **Swap the plant.** A different SIL model is deployed. Its lexicon is seeded into DCM,
   `LEXICON_TARGET_KEY` and `PLANT_KEY` are changed on the dashboard deployment, a new layout
   is built. No dashboard code is touched.

## 5. Proposed design

**One image, one process.** A multi-stage Dockerfile builds the Next.js app to a **static
export** with `node:20`, then copies the exported `out/` into a `python:3.13-slim` runtime.
At runtime the container runs exactly one process: Python. `Application.run(sdf)` owns the
main thread (it installs SIGINT/SIGTERM handlers and must); uvicorn, the `dashboard-in`
writer, and the lexicon refresher run on worker threads. FastAPI serves the exported assets,
the JSON API and the WebSocket from **one origin** — so there is no CORS surface, no second
public URL, and no WebSocket-through-a-proxy problem. §6.1 has the full argument and the two
rejected alternatives.

**WebSocket, transport-neutral framing.** One socket per tab carries batched telemetry frames
down and control writes up. Framing is deliberately specified so that the SSE + POST fallback
in §8 R1 is a swap of one client module and one server module, not a redesign.

**Everything the UI knows, it learns at runtime.** The lexicon drives the picker, the
datatypes, the ranges, the units, the labels and the chart axes. The layout document stores
only element ids, types, positions, options and a `{collection, name, direction}` binding
triple — the triple, not a bare name, because Phase 1 §6.1 makes `signals` unique on
`(name, direction)` and `requested_power_w` legitimately exists twice.

**Model-agnosticism is a testable property, not an intention.** §6.9 defines a grep gate the
Tester runs: no lexicon name from any plant may appear in dashboard source.

---

## 6. Work breakdown

### 6.1 Service shape — one deployment, one image, one process

**Owner:** ArchDev · **Depends on:** nothing · **This is the decision everything else rests on.**

Two hard constraints collide:

- **The Kafka side must be Python.** QuixStreams ships no JavaScript client, and the standing
  QuixStreams-first rule forbids hand-rolling a Kafka consumer/producer or a JSON serde in
  Node. So `dashboard-out` consumption and `dashboard-in` production live in Python.
- **The UI must be Next.js 14 / React 18 / shadcn / Tailwind (D4).** That is a Node toolchain.

Three ways to satisfy both. The chosen one is **C**.

| | Shape | Verdict |
|---|---|---|
| A | **Two deployments** — `dashboard-frontend` (Node, `next start`) + `dashboard-backend` (Python), wired by `network.serviceName`. This is exactly what `C:\repos\TestManager\Quix.TestManager\quix.yaml` does. | **Rejected.** Next.js `rewrites` proxy HTTP only — they do not carry a WebSocket upgrade. The browser would need a second public URL straight to the Python backend, which means a second ingress, a CORS policy, and a second thing to get wrong. It also contradicts CLAUDE.md §2 ("a plug-in service with its own image"). |
| B | **One container, two processes** — supervisord running `uvicorn` and `next start`. | **Rejected.** Two language runtimes and a process supervisor in one image, permanently, to buy a build-time convenience. A dead Node process leaves the deployment green. One process per container exists for a reason. |
| C | **One container, one process** — `next build` with `output: 'export'` in a `node:20` build stage; `python:3.13-slim` runtime serving the exported files with `StaticFiles`, plus the API and the WebSocket. | **Chosen.** |

**What C costs, stated plainly.** Static export drops Next.js server-side rendering, server
components, route handlers, middleware and the `next/image` optimizer. That is acceptable
here and nowhere close to abandoning D4:

- D4 names a *stack* — App Router, React 18, TypeScript, shadcn/ui over Radix, Tailwind,
  Playwright. Every one of those survives verbatim; shadcn and Radix are client-side
  component libraries and Tailwind is a build-time CSS pass.
- A realtime grid is a client app end to end. `react-grid-layout`, uPlot and the WebSocket
  are all browser-only; SSR would render an empty skeleton and immediately discard it.
- TestManager needs SSR mainly to proxy `/api/v1/*` to its backend without CORS
  (`frontend/next.config.js:17-30`). When one process serves both, that need disappears.

**Concrete Next.js configuration** (`dashboard/frontend/next.config.js`):

```js
module.exports = {
  output: 'export',          // static HTML/JS into out/, no Node at runtime
  trailingSlash: true,       // every route becomes <route>/index.html — StaticFiles-friendly
  images: { unoptimized: true },
  reactStrictMode: true,
}
```

- **No dynamic route segments.** `generateStaticParams` cannot know layout ids at build time.
  Layout selection is a query parameter: `/?layout=battery-bring-up`. One route, `/`.
- Client-only libraries are imported with `next/dynamic(..., { ssr: false })`, because static
  export still pre-renders HTML at build time.

**Threading** (the `quix-rocksdb-state-api` §6 rule, and the shape `dc-battery-sim/main.py:474-496`
already proves in this repo):

| Thread | Runs |
|---|---|
| main | `consumer_app.run(sdf)` — consumes `dashboard-out`, folds into the window, fans out to the hub |
| worker `http` | `uvicorn.Server.serve()` on `HTTP_PORT` |
| worker `writer` | the single `producer_app.get_producer()` context; drains the write queue, coalesces, produces to `dashboard-in` |
| worker `lexicon` | periodic lexicon re-fetch |
| worker `flush` | the per-connection WebSocket flush timer (asyncio task inside the uvicorn loop, not an OS thread) |

Only the `writer` thread ever calls `produce()`. That makes producer thread-safety a
non-question and gives the write coalescer (§6.8) one obvious home.

A **supervisor** wraps each worker thread, copying the earned guard at
`dc-battery-sim/main.py:437-451`: on any exception, log `CRITICAL` with a traceback, then
`consumer_app.stop(fail=True)` so `__main__` can `raise SystemExit(1)`. That guard is earned
by the same failure class Phase 1 hit — a daemon thread dying while the deployment stays
green. Here it would mean a dashboard serving a frozen page forever.

**File layout.** `quixstreams-idioms` §0 says a QuixStreams deployment is one `main.py`. That
rule governs a *stream topology*, and this service is a web application with a stream leg, so
it is applied where it fits and departed from where it does not — stated here rather than
silently:

```
dashboard/
  main.py                 ~120  env, Applications, topics, the SDF, thread start, app.run()
  backend/lexicon.py      ~150  DCM REST read, boot retry, cache, rev counter, coerce/validate
  backend/layouts.py      ~130  DCM CRUD for type `dashboard-layout`
  backend/window.py        ~90  time-bounded rolling window + columnar projection + last `applied`
  backend/hub.py          ~170  WS registry, bounded per-connection queue, flush, pause/resume, ping
  backend/writer.py        ~90  validate, coalesce, produce to `dashboard-in`
  backend/api.py          ~180  FastAPI routes + StaticFiles + SPA fallback
  requirements.txt  dockerfile  app.yaml  README.md
  frontend/                     the Next.js app (see 6.5–6.7)
```

The **whole stream topology stays in `main.py`** and is four lines, readable at a glance, as
§0 demands. What moved out is not topology.

**Rejected alternative kept on the shelf.** If SSR is ever genuinely needed, the migration is
`output: 'export'` → `output: 'standalone'` plus splitting into shape A. Nothing else changes,
because the frontend already talks to `/api/*` on its own origin.

---

### 6.2 Backend — lexicon from DCM REST (D3)

**Owner:** ArchDev · **Depends on:** 6.1

The lexicon lives in DCM as configuration **type `sil-lexicon`**, **`target_key` = the plant
model name** (`dc-battery-sim`), content = the document validated by
`dev-planning/parameter-contract/spec.md` §6.1.

Client, copied from the working prior art at
`C:\repos\TestManager\Quix.TestManager\backend\api\config_api.py`:

```python
httpx.Client(
    base_url=os.environ["CONFIG_API_URL"],
    headers={"Authorization": f"Bearer {os.environ['Quix__Sdk__Token']}"},
    timeout=10.0,
)
```

**Read path.** The configuration id is deterministic —
`sha1(f"{LEXICON_TYPE}-{LEXICON_TARGET_KEY}".encode()).hexdigest()` (the
`quix-service-create` skill §4c, and `dcm-seed-dbc/main.py:65-71` computes exactly this). So
the backend fetches content in **one** request, with no search round-trip:

```
GET {CONFIG_API_URL}/api/v1/configurations/{lexicon_config_id}/content
```

That endpoint returns the content object directly — *not* wrapped in a `data` envelope, unlike
every other endpoint on this API. Getting that wrong is a silent `KeyError` at boot.

**Boot policy — fail loud, never fall back.** Retry with exponential backoff for
`LEXICON_BOOT_TIMEOUT_S` (default 60). If the lexicon still cannot be read, log `CRITICAL` and
`SystemExit(1)` so the platform restarts the pod. **Do not bundle a fallback copy of
`lexicon.json` in the image.** A stale bundled lexicon that disagrees with the running plant
makes every knob lie — the same argument Phase 1 §6.2 used to put the file in one place.
`/healthz` returns 503 until the lexicon is loaded.

**Validation on load.** Reject and refuse to start if:
- `lexicon_version` major ≠ `1` (Phase 1 §6.1: "the dashboard refuses an unknown major");
- any load-time rule R1–R6 from Phase 1 §6.1 is violated. The dashboard re-runs those checks
  rather than trusting the document, because a malformed descriptor becomes a broken control
  and the DCM does not validate content.

**Cache and refresh.**
- In-memory. The document is ~30 descriptors; there is no reason for RocksDB, and D3 already
  ruled out the `quix-rocksdb-state-api` pattern here.
- State kept: `{document, sha256, rev, fetched_at}`. `rev` is a monotonic integer bumped only
  when the `sha256` changes.
- **M1 refresh: TTL poll** every `LEXICON_REFRESH_S` (default 900 s) on the `lexicon` worker
  thread, plus `POST /api/lexicon/refresh` for a manual kick.
- **M2 refresh: event-driven.** `quixstreams-idioms` §8 is explicit that polling for change is
  the anti-pattern and consuming the change topic is the native answer. The DCM already
  publishes a change event on its config topic (`dashboard-config`, the `dcm_config` variable
  in `.env.example:48`). Add a **second SDF branch on the same `consumer_app`** that consumes
  that topic, filters to our `type`/`target_key`, and triggers a REST re-fetch. Keep the TTL
  poll as a long-period safety net.
  > **This is not `join_lookup` and D3 is untouched.** D3 rules out
  > `QuixConfigurationService`/`join_lookup` as the *read* path, and the read stays REST. The
  > topic is used only as an invalidation signal.
  > **Before writing the filter, ArchDev must read one real message off `dashboard-config` and
  > record its actual key and field names in the README.** Guessing the DCM event schema is
  > how this ships filtering on a field that does not exist and silently never refreshes —
  > which is precisely why it is M2 and not M1.
- On a `rev` bump, push `{"t":"lexicon","rev":N}` to every connected client; clients refetch
  `GET /api/lexicon` and re-resolve every binding (§6.7).

---

### 6.3 Backend — `dashboard-out` consumer and the rolling window (D6)

**Owner:** ArchDev · **Depends on:** 6.2

```python
consumer_app = Application(
    consumer_group=os.getenv("DASHBOARD_CONSUMER_GROUP", "sil-dashboard"),
    auto_offset_reset="latest",
)
telemetry_topic = consumer_app.topic(os.environ["telemetry_in"], value_deserializer="json")
sdf = consumer_app.dataframe(telemetry_topic)
sdf = sdf.filter(is_telemetry).update(ingest, metadata=True)
consumer_app.run(sdf)
```

- **`auto_offset_reset="latest"` is not a preference.** A dashboard that replays history at
  boot would fill its 60 s window with stale samples and, worse, apply an old `applied` block
  as the current control state.
- `is_telemetry` is `isinstance(value, dict)` — a `filter` before the step, never a
  `try`/`except` inside it (`quixstreams-idioms`, "No try/except in the pipeline"). This is the
  same guard Phase 1 put on the sim's input, for the same reason: an exception inside
  `sdf.update` takes the whole application down.
- `metadata=True` gives `ingest(value, key, timestamp, headers)`. **Use the Kafka message
  timestamp as the sample time, not `value["timestamp"]`.** The payload field name is
  plant-specific; the Kafka timestamp is not. Fall back to the payload field only when the
  broker timestamp is absent. This removes a real model-agnosticism leak — see §6.9 L3.

**Fold rule.** For each message:
1. Split off the optional `applied` key (Phase 1 §6.4.1) and store it whole as
   `last_applied = {signals, parameters, received_at}`. It is served in every `snapshot` so a
   reconnecting client gets control state instantly instead of waiting for the 5 s heartbeat.
2. Of the remaining keys, keep only those that the lexicon describes as a signal with
   `direction: "output"`. Unknown keys are counted and logged once per 1000 — not per message,
   which at 10 Hz would make the deployment log useless (the lesson of Phase 1 §6.5.8).
3. Append `(ts_ms, projected_dict)` to the window deque.

**The window is time-bounded, not count-bounded.** On append, evict from the left while
`newest_ts - oldest_ts > HISTORY_SECONDS * 1000`, and hard-cap length at
`HISTORY_MAX_SAMPLES` (default 6000). D6's "60 s ≈ 600 samples" is exactly this for a 10 Hz
plant; expressing it in time rather than samples is what lets a 100 Hz plant work with no code
change and keeps the backend from needing to know a parameter called `SAMPLE_TIME` exists.
Memory at the cap is ~6000 rows × ~14 floats ≈ 8 MB — comfortable inside a 1000 MB limit.

**Snapshot projection.** `window.snapshot(names, seconds)` returns **columnar** data:

```json
{ "ts": [1757937600000, 1757937600100, ...],
  "series": { "temperature_c": [20.0, 20.01, ...], "soc_percent": [50.0, 49.99, ...] },
  "gaps": [] }
```

Columnar because uPlot consumes `[xs, ys1, ys2, ...]` directly — the wire format was chosen to
feed the chart library without a transform. A signal missing from a row (possible only if the
plant's payload is ragged) is emitted as `null`, which uPlot renders as a break.

---

### 6.4 Browser transport — WebSocket

**Owner:** ArchDev · **Depends on:** 6.3

**Choice: one WebSocket per tab.** Why not the alternatives:

- **Polling.** 10 Hz means 10 requests/second/client plus HTTP overhead per sample. Not viable.
- **SSE.** Genuinely close: `EventSource` has reconnect built in and traverses any HTTP proxy.
  Rejected as the primary because (a) it is one-directional, so control writes need a second
  channel and lose ordering against telemetry; (b) it gives the server no client-side signal,
  so the backgrounded-tab and lagging-client cases in this section have no clean answer;
  (c) on HTTP/1.1 it burns one of the browser's six connections per origin.
- **WebSocket.** Bidirectional over one connection, lets the client declare which signals it
  needs and pause when hidden, and — because §6.1 puts the socket in the same process that
  serves the page — never crosses an internal proxy hop.

> **Risk, and the reason the framing below is transport-neutral.** WebSocket upgrade over the
> Quix public ingress is unverified in this workspace; nothing in TestManager or
> comma-car-segments-ingest uses it. §8 R1 makes proving it the **first** task of M1. If it is
> blocked, the fallback is SSE for `GET /api/stream` plus `POST /api/control`, carrying the
> **identical message objects**. That swaps `backend/hub.py` and one client transport module
> and touches nothing else.

**Endpoint:** `GET /ws` (upgrade). Text frames, JSON, one object per frame.

**Client → server**

| `t` | Payload | Meaning |
|---|---|---|
| `hello` | `{lexicon_rev, signals: [{name, direction}], history_s}` | First frame after connect. Declares what this tab needs. Server replies with `snapshot`. |
| `sub` | `{signals: [{name, direction}]}` | Replaces the subscription set (the layout changed). Server replies with `snapshot` for any newly added signal. |
| `write` | `{seq, signals: {...}, parameters: {...}}` | A control write. `seq` is a per-connection monotonic integer used only for client-side correlation. |
| `pause` / `resume` | `{}` | Tab hidden / visible. |
| `pong` | `{}` | Reply to `ping`. |

**Server → client**

| `t` | Payload |
|---|---|
| `snapshot` | `{lexicon_rev, ts: [...], series: {...}, applied: {signals, parameters} \| null, applied_age_ms, history_s}` |
| `frames` | `{ts: [...], series: {...}, dropped: N}` — a batch, never a single sample |
| `applied` | `{signals, parameters, ts}` — pushed the moment an `applied` block arrives |
| `lexicon` | `{rev}` — refetch and re-resolve |
| `status` | `{telemetry_stale: bool, last_sample_age_ms, ws_clients}` — every 5 s |
| `ping` | `{}` |

**Batching.** The hub accumulates samples and flushes every `1000 / WS_FLUSH_HZ` ms
(`WS_FLUSH_HZ` default 10, i.e. pass-through at the plant's rate). Always an array, even for
one sample, so lowering `WS_FLUSH_HZ` under load needs no client change.

**Backpressure.** Per connection, a bounded `asyncio.Queue` of `WS_QUEUE_MAX` frames
(default 20 ≈ 2 s at 10 Hz).

- On overflow, **drop the oldest `frames` message** and increment that connection's `dropped`
  counter, which rides along on the next `frames`. The client shows a "lagging" badge.
- `applied`, `lexicon` and `status` are **never dropped**. Losing an `applied` would leave a
  control permanently wrong; losing a telemetry frame costs 100 ms of chart.
- If the queue stays full for `WS_STALL_TIMEOUT_S` (default 30), close with code **1013**
  (Try Again Later). The client reconnects and gets a clean `snapshot` — cheaper and more
  correct than nursing a stalled socket.

**Backgrounded tab — the specific answer.** The client listens to `visibilitychange`. On
hidden it sends `pause`; the server stops enqueuing `frames` for that connection entirely
(control frames still flow). On visible it sends `resume`, and the server replies with a fresh
`snapshot` rather than a backlog. **Nothing is buffered for a tab that is not looking, and the
gap is filled from the rolling window on return.** This is why D6's window is the right shape:
it makes "just re-snapshot" a complete answer.

**Reconnect.** Client-side exponential backoff 0.5 s → 8 s with full jitter, unbounded
attempts. Every reconnect restarts at `hello` → `snapshot`, so there is exactly one code path
for first-connect and recovery. Server sends `ping` every 15 s; a connection with no `pong`
inside 30 s is closed.

**Fan-out cost.** Each `frames` message is serialised **once** per flush tick for the union of
subscribed signals, then sliced per connection. At 10 Hz with 10 tabs this is ~100 small
`send_text` calls/second — nothing, but it is the number to watch if the grid ever gets busy.

---

### 6.5 Grid and element model (D5)

**Owner:** ArchDev + FrontEndEsthetic · **Depends on:** 6.2

Normative schema: **`dev-planning/dashboard-service/layout-schema.json`**
(`$id` `https://quix.io/schemas/dashboard-layout/1.0.json`).
Worked instance: **`dev-planning/dashboard-service/example-layout.json`**.

Shape:

```
layout_version, layout_id, name, description
model: { name, lexicon_version }
grid:  { cols: 12, row_height, margin: [x,y], compact_type }
elements: [ { id, type, title, binding, position, options } ]
created_at, updated_at, updated_by
```

Decisions inside that schema worth stating out loud:

- **`binding` is the triple `{collection, name, direction}`**, never a bare name. Phase 1 §6.1
  makes `signals` unique on `(name, direction)`; `requested_power_w` and `ambient_temp_c` each
  exist as both an input and an output echo. A bare name is genuinely ambiguous for exactly the
  two signals a user is most likely to bind.
- **Positions are stored once, in the 12-column `lg` space.** `react-grid-layout`'s
  `Responsive` + `WidthProvider` reflows to `md/sm/xs/xxs` at render time. Storing five
  breakpoint variants would triple the schema and immediately drift; the library's own
  breakpoints are configuration, not hand-rolled CSS, so the no-hand-rolled-breakpoints rule
  holds.
- **`title: null` means "render the descriptor's `label`".** An explicit null rather than an
  absent key, for the same absent-vs-null reason Phase 1 §6.1 gives.
- **Element option `min`/`max` may only narrow.** A knob may restrict `requested_power_w` to
  ±50 kW for ergonomics, but never widen past the lexicon's ±250 kW. The backend rejects a
  widening on save with a 422 naming the field — the lexicon is the ceiling, always.
- **A broken binding is preserved, not nulled.** If `name` no longer resolves, the element
  keeps its binding and renders unbound (§6.7). A lexicon regression must not silently destroy
  a layout the user spent an afternoon on.

**Round-trip through DCM** (D5). Layouts are configuration **type `dashboard-layout`**,
**`target_key` = `layout_id`**, **`category` = `model.name`** — so listing the layouts for the
current plant is one call, `GET /api/v1/configurations?type=dashboard-layout&category=dc-battery-sim`,
which returns metadata only (id, type, target_key, category, version, created_at). Content is
fetched per id.

| Operation | DCM call |
|---|---|
| list | `GET /api/v1/configurations?type={LAYOUT_TYPE}&category={model}&limit=200&sort=created_at` |
| load | `GET /api/v1/configurations/{id}/content` |
| save (create **or** new version) | `POST /api/v1/configurations` with `{"metadata":{"type":LAYOUT_TYPE,"target_key":layout_id,"category":model},"content":{...},"replace":true}` |
| delete | `DELETE /api/v1/configurations/{id}` |
| version list | `GET /api/v1/configurations/{id}/versions` |
| restore a version | `GET /api/v1/configurations/{id}/versions/{v}/content` → re-save as a new version |

`id = sha1(f"{LAYOUT_TYPE}-{layout_id}")`, computed locally.

**Save uses `POST … replace: true`, not `PUT /{id}`.** The skill (`quix-service-create` §4c)
documents POST-with-replace as the create-or-version primitive and `PUT` as update-only,
404-ing on an unknown id; `dcm-seed-dbc/main.py:105-118` is the working precedent. POST is also
idempotent in the sense that matters here — re-saving an identical layout is safe. Versioning
comes free, which is where layout undo comes from at no extra cost.

**Validation on save** — server-side, always, even though the client validated first:
1. JSON Schema validation against `layout-schema.json`.
2. `layout_version` major must be `1`.
3. Element `id` uniqueness.
4. Every non-null `binding` must resolve in the current lexicon **and** satisfy the §6.6
   element/datatype/direction rules, including D1.
5. Knob `min`/`max` must be inside the descriptor's range.

Failures return `422` with a per-element list. A layout is never partially saved.

---

### 6.6 Element catalogue

**Owner:** FrontEndEsthetic · **Depends on:** 6.5

CLAUDE.md §4's table is the starting point. Designing it concretely surfaced three places where
it is underspecified or wrong; each is marked **§4 gap** and carries a recommendation. §8 OQ-1
to OQ-4 track them.

| Element | Kind | Offers (picker filter) | Accepts datatype | Renders |
|---|---|---|---|---|
| **Switch** | control | `signals` with `direction: input`; `parameters` with `tunable: true` | `bool`, `enum` | `bool` or 2-member `enum` → Radix `Switch` with member labels; 3–6 members → segmented button group; >6 → `Select` |
| **Knob** | control | same | `uint`, `int`, `float` | Radix `Slider` + optional numeric input; log scale available |
| **Type-in** | control | same | **`uint`, `int`, `float` only** (§4 gap — see below) | `Input` with inline validation, commit on blur/enter |
| **Readout** | visualisation | `signals` with `direction: output` **and all `parameters`, read-only** (§4 gap) | any | formatted value + unit + threshold tinting |
| **Chart** | visualisation | `signals` with `direction: output` only | `uint`, `int`, `float` | uPlot line, 60 s window |

**Per-element behaviour.**

**Switch.** `on_value`/`off_value` default from the descriptor: `bool` → `true`/`false`;
2-member `enum` → member[1]/member[0] by array order. For ≥3 members the segmented group
renders `enum[i].label` and publishes `enum[i].value` verbatim — no coercion, because Phase 1
§6.7 canonicalises enum membership on the sim side and a wire `1.0` for an integer member is
accepted there but a `"1"` is not. Unbound: a disabled switch with a dashed border.

**Knob.** `min`/`max` come from the descriptor unless narrowed in options. `step` defaults to
`1` for `int`/`uint` and `(max − min) / 200` for `float`. **`scale: "log"` exists for a real
case:** `A_THERMAL` spans 2 × 10⁻⁵ … 2 × 10⁻³, and a linear slider over that range has no
usable resolution anywhere. Log requires both endpoints strictly positive; the picker disables
the option otherwise.
Phase 1 load-rule R2 guarantees that every `uint`/`int`/`float` descriptor has numeric `min`
and `max`, so §4's "(needs `min`/`max`)" caveat can never fail for a valid lexicon — worth
knowing, because §4 reads as though a knob might have to cope with a missing range.

**Type-in — §4 gap.** §4 says type-in accepts "any" datatype. Concretely that is wrong for two
of the five. A free-text box over an `enum` can only ever produce values the sim will reject
(Phase 1 §6.7 canonicalises membership, so a typed label is a rejection and a typed number is
just a worse Switch); over a `bool` it is a checkbox with extra failure modes.
**Recommendation: type-in offers `uint`, `int`, `float` only**; `bool` and `enum` are served by
Switch. Tracked as OQ-2.

**Readout — §4 gap, and the sharper one.** §4 filters readouts on `direction: output`.
Parameters have `direction: null` by construction (Phase 1 §6.1), so under §4 as written **no
element can ever display a parameter** — yet §4 itself says fixed parameters "may be shown
read-only", and Phase 1 §6.4.1 puts `Q_MAX_AH` and `SAMPLE_TIME` into the `applied` block
explicitly "so the dashboard can display fixed parameters". §4's picker rule and the Phase 1
contract disagree.
**Recommendation: Readout offers output signals *and* all parameters, tunable or not, rendered
read-only.** This keeps D1 intact — D1 forbids binding a *control* to a fixed parameter, not
displaying one — and makes the `applied` block's stated purpose reachable. Tracked as OQ-3.
Parameter readouts update from `applied` (~2 messages/minute plus every accepted write), so
they carry an "as of Ns ago" hint rather than pretending to be live.

**Chart.** Output signals only. A parameter is deliberately not chartable: `applied` is emitted
on change plus a 5 s heartbeat, so a parameter "series" would be a 12-samples-per-minute
staircase pretending to be a signal. Axis defaults to the descriptor's `min`/`max`, which
Phase 1 §6.1 defines as a display hint for output signals — the plant may legitimately exceed
it during a transient and that must not read as an error, so the axis expands rather than the
value clipping.

**Four rendering states, every element, no exceptions.**

| State | When | Render |
|---|---|---|
| **unbound** | `binding` is `null` | Dashed outline, element-type icon, "Bind…" button |
| **broken** | `binding` set but does not resolve in the current lexicon | Solid outline, amber badge, struck-through intended name, tooltip "`<name>` is not in lexicon rev N", "Re-bind" button. Never publishes, never subscribes, never removed from the layout |
| **incompatible** | resolves, but the descriptor's `datatype` is no longer valid for this element type | Same treatment, different message: "`<name>` is now `enum`; a Chart cannot show it". §4 does not cover this case at all |
| **stale** | bound and resolving, but no sample for `stale_after_ms` | Last value dimmed with a clock badge. A stopped plant is a legitimate state, not an error |

**Client-side validation before publish** — the §4 rule, made precise. Every control runs this
before it is allowed to emit, and the identical function runs again server-side in §6.8:

1. Entry resolves in the current lexicon.
2. `collection`/`direction`/`tunable` permit a write (input signal, or `tunable: true`
   parameter). **D1 is enforced here and in the picker.**
3. Datatype: `bool` must be a JSON boolean; `int`/`uint` must be integral (no `0.5`);
   `float` accepts an integer and sends `float(v)`; `enum` must be a member `value`.
   **Never send a string for a numeric field** — Phase 1 §6.7 removed string coercion from the
   sim deliberately, so `"‑8000"` from a type-in is now a silent drop.
4. `min ≤ v ≤ max`. Out of range is **blocked, not clamped** — a clamped write looks accepted
   and leaves the control and the plant permanently disagreeing (D1).

A blocked value shows inline under the control and does not publish. The user is never left
guessing which of several fields the plant refused, because the plant never sees it.

**Edit mode — §4 gap.** §4 never mentions modes, but without one, dragging a knob would move
the element instead of setting the value. A global **Edit** toggle gates it:
`isDraggable`/`isResizable` on `ResponsiveGridLayout` are `false` in view mode and `true` in
edit mode; in edit mode controls are inert and each element shows a drag handle plus a config
button. Tracked as OQ-4.

---

### 6.7 The binding picker

**Owner:** FrontEndEsthetic · **Depends on:** 6.6

A `Dialog` + `Command` (both already in TestManager's `components/ui`) opened from an element's
config button. It lists candidates from the current lexicon, filtered by the element type's row
in §6.6, and never from any other source.

**Candidate list construction:**

```
candidates(elementType) =
    signals.filter(s => s.direction ∈ allowedDirections(elementType)
                     && s.datatype  ∈ allowedDatatypes(elementType))
  ∪ parameters.filter(p => (isControl(elementType) ? p.tunable === true : true)
                        && p.datatype ∈ allowedDatatypes(elementType))
```

- `isControl(type)` is `switch | knob | typein`. For those, `p.tunable === true` is the D1
  gate. **`tunable: false` entries are removed from the candidate list, not disabled** — a
  greyed row invites a support question; an absent row does not. They remain reachable through
  a Readout, which is the point of OQ-3.
- Rows show `label`, `name` in monospace, `unit`, `datatype`, and the range or enum members.
  `description` is the row's tooltip. All from the descriptor; none from code.
- **Grouping:** *Input signals* / *Output signals* / *Tunable parameters* / *Fixed parameters
  (read-only)*. `Command`'s fuzzy filter searches `label`, `name` and `description`.
- Where a name exists in both directions (`requested_power_w`), both rows appear, in their
  respective groups, disambiguated by the group heading. The stored binding records
  `direction`, so the choice is not lost.
- **Empty state is a real state.** A Chart on a plant with no numeric output signals shows
  "No output signal in this lexicon can be charted" — not an empty box.

**When a bound entry disappears.** On `lexicon` rev change and on every layout load, each
element re-resolves its binding:

- resolves and is compatible → nothing happens; the element keeps its live value.
- does not resolve → **broken** state (§6.6). Reason shown, "Re-bind" offered.
- resolves but the datatype is no longer valid for the element type → **incompatible** state.
- **In neither case is the stored layout modified.** The layout in DCM changes only on an
  explicit user save. A lexicon rollback therefore heals every broken element automatically.
- A banner summarises: "3 elements lost their binding in lexicon rev 7" with a jump-to action,
  so the user is not hunting a badge on a 4K grid.

---

### 6.8 Control write path

**Owner:** ArchDev + FrontEndEsthetic · **Depends on:** 6.6

```
interaction → optimistic local state → client validate (§6.6) → WS {"t":"write", seq}
   → backend re-validate → write queue → coalescer (WRITE_COALESCE_MS)
   → one nested envelope → producer.produce(key=PLANT_KEY) → dashboard-in
```

**Throttle and debounce, by control type:**

| Control | Rule |
|---|---|
| Knob (dragging) | **Trailing-edge** throttle at `CONTROL_THROTTLE_MS` (default 100 ms, the plant's sample period), plus a **guaranteed final emit on pointer-up**. Leading-edge-only throttle drops the value the user actually released on — the single most likely way this ships feeling broken. |
| Knob (keyboard/arrow) | 200 ms trailing debounce, so a held arrow key sends one write per 200 ms. |
| Switch | Immediate, no throttle. Discrete and rare. |
| Type-in | On `commit_on` (blur / enter / both). **Never on keystroke** — a partially typed `-4` is a valid, wrong, publishable number. |

**Server-side coalescing.** The writer thread drains the queue every `WRITE_COALESCE_MS`
(default 50) and merges everything pending into **one** envelope, last-write-wins per field:

```json
{ "signals": { "requested_power_w": -40000.0 },
  "parameters": { "KE": 6.0 } }
```

This bounds `dashboard-in` traffic regardless of how many tabs are dragging, and it preserves
Phase 1 §6.3's partial-update semantics exactly: absent key means untouched, at both levels.

**Keying is mandatory.** Every message is produced with `key=PLANT_KEY`. Phase 1 §6.3.2 is
explicit: partial update is last-write-wins, last-write-wins needs a total order, and unkeyed
messages round-robin across partitions so a `parameters` write can overtake a `signals` write.
`PLANT_KEY` is a **required** env var with no default — see §6.9 L1, this is the one piece of
the plant contract the lexicon does not carry.

**Server-side re-validation.** The backend runs the same checks as §6.6 against its own lexicon
cache and **rejects with `422` rather than forwarding**, because `POST /api/control` is a public
route and the client is not the only possible caller. A rejected field is never produced.

**Feedback, with no ack channel (Phase 1 §6.7).** The `applied` block is the only signal, so
the UI is explicit about the three outcomes:

| Client state | Trigger | Render |
|---|---|---|
| `pending` | write published | Control shows the optimistic value with a subtle pulse |
| `confirmed` | an `applied` arrives whose value for this field matches what was sent | Pulse clears |
| `rejected` | an `applied` arrives whose value **differs** from what was sent | Control **snaps to the echoed value** and a toast names the field: "The plant did not accept *Max Battery Temperature* = 150. Reason is in the deployment log." |
| `unconfirmed` | no `applied` within `APPLIED_TIMEOUT_MS` (default 7000 = the 5 s heartbeat + 2 s slack) | Amber outline, optimistic value **kept**, tooltip "not confirmed by the plant" |

Two details that decide whether this feels right or maddening:

- **Float comparison:** `abs(sent − echoed) <= 1e-9 + 1e-6 * abs(echoed)`. An exact `==` on a
  round-tripped float would report a rejection on a successful write roughly whenever the step
  size is not a binary fraction.
- **Never reconcile a control the user is holding.** While a control has pointer capture or
  keyboard focus, incoming `applied` values are recorded but not applied to it; reconciliation
  runs on release. Without this, the 5 s heartbeat yanks the slider out from under a dragging
  finger.

Because a rejected write changes nothing on the plant, the echo carries the old value and the
snap-back is automatic. That is the implicit NACK Phase 1 §6.4.1 designed for, now actually
consumed.

`POST /api/control` accepts the identical body for scripting and Playwright, and funnels into
the same validate-and-enqueue function. One code path, two entry points.

---

### 6.9 Model-agnosticism walkthrough

**Owner:** Buddy (analysis) · **Verified by:** Tester

Swapping `dc-battery-sim` for a different plant — say a hydraulic actuator rig. Every place a
change is needed:

| # | Change | Kind |
|---|---|---|
| 1 | Seed the new plant's `lexicon.json` into DCM as `type=sil-lexicon`, `target_key=<model name>` | data |
| 2 | `LEXICON_TARGET_KEY` on the dashboard deployment | config |
| 3 | `PLANT_KEY` on the dashboard deployment | config |
| 4 | `telemetry_in` / `control_out` if the new plant uses different topic names | config |
| 5 | Build a layout in the UI (or seed one with `category=<new model name>`) | data |
| 6 | **Dashboard source code** | **none** |

**Verification the Tester runs, not a claim:**

```
rg -n -w 'soc_percent|q_act_as|ocv_v|dc_voltage_v|dc_current_a|rc1_voltage_v|rc2_voltage_v|temperature_c|heat_j|derating_factor|requested_power_w|ambient_temp_c|chiller_setting|heater_setting|A_THERMAL|Q_MAX_AH|SAMPLE_TIME|KT0|KT1|KT2|KE|TAU1|TAU2|R0|R1|R2|COOLANT_TEMP|MAX_BATTERY_TEMP' \
   dashboard/ --glob '!**/*.test.*' --glob '!**/e2e/**' --glob '!dashboard/README.md'
```

**Must return zero matches.** Fixtures, e2e tests and the README may name battery signals;
nothing else may.

**Leaks found while walking it, and what was done about each:**

- **L1 — the `dashboard-in` message key is not in the lexicon.** Phase 1 §6.3.2 requires the
  literal `"battery-sim"`, while the lexicon's `model.name` is `"dc-battery-sim"`. They differ,
  so the key **cannot** be derived from the lexicon. Handled with a required `PLANT_KEY` env
  var — config, not code, so not a design bug. But it is a second thing to remember at swap
  time and it should not be. **Recommend lexicon v1.1 adds `model.instance_key`.** → OQ-6.
- **L2 — the sample timestamp.** Taking the sample time from `value["timestamp"]` hardcodes
  this plant's payload field name. **Fixed in §6.3 by using the Kafka message timestamp via
  `metadata=True`**, with the payload field as a fallback only. Leak closed, and the fix is the
  native primitive rather than a workaround.
- **L3 — the window length.** Deriving `WINDOW_SAMPLES` from a parameter called `SAMPLE_TIME`
  would have been the obvious implementation and would have hardcoded a battery parameter name
  into the backend. **Fixed in §6.3 by making the window time-bounded.** Leak closed.
- **L4 — unknown payload keys.** The consumer folds only keys the lexicon declares as output
  signals, so a plant emitting extra diagnostics does not corrupt the window. Already correct.
- **L5 — `applied` is a reserved key.** The consumer must strip it before folding, or a plant
  without it and a plant with it would behave differently. Handled in §6.3.

**Residual, and it is honest:** the dashboard assumes the Phase 1 *envelope* (`{signals,
parameters}` in, flat + optional `applied` out) and the Phase 1 *lexicon schema*. Those are the
contract every plant must meet to be drivable at all, not model-specific knowledge. A plant that
meets the contract needs no dashboard change.

---

### 6.10 Frontend composition

**Owner:** FrontEndEsthetic · **Depends on:** 6.5–6.8

Base: a copy of TestManager's frontend scaffolding — `tailwind.config.ts`, `postcss.config.js`,
`components/ui/*`, `lib/utils/cn.ts`, `lib/contexts/theme-context.tsx`,
`components/layout/main-layout.tsx`, `playwright.config.ts`. **Do not re-derive the design
system.** Already present there and reused as-is: `dialog`, `command`, `select`, `input`,
`label`, `switch` (Radix), `slider` (add), `card`, `badge`, `tooltip`, `toast`/`toaster`,
`skeleton`, `separator`, `dropdown-menu`, `alert-dialog`.

Additions D4 names, pinned:

| Package | Version | Why |
|---|---|---|
| `react-grid-layout` | `^1.4.4` | The grid. `Responsive` + `WidthProvider`; import its CSS and `react-resizable`'s. Client-only → `dynamic(..., { ssr: false })`. |
| `uplot` | `^1.6.30` | 10 Hz charts. Canvas, no virtual DOM per sample. Recharts re-renders an SVG tree per update and will not hold 10 Hz across a dozen charts. |
| `@radix-ui/react-slider` | `^1.2.0` | The Knob. Not in TestManager's set. |
| `ajv` + `ajv-formats` | `^8` | Client-side layout validation against `layout-schema.json`, shipped as a build asset so client and server validate against one file. |

Structure:

```
frontend/
  app/page.tsx                      the single route (layout via ?layout=<id>)
  app/layout.tsx                    theme + toaster + providers
  components/grid/grid-canvas.tsx           ResponsiveGridLayout, edit-mode toggle
  components/grid/element-frame.tsx         title, unbound/broken/incompatible/stale chrome
  components/elements/switch-element.tsx
  components/elements/knob-element.tsx
  components/elements/typein-element.tsx
  components/elements/readout-element.tsx
  components/elements/chart-element.tsx     uPlot + a Float64Array ring buffer
  components/binding/binding-picker.tsx
  components/layouts/layout-menu.tsx        list / new / save / delete / version restore
  lib/transport/socket.ts           WS client: hello/sub/write, reconnect, pause/resume
  lib/lexicon/resolve.ts            binding → descriptor; candidate filters; D1 gate
  lib/lexicon/validate.ts           the §6.6 validation, shared by every control
  lib/store/telemetry.ts            latest-value map + per-signal ring buffers
  lib/store/layout.ts               layout state, dirty tracking, storage adapter
  lib/api/client.ts                 typed fetch for /api/*
```

**Chart implementation note.** Each chart owns a `Float64Array` ring buffer sized from the
observed inter-sample interval (measured from the `snapshot`'s `ts` array — never from a
lexicon parameter, see L3) times `window_s`, with 25 % headroom. Frames append; a
`requestAnimationFrame` tick calls `u.setData(data, false)` at most once per frame. Charts
share one rAF scheduler so twelve charts cost one frame's work, not twelve.

**Responsive.** `ResponsiveGridLayout` handles the grid at `lg/md/sm/xs/xxs`; Tailwind handles
everything around it. Below `sm` the element chrome collapses (title hidden, unit inlined) via
Tailwind classes. No custom media query is written anywhere.

**Storage adapter.** `lib/store/layout.ts` talks to an interface with
`list/load/save/remove`. M1 binds it to `localStorage`; M2 binds it to `/api/layouts`. The
document written is **already the D5 schema** in both cases, so M2 is a one-file swap.

---

### 6.11 Deployment

**Owner:** ArchDev · **Depends on:** all · Follow the `quix-service-create` checklist.

**`dashboard/dockerfile`**

```dockerfile
FROM node:20-bookworm-slim AS webbuild
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build            # next build with output:'export' -> /web/out

FROM python:3.13-slim-bookworm
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONIOENCODING=UTF-8
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py ./
COPY backend/ ./backend/
COPY --from=webbuild /web/out ./static
ENTRYPOINT ["python3", "main.py"]
```

Node appears only in the build stage. The shipped image is a Python image.

**`dashboard/requirements.txt`** — `quixstreams==3.23.1` (matching `dc-battery-sim`),
`fastapi`, `uvicorn[standard]` (its `websockets` extra is what serves `/ws`), `httpx`,
`jsonschema`. Pin all five.

**`dashboard/app.yaml`** — `name: dashboard`, `language: python`, `dockerfile: dockerfile`,
`runEntryPoint: main.py`, `defaultFile: main.py`, and:

| Variable | inputType | defaultValue | required | Note |
|---|---|---|---|---|
| `telemetry_in` | InputTopic | `dashboard-out` | true | **The naming trap, inverted.** The dashboard's *input* is the topic called `dashboard-out`. Named `telemetry_in`/`control_out` rather than `input`/`output` precisely so nobody has to re-derive that at 2 a.m. |
| `control_out` | OutputTopic | `dashboard-in` | true | |
| `PLANT_KEY` | FreeText | *(none)* | true | Kafka key on every `dashboard-in` message. Phase 1 §6.3.2. `battery-sim` for this plant. |
| `CONFIG_API_URL` | FreeText | `http://config-api-svc` | true | Cluster-internal DCM service name. |
| `LEXICON_TYPE` | FreeText | `sil-lexicon` | true | |
| `LEXICON_TARGET_KEY` | FreeText | *(none)* | true | `dc-battery-sim`. The one variable that names the plant. |
| `LAYOUT_TYPE` | FreeText | `dashboard-layout` | false | |
| `LEXICON_REFRESH_S` | FreeText | `900` | false | |
| `LEXICON_BOOT_TIMEOUT_S` | FreeText | `60` | false | |
| `HISTORY_SECONDS` | FreeText | `60` | false | D6. |
| `HISTORY_MAX_SAMPLES` | FreeText | `6000` | false | Memory cap for a fast plant. |
| `WS_FLUSH_HZ` | FreeText | `10` | false | |
| `WS_QUEUE_MAX` | FreeText | `20` | false | |
| `WS_STALL_TIMEOUT_S` | FreeText | `30` | false | |
| `WRITE_COALESCE_MS` | FreeText | `50` | false | |
| `APPLIED_TIMEOUT_MS` | FreeText | `7000` | false | 5 s heartbeat + slack. |
| `HTTP_PORT` | FreeText | `8080` | false | |
| `STATIC_DIR` | FreeText | `static` | false | |
| `DASHBOARD_CONSUMER_GROUP` | FreeText | `sil-dashboard` | false | |
| `LOG_LEVEL` | FreeText | `INFO` | false | |

All names are alphanumeric + `_`; no hyphens (deployment creation rejects those). Topics are
read with `os.environ[...]`, never `.get(..., "fallback")` — a hardcoded fallback boots fine
and draws **no edge** in the pipeline graph.

**Root `quix.yaml` does not exist yet** (CLAUDE.md §5, parameter-contract `architecture.md` §5).
This spec creates it. The dashboard block:

```yaml
  - name: SIL Dashboard
    application: dashboard
    version: latest
    deploymentType: Service
    resources:
      cpu: 500
      memory: 1000
      replicas: 1
    publicAccess:
      enabled: true
      urlPrefix: sil-dashboard
    network:
      serviceName: sil-dashboard
      ports:
        - port: 80
          targetPort: 8080
    plugin:
      embeddedView: { enabled: true, hideHeader: true, default: true }
      sidebarItem:  { show: true, label: SIL Dashboard, icon: dashboard, order: 0 }
    variables:
      # every app.yaml variable repeated here with `value:` — an app.yaml-only
      # variable never reaches the running deployment
```

- **`replicas: 1` is a correctness requirement, not a sizing choice.** All `dashboard-out`
  traffic carries one key, so it lands on one partition. Two replicas in one consumer group
  split partitions, and the replica without that partition would serve blank charts to
  whichever browsers the ingress happened to route there. If the dashboard ever must scale,
  each replica needs its **own** consumer group, not more replicas in one.
- **No `state:` block.** D6 is in-memory and the SDF uses no stateful op, so there is no
  RocksDB volume and no changelog topic.
- `resources` are sized for uvicorn + the QS consumer + an 8 MB window + the WS fan-out; the
  template default (200 m / 500 MB) is too small for a Python process doing JSON at 10 Hz plus
  serving static assets.

**Also in `quix.yaml`:** deployment blocks for `dc-battery-sim` (with `value:` entries for
`telemetry`/`input`/`output` and the three variables Phase 1 added), MongoDB, and the DCM.
The DCM block, copied from the working one at
`C:\repos\TestManager\TestManagerEnv\comma-car-segments-ingest\quix.yaml:209-231`:

```yaml
  - name: Dynamic Configuration Manager
    application: DynamicConfiguration
    version: latest
    deploymentType: Managed
    resources: { cpu: 200, memory: 500, replicas: 1 }
    publicAccess: { enabled: true, urlPrefix: config-api-svc }
    network: { serviceName: config-api-svc }
    configuration:
      topic: dashboard-config
      mongoHost: mongodb
      mongoPort: 27017
      mongoUser: dashboard
      mongoPasswordSecret: MONGO_PASSWORD
      mongoDatabase: quix
      mongoCollection: configuration-api
```

Topics section: `dashboard-in`, `dashboard-out`, `dashboard-config`. **Partition count is
decided now, before there is data: 1 each.** Everything is single-keyed and single-consumer;
a higher count buys nothing and would break the ordering Phase 1 §6.3.2 depends on.

**Deployment order — DCM first, and the order is load-bearing:**

1. **MongoDB** — the DCM's content store. Nothing else works without it.
2. **Dynamic Configuration Manager** (Managed). Confirm `GET {CONFIG_API_URL}/api/v1/metadata`
   answers before going further.
3. **Seed the lexicon.** M1: one documented `curl` in `dashboard/README.md`. M2: a
   `dcm-seed-lexicon/` Job modelled on
   `C:\repos\TestManager\TestManagerEnv\comma-car-segments-ingest\dcm-seed-dbc\main.py` —
   `POST /api/v1/configurations` with `replace: true`, idempotent, committed so recovery after a
   content-store wipe is reproducible (the skill's standing instruction).
   > Each Quix app is its own Docker build context, so the seeder cannot read
   > `dc-battery-sim/lexicon.json` and must carry a copy. **Tester gates that copy against the
   > original with a byte-comparison in the smoke checks** — two lexicons that drift is exactly
   > the failure Phase 1 §6.2 refused to allow.
4. **`dc-battery-sim`** — already built; only needs its `quix.yaml` block.
5. **`dashboard`** — last. It fails loudly at boot if 2 or 3 did not happen, which is the
   intended behaviour.

Ship sequence per the skill: commit + push to `devDB` → `POST /workspaces/{ws}/pull` → sync.
An app-only commit is rejected, so the `quix.yaml` change must be in the same commit.

**API surface served by the one process:**

| Route | Purpose |
|---|---|
| `GET /healthz` | `{status, lexicon_rev, lexicon_loaded, ws_clients, window_rows, last_sample_age_ms, telemetry_stale, dropped_frames}`. **503 only when the lexicon is not loaded.** A stale `dashboard-out` is reported as a flag, not a failure — a stopped plant is legitimate and must not crash-loop the dashboard. |
| `GET /api/lexicon` | `{rev, sha256, fetched_at, document}` |
| `POST /api/lexicon/refresh` | Force a re-fetch; returns the new `rev` |
| `GET /api/layouts` | List for the current model |
| `GET /api/layouts/{layout_id}` | The document |
| `PUT /api/layouts/{layout_id}` | Validate (§6.5) then DCM `POST … replace: true` |
| `DELETE /api/layouts/{layout_id}` | |
| `GET /api/layouts/{layout_id}/versions` | |
| `GET /api/layouts/{layout_id}/versions/{v}` | For restore |
| `POST /api/control` | Same body as the WS `write` frame — scripting and e2e |
| `GET /api/snapshot?signals=a,b&seconds=60` | The WS `snapshot` payload over HTTP — debugging and e2e |
| `WS /ws` | §6.4 |
| `GET /*` | `StaticFiles` from `STATIC_DIR`, with an SPA fallback to `index.html`. **The catch-all is mounted last and must not shadow `/api` or `/ws`.** |

---

### 6.12 Phasing

**M1 — walking skeleton.** One deployable thing that closes the whole loop.

0. **Prove WebSocket upgrade over the Quix public ingress** with a throwaway `/ws/echo` on a
   deployed build. **Before anything else.** If it fails, take the §8 R1 SSE fallback now, not
   after the hub is written.
1. `dashboard/` app skeleton, dockerfile, `app.yaml`, root `quix.yaml`, deployed with a public
   URL and a green `/healthz`.
2. Lexicon fetch from DCM REST + boot retry + `GET /api/lexicon`. Lexicon seeded by curl.
3. `dashboard-out` consumer + time-bounded window + `last_applied` cache.
4. WS hub: `hello` → `snapshot` → `frames`, with the bounded queue and pause/resume.
5. Next.js static export served by FastAPI; one route; theme + `components/ui` copied from
   TestManager.
6. Grid with **three** element types — **readout, chart, knob** — plus the binding picker with
   the §6.6 filters and the D1 gate.
7. Write path: client validation, trailing throttle, WS `write`, server re-validation,
   coalescer, keyed produce, and `pending`/`confirmed`/`rejected` reconciliation.
8. Layout in `localStorage`, **already in the D5 schema**, through the storage adapter.

M1 explicitly excludes: DCM layout CRUD, switch and type-in, versions/restore, the
config-topic invalidation branch, the seeder Job, the `incompatible` state, Playwright.

**M2 — full feature.**

9. DCM layout CRUD + version list + restore; the storage adapter repointed to `/api/layouts`.
10. Switch and Type-in elements; segmented/select switch styles; knob log scale.
11. All four element states including `broken` and `incompatible`, plus the summary banner.
12. Lexicon invalidation via the `dashboard-config` topic branch — **after** confirming the
    event schema from a live message.
13. `dcm-seed-lexicon` Job + the Tester drift gate.
14. Responsive pass phone → 4K; Playwright e2e over `/api/control` + `/api/snapshot`.
15. `dashboard/README.md` and CLAUDE.md updates (DocuGuy — including the D5/D6 gap in §8).

---

## 7. Data & interface contracts — summary

| Contract | Defined in | Artifact |
|---|---|---|
| Layout document | §6.5 | `dev-planning/dashboard-service/layout-schema.json`, `$id` `dashboard-layout/1.0` |
| Worked layout | §6.5 | `dev-planning/dashboard-service/example-layout.json` |
| Lexicon document | Phase 1 §6.1 | consumed unchanged; major version `1` enforced |
| `dashboard-in` envelope | Phase 1 §6.3 | produced by §6.8, keyed `PLANT_KEY` |
| `dashboard-out` payload | Phase 1 §6.4 | consumed by §6.3, `applied` split off and cached |
| DCM lexicon read | §6.2 | `GET /api/v1/configurations/{sha1(type-target)}/content` |
| DCM layout CRUD | §6.5 | type `dashboard-layout`, `target_key`=id, `category`=model |
| WebSocket framing | §6.4 | 5 client verbs, 6 server verbs, transport-neutral |
| HTTP API | §6.11 | 12 routes + static |
| Env vars | §6.11 | 20 variables, all in `app.yaml` **and** the `quix.yaml` block |

## 8. Risks, constraints, and open questions

### Risks

- **R1 — WebSocket upgrade over the Quix public ingress is unproven.** No deployment in
  TestManager or comma-car-segments-ingest uses one. *Mitigation:* M1 task 0 proves it on a
  deployed build before the hub is written; §6.4's framing is transport-neutral so the SSE +
  POST fallback swaps two modules. *If ArchDev skips task 0, this becomes a late rewrite.*
- **R2 — 10 Hz × N charts in a browser.** Twelve uPlot charts at 10 Hz is ~120 canvas redraws
  per second if done naively. *Mitigation:* one shared `requestAnimationFrame` scheduler
  (§6.10) caps it at the display refresh rate regardless of chart count, and `WS_FLUSH_HZ`
  gives a server-side lever.
- **R3 — DCM content-store durability.** `contentStore` defaults to `mongo`, which stores
  content inside the Mongo document. A wiped store loses **every layout and the lexicon**. The
  config topic outlives the store and the SDK rebuilds versions from topic events with no
  liveness check. *Mitigation:* the committed seeder (§6.11 step 3) makes the lexicon
  reproducible. **Layouts are not covered** — they are user data with no source of truth
  outside DCM. Consider `contentStore: file` (blob-backed) for this workspace. → OQ-7.
- **R4 — a layout that outlives its lexicon.** A layout saved against lexicon 1.0 loaded
  against a 2.x lexicon. *Mitigation:* `model.lexicon_version` is stored in the layout and a
  major mismatch shows a banner; individual bindings degrade to `broken`/`incompatible` rather
  than the grid failing. The layout is never auto-rewritten.
- **R5 — first 5 seconds after a backend restart.** With no writes happening and no cached
  `applied`, controls render lexicon defaults until the heartbeat arrives. *Mitigation:* the
  `last_applied` cache makes this affect only the first client after a restart, and controls
  render in a `syncing` state rather than pretending the default is live. OQ-5 would remove it
  entirely.
- **R6 — `/api/control` is unauthenticated inside the workspace.** Anything that can reach the
  service can drive the plant. *Mitigation:* the deployment sits behind the portal's access
  control and the plant is a simulator. Worth a line in the README, not a feature.

### Open questions

- **OQ-1 — should a chart carry more than one series?** CLAUDE.md §4 says "each element is
  bound to exactly one lexicon entry", so v1.0 honours that. But overlaying `ocv_v` and
  `dc_voltage_v`, or `rc1_voltage_v` and `rc2_voltage_v`, is the first thing anyone will want
  on this plant, and a one-series-per-chart grid makes that impossible. The forward-compatible
  seam is `binding` → `bindings: []` with `binding` retained as the singular alias.
  **Recommendation: allow multi-series charts in layout v1.1 and amend §4.** Not taken
  unilaterally, because §4 is a stated contract.
- **OQ-2 — narrow Type-in to numeric datatypes?** §4 says "any". A free-text box over an
  `enum` produces only rejections and over a `bool` is a worse checkbox. **Recommendation:
  yes, `uint`/`int`/`float` only; amend §4.**
- **OQ-3 — may a Readout display a parameter?** §4's `direction: output` filter makes it
  impossible, while §4's own prose and Phase 1 §6.4.1 both assume it is possible. This is a
  genuine contradiction between §4 and the Phase 1 contract. **Recommendation: yes,
  read-only, all parameters; amend §4's picker row for Readout.**
- **OQ-4 — confirm the Edit/View mode toggle.** §4 does not mention modes, but without one a
  drag is ambiguous. **Recommendation: a global Edit toggle as specified in §6.6.** Low risk,
  flagged only because it adds UI §4 does not describe.
- **OQ-5 — re-raise Phase 1 OQ-4: a separate `dashboard-state` topic for `applied`.** Now that
  the dashboard exists, a compacted `dashboard-state` topic read from `earliest` would give the
  backend the plant's full control state at boot instead of waiting up to 5 s, closing R5
  completely. Costs one topic and one subscription, and would need a `dc-battery-sim` change —
  out of scope here by §3. **Recommendation: keep the in-band `applied` key. The `last_applied`
  cache already reduces this to a once-per-restart annoyance.** Revisit if reload latency is
  actually felt.
- **OQ-6 — lexicon v1.1 should carry `model.instance_key`.** §6.9 L1: the `dashboard-in` Kafka
  key is part of the plant contract but has no home in the lexicon, so it lives in a required
  env var that must be remembered on every plant swap. Would also cover §8 R4 in
  parameter-contract (a `constraints` section) in the same minor bump.
- **OQ-7 — DCM `contentStore`: `mongo` or `file`?** R3. Layouts are user data with no other
  source of truth. `file` puts content in blob storage and survives a pod replacement. Needs
  blob storage configured on the workspace. **Recommendation: `file`.**
- **OQ-8 — CLAUDE.md is behind the decisions this spec is built on.** §7 stops at **D4**;
  **D5 (layouts in DCM) and D6 (in-memory rolling window) are not recorded there at all**, and
  §8 still lists both as open questions ("1. Dashboard layout persistence", "3. Chart history
  depth" — note the numbering skips 2, while `parameter-contract/spec.md` §3 refers to
  "§8 Q2" for layout persistence, so the two documents already disagree about the numbers).
  This spec treats D5 and D6 as binding per the brief. **DocuGuy must append D5 and D6 to
  CLAUDE.md §7 and delete the resolved entries from §8** — until then, the project charter and
  this spec contradict each other, and the charter is the file the next agent reads first.

## 9. Alternatives considered

- **Two deployments (Node frontend + Python backend), the TestManager shape.** Rejected in
  §6.1: Next.js `rewrites` do not carry a WebSocket upgrade, so it forces a second public URL
  and a CORS policy, and it contradicts CLAUDE.md §2. Kept documented as the migration target
  if SSR is ever needed.
- **One container running two processes under supervisord.** Rejected in §6.1: a permanent
  operational cost — two runtimes, a supervisor, ambiguous liveness — for a build-time
  convenience.
- **Server-Sent Events as the primary transport.** Rejected in §6.4: one-directional, no
  client signal for pause/backpressure, and an HTTP/1.1 connection-limit tax. Retained as the
  specified fallback, which is why the framing is transport-neutral.
- **HTTP polling at 10 Hz.** Rejected: 10 requests/second/client.
- **Recharts for the charts.** Rejected per D4 and on mechanism: it re-renders an SVG tree per
  update, which does not hold at 10 Hz across a dozen charts. uPlot draws to canvas.
- **Layouts in `localStorage` permanently.** Rejected by D5, and rightly: a layout that does
  not follow the user to another browser is not a shared dashboard. Retained as M1's temporary
  storage adapter only, writing the same D5 document.
- **Layouts on a dedicated Kafka topic.** Would need a compacted topic and a consumer just to
  read the user's own config back — a streaming answer to a request/response question. DCM
  already versions JSON documents, which is what D5 uses.
- **`join_lookup` / `QuixConfigurationService` for the lexicon.** Forbidden by D3 and
  wrong-shaped: it resolves configuration *per record by message key*, which is an enrichment
  idiom. A picker needs one plain request/response read.
- **RocksDB State for the rolling window.** Rejected by D6, and independently by the
  `quix-rocksdb-state-api` skill's own first constraint: State is in-context only and the HTTP
  thread cannot read it, so serving a 60 s window from it would need a per-request round-trip
  through an events topic — enormous machinery for data that is intrinsically ephemeral.
- **A bundled fallback `lexicon.json` in the dashboard image.** Rejected in §6.2: a lexicon
  that disagrees with the running plant makes every control lie, and silently.
- **Clamping out-of-range control values instead of blocking them.** Forbidden by D1 and by
  Phase 1 §6.7. A clamped write looks accepted.

## 10. References

- `C:\repos\dashboard-tests\CLAUDE.md` — §2 pipeline + naming trap, §3 lexicon, §4 UI contract,
  §7 D1–D4, §8 open questions (see OQ-8), §9 working agreements
- `C:\repos\dashboard-tests\dev-planning\parameter-contract\spec.md` — §6.1 lexicon schema,
  §6.3 `dashboard-in` envelope, §6.4 `dashboard-out` + `applied`, §6.7 rejection semantics
- `C:\repos\dashboard-tests\dev-planning\parameter-contract\architecture.md` — as-built sim behaviour
- `C:\repos\dashboard-tests\dc-battery-sim\lexicon.json` — the live 30-entry lexicon
- `C:\repos\dashboard-tests\dc-battery-sim\main.py` — `:326-434` producer loop and `applied`
  emission; `:437-451` the thread supervisor copied in §6.1; `:474-496` the two-Application shape
- `C:\repos\dashboard-tests\.env.example` — topic names, `dcm_config=dashboard-config`
- `C:\repos\openapi_dcm.json` — the DCM REST contract; note `/{id}/content` returns content
  **unwrapped**, unlike every other endpoint
- `C:\repos\quix-samples\managed\dynamic-configuration\` — the DCM library item and its
  required configuration
- `C:\repos\TestManager\Quix.TestManager\quix.yaml` — the two-deployment shape rejected in §6.1
- `C:\repos\TestManager\TestManagerEnv\comma-car-segments-ingest\quix.yaml:209-231` — the
  working DCM deployment block copied in §6.11
- `C:\repos\TestManager\TestManagerEnv\comma-car-segments-ingest\dcm-seed-dbc\main.py` — the
  seeder pattern (`replace: true`, `sha1(f"{type}-{target_key}")`)
- `C:\repos\TestManager\Quix.TestManager\backend\api\config_api.py` — the DCM httpx client
- `C:\repos\TestManager\Quix.TestManager\frontend\` — D4's reference stack; `components/ui`,
  `components/layout`, `app/config-manager`
- Skills: `quix-service-create` (3-step procedure, §3 `app.yaml`, §4 `quix.yaml`, §4c DCM),
  `quixstreams-idioms` (§0 service shape, §1 Application/topics, §8 anti-patterns,
  "no try/except in the pipeline"), `quix-rocksdb-state-api` (§6 threading: `app.run()` on main,
  uvicorn on a worker), `quix-dcm-join-lookup`

---

## Sanity print — `example-layout.json`

| Element id | Type | Bound name | Kind | Direction | Datatype | Grid (x, y, w, h) |
|---|---|---|---|---|---|---|
| `el-power` | knob | `requested_power_w` | signal | `input` | float | 0, 0, 4, 3 |
| `el-chiller` | switch | `chiller_setting` | signal | `input` | enum | 4, 0, 4, 3 |
| `el-ke` | knob | `KE` | parameter | `null` | float | 8, 0, 4, 3 |
| `el-soc` | readout | `soc_percent` | signal | `output` | float | 0, 3, 3, 2 |
| `el-current` | readout | `dc_current_a` | signal | `output` | float | 3, 3, 3, 2 |
| `el-temp-chart` | chart | `temperature_c` | signal | `output` | float | 6, 3, 6, 4 |
| `el-voltage-chart` | chart | `dc_voltage_v` | signal | `output` | float | 0, 5, 6, 4 |

**3 controls, 4 visualisations. Every bound name exists in `dc-battery-sim/lexicon.json`**
(`requested_power_w` at `:11` as the `input` entry — it also exists at `:219` as the `output`
echo, which is exactly why the binding stores `direction`; `chiller_setting` `:37`, `KE` `:325`,
`soc_percent` `:89`, `dc_current_a` `:141`, `temperature_c` `:180`, `dc_voltage_v` `:128`).

**Every binding satisfies CLAUDE.md §4 and D1:**
- Controls bind only `direction: input` signals (`requested_power_w`, `chiller_setting`) and
  `tunable: true` parameters (`KE` — `lexicon.json:335`).
- No control binds `Q_MAX_AH` or `SAMPLE_TIME`, the two `tunable: false` parameters. **D1 holds.**
- Knobs bind `float` entries with numeric `min`/`max`; `el-power` narrows ±250 kW → ±50 kW,
  which is permitted (narrowing only). `el-ke` leaves `min`/`max` null and inherits 0 … 16.
- The switch binds an `enum` with 3 members → `style: "segmented"`, per §6.6.
- Visualisations bind only `direction: output` signals; both charts bind `float`.
- No element overlaps another: rows y0–2, y3–4, y3–6 (x6–11) and y5–8 (x0–5) are disjoint.
