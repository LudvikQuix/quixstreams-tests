"""Read the three probe output topics and print the verdict for one run.

Run locally by the operator once all three generator phases have completed. This is the
only place an assertion lives: EXPECTED holds section 6.1 of
dev-planning/session-windows-live/spec.md verbatim, and the exit code is the
machine-readable answer.

    python tools/collect_results.py --run-id r1
"""

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

from quixstreams import Application

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = REPO_ROOT / "dev-planning" / "session-windows-live" / "results"

# Phase `main`'s event-time band, used by the base_ms fallback.
MAIN_BAND_MS = 1200000

TOPICS = {
    "key": "session-out-key",
    "partition": "session-out-partition",
    "current": "session-out-current",
}

SEQS_TEN = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9)

# `final` probes: scenario -> list of (start, end, count, seqs), compared as a multiset.
# Every offset is relative to base_ms. `seqs` is in event-time order, not arrival order,
# because Collect stores values under id=timestamp and reads them back in id order.
EXPECTED_KEY: dict[str, list[tuple[int, int, int, tuple[int, ...]]]] = {
    "s1": [(1200000, 1740001, 10, SEQS_TEN)],
    "s2": [(1200000, 1320001, 4, (0, 1, 3, 2))],
    "s3": [(1200000, 1400001, 4, (0, 1, 3, 2))],
    "s4": [(1200000, 1260001, 2, (0, 1))],
    # The key probe's watermark is per key, so an idle key never closes.
    "s5a": [],
    "s5b": [(1200000, 1740001, 10, SEQS_TEN)],
    "s7": [(0, 540001, 10, SEQS_TEN)],
}

EXPECTED_PARTITION = {
    **EXPECTED_KEY,
    # The one record that separates the two closing strategies.
    "s5a": [(1200000, 1260001, 2, (0, 1))],
}

# `current` probe: scenario -> ordered list of (start, end, count).
EXPECTED_CURRENT: dict[str, list[tuple[int, int, int]]] = {
    "s1": [
        (1200000, 1200001, 1),
        (1200000, 1260001, 2),
        (1200000, 1320001, 3),
        (1200000, 1380001, 4),
        (1200000, 1440001, 5),
        (1200000, 1500001, 6),
        (1200000, 1560001, 7),
        (1200000, 1620001, 8),
        (1200000, 1680001, 9),
        (1200000, 1740001, 10),
    ],
    "s2": [
        (1200000, 1200001, 1),
        (1200000, 1260001, 2),
        (1200000, 1320001, 3),
        (1200000, 1320001, 4),
    ],
    # The supersession: update 3 announces a session starting at 1400000 and update 4
    # replaces it with one starting at 1200000.
    "s3": [
        (1200000, 1200001, 1),
        (1200000, 1260001, 2),
        (1400000, 1400001, 1),
        (1200000, 1400001, 4),
    ],
    "s4": [
        (1200000, 1200001, 1),
        (1200000, 1260001, 2),
    ],
    "s5a": [
        (1200000, 1200001, 1),
        (1200000, 1260001, 2),
    ],
    "s5b": [
        (1200000, 1200001, 1),
        (1200000, 1260001, 2),
        (1200000, 1320001, 3),
        (1200000, 1380001, 4),
        (1200000, 1440001, 5),
        (1200000, 1500001, 6),
        (1200000, 1560001, 7),
        (1200000, 1620001, 8),
        (1200000, 1680001, 9),
        (1200000, 1740001, 10),
    ],
    # Five updates before the restart and five after, all starting at 0.
    "s7": [
        (0, 1, 1),
        (0, 60001, 2),
        (0, 120001, 3),
        (0, 180001, 4),
        (0, 240001, 5),
        (0, 300001, 6),
        (0, 360001, 7),
        (0, 420001, 8),
        (0, 480001, 9),
        (0, 540001, 10),
    ],
}

EXPECTED: dict[str, dict[str, list[tuple]]] = {
    "key": EXPECTED_KEY,
    "partition": EXPECTED_PARTITION,
    "current": EXPECTED_CURRENT,
}


def load_env(path: Path) -> None:
    """Fill os.environ from a KEY=VALUE file. An already exported value wins."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


def is_closer(record: dict, probe: str) -> bool:
    """True for the one-event session of an artificial closer event."""
    if probe == "current":
        return record["count"] == 1 and record["last_seq"] == -1
    return record["seqs"] == [-1]


def normalise(record: dict, base_ms: int, probe: str) -> tuple:
    """Record as (start, end, count[, seqs]), offsets relative to base_ms."""
    start = record["start"] - base_ms
    end = record["end"] - base_ms
    if probe == "current":
        return (start, end, record["count"])
    return (start, end, record["count"], tuple(record["seqs"]))


def write_jsonl(by_probe: dict[str, list[dict]], run_id: str) -> Path:
    """Write every received record, raw, one file per probe."""
    out_dir = RESULTS_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for probe, records in by_probe.items():
        path = out_dir / f"{probe}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, default=str) + "\n")
    return out_dir


def derive_base_ms(kept: dict[str, list[dict]]) -> tuple[int | None, str]:
    """base_ms from the key probe's s7 record, else from its s1 record."""
    s7 = [r["start"] for r in kept["key"] if r["scenario"] == "s7"]
    if s7:
        return min(s7), "key probe s7 start"
    s1 = [r["start"] for r in kept["key"] if r["scenario"] == "s1"]
    if s1:
        return min(s1) - MAIN_BAND_MS, "key probe s1 start - 1200000"
    return None, "no s7 and no s1 record on the key probe"


def judge(
    probe: str, scenario: str, expected: list[tuple], observed: list[tuple]
) -> tuple[str, list[str]]:
    """Compare one cell of the verdict table; return its verdict and its findings."""
    exp_counts = Counter(expected)
    obs_counts = Counter(observed)
    notes: list[str] = []
    for record, count in (exp_counts - obs_counts).items():
        notes.append(f"MISSING    {probe:<10} {scenario:<4} x{count} {record}")
    for record, count in (obs_counts - exp_counts).items():
        label = "DUPLICATE" if record in exp_counts else "UNEXPECTED"
        notes.append(f"{label:<10} {probe:<10} {scenario:<4} x{count} {record}")

    # The current probe asserts order too: s3's supersession is only evidence if the
    # 1400000 update arrives before the 1200000 one that replaces it.
    if probe == "current" and not notes and observed != expected:
        notes.append(f"ORDER      {probe:<10} {scenario:<4} {observed}")

    return ("FAIL" if notes else "PASS"), notes


def report(records: list[dict], run_id: str) -> int:
    by_probe: dict[str, list[dict]] = {probe: [] for probe in TOPICS}
    unknown_probe: list[dict] = []
    for record in records:
        by_probe.get(record.get("probe"), unknown_probe).append(record)

    out_dir = write_jsonl(by_probe, run_id)
    print(f"raw records written to {out_dir}\n")

    other_run = {probe: 0 for probe in TOPICS}
    closers = {probe: 0 for probe in TOPICS}
    kept: dict[str, list[dict]] = {probe: [] for probe in TOPICS}
    for probe, probe_records in by_probe.items():
        for record in probe_records:
            if record["run_id"] != run_id:
                other_run[probe] += 1
            elif is_closer(record, probe):
                closers[probe] += 1
            else:
                kept[probe].append(record)

    base_ms, how = derive_base_ms(kept)
    if base_ms is None:
        print(f"FAIL: cannot derive base_ms - {how}")
        return 1
    print(f"base_ms={base_ms} ({how})\n")

    failures = 0
    rows: list[tuple[str, str, int, int, str]] = []
    details: list[str] = []
    for probe in TOPICS:
        expected = EXPECTED[probe]
        observed: dict[str, list[tuple]] = {}
        for record in kept[probe]:
            observed.setdefault(record["scenario"], []).append(
                normalise(record, base_ms, probe)
            )
        for scenario in sorted(set(expected) | set(observed)):
            exp = expected.get(scenario, [])
            obs = observed.get(scenario, [])
            verdict, notes = judge(probe, scenario, exp, obs)
            rows.append((scenario, probe, len(exp), len(obs), verdict))
            details.extend(notes)
            if verdict == "FAIL":
                failures += 1

    header = f"{'scenario':<9}{'probe':<11}{'expected':>9}{'observed':>9}  verdict"
    print(header)
    print("-" * len(header))
    for scenario, probe, n_exp, n_obs, verdict in rows:
        print(f"{scenario:<9}{probe:<11}{n_exp:>9}{n_obs:>9}  {verdict}")

    print()
    if details:
        for line in details:
            print(line)
    else:
        print("no missing, unexpected or duplicate records")

    print()
    for probe in TOPICS:
        print(
            f"{probe:<10} kept={len(kept[probe]):<4} "
            f"closer-only dropped={closers[probe]:<3} "
            f"other-run dropped={other_run[probe]}"
        )
    if unknown_probe:
        print(f"records with an unknown probe field: {len(unknown_probe)}")
        failures += 1

    print(f"\n{'PASS' if failures == 0 else 'FAIL'}: {failures} failing check(s)")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Session-window rig verdict")
    parser.add_argument("--run-id", default="r1", help="RUN_ID of the run to judge")
    args = parser.parse_args()
    run_id = args.run_id

    load_env(REPO_ROOT / ".env")

    app = Application(
        quix_sdk_token=os.environ["Quix__Pat__Token"],
        quix_portal_api=os.environ["Quix__Portal__Api"],
        consumer_group=f"session-collect-{run_id}-{int(time.time())}",
        auto_offset_reset="earliest",
    )
    for name in TOPICS.values():
        topic = app.topic(name, value_deserializer="json", key_deserializer="str")
        app.dataframe(topic)

    print(f"run_id={run_id} workspace={os.environ['Quix__Workspace__Id']}")
    print("reading " + ", ".join(TOPICS.values()))
    # `timeout` is an idle timeout and RunTracker gives it a 60 s head start, so
    # nothing is printed for ~90 s.
    records = app.run(timeout=30, metadata=True)
    print(f"{len(records)} records read\n")

    return report(records, run_id)


if __name__ == "__main__":
    sys.exit(main())
