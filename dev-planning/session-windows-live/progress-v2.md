# Build progress — session-windows-live phase 2 (spec-v2)

- step 0 READ: spec-v2.md (all), spec.md §2, session-probe/main.py (describe() splits
  `r3-k007` on the first hyphen and already decodes a bytes key — no change needed),
  session-generator/main.py, tools/collect_results.py, quix.yaml, architecture.md.
  SDK read at 74275f72: session.py (the three rules), sources/base/source.py
  (run/running/serialize/produce/flush), app.py::run (timeout/count/collect/metadata),
  runtracker.py (flat records, 60 s timeout head start), aggregations.py
  (Earliest keeps incumbent on a tie, Latest replaces), windowed/transaction.py
  (update_window takes max of the latest timestamp; add_to_collection keys on
  (id, counter)), base.py (`.current()` yields updates only, expired are drained).
- step 1 GENERATOR V2: session-generator-v2/{main.py, app.yaml, dockerfile,
  requirements.txt, README.md}. Params dataclass built in main() (module stays
  importable without env); min-heap schedule; hold-back injection released BEFORE the
  first scheduled event reaching event_ms+delay (guarantees admissibility for
  OUT_OF_ORDER_DELAY_MS <= GAP_MS+GRACE_MS); closers for non-idle keys at
  max_event+3G+g; idle loop on self.running. logging.basicConfig in main() because
  configure_logging only touches the quixstreams logger.
- step 2+3 VERDICT + ORACLE: session-verdict/{main.py, app.yaml, dockerfile,
  requirements.txt, README.md}. expected_sessions(events, gap_ms, grace_ms, scope)
  written from spec-v2 section 3; no expiry cursor, no partition checkpoint; watermark
  and R3 sweep scope per partition when scope="partition". `python main.py --selftest`
  replays spec-v2 7.1 and asserts 6 / 7 / 24 - RUN AND GREEN.
- step 4 DEPLOY BLOCKS: dev-planning/session-windows-live/quix-v2-blocks.yaml - 5
  deployment blocks (generator-v2 Service/Stopped, 3 session-probe V2 deployments with
  state 1 GiB/Running, verdict Job) + 5 topic blocks (session-v2-in with 2 partitions,
  four 1-partition topics). quix.yaml NOT touched. Variable-name sets verified
  identical between each app.yaml and its block.
- step 5 ARCHITECTURE: dev-planning/session-windows-live/architecture-v2.md (heap,
  hold-back release rule and its admissibility proof, oracle algorithm, per-partition
  watermark, 12 deviations). BUILD COMPLETE - no commit, no deploy, no lint run.
- step 6 POLISH: session-verdict/main.py reshaped so `ruff format` is a no-op (no line
  over 88, no wrapping ruff would collapse). Oracle selftest re-run GREEN after the
  edits. session-probe/main.py's new `to_topic(key=lambda value: value["key"])` needs
  no oracle change - the oracle reads `record["key"]` from probe output, which is the
  str key describe() already decoded.
