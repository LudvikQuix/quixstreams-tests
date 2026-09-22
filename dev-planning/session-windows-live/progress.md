# Build progress — session-windows-live rig

- step 1 WIPE: deleted config-seeder/, data-generator/, lake-sink/, lookup-sink/,
  mongodb/, tools/negative-tests/ (and the stale .ruff_cache/). quix.yaml and README.md
  rewritten in later steps. dev-planning/pr1110-grace-period/ untouched.
- step 2 GENERATOR: session-generator/{main.py, app.yaml, dockerfile,
  requirements.txt, README.md}. EVENTS table module-level and importable
  (env reads live in main(), Application built under __main__), rows_for_phase(phase),
  flush per row, pin 74275f72... confirmed public via git ls-remote.
- step 3 PROBE: session-probe/{main.py, app.yaml, dockerfile, requirements.txt,
  README.md}. No state_dir argument; startup block logs version, app.config.state_dir,
  Quix__Deployment__State__{Path,Enabled}, the seven config vars and both resolved topic
  names; aggregations conditional on EMIT_MODE; on_late returns True (spec 8.1).
- step 4 QUIX.YAML: rewritten - 4 deployments (3 probes Service/state 1 GiB/Running,
  generator Job), 4 one-partition topics, metadata.version 2.0. PR1110 blocks removed.
- step 5 COLLECTOR: tools/collect_results.py + tools/requirements.txt. EXPECTED holds
  spec 6.1 verbatim (key 6 / partition 7 / current 42 ordered); JSONL per probe under
  dev-planning/session-windows-live/results/<run-id>/; --run-id defaults to r1.
- step 6 README + step 7 GITIGNORE: README.md rewritten (proof, scenario table, runbook,
  verdict reading, S7 caveat, reset); .gitignore gained the results dir and .ruff_cache/.
- step 8 ARCHITECTURE: dev-planning/session-windows-live/architecture.md (generator
  ordering/timestamps, probe mode split, state-dir resolution with app.py line numbers,
  collector matching rule, 5 deviations). BUILD COMPLETE - no commit, no deploy.
- fix PARTITION-KEY BYTES: session-probe/main.py describe() now decodes a bytes key -
  partition-mode expiry emits the raw store prefix (windows/session.py:293-295, same
  contract as time_based.py:277-291), which crashed the Partition deployment on
  key.partition("-"). Record schema unchanged and now identical across all three probes.
  README "Output records" notes the prefix keying. No commit, no deploy.
- fix PARTITION-KEY BYTES (message key): describe() only fixed the record field - the
  outgoing message key was still the raw store prefix, which the output topic's
  key_serializer="str" hit with .encode() (r3, commit dfc464a0, zero records emitted).
  session-probe/main.py now calls sdf.to_topic(output_topic, key=lambda value:
  value["key"]); partition-mode expiry yields the prefix as key for every window type
  (state/rocksdb/windowed/transaction.py:543,565), so this is a no-op for the key and
  current probes, which already carry a str key. README sentence extended. No commit,
  no deploy.
