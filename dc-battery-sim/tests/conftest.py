"""Shared fixtures for the dc-battery-sim red-first test suite.

Phase 1 / red-first (dev-planning/parameter-contract/spec.md, dev-planning/
parameter-contract/red-test-report.md). These fixtures exist to make main.py's
mostly-non-importable surface reachable from pytest WITHOUT reimplementing any
of its logic:

- `fresh_main` reimports main.py as a plain module so each test starts from a
  clean set of module-level globals (main.py computes most of its "constants"
  once at import time from env vars).
- `main_dunder_globals` executes main.py's literal source as `__main__` (via
  `runpy.run_path`) with QuixStreams and the background thread stubbed out, so
  that `handle_command` — defined only inside `if __name__ == "__main__":`
  (main.py:221) and therefore not on the importable module surface — can be
  reached and called directly. This drives the real source; it does not
  reimplement handle_command's body.
- `run_ticks` / `SimulationHarness` drive the real `run_simulation` against a
  fake QuixStreams producer/topic so RC-circuit and thermal behaviour can be
  observed from the actual published payloads.
"""

import importlib
import runpy
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import quixstreams

SIM_DIR = Path(__file__).resolve().parent.parent
MAIN_PY = SIM_DIR / "main.py"

if str(SIM_DIR) not in sys.path:
    sys.path.insert(0, str(SIM_DIR))


@pytest.fixture
def fresh_main():
    """Import (or re-import) dc-battery-sim/main.py with a clean module cache.

    main.py computes every tunable "constant" (R1, R2, ALPHA1, ...) once at
    import time from os.getenv(...). Any test that needs a specific
    configuration must set env vars (monkeypatch.setenv) *before* requesting
    this fixture, otherwise it sees whatever a previous import left behind.
    Only the module-level code runs — the `if __name__ == "__main__":` block
    is skipped, so no Application/broker/thread is touched.
    """
    sys.modules.pop("main", None)
    module = importlib.import_module("main")
    yield module
    sys.modules.pop("main", None)


class _FakeTopic:
    name = "fake-topic"

    def serialize(self, key, value):
        return SimpleNamespace(key=key, value=value)


class _CapturingProducer:
    def __init__(self):
        self.produced = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def produce(self, topic, value, key):
        self.produced.append(value)


class _CapturingProducerApp:
    def __init__(self):
        self.producer = _CapturingProducer()

    def get_producer(self):
        return self.producer


@pytest.fixture
def fake_producer_app():
    """A get_producer()-compatible fake with no real broker — for tests that
    only need run_simulation to reach its first loop iteration (e.g. the C1
    KeyError, which fires before any payload is produced)."""
    return _CapturingProducerApp()


@pytest.fixture
def fake_out_topic():
    return _FakeTopic()


class SimulationHarness:
    """Runs the real `run_simulation` in a background daemon thread against a
    fake producer/topic and lets a test collect published payloads on demand,
    mutating the module's live state (cmd / params) in between calls.

    One harness = one continuous run of run_simulation, so state (q_act,
    v_rc1, v_rc2, heat, temperature) persists across `wait_for_ticks` calls —
    required to test behaviour across a live mid-run change (e.g. OQ-3's
    A_THERMAL rebase).
    """

    def __init__(self, module, timeout=5.0):
        self.module = module
        self.timeout = timeout
        self.producer_app = _CapturingProducerApp()
        self.out_topic = _FakeTopic()
        self.exception = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        # run_simulation has no try/except of its own (that is exactly the C1 bug:
        # an uncaught KeyError silently kills this daemon thread). We catch it here
        # only to relay it back to the test's thread — the exception is re-raised
        # unmodified by wait_for_ticks below, it is never swallowed.
        try:
            self.module.run_simulation(self.producer_app, self.out_topic)
        except BaseException as exc:
            self.exception = exc

    def wait_for_ticks(self, count):
        deadline = time.monotonic() + self.timeout
        produced = self.producer_app.producer.produced
        while len(produced) < count and time.monotonic() < deadline:
            if self.exception is not None:
                raise self.exception
            time.sleep(0.01)
        if self.exception is not None:
            raise self.exception
        if len(produced) < count:
            raise AssertionError(
                f"only {len(produced)} of {count} expected ticks were "
                f"produced within {self.timeout}s — run_simulation thread "
                f"may have died (check for an uncaught exception)"
            )
        return list(produced[:count])


@pytest.fixture
def run_ticks():
    """One-shot helper: start a fresh SimulationHarness for `module` and
    return the first `count` published payloads."""

    def _run(module, count, timeout=5.0):
        harness = SimulationHarness(module, timeout=timeout)
        return harness.wait_for_ticks(count)

    return _run


@pytest.fixture
def sim_harness():
    """Factory for a SimulationHarness, for tests that need to mutate state
    mid-run between two `wait_for_ticks` calls."""

    def _make(module, timeout=5.0):
        return SimulationHarness(module, timeout=timeout)

    return _make


class _FakeSDF:
    def update(self, fn):
        self.update_fn = fn
        return self

    def filter(self, fn):
        self.filter_fn = fn
        return self


class _FakeApplication:
    """Stand-in for quixstreams.Application: no broker, no network, just
    enough surface for main.py's `__main__` block to run to completion."""

    def __init__(self, *args, **kwargs):
        pass

    def topic(self, name):
        return _FakeTopic()

    def get_producer(self):
        return _CapturingProducer()

    def dataframe(self, topic):
        return _FakeSDF()

    def run(self, sdf):
        return None


@pytest.fixture
def main_dunder_globals(monkeypatch):
    """Execute main.py's literal source as `__main__` and return its globals.

    `handle_command` (main.py:221-231) is defined inside the
    `if __name__ == "__main__":` block, so it is not reachable via a normal
    import. Reimplementing it in the test would prove nothing about the real
    bug, so instead we run the actual file with its two external dependencies
    stubbed:
      - quixstreams.Application -> _FakeApplication (no broker)
      - threading.Thread.start -> no-op (the daemon simulation thread never
        actually runs; we don't need it for handle_command tests, and this
        avoids leaking a live background loop into the test session)
    `runpy.run_path(..., run_name="__main__")` then returns the executed
    module's global namespace, which includes the real `handle_command`,
    `cmd`, and `cmd_lock` objects bound by the real source.
    """
    monkeypatch.setattr(quixstreams, "Application", _FakeApplication)
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    return runpy.run_path(str(MAIN_PY), run_name="__main__")
