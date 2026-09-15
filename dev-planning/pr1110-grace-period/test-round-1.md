# PR1110 grace-period rig — verification gate, Round 1

**Spec:** dev-planning/pr1110-grace-period/spec.md
**Architecture:** dev-planning/pr1110-grace-period/architecture.md
**Scope:** static gate only (Phase 1) — no broker, no Quix environment available.

## Gate table

| check | status | detail |
|---|---|---|
| Lint — `ruff check` | pass | ruff 0.15.12 (local; no `.pre-commit-config.yaml` pins a version). 0 violations across `data-generator config-seeder lookup-sink lake-sink tools` (5 Python files). |
| Lint — `ruff format --check` | pass | Same scope, ruff 0.15.12. "5 files already formatted". |
| Parse — `quix.yaml` + 5 `app.yaml` | pass | `yaml.safe_load` succeeds on all 6 files. |
| Compile — 4 `main.py` + `test_negative.py` | pass | `python -m py_compile` clean on all 5. |
| Compile — `mongodb/init.sh` | pass | `sh -n` clean. |
| Negative-test script — install | pass | `pip install -r tools/negative-tests/requirements.txt` succeeded in an isolated scratch venv (git + network available). Built `quixstreams` wheel from the pinned SHA. |
| Negative-test script — run | **fail** | Completed in ~10s, well under the 120s hard timeout — **did not hang**. Result: `5 / 6 guards fired`, exit code 1, not the expected `6 / 6` / exit 0. See Bug 1.1. |
| Cross-file — env vars (main.py vs app.yaml vs quix.yaml) | pass | Scripted check (direct `os.environ`/`os.getenv` + all `_env*`/`_positive_int` wrapper calls) across all 5 deployments; every var read is declared in both `app.yaml` and its `quix.yaml` deployment block. Platform-injected vars (`Quix__Lakehouse__Catalog__Url`, `Quix__Lakehouse__Catalog__AuthToken`, `Quix__Workspace__Id`, `CATALOG_URL` fallback) correctly undeclared. |
| Cross-file — no hyphenated var names | pass | All declared variable names across `quix.yaml` and all `app.yaml` are `[A-Za-z_][A-Za-z0-9_]*`. Hyphens only appear in topic names (`sensor-data`, `config-updates`, `enriched-sensor-data`), which is expected and not a variable name. |
| Cross-file — `CONFIG_TYPE` / `SEEDED_DEVICE_COUNT` agreement | pass | `CONFIG_TYPE=device` identical in `config-seeder` and both `lookup-sink` arms. `SEEDED_DEVICE_COUNT=50` identical in `data-generator`, `config-seeder`, and both `lookup-sink` arms. |
| Cross-file — arm `CONSUMER_GROUP` difference | pass | `pr1110_buffered_r1` (buffered) vs `pr1110_control_r1` (control) — distinct. |
| Cross-file — buffered arm has `state`, control does not | pass | `Lookup Sink - Buffered` has `state: {enabled: true, size: 1}`; `Lookup Sink - Control` has no `state:` key at all (quix.yaml:179-260). |
| Cross-file — topics referenced exist | pass | `sensor-data`, `config-updates`, `enriched-sensor-data` all declared under `topics:` and match every `input`/`output`/`config_topic` value used. |
| Cross-file — `quixstreams` pinned to the 40-char SHA everywhere | pass | All 5 `requirements.txt` (`data-generator`, `config-seeder`, `lookup-sink`, `lake-sink`, `tools/negative-tests`) pin `git+...@6632b46cdb2d493e4facf6c00b78c608ae70af87` verbatim, not a branch. |
| Cross-file — dockerfiles installing from git ref also install `git` | pass | All 4 app dockerfiles (`data-generator`, `config-seeder`, `lookup-sink`, `lake-sink`) run `apt-get install -y --no-install-recommends git` before `pip install`. `mongodb/dockerfile` doesn't install from a git ref, so it's out of scope for this check. |

**Note on the ruff version:** local `ruff` here is **0.15.12**. There is no `.pre-commit-config.yaml` in this repo to pin against, per the brief, so this is the only lint gate available. quix-streams itself pins ruff `0.6.3` — a 9-minor-version gap. Both lint checks came back fully clean at 0.15.12, so there is nothing to caveat as a version artefact in this round, but a re-run against 0.6.3 (e.g. `pip install ruff==0.6.3`) would be worth doing before treating this as CI-equivalent, since newer ruff versions can both add and drop default lints.

---

## Bug 1.1: Negative-test script's NT1 case cannot pass on a broker-less workstation — contradicts its own stated design and the brief's "6/6" expectation

**Test:** `tools/negative-tests/test_negative.py::nt1_field_without_default` (invoked via `main()`), run as `python tools/negative-tests/test_negative.py`
**Spec/doc reference:**
- `tools/negative-tests/README.md` lines 23-28: *"NT1 passes a stub `BaseLookup` rather than a real `QuixConfigurationService`... `join_lookup` calls `buffer.validate_fields(fields)` before it touches the lookup at all, so the stub exercises the same code path"* — implying no broker touch occurs.
- `architecture.md` §6.1: *"`Application`, `app.topic` and `app.dataframe` are broker-free — the broker availability check lives in `run()`."*
- Task brief §4.4: *"Expect `6 / 6 guards fired` and exit 0."*

**Expected:** All 6 guards fire; script prints `6 / 6 guards fired` and exits 0, with no broker needed for any case (per the README's own claim, and per the brief).

**Actual:** `5 / 6 guards fired`, exit code 1.
```
FAIL  NT1  field with no default= under a buffer
      expected ValueError, got KafkaException: KafkaError{code=_TRANSPORT,val=-195,str="Failed to get metadata: Local: Broker transport failure"}
PASS  NT2  grace_ms = 0
PASS  NT4  is_resolved not callable
PASS  NT5  on_timeout = 'discard'
PASS  NT6  on_overflow = 'drop-oldest'
PASS  NT7  max_buffered_per_key = 0

5 / 6 guards fired as specified
```

**Root cause (traced, reproduced in isolation with a full traceback against the pinned SHA):** `nt1_field_without_default()` (test_negative.py:54-64) calls `app.topic("sensor-data", key_deserializer="str")` *before* constructing the `LookupBuffer` and calling `join_lookup(...)`. At the installed pinned SHA (`6632b46cdb2d493e4facf6c00b78c608ae70af87`, built as `quixstreams==3.25.0`), `Application.topic()` is **not** broker-free:

```
app.topic("sensor-data", key_deserializer="str")
  -> quixstreams/app.py:573 Application.topic()
  -> quixstreams/models/topics/manager.py:179 TopicManager.topic()
  -> quixstreams/models/topics/manager.py:388 _get_or_create_broker_topic()
  -> quixstreams/models/topics/manager.py:371 _fetch_topic()
  -> quixstreams/models/topics/admin.py:98 inspect_topics() -> list_topics()
  -> confluent_kafka.admin.AdminClient.list_topics()
  -> cimpl.KafkaException (Local: Broker transport failure)
```

So `app.topic()` eagerly fetches topic metadata from the broker at `localhost:9092`, which does not exist on a workstation. This exception fires before `sdf.join_lookup(...)` — and therefore before `buffer.validate_fields(fields)` — is ever reached. `validate_fields()` itself is correctly implemented and *is* the first statement inside `join_lookup`'s buffered branch (confirmed by reading `dataframe.py:1920-1997` in the installed package) — the architecture doc's claim about `join_lookup`'s internal ordering is correct, but the claim that `app.topic` is broker-free is not, and NT1 never reaches `join_lookup` at all.

This is distinct from the hang failure mode the brief warned about (`QuixConfigurationService.__init__` blocking forever) — that failure mode was correctly avoided by using `_StubLookup`. This is a different, second broker dependency, earlier in the call chain, that the architecture doc's §6.1 resolution didn't account for.

**Reproduction:**
```bash
python tools/negative-tests/test_negative.py
```
or minimally:
```python
from quixstreams import Application
app = Application(broker_address="localhost:9092", consumer_group="nt1")
app.topic("sensor-data", key_deserializer="str")  # raises cimpl.KafkaException here
```

**Root cause layer:** architecture — §6.1's resolution explicitly asserts "`Application`, `app.topic` and `app.dataframe` are broker-free" as the load-bearing justification for NT1's design; that assertion is factually wrong for this SDK version, so the resolution needs to be revisited, not just the code.

**Suspected root cause:** `TopicManager.topic()` fetches broker metadata eagerly (to validate configured partition count / retention against the broker) rather than deferring to `run()`. This is pinned-SDK behavior at the tested SHA, not something this repo's test script can change.

**Suggested fix (options for ArchDev to weigh, not prescriptive):** Restructure NT1 to reach `buffer.validate_fields(fields)` / `join_lookup(...)` without calling `app.topic()` against a real (even if fake-address) `Application` — e.g. call `LookupBuffer.validate_fields(fields)` directly (it's a plain method taking a `fields` mapping, no `Application`/`Topic`/`join_lookup` machinery required, per `buffer.py:191`), or catch/tolerate `KafkaException` alongside `ValueError` in this one case with a comment explaining why, or use `quixstreams.models.topics.Topic` construction directly instead of `app.topic()`. Update `tools/negative-tests/README.md` lines 23-28 and `architecture.md` §6.1 to match whatever is decided, since both currently assert something false.

---

## Summary

All static structural checks (lint, parse, compile, cross-file consistency — the highest-value group per the brief) are clean: 15/16 gate rows pass. The one failure is the negative-test script's NT1 case, which cannot reach the assertion it's meant to test because of an SDK-level broker dependency in `app.topic()` that neither the script's own README nor architecture.md §6.1 accounted for. The script did **not** hang (exit in ~10s, well inside the 120s budget) — it failed cleanly with a legible, correctly-caught exception, just the wrong one.
