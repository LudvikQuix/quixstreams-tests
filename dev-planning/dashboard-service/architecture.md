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
       DCM REST ◄───────►│ lexicon  LexiconCache.refresh_loop (TTL, seed, recovery)       │
                         └────────────────────────────────────────────────────────────────┘
```

- `Application.run()` installs the SIGINT/SIGTERM handlers, so it owns the **main** thread.
  Everything else is a worker (`quix-rocksdb-state-api` §6; the same shape
  `dc-battery-sim/main.py:499-518` already uses in this repo).
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
| `main.py` | 213 | Env, the two `Application`s, the four-line SDF topology, `ingest`, the thread supervisor, startup log |
| `backend/settings.py` | 98 | One frozen dataclass built from env. Nothing else reads `os.environ` |
| `backend/lexicon.py` | 390 | The **pair**: merge, model-mismatch refusal, immutable snapshot + rev counter, degraded boot, TTL/recovery refresh |
| `backend/lexicon_config.py` | 259 | **One** DCM configuration: deterministic id, GET, the seed write, its own rev and its own error |
| `backend/lexicon_rules.py` | 223 | `LexiconError`/`LexiconMissing` and the six Phase 1 load rules, plus the pair rules that need both documents |
| `backend/window.py` | 80 | Time-bounded rolling window, `last_applied` cache, columnar snapshot projection |
| `backend/hub.py` | 325 | WebSocket registry, bounded per-connection queue, flush/status loops, pause/resume, ping/pong, stall reaping |
| `backend/writer.py` | 169 | Server-side re-validation (`coerce`/`validate`), coalescer, keyed produce |
| `backend/api.py` | 186 | FastAPI routes, `/ws`, `/ws/echo`, StaticFiles + SPA fallback |
| `seed/signals.json` | — | Copy of `dc-battery-sim/signals.json`, written to the DCM only when `sil-signals` is absent |
| `seed/parameters.json` | — | Copy of `dc-battery-sim/parameters.json`, same rule for `sil-parameters`. Seeds independently of the signal document |
| `app.yaml`, `dockerfile`, `requirements.txt` | — | Deployment surface |

`backend/lexicon.py` used to be 510 lines and one class, and the note here used to argue that
the only available seam — the pure validation functions — was artificial, because they
`raise LexiconError` and moving them would separate the exception class from its raisers.

D9 settled that argument. With two configurations the rules are applied by two independent
caches and to two bundled seed files, so they are no longer one module's private business, and
`LexiconError` moves *with* them into `lexicon_rules.py` rather than being stranded. The second
cut follows the same test: `ConfigCache` knows a type, a target key, an id, a validator and a
seed file and nothing about the other configuration, while `LexiconCache` knows only how to
pair two of them. Three files, three one-sentence responsibilities, all inside the ceiling.

`lexicon.py` re-exports `LexiconError`, `LexiconMissing`, `ConfigCache` and `config_id` through
`__all__`, so `api.py`, `hub.py` and `writer.py` still import the whole lexicon vocabulary from
one module and the split is invisible to them.

`quixstreams-idioms` §0 asks for one `main.py` per deployment. That rule governs a *stream
topology*; this is a web application with a stream leg. The **whole topology is still four
lines in `main.py`** and reads at a glance; what moved out is a web server, a socket hub and
an HTTP client, none of which is topology. The split is also what keeps every file at or
around the ~500-line ceiling in CLAUDE.md §9.

### Frontend (`dashboard/frontend/`)

| File | Responsibility |
|---|---|
| `app/page.tsx` | The single route. Header (model label, connection/stale/lagging badges, dark-mode toggle, Edit toggle, add-element buttons, Save) + the dynamically imported grid, and the `NoLexicon` empty state |
| `app/layout.tsx` | Theme provider, toaster, the three vendor stylesheets |
| `lib/store/dashboard-context.tsx` | Wires lexicon + socket + layout; owns the write path, the subscription set, and the single lexicon load path (`loadToken`) that the retry button, the 10 s auto-retry and a `lexicon` frame all drive |
| `lib/store/telemetry.ts` | Latest values, per-signal history, `applied` cache, pending/confirmed/rejected bookkeeping |
| `lib/store/layout.ts` | The storage adapter interface + the localStorage implementation |
| `lib/store/raf.ts` | One shared `requestAnimationFrame` loop for every chart |
| `lib/transport/socket.ts` | WS client: hello/sub/write/pause/resume/pong, backoff with full jitter |
| `lib/api/client.ts` | Typed same-origin fetch; `ApiError` carries the backend's `detail` and 503 body so the empty state can explain itself |
| `lib/lexicon/resolve.ts` | Binding resolution, candidate filters, **the D1 gate** |
| `lib/lexicon/validate.ts` | The pre-publish value gate and the float comparison for echo matching |
| `lib/types/layout.ts` | The D5 document types, defaults, and the chart multi-binding helpers |
| `components/grid/grid-canvas.tsx` | `Responsive` + `WidthProvider`, edit-mode gating, lg-only persistence |
| `components/grid/element-frame.tsx` | Title, state badge, bind/remove affordances |
| `components/elements/readout-element.tsx` | Numeric / enum readout; dims on stale signal |
| `components/elements/chart-element.tsx` | uPlot line chart; themes axis and grid colours from CSS variables on every render |
| `components/elements/knob-element.tsx` | Slider control for numeric datatypes (uint/int/float): Radix slider with trailing throttle |
| `components/elements/rotary-element.tsx` | Rotary detent knob for bool/enum: SVG dial, drag + keyboard (arrows/Home/End) + direct-tap buttons; one detent per lexicon enum member |
| `components/elements/element-view.tsx` | Resolves bindings, picks the render state; for type `knob` dispatches `RotaryElement` (bool/enum) or `KnobElement` (numeric) based on the descriptor's datatype |
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

### 5.3 Lexicon — two configurations, one snapshot

D9 publishes the lexicon as **two** DCM configurations that version independently.
`ConfigCache` owns one of them; `LexiconCache` owns the pair.

```
LexiconCache.ensure_loaded()  ── the ONE entry point; boot, TTL loop and
                                 POST /api/lexicon/refresh all call it
  ├─ ConfigCache(signals).ensure_loaded()      ─┐  independent. Neither
  └─ ConfigCache(parameters).ensure_loaded()   ─┘  waits on the other, and
  │                                                neither can raise past here
  │      each one:
  │      GET {CONFIG_API_URL}/api/v1/configurations/{sha1("<type>-<target>")}/content
  │        ├─ 200 ─► validate_signals / validate_parameters
  │        │         (major version 1, model.name present, load rules 1-6)
  │        │         └─ store document + bump THIS config's rev on a sha change
  │        ├─ 404 ─► LexiconMissing ─► _seed() ─► POST /api/v1/configurations
  │        │                           {metadata:{type,target_key,valid_from,category},
  │        │                            content: seed/<collection>.json}   (NO `replace`)
  │        │                           └─ re-GET; the READ is what decides
  │        └─ anything else ─► recorded as THIS config's error; the bundle is
  │                            never written over a store that may hold a document
  ▼
_merge()
  ├─ neither loaded          ─► LexiconError, no snapshot           (degraded)
  ├─ both loaded, model.name ─► LexiconError, snapshot DROPPED      (degraded)
  │  disagree or a name is
  │  in both collections
  └─ otherwise ─► {lexicon_version, model, signals: [...] | [], parameters: [...] | []}
                  sha256 ─► rev bump on change ─► LexiconSnapshot, carrying
                  signals_loaded / parameters_loaded

rev bump ─► hub broadcasts {"t":"lexicon","rev":N} ─► every client reloads /api/lexicon
            and re-resolves every binding, WITHOUT rewriting the stored layout
```

`/{id}/content` returns the content object **unwrapped**, unlike every other endpoint on that
API — reaching for `["data"]` here is a silent `KeyError`, so the client does not.

**Why the merge exists at all.** Everything downstream of the cache — `writer.validate`,
`ingest`'s `output_names` projection, the frontend's `resolve()` and `candidates()` — asks
"what does this plant expose", not "which configuration was it published as". Merging once, in
the one place that knows both halves, is what kept the split from reaching any of them: not one
line of `hub.py`, `window.py`, `resolve.ts` or `validate.ts` changed.

**Per-configuration boot state.** Each configuration walks the same four cases on its own, and
`/healthz` reports both:

| DCM, for ONE configuration | Behaviour | its `state()` |
|---|---|---|
| holds the document | read, validated, indexed. The bundle is never consulted | `loaded: true`, `rev ≥ 1`, `error: null` |
| empty (404), seed succeeds | bundle POSTed once, read straight back, rev 1 | `loaded: true`, `seeded_by_this_pod: true` |
| empty, seed declined or fails | that half is absent; retried every 30 s | `loaded: false`, `error` = the DCM's answer |
| unreachable / 403 / 5xx | that half is absent, **never seeded** | `loaded: false`, `error` = `DCM unreachable at …` |

**What the pair does with those.** `/healthz` is always 200 and `status` is three-valued,
because two configurations have three outcomes and not two:

| signals | parameters | `status` | `/api/lexicon` | What the user gets |
|---|---|---|---|---|
| loaded | loaded | `ok` | 200 | The full dashboard |
| loaded | absent | `partial` | 200, `parameters: []` | Charts and readouts on signals work; the picker offers no parameters, a parameter binding renders unbound, a parameter write is refused with "the parameters configuration is not loaded". Header badge: *no parameters · read-only* |
| absent | loaded | `partial` | 200, `signals: []` | Parameter readouts and tunable-parameter controls work; no telemetry binding resolves. Header badge: *no signals · no telemetry bindings* |
| absent | absent | `degraded` | 503 + `{detail, signals:{…}, parameters:{…}, …}` | The "No lexicon yet" empty state, naming both configurations and why each is missing |
| mismatched pair | | `degraded` | 503 | Same empty state; `lexicon_pair_error` names the two `model.name` values |

`lexicon_error` is still there as the one-line summary a probe can act on, but it is now derived
from the two per-configuration errors rather than being the only thing recorded. Collapsing them
was the thing D9 explicitly forbids: signals at rev 4 with parameters absent is a *working*
dashboard, and it must not read the same as a dashboard with nothing at all.

**A mismatched pair is a misconfiguration, not a merge.** Both documents carry `model`, which is
the only way to tell that two independently versioned configurations still describe the same
plant. On disagreement — or on a name appearing in both collections, the half of load rule 6 that
no single document can check — `_merge` logs `ERROR`, records `lexicon_pair_error`, **drops any
existing snapshot** and raises. Serving the old snapshot instead would mean the dashboard had
read a DCM change and silently ignored it; serving a merged one would mean rendering controls
that write fields the running plant has never heard of, on a page that looks perfectly healthy.

**The bundles seed, they never serve.** `dashboard/seed/signals.json` and
`dashboard/seed/parameters.json` are written to the DCM only on a 404 for their own
configuration and only with `replace` absent, so each POST creates or is declined — neither can
version over what an operator edited, and neither depends on the other succeeding. Each document
is put through the same validator as a document read back, because seeding something this
service would refuse to read poisons the store permanently. Once the DCM holds a configuration,
the DCM is the only source the dashboard ever reads it from; there is still no fallback copy
served from the image, which is what would let a lexicon disagree with the running plant.
`LEXICON_SEED_ENABLED=false` turns both seeds off once the DCM is the managed source.

The two seed files are **copies** of `dc-battery-sim/signals.json` and
`dc-battery-sim/parameters.json` — the image build context is `dashboard/`, so a
`COPY ../dc-battery-sim/...` is impossible. They must be re-synced by hand whenever the plant's
lexicon changes; drift only matters for a *first* boot against an empty DCM, but on that boot it
is what gets written.

**Recovery cadence.** `refresh_loop` polls every `DEGRADED_RETRY_S` (30 s) while *either*
configuration is missing and `LEXICON_REFRESH_S` (15 min) only once both are in hand. A
dashboard running on signals alone is degraded even though it has a snapshot, so it stays on the
fast cadence until its parameters arrive.

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

The normative schema is `dev-planning/dashboard-service/layout-schema.json`, at **1.1** since
fix round 1. 1.1 adds exactly one optional, chart-only key — `bindings`, an array of the same
`binding` object — so a chart can carry the 1..N series D7 requires. Because the key is
optional and nothing was removed or tightened, **every 1.0 document validates against 1.1
unchanged**; the bump is additive, not breaking. The writer's obligation, which JSON Schema
cannot express, is that `binding` always equals `bindings[0]`, so a consumer that only knows
1.0 still reads a valid single-series chart out of a 1.1 document. `withBindings()` in
`frontend/lib/types/layout.ts:122` is the only place that maintains that invariant. On a
non-chart element it sets `bindings` to `undefined`, which `JSON.stringify` then omits, so the
persisted document carries no `bindings` key at all and the schema's chart-only rule holds. A
storage layer that ever serialised `undefined` as `null` would break that — the one thing to
re-check when M2 repoints `LayoutStorage` at `/api/layouts`.

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
   `sdf.update` takes down `consumer_app.run()` and the whole service, so the QuixStreams
   pipeline in `main.py` holds no `try` at all: a malformed message is filtered out, never
   caught. Outside the pipeline every `except` is deliberate and carries a comment naming what
   it catches and why swallowing it is right — the lexicon refresh (must not drop a good
   snapshot), JSON parsing of a browser frame (one malformed frame must not kill a healthy
   connection), and the two `LexiconError` guards on the write paths added in fix round 1
   (`api.py` answers 503, `hub.py` answers `write_error`), so an unloaded lexicon cannot become
   a bare 500 or a killed socket.
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
| D-1 | Chart binds exactly one entry (`binding`); OQ-1 left open | Chart binds 1..N via an additive `bindings: []`, with `binding` kept as the singular alias mirroring `bindings[0]`; documents declare `layout_version "1.1"` | CLAUDE.md §4 **as amended by D7** says a chart takes 1..N, and the brief makes D1–D7 binding. The spec's OQ-1 quotes the pre-D7 §4. This is exactly OQ-1's own recommended seam. A v1.0 document still loads unchanged. **Closed in fix round 1: `layout-schema.json` is now `dashboard-layout/1.1.json` and models `bindings` — see §11.** |
| D-2 | `main.py ~120` holds env | `backend/settings.py` holds one frozen `Settings.from_env()`, called from `main.py` | Five modules need the values; threading them through constructors from `main.py` would be worse than one dataclass. Still exactly one place that reads `os.environ` |
| D-3 | Fall back to the payload timestamp field when the broker timestamp is absent | Falls back to the wall clock | Naming a payload timestamp field would reintroduce the model-agnosticism leak the broker timestamp was chosen to close. There is no plant-agnostic name to fall back to |
| D-4 | Server verbs: snapshot, frames, applied, lexicon, status, ping | Adds `write_error {seq, errors}` | A rejected WS write otherwise has no reply at all, and `seq` exists for correlation. Ten lines; prevents a silent failure on the adversarial path |
| D-5 | `GET /api/lexicon`, `/api/snapshot`, … | Adds `GET /api/config` | The client needs `applied_timeout_ms` (and `history_seconds` before its first snapshot). The alternative was hardcoding a backend default in the browser |
| D-6 | `python:3.13-slim-bookworm`, `npm ci`, `jsonschema` pinned | `python:3.12.5-slim-bookworm`, `npm ci`, no `jsonschema` | 3.12.5 is the base image this repo already builds quixstreams against (`dc-battery-sim/dockerfile`). The `npm install` deviation is closed in fix round 1: Tester generated `dashboard/frontend/package-lock.json` (lockfileVersion 3, 458 packages), it is now staged, and the dockerfile is back on `npm ci`. `jsonschema` is only needed for M2's layout-save validation |
| D-7 | `LAYOUT_TYPE` declared in `app.yaml` | Omitted | M1 has no DCM layout CRUD, so no code reads it. A declared variable nothing reads is dead config; M2 adds it with the layout store |
| D-8 | Root `quix.yaml` does not exist yet; this spec creates it | It already exists; one deployment block was appended by hand | The file was created by earlier Phase-1 work. Matches the file's existing `resources.limits` style rather than the spec snippet's flat form, for consistency with its neighbours |
| D-9 | §6.2: no fallback copy in the image; exit non-zero when the lexicon cannot be loaded | `dashboard/seed/lexicon.json` ships in the image and is POSTed to the DCM **when the DCM has none**; the process never exits over a lexicon | Hotfix brief, 2026-09-15, on a live crash-loop. Nothing had ever seeded the DCM and the spec defers the seeder Job to M2, so `load_at_boot` could not succeed, the pod restarted forever and the ingress 503'd every route. The spec's real requirement — never *serve* a lexicon the DCM does not have — is preserved exactly: the bundle is only ever written to an empty store (create-only, no `replace`), never served from disk. Recorded as OP-4 for Buddy to fold into §6.2 |
| D-10 | §6.11: `GET /healthz` is **503 when the lexicon is not loaded** | 200 with `status: "degraded"` and the reason in the body; `/api/healthz` added as an alias | Same brief. A 503 from the app is indistinguishable from the ingress's own 503 for "no pod is running", which is the exact confusion that cost a debugging cycle here. The information the spec wanted is still there and is now richer: `lexicon_loaded`, `lexicon_error`, `lexicon_config_id`, `dcm_token_present`. `GET /api/lexicon` keeps 503, which is where a client that *needs* the document should learn it is absent. Also OP-4 |

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
topic; Playwright e2e; `dashboard/README.md` (DocuGuy's).

The `dcm-seed-lexicon` Job is **not** deferred any more — it is cancelled. A Job would be a
second deployment, which CLAUDE.md D8 forbids, and the dashboard seeds itself (§5.3). Seeding
by hand is still possible and is the documented way to *replace* a lexicon the dashboard
seeded, because the dashboard itself will never overwrite one:

```
curl -X POST "$CONFIG_API_URL/api/v1/configurations" \
  -H "Authorization: Bearer $Quix__Sdk__Token" -H "Content-Type: application/json" \
  -d "{\"metadata\":{\"type\":\"sil-signals\",\"target_key\":\"dc-battery-sim\"},
       \"content\":$(cat dc-battery-sim/signals.json),\"replace\":true}"

curl -X POST "$CONFIG_API_URL/api/v1/configurations" \
  -H "Authorization: Bearer $Quix__Sdk__Token" -H "Content-Type: application/json" \
  -d "{\"metadata\":{\"type\":\"sil-parameters\",\"target_key\":\"dc-battery-sim\"},
       \"content\":$(cat dc-battery-sim/parameters.json),\"replace\":true}"
```

Two calls since D9, and **that is the point**: tuning a parameter versions `sil-parameters`
alone and leaves the signal lexicon's history untouched, which is what makes "what changed in
the signal set" answerable at all. Either call can be made without the other.

`replace: true` is the difference from the service's own seed: it versions an existing
configuration, which the seed refuses to do. The dashboard picks the new version up on its next
poll (≤ `LEXICON_REFRESH_S`, or 30 s while either configuration is missing) or immediately on
`POST /api/lexicon/refresh`.

---

## 10. Verification checklist (Tester owns all of it — none of it was run here)

### 10.1 Lint scope

| Command | Scope | Notes |
|---|---|---|
| `pre-commit run --all-files` | whole repo, pinned ruff `v0.6.3` | New Python: `dashboard/main.py`, `dashboard/backend/*.py`. Formatted by the `ruff-format` hook in fix round 1; it should now be a no-op |
| `npm ci && npm run lint` in `dashboard/frontend` | new TypeScript | `next lint` with `next/core-web-vitals`. Expect the `react-hooks/exhaustive-deps` disables in `dashboard-context.tsx`, `chart-element.tsx`, `knob-element.tsx`, `element-view.tsx` — each is commented with why |
| `npm run type-check` in `dashboard/frontend` | `tsc --noEmit` | Highest-value frontend check. The uPlot scale-range callback and the `react-grid-layout` `compactType` prop are the two typings most likely to argue |
| `docker build dashboard/` | the image | Proves the two-stage build and the static export; also the first thing the platform will do |

### 10.2 Module-by-module

| Module | What must hold |
|---|---|
| `backend/settings.py` | Missing `telemetry_in` / `control_out` / `PLANT_KEY` / `CONFIG_API_URL` / `LEXICON_TARGET_KEY` raises `KeyError` at import — not a silent default. `SIGNALS_TYPE` / `PARAMETERS_TYPE` default to `sil-signals` / `sil-parameters`; `LEXICON_TYPE` and `LEXICON_SEED_PATH` no longer exist and setting them does nothing |
| `backend/lexicon_rules.py` | `validate_signals` / `validate_parameters` reject major ≠ 1, a document with no `model.name`, a missing collection array, and each of the six load rules (the code says "load rule 1..6" rather than "R1..R6", deliberately: `R0`/`R1`/`R2` are also battery parameter names and would trip the model-agnosticism grep in 10.3). A signal document with the same name at both directions is **valid** (the sim echoes two setpoints). `pair_problems` returns a problem for disagreeing `model.name` and for a name present in both collections, and `[]` otherwise |
| `backend/lexicon_config.py` | The four per-configuration boot cases in §5.3; `_seed()` sends **no** `replace` key, so a second call against a seeded DCM is declined and the document still comes from the store; `_read_bundle()` validates before POSTing and reads with `encoding="utf-8"`; `config_id("sil-signals", "dc-battery-sim")` and `config_id("sil-parameters", "dc-battery-sim")` equal the ids the two seeding curls use; a second `_fetch()` with unchanged content does **not** bump that configuration's `rev`; `ensure_loaded()` returns a bool and **never raises**, whatever the DCM answers |
| `backend/lexicon.py` | One configuration failing does not stop the other being read or seeded; the merged document carries `[]` for a missing collection and the snapshot's `signals_loaded`/`parameters_loaded` say which; the merged `rev` bumps only when the merged sha changes; a `model.name` mismatch raises, records `lexicon_pair_error`, and **drops** any existing snapshot; `refresh_loop` uses the 30 s cadence while either half is missing, even though a snapshot exists |
| `backend/window.py` | Appending past `HISTORY_SECONDS` evicts from the left; past `HISTORY_MAX_SAMPLES` hard-caps; `snapshot(names, s)` returns equal-length `ts` and every `series` column, with `null` for a missing name |
| `backend/hub.py` | Queue overflow drops the **oldest `frames`** and increments `dropped`; `applied`/`lexicon`/`status` are never dropped; `hello` and `resume` both answer with a snapshot; `pause` stops frames but not `applied`; a `write` frame arriving before the lexicon has loaded answers `write_error` instead of killing the socket |
| `backend/writer.py` | `coerce` rejects `True` for a numeric field, rejects `0.5` for an int, accepts an int for a float and stores `float`, canonicalises an enum to the member's `value`, and **blocks** rather than clamps out-of-range; `validate` rejects `SAMPLE_TIME`/`Q_MAX_AH` (D1); two writes inside `WRITE_COALESCE_MS` produce **one** message |
| `backend/api.py` | `/healthz` and `/api/healthz` both answer 200 always, with `status` `ok` vs `degraded` and a non-null `lexicon_error` when degraded; `/api/lexicon` is 503 **with a JSON body carrying `detail` and the state fields** when there is no lexicon; `/api/lexicon/refresh` seeds as well as re-reads; `/api/control` is 422 on a bad field, 503 with a `detail` body when the lexicon has not loaded, and 202 otherwise; the static catch-all does not shadow `/api`, `/ws` or `/healthz` |
| `frontend/lib/lexicon/resolve.ts` | `candidates()` never returns a `tunable: false` parameter for `switch`/`knob`/`typein`, and does return it for `readout` |
| `frontend/lib/store/telemetry.ts` | An echo inside `SETTLE_MS` of a write neither confirms nor rejects it; one outside it with a different value fires the reject handler once |

### 10.3 Smoke checks, in order

1. **Task 0 (blocking).** Deploy, then `wss://<public-url>/ws/echo`, send `hello`, expect
   `101` then `echo:hello`. **If this fails, stop and take the SSE fallback** before anything
   else.
2. **Self-seeding (this hotfix's reason to exist).** Against a DCM with no `sil-lexicon` /
   `dc-battery-sim` configuration: the deployment must reach *Running* and stay there.
   `GET /api/healthz` → 200. Within one boot it should read `status: "ok"`,
   `lexicon_rev: 1`, `lexicon_seeded_by_this_pod: true`; the log carries
   `[LEXICON] seeded sil-lexicon/dc-battery-sim (id=…)`. Restart the deployment: the second
   boot logs no seed (or a declined one), `lexicon_seeded_by_this_pod` is `false`, and
   `lexicon_rev` is still 1 — **the stored document must not gain a version.**
3. **Degraded path.** Set `CONFIG_API_URL` to a name that does not resolve and redeploy: the
   deployment still reaches Running, `GET /api/healthz` → 200 with `status: "degraded"` and a
   `lexicon_error` naming the DCM, `GET /api/lexicon` → 503 with a `detail`, `GET /` renders
   the "No lexicon yet" page with a working Retry button. Put the URL back, press Retry (or
   wait ≤ 30 s) and the grid appears **without a redeploy**.
4. `GET /api/lexicon` → 30 descriptors, `rev` 1.
5. `GET /` → the exported HTML, not a 404; assets load from the same origin.
6. `GET /api/snapshot?signals=<two output names from the lexicon>&seconds=10` → non-empty `ts`
   once the sim is running, and `applied` non-null within 5 s.
7. `POST /api/control` with a valid in-range field → 202, and the field's value moves in the
   next `applied`. With an out-of-range value → **422**, and `dashboard-in` sees nothing.
8. `POST /api/control` naming `SAMPLE_TIME` → 422 (D1), nothing produced.
9. **UI end to end:** open the public URL → Edit → add a Chart → bind two output signals →
   leave Edit → both traces draw within one sample period. Add a Knob → bind an input signal →
   drag it → a readout bound to a dependent output moves within ~200 ms and the knob's pulse
   clears when the echo lands.
10. Reload the page: the grid returns from localStorage and every chart is populated
   immediately from the backend window (not empty, not waiting).
11. Background the tab for a minute, return: charts refill from a fresh snapshot with a visible
    gap, and the backend's memory is unchanged.
12. Model-agnosticism grep (spec §6.9) — must return **zero** matches:
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

---

## 11. Fix round 1 (2026-09-15) — Tester's Round-1 bug log

Source: `dev-planning/dashboard-service/bugs/dashboard-service.md`, Round 1.
Nothing below changes the topology, the thread model or any wire format.

| Bug | Layer | Verdict | What changed |
|---|---|---|---|
| 1.1 — `F841` on `last` in `load_at_boot` | code | **fixed** | `backend/lexicon.py:294-308`. `last` was genuinely dead, not a missing raise: `load_at_boot`'s `while True` has no `break`, so the loop can only leave through `return self.fetch()` or through `raise LexiconError(...) from exc` on the deadline branch, and at that raise `exc` **is** the last exception. There is no fall-through path for `last` to have served. Both the annotation line and the assignment were deleted; the raise and its `from exc` chain are untouched |
| 1.2 — `ruff-format` reformats `lexicon.py` | code | **fixed** | `backend/lexicon.py`, four `_problem(...)` calls in `_validate_range`/`_validate_default` wrapped onto three lines each. Behaviour-identical; it is the formatter's own output, kept as the hook produced it |
| 1.3 / OP-1 — schema 1.0 rejects `bindings` | spec | **fixed** | `layout-schema.json` → `dashboard-layout/1.1.json`; see §5.4. `example-layout.json` now declares `1.1` and its `el-voltage-chart` overlays `ocv_v` and `dc_voltage_v` (both unit `V`), with `stroke: null` so the palette gives each trace its own colour — a non-null `stroke` is applied to every series by `chart-element.tsx:67`, which would draw both traces identically. `el-temp-chart` stays single-binding on purpose, so one document exercises both shapes |
| 1.4 — `POST /api/control` 500s with no lexicon | code | **fixed** | `backend/api.py:110-121`. `writer.submit()` is wrapped in `except LexiconError` returning **503** with a `detail` body, matching `GET /api/lexicon:85` rather than `refresh_lexicon`'s 502 — the failure is "not loaded yet", not "DCM refused" |
| (not filed) — the same hole on the WebSocket `write` frame | code | **fixed** | `backend/hub.py:184-194`. `_on_client_message` called `self._submit_write()` unguarded, so the identical `LexiconError` would have escaped `_on_client_message` → `serve()`, which catches only `WebSocketDisconnect` and `RuntimeError`, and killed a healthy socket with a traceback. Now caught and returned through the existing `write_error` verb (D-4), so both write entry points fail the same way |

Also closed: **OP-3**. `dashboard/frontend/package-lock.json` (lockfileVersion 3) is staged and
`dashboard/dockerfile:10-11` is back on `npm ci`. The lockfile and the dockerfile change must
land in the same commit — `npm ci` without a committed lockfile fails the image build outright.

Unreachability, stated plainly: `main.py` calls `lexicon.load_at_boot()` before it starts the
HTTP thread, and that call either returns a snapshot or kills the process, so neither 1.4 nor
its WebSocket twin is reachable through the normal boot path today. Both are fixed because
M2's config-topic invalidation is the change that makes "lexicon momentarily absent" a real
state, and a guard added then would be a guard added after the incident.

---

## 12. Hotfix round 2 (2026-09-15) — the dashboard could not boot against an empty DCM

Source: the user's M1 hotfix brief, raised on a live deployment where every route answered
503. Not a Tester bug log; nothing here came from a failing test.

**What was actually wrong.** Nothing had ever seeded the DCM — §9 of this document deferred
the `dcm-seed-lexicon` Job to M2 and made seeding a manual `curl` nobody had run. With an
empty store `lexicon.load_at_boot()` could never succeed, `main.py` turned that into
`SystemExit(1)`, the pod crash-looped, and the ingress returned 503 for `/`, `/api/lexicon`
and the health check alike. The failure looked like a routing problem and was a boot problem.

| Change | Where | Why |
|---|---|---|
| `SystemExit(1)` on a lexicon failure removed | `dashboard/main.py:192` | A config service being empty or briefly down must not make the dashboard undeployable. `load_at_boot()` now returns `None` and the process carries on |
| Seed-if-absent | `backend/lexicon.py` — `ensure_loaded`, `_load`, `_seed`, `_read_bundle`, `LexiconMissing` | Only a 404 triggers it; the POST omits `replace`, so it creates or is declined and never versions over an operator's document. The bundle is validated before it is written |
| Recovery cadence | `backend/lexicon.py` — `DEGRADED_RETRY_S` | 30 s while nothing is loaded, `LEXICON_REFRESH_S` once something is. A 15-minute TTL is the wrong interval on which to discover the DCM came back |
| Honest health | `backend/api.py` — `/healthz` + `/api/healthz` | 200 always, `status: ok\|degraded`, plus `lexicon_error`, `lexicon_config_id`, `lexicon_seeded_by_this_pod`, `dcm_token_present`. See deviation D-10 |
| Explaining 503 | `backend/api.py` — `GET /api/lexicon` | Still 503, but with a body the UI can render instead of a bare status |
| Seeding retry from the UI | `backend/api.py` — `POST /api/lexicon/refresh` | Calls `ensure_loaded`, so the empty state's Retry button can seed, not just re-read |
| "No lexicon yet" empty state | `frontend/app/page.tsx`, `lib/store/dashboard-context.tsx`, `lib/api/client.ts` | The page used to render "Dashboard unavailable" and stay that way until a reload. It now names the missing configuration, retries every 10 s, and recovers in place |
| Bundled copy + variables | `dashboard/seed/lexicon.json`, `dashboard/dockerfile`, `dashboard/app.yaml`, `quix.yaml` | `LEXICON_SEED_ENABLED` (default `true`) and `LEXICON_SEED_PATH` |

**What did not change:** the topology, the thread model, every wire format, the write path, the
window, the hub, and the rule that the DCM is the only source the dashboard ever *reads* a
lexicon from.

Two deviations from the spec come out of this — D-9 (a bundled copy exists at all) and D-10
(`/healthz` no longer 503s) — both recorded in §7 and raised as **OP-4** for Buddy to fold into
spec §6.2 and §6.11.

---

## 13. Icon set (Phase 2 polish)

### Files

| File | Location after `next build` | Purpose |
|---|---|---|
| `dashboard/frontend/public/favicon.svg` | `out/favicon.svg` | Primary favicon; SVG with embedded `prefers-color-scheme` media query |
| `dashboard/frontend/public/site.webmanifest` | `out/site.webmanifest` | PWA manifest — name, short_name, theme_color, background_color, icon |
| `dashboard/frontend/public/favicon-32.png` | `out/favicon-32.png` | 32×32 raster fallback — **must be generated from favicon.svg before ship** |
| `dashboard/frontend/public/apple-touch-icon.png` | `out/apple-touch-icon.png` | 180×180 iOS home-screen icon — **must be generated before ship** |

### Serving

All four files live under `public/` and are copied verbatim to `out/` during `next build --export`. FastAPI mounts `out/` via `StaticFiles` at `/` (see `api.py`). The `StaticFiles` handler serves actual files before the SPA catch-all fires, so `GET /favicon.svg` and `GET /site.webmanifest` resolve correctly without any route addition.

### Icon concept

A square-wave pulse: a horizontal line that steps up and back down. Reads as "signal / telemetry" at 16×16 without being a generic chart icon.

### Dark-mode mechanism

The SVG embeds a `<style>` block with `@media (prefers-color-scheme: dark)` that switches the stroke from `#1a1a1a` (near-black, for light browser chrome) to `#eaedf0` (the dark-theme foreground colour from `globals.css`, for dark browser chrome). One file, one `<link rel="icon">` entry in the metadata. The Next.js `Metadata` API has no `media` attribute on icon entries, so the two-`<link>` approach (one per scheme) is not available without hand-writing `<head>` tags in a component; the in-SVG media query avoids that.

### Generating the raster files

```bash
# requires svgexport (npm i -g svgexport) or any rasteriser
npx svgexport dashboard/frontend/public/favicon.svg \
              dashboard/frontend/public/favicon-32.png 32:32
npx svgexport dashboard/frontend/public/favicon.svg \
              dashboard/frontend/public/apple-touch-icon.png 180:180
```

Alternatives: `rsvg-convert -w 32 -h 32`, `inkscape --export-png`, or `sharp` CLI. The apple-touch-icon should have a solid `#0064ff` background added (the Quix primary brand colour) so it reads as a home-screen icon rather than a transparent mark.

### Manifest colours

`theme_color: "#0064ff"` and `background_color: "#1a1a1a"` are hex equivalents of `--primary: 217 100% 50%` and `--background: 0 0% 10.2%` (dark theme) from `globals.css`. If either CSS variable changes, update the manifest to match.
