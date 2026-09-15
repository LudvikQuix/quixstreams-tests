# Dashboard service — as-built architecture (M1)

**Feature:** dashboard-service · **Milestone:** M1 walking skeleton
**Spec:** `dev-planning/dashboard-service/spec.md` · **Branch:** `devDB`
**Written:** 2026-09-15

---

## 1. What the code does

`dashboard/` is one Quix deployment that serves a user-buildable grid UI, reads a plant's
**lexicon** from the Dynamic Configuration Manager over REST, consumes `dashboard-out` at
10 Hz into a 60-second in-memory window, streams that window and the live samples to every
open browser tab over a WebSocket, and publishes validated, coalesced control writes back to
`dashboard-in` under a fixed plant key. No signal name, parameter name, unit or range appears
anywhere in its source: the lexicon supplies all of them at runtime.

---

## 2. Task 0 — does a WebSocket upgrade survive the Quix public ingress?

The spec made this the first task of M1 because the hub is built on the answer.

**Answer: yes, on documentary and first-party evidence. Not yet proven on a deployed build,
because proving it requires a push and a pipeline sync, which this work is not authorised to
perform.** What was established:

| Evidence | Where | What it shows |
|---|---|---|
| Quix ships a **WebSocket server** connector in its own catalogue | `C:\repos\quix-samples\python\destinations\websocket\` | `library.json` `DeploySettings` sets `PublicAccess: true`, `UrlPrefix: websocket`, and `main.py` binds `websockets.serve(...)` on `0.0.0.0:80` |
| Its README instructs clients to connect through the public URL | same, `README.md` | "if deploying on a secure port use `wss://` in place of `ws://` (Quix Cloud uses secure connections)" |
| The public-service how-to imposes only a bind requirement | `C:\repos\quix-docs\docs\quix-cloud\deployments\deploy-public-page.md` | host `0.0.0.0`, port 80; nothing excludes an upgrade |
| A live handshake probe against this workspace's ingress | `https://config-api-svc-…/` | Returned 200 from the DCM's own uvicorn, i.e. **inconclusive** — that uvicorn serves no `/ws` route and is very likely started without WebSocket support, so this probe cannot separate "ingress stripped the upgrade" from "origin ignored it". Recorded here so nobody re-runs it and mistakes it for a negative. |

**Consequence for the build:** the WebSocket hub was written as specified, and the framing is
transport-neutral exactly as the spec requires, so the SSE + `POST /api/control` fallback
remains a swap of `backend/hub.py` plus `frontend/lib/transport/socket.ts`.

**A `GET /ws/echo` endpoint ships in this build** (`backend/api.py`) so the empirical proof is
one command on the first deployed build:

```bash
python - <<'EOF'
import asyncio, websockets

URL = "wss://sil-dashboard-<workspace>.<cluster>/ws/echo"

async def main():
    async with websockets.connect(URL) as ws:
        await ws.send("hello")
        print(await ws.recv())        # expect: echo:hello

asyncio.run(main())
EOF
```

A `101 Switching Protocols` and an `echo:hello` reply closes R1. If it fails, take the SSE
fallback before adding anything else to the hub. `/ws/echo` is removed in M2.

---

## 3. Process and thread model

One container, one process, four threads.

```
                         ┌──────────────────────── python main.py ────────────────────────┐
 dashboard-out ─────────►│ MAIN     consumer_app.run(sdf)                                 │
                         │            filter(is_telemetry).update(ingest, metadata=True)  │
                         │            ├─ applied ──► RollingWindow.set_applied + Hub      │
                         │            └─ row     ──► RollingWindow.append   + Hub         │
                         │                                                                │
  browser  ◄── :8080 ────│ http     uvicorn  ── FastAPI ── StaticFiles(out/) + /api + /ws │
                         │            asyncio: Hub._flush_loop, Hub._status_loop          │
                         │                                                                │
 dashboard-in ◄──────────│ writer   producer_app.get_producer() ── ControlWriter.run      │
                         │                                                                │
       DCM REST ◄────────│ lexicon  LexiconCache.refresh_loop (TTL)                       │
                         └────────────────────────────────────────────────────────────────┘
```

- `Application.run()` installs the SIGINT/SIGTERM handlers, so it owns the **main** thread.
  Everything else is a worker (`quix-rocksdb-state-api` §6; the same shape
  `dc-battery-sim/main.py:474-496` already uses in this repo).
- Every worker runs under `supervise()` in `main.py`: on any exception it logs `CRITICAL`
  with a traceback, sets a failure flag, and calls `consumer_app.stop(fail=True)` so
  `__main__` exits non-zero. That guard is the one Phase 1 earned — a daemon thread dying
  while the deployment stays green. Here it would mean serving a frozen page forever.
- **Only the `writer` thread ever calls `produce()`.** Producer thread-safety is therefore a
  non-question, and the write coalescer has one obvious home.
- Cross-thread hand-off is deliberately minimal: the consumer thread only appends to a list
  behind a `threading.Lock` (`Hub.publish_sample`) and to a deque behind another
  (`RollingWindow.append`). It never touches the asyncio loop, never blocks on a socket, and
  never serialises JSON.

---

## 4. Module inventory

### Backend (`dashboard/`)

| File | Lines | Responsibility |
|---|---|---|
| `main.py` | 199 | Env, the two `Application`s, the four-line SDF topology, `ingest`, the thread supervisor, startup log |
| `backend/settings.py` | 86 | One frozen dataclass built from env. Nothing else reads `os.environ` |
| `backend/lexicon.py` | 306 | DCM REST read, the six Phase 1 load rules, immutable snapshot + rev counter, boot retry, TTL refresh |
| `backend/window.py` | 80 | Time-bounded rolling window, `last_applied` cache, columnar snapshot projection |
| `backend/hub.py` | 325 | WebSocket registry, bounded per-connection queue, flush/status loops, pause/resume, ping/pong, stall reaping |
| `backend/writer.py` | 169 | Server-side re-validation (`coerce`/`validate`), coalescer, keyed produce |
| `backend/api.py` | 159 | FastAPI routes, `/ws`, `/ws/echo`, StaticFiles + SPA fallback |
| `app.yaml`, `dockerfile`, `requirements.txt` | — | Deployment surface |

`quixstreams-idioms` §0 asks for one `main.py` per deployment. That rule governs a *stream
topology*; this is a web application with a stream leg. The **whole topology is still four
lines in `main.py`** and reads at a glance; what moved out is a web server, a socket hub and
an HTTP client, none of which is topology. The split is also what keeps every file inside the
~500-line ceiling in CLAUDE.md §9.

### Frontend (`dashboard/frontend/`)

| File | Responsibility |
|---|---|
| `app/page.tsx` | The single route. Header (model label, connection/stale/lagging badges, Edit toggle, add-element buttons, Save) + the dynamically imported grid |
| `app/layout.tsx` | Theme provider, toaster, the three vendor stylesheets |
| `lib/store/dashboard-context.tsx` | Wires lexicon + socket + layout; owns the write path and the subscription set |
| `lib/store/telemetry.ts` | Latest values, per-signal history, `applied` cache, pending/confirmed/rejected bookkeeping |
| `lib/store/layout.ts` | The storage adapter interface + the localStorage implementation |
| `lib/store/raf.ts` | One shared `requestAnimationFrame` loop for every chart |
| `lib/transport/socket.ts` | WS client: hello/sub/write/pause/resume/pong, backoff with full jitter |
| `lib/lexicon/resolve.ts` | Binding resolution, candidate filters, **the D1 gate** |
| `lib/lexicon/validate.ts` | The pre-publish value gate and the float comparison for echo matching |
| `lib/types/layout.ts` | The D5 document types, defaults, and the chart multi-binding helpers |
| `components/grid/grid-canvas.tsx` | `Responsive` + `WidthProvider`, edit-mode gating, lg-only persistence |
| `components/grid/element-frame.tsx` | Title, state badge, bind/remove affordances |
| `components/elements/{readout,chart,knob}-element.tsx` | The three M1 element bodies |
| `components/elements/element-view.tsx` | Resolves bindings, picks the render state, dispatches |
| `components/binding/binding-picker.tsx` | Dialog + Command over the filtered candidate list |
| `components/ui/*` | Copied verbatim from TestManager (D4), plus a new `slider.tsx` |

---

## 5. Data flows

### 5.1 Telemetry, plant → pixel

```
dc-battery-sim ──► dashboard-out
   │
   ▼  consumer thread, sdf.filter(is_telemetry).update(ingest, metadata=True)
ingest(value, key, timestamp, headers)
   ├─ value["applied"] present?  ──► window.set_applied() ─┐
   │                                  hub.publish_applied()│ never dropped
   └─ project: keep only names the lexicon calls a         │
      direction:"output" signal; unknown keys counted,     │
      logged once per 1000                                 │
          ├─ window.append(ts_ms, row)   (time-bounded)    │
          └─ hub.publish_sample(...)  → pending list       │
                                                           │
   ▼  uvicorn loop, Hub._flush_loop every 1000/WS_FLUSH_HZ ms
build one projection per distinct subscription set ────────┘
   ▼  per connection, bounded deque (WS_QUEUE_MAX)
{"t":"frames","ts":[…],"series":{…},"dropped":N}
   ▼  browser
TelemetryStore.ingest → trim to history window → chart rAF tick / readout re-render
```

- **The sample time is the Kafka message timestamp**, obtained with `metadata=True`. A payload
  field name would be plant-specific; the broker timestamp is not. This closes leak L3 in the
  spec's model-agnosticism walkthrough.
- **The window is time-bounded, not count-bounded** (`HISTORY_SECONDS`, hard-capped at
  `HISTORY_MAX_SAMPLES`). Deriving a sample count from a parameter called `SAMPLE_TIME` would
  have hardcoded this plant's parameter name into the backend.
- **Snapshots are columnar** (`{ts: [...], series: {name: [...]}}`) because uPlot consumes
  `[xs, ys1, ys2, …]` directly. The wire format was chosen to feed the chart with no
  transform.
- **Backpressure:** on a full queue the hub discards the *oldest* `frames` message and
  increments that connection's `dropped`, which rides along on the next frame. `applied`,
  `lexicon` and `status` are never dropped — losing an `applied` would leave a control
  permanently wrong, losing a frame costs 100 ms of chart. A queue that stays full for
  `WS_STALL_TIMEOUT_S` is closed with code 1013 and the client reconnects into a clean
  snapshot.
- **A hidden tab buffers nothing.** `visibilitychange` → `pause`; the hub stops enqueuing
  frames for that connection entirely; `resume` answers with a fresh snapshot, so the gap is
  filled from the window rather than from a backlog.

### 5.2 Control, pixel → plant

```
knob drag ──► optimistic local state
   ├─ trailing throttle 100 ms  +  guaranteed emit on pointer-up (Radix onValueCommit)
   ▼
validateValue(descriptor, v, narrowedRange)      ← client gate, blocks, never clamps
   ▼  socket {"t":"write", seq, signals:{…}, parameters:{…}}
Hub._on_client_message ──► ControlWriter.submit
   ▼  validate() again, against the backend's own lexicon snapshot  (D1 enforced here too)
   ▼  queue.Queue  ──► writer thread: hold WRITE_COALESCE_MS, merge last-write-wins per field
   ▼  topic.serialize(key=PLANT_KEY) ──► producer.produce() ──► dashboard-in
```

Feedback has no ack channel by Phase 1's deliberate choice, so the `applied` echo carries it:

| Client state | Trigger | Render |
|---|---|---|
| `pending` | write published | optimistic value, subtle pulse |
| `confirmed` | an `applied` whose value matches within `1e-9 + 1e-6·|echo|` | pulse clears |
| `rejected` | an `applied` whose value differs | control snaps to the echo, toast names the field |
| `unconfirmed` | no `applied` within `APPLIED_TIMEOUT_MS` | amber, optimistic value kept |

Three details decide whether this feels right rather than maddening:

- **Float tolerance** `abs(sent − echoed) <= 1e-9 + 1e-6·abs(echoed)`. An exact `===` on a
  round-tripped float reports a rejection on a *successful* write whenever the step size is not
  a binary fraction.
- **Never reconcile a control the user is holding.** While a slider has pointer capture or
  keyboard focus, incoming `applied` values are recorded but not pushed into it; reconciliation
  runs on release. Without this the 5 s heartbeat yanks the slider out from under a dragging
  finger.
- **A 400 ms settle window before an echo may judge a write**
  (`SETTLE_MS`, `lib/store/telemetry.ts`). The round trip is coalesce (50 ms) + one plant tick
  (100 ms) + one flush (100 ms) plus slack, so an `applied` arriving inside that window may
  have been generated *before* the write reached the plant. Judging against it would report a
  rejection for a write that was about to succeed, complete with a snap-back and a toast. There
  is no correlation id to do better with — Phase 1 §6.7 deferred the ack channel deliberately —
  so early echoes are ignored rather than trusted, and the next one (≤ 5 s away) decides. This
  detail is not in the spec; it was found by walking the timing, and it is the difference
  between "the knob works" and "the knob randomly fights me".

### 5.3 Lexicon

```
boot ─► GET {CONFIG_API_URL}/api/v1/configurations/{sha1("sil-lexicon-<target>")}/content
        └─ retry with exponential backoff up to LEXICON_BOOT_TIMEOUT_S, then SystemExit(1)
        └─ validate_document(): major version 1, plus Phase 1 rules R1–R6
        └─ index into an immutable LexiconSnapshot {document, sha256, rev, signals, parameters}
TTL every LEXICON_REFRESH_S ─► same fetch; rev bumps only when the sha256 changes
rev bump ─► hub broadcasts {"t":"lexicon","rev":N} ─► every client refetches /api/lexicon
            and re-resolves every binding, WITHOUT rewriting the stored layout
```

`/{id}/content` returns the content object **unwrapped**, unlike every other endpoint on that
API — reaching for `["data"]` here is a silent `KeyError`, so the client does not.

There is deliberately **no bundled fallback copy** of `lexicon.json` in the image: a lexicon
that disagrees with the running plant makes every control lie, silently. `/healthz` answers
503 until a lexicon is loaded; a *stale plant*, by contrast, is reported as a flag and never
fails the health check, because a stopped simulation is a legitimate state.

### 5.4 Layout round-trip

```
localStorage["sil-dashboard:layout:<id>"]  ◄──►  LayoutStorage {list, load, save, remove}
                                                        ▲
                                  DashboardProvider ─────┘  (M2 repoints this to /api/layouts)
```

The document written to localStorage is **already the D5 schema**, so M2 swaps one file and
performs no migration. `?layout=<id>` selects it; the default id is `default`. Positions are
stored once in the 12-column `lg` space and only written back while the viewport is actually
at `lg` — narrower breakpoints are derived by `react-grid-layout` at render time and never
persisted.

---

## 6. Decisions worth knowing in six months

1. **One image, one process (spec shape C).** Static-exported Next.js served by FastAPI. The
   alternative — a Node frontend deployment proxying to a Python backend, as TestManager does
   — cannot carry a WebSocket upgrade through `next rewrites`, so it would force a second
   public URL and a CORS policy. Cost: no SSR, no route handlers, no `next/image` optimizer.
   All acceptable for a realtime grid, which is a client app end to end.
2. **`auto_offset_reset="latest"` is not a preference.** Replaying history at boot would fill
   the window with stale samples and, worse, apply an old `applied` block as current control
   state.
3. **Shape checks are filters, never `try`/`except` inside a step.** An exception inside
   `sdf.update` takes down `consumer_app.run()` and the whole service. The only two `except`
   blocks in backend code are on the lexicon refresh (must not drop a good snapshot) and on
   JSON parsing of a browser frame (one malformed frame must not kill a healthy connection);
   both carry a comment naming what they catch.
4. **Per-request write rejection, not per-field.** The plant is field-granular and stays that
   way. At the dashboard boundary an all-or-nothing rejection is what lets the UI tell the user
   *which* field was refused; a partially-forwarded write with no ack channel cannot.
5. **Charts redraw from a shared rAF tick, not from React.** Readouts re-render through
   `useSyncExternalStore` at the flush rate; charts never do. Twelve charts cost one frame's
   scheduling.
6. **`replicas: 1` is correctness.** All `dashboard-out` traffic carries one key, so it lands
   on one partition; a second replica in the same consumer group gets no partition and serves
   blank charts to whichever browsers the ingress routes to it.

---

## 7. Deviations from the spec, and why

| # | Spec said | Built | Why |
|---|---|---|---|
| D-1 | Chart binds exactly one entry (`binding`); OQ-1 left open | Chart binds 1..N via an additive `bindings: []`, with `binding` kept as the singular alias mirroring `bindings[0]`; documents declare `layout_version "1.1"` | CLAUDE.md §4 **as amended by D7** says a chart takes 1..N, and the brief makes D1–D7 binding. The spec's OQ-1 quotes the pre-D7 §4. This is exactly OQ-1's own recommended seam. A v1.0 document still loads unchanged. **`layout-schema.json` needs the matching 1.1 bump — open point OP-1.** |
| D-2 | `main.py ~120` holds env | `backend/settings.py` holds one frozen `Settings.from_env()`, called from `main.py` | Five modules need the values; threading them through constructors from `main.py` would be worse than one dataclass. Still exactly one place that reads `os.environ` |
| D-3 | Fall back to the payload timestamp field when the broker timestamp is absent | Falls back to the wall clock | Naming a payload timestamp field would reintroduce the model-agnosticism leak the broker timestamp was chosen to close. There is no plant-agnostic name to fall back to |
| D-4 | Server verbs: snapshot, frames, applied, lexicon, status, ping | Adds `write_error {seq, errors}` | A rejected WS write otherwise has no reply at all, and `seq` exists for correlation. Ten lines; prevents a silent failure on the adversarial path |
| D-5 | `GET /api/lexicon`, `/api/snapshot`, … | Adds `GET /api/config` | The client needs `applied_timeout_ms` (and `history_seconds` before its first snapshot). The alternative was hardcoding a backend default in the browser |
| D-6 | `python:3.13-slim-bookworm`, `npm ci`, `jsonschema` pinned | `python:3.12.5-slim-bookworm`, `npm install`, no `jsonschema` | 3.12.5 is the base image this repo already builds quixstreams against (`dc-battery-sim/dockerfile`). `npm ci` needs a committed `package-lock.json`, which does not exist yet — see the checklist. `jsonschema` is only needed for M2's layout-save validation |
| D-7 | `LAYOUT_TYPE` declared in `app.yaml` | Omitted | M1 has no DCM layout CRUD, so no code reads it. A declared variable nothing reads is dead config; M2 adds it with the layout store |
| D-8 | Root `quix.yaml` does not exist yet; this spec creates it | It already exists; one deployment block was appended by hand | The file was created by earlier Phase-1 work. Matches the file's existing `resources.limits` style rather than the spec snippet's flat form, for consistency with its neighbours |

---

## 8. Integration points

- **`dc-battery-sim`** — untouched. Coupled only through the Phase 1 contracts: the
  `dashboard-in` envelope (§6.3), the `dashboard-out` payload plus the optional `applied` block
  (§6.4), and the message key `battery-sim` (carried as `PLANT_KEY`, because it is *not* the
  lexicon's `model.name`).
- **DCM** — read-only in M1, over REST at `http://config-api-svc` in-cluster, authorised with
  `Quix__Sdk__Token`. M2 adds layout CRUD under type `dashboard-layout` and lexicon
  invalidation from the `config-updates` topic.
- **Topics** — `dashboard-out` in, `dashboard-in` out, both 1 partition, both already declared.
  No new topic.
- **Pipeline graph** — unchanged in shape: `SIL Dashboard ⇄ dashboard-in / dashboard-out ⇄ DC
  Battery Sim`, alongside the existing MongoDB and DCM. Exactly one deployment block added.

---

## 9. What M1 deliberately leaves out

DCM layout CRUD, version list and restore; the Switch and Type-in elements; knob log scale;
the standalone `incompatible` render state (it currently shares the `broken` treatment with a
distinct message); the summary banner for lost bindings; lexicon invalidation from the config
topic; the `dcm-seed-lexicon` Job; Playwright e2e; `dashboard/README.md` (DocuGuy's, and it is
where the seeding `curl` belongs).

Seeding the lexicon is still a manual step before the first boot:

```
curl -X POST "$CONFIG_API_URL/api/v1/configurations" \
  -H "Authorization: Bearer $Quix__Sdk__Token" -H "Content-Type: application/json" \
  -d "{\"metadata\":{\"type\":\"sil-lexicon\",\"target_key\":\"dc-battery-sim\"},
       \"content\":$(cat dc-battery-sim/lexicon.json),\"replace\":true}"
```

The dashboard fails loudly at boot if this has not happened, which is the intended behaviour.

---

## 10. Verification checklist (Tester owns all of it — none of it was run here)

### 10.1 Lint scope

| Command | Scope | Notes |
|---|---|---|
| `pre-commit run --all-files` | whole repo, pinned ruff `v0.6.3` | New Python: `dashboard/main.py`, `dashboard/backend/*.py`. `ruff-format` will be the noisy one — the code was written to Black/ruff-format shape by hand and never formatted by the tool |
| `npm install && npm run lint` in `dashboard/frontend` | new TypeScript | `next lint` with `next/core-web-vitals`. Expect the `react-hooks/exhaustive-deps` disables in `dashboard-context.tsx`, `chart-element.tsx`, `knob-element.tsx`, `element-view.tsx` — each is commented with why |
| `npm run type-check` in `dashboard/frontend` | `tsc --noEmit` | Highest-value frontend check. The uPlot scale-range callback and the `react-grid-layout` `compactType` prop are the two typings most likely to argue |
| `docker build dashboard/` | the image | Proves the two-stage build and the static export; also the first thing the platform will do |

### 10.2 Module-by-module

| Module | What must hold |
|---|---|
| `backend/settings.py` | Missing `telemetry_in` / `control_out` / `PLANT_KEY` / `CONFIG_API_URL` / `LEXICON_TARGET_KEY` raises `KeyError` at import — not a silent default |
| `backend/lexicon.py` | `config_id("sil-lexicon", "dc-battery-sim")` equals the id the seeding curl used; `validate_document` rejects major ≠ 1 and each of the six load rules (the code says "load rule 1..6" rather than "R1..R6", deliberately: `R0`/`R1`/`R2` are also battery parameter names and would trip the model-agnosticism grep in 10.3); a second `fetch()` with unchanged content does **not** bump `rev` |
| `backend/window.py` | Appending past `HISTORY_SECONDS` evicts from the left; past `HISTORY_MAX_SAMPLES` hard-caps; `snapshot(names, s)` returns equal-length `ts` and every `series` column, with `null` for a missing name |
| `backend/hub.py` | Queue overflow drops the **oldest `frames`** and increments `dropped`; `applied`/`lexicon`/`status` are never dropped; `hello` and `resume` both answer with a snapshot; `pause` stops frames but not `applied` |
| `backend/writer.py` | `coerce` rejects `True` for a numeric field, rejects `0.5` for an int, accepts an int for a float and stores `float`, canonicalises an enum to the member's `value`, and **blocks** rather than clamps out-of-range; `validate` rejects `SAMPLE_TIME`/`Q_MAX_AH` (D1); two writes inside `WRITE_COALESCE_MS` produce **one** message |
| `backend/api.py` | `/healthz` is 503 only without a lexicon and 200 with a stale plant; `/api/control` is 422 on a bad field and 202 otherwise; the static catch-all does not shadow `/api`, `/ws` or `/healthz` |
| `frontend/lib/lexicon/resolve.ts` | `candidates()` never returns a `tunable: false` parameter for `switch`/`knob`/`typein`, and does return it for `readout` |
| `frontend/lib/store/telemetry.ts` | An echo inside `SETTLE_MS` of a write neither confirms nor rejects it; one outside it with a different value fires the reject handler once |

### 10.3 Smoke checks, in order

1. **Task 0 (blocking).** Deploy, then `wss://<public-url>/ws/echo`, send `hello`, expect
   `101` then `echo:hello`. **If this fails, stop and take the SSE fallback** before anything
   else.
2. `GET /healthz` → 503 before the lexicon is seeded, 200 after, with `lexicon_rev >= 1`.
3. `GET /api/lexicon` → 30 descriptors, `rev` 1.
4. `GET /` → the exported HTML, not a 404; assets load from the same origin.
5. `GET /api/snapshot?signals=<two output names from the lexicon>&seconds=10` → non-empty `ts`
   once the sim is running, and `applied` non-null within 5 s.
6. `POST /api/control` with a valid in-range field → 202, and the field's value moves in the
   next `applied`. With an out-of-range value → **422**, and `dashboard-in` sees nothing.
7. `POST /api/control` naming `SAMPLE_TIME` → 422 (D1), nothing produced.
8. **UI end to end:** open the public URL → Edit → add a Chart → bind two output signals →
   leave Edit → both traces draw within one sample period. Add a Knob → bind an input signal →
   drag it → a readout bound to a dependent output moves within ~200 ms and the knob's pulse
   clears when the echo lands.
9. Reload the page: the grid returns from localStorage and every chart is populated
   immediately from the backend window (not empty, not waiting).
10. Background the tab for a minute, return: charts refill from a fresh snapshot with a visible
    gap, and the backend's memory is unchanged.
11. Model-agnosticism grep (spec §6.9) — must return **zero** matches:
    `rg -n -w 'soc_percent|…|MAX_BATTERY_TEMP' dashboard/ --glob '!**/*.test.*' --glob '!**/e2e/**'`

### 10.4 Spec sections each path is meant to satisfy

| Code | Spec |
|---|---|
| `main.py` topology + threads | §6.1, §6.3 |
| `backend/lexicon.py` | §6.2, Phase 1 §6.1 (the six load rules) |
| `backend/window.py` | §6.3, D6 |
| `backend/hub.py` | §6.4 |
| `backend/writer.py` | §6.8, Phase 1 §6.3, §6.7 |
| `backend/api.py` | §6.11 route table |
| `frontend/lib/lexicon/*` | §6.6, §6.7, D1 |
| `frontend/components/elements/*` | §6.6 (readout, chart, knob only in M1) |
| `frontend/lib/store/layout.ts` | §6.5, D5 (localStorage adapter, D5 document) |
| `quix.yaml` + `app.yaml` | §6.11 |
