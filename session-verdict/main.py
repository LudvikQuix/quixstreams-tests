"""In-cluster verdict for phase 2 of the live session-window rig.

Reads the v2 input topic and the three v2 probe output topics in one
`app.run(timeout=..., metadata=True)`, derives from the input what each probe must
have emitted, and compares.

`expected_sessions` implements the same three rules `session.py` implements - join,
late, close - written from spec-v2 section 3 and not ported from the SDK. A
disagreement is a finding **in one of them**, and a bug in this oracle is exactly as
likely as a bug in the SDK until someone traces the disagreement by hand. In
particular the two expiry cursors (`expire_by_key`'s per-key cursor,
`expire_by_partition`'s partition checkpoint) are deliberately **not** modelled: they
are optimisations that must be invisible from the outside, so this oracle closes every
session whose `end <= close_before`, every time.

    python main.py --selftest

replays the hand-derived run of spec-v2 section 7.3 through the oracle and asserts its
6 / 7 / 24 records. If the oracle disagrees with that table, the oracle is wrong.
"""

import argparse
import json
import logging
import os
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from importlib.metadata import version

from quixstreams import Application

logger = logging.getLogger(__name__)

PROBES = ("key", "partition", "current")


@dataclass(eq=False)
class Session:
    """One stored session. `eq=False` so list.remove() matches by identity."""

    start: int
    end: int
    events: list[tuple[int, int, int]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.events)

    @property
    def seqs(self) -> tuple[int, ...]:
        # Collect stores under (timestamp_ms, monotonic counter), so collected
        # values come back in event-time order, ties broken by arrival order.
        return tuple(seq for _, _, seq in sorted(self.events))


@dataclass
class OracleResult:
    """What the probes must emit for one closing strategy."""

    finals: Counter  # (key, start, end, count, seqs) -> multiplicity
    updates: dict[str, list[tuple[int, int, int, int, int]]]
    late_drops: list[tuple[str, int]]


def _join(
    stored: list[Session], timestamp: int, arrival: int, seq: int, gap_ms: int
) -> Session:
    """R1: extend one neighbouring session, merge two, or open a new one."""
    previous: Session | None = None
    following: Session | None = None
    for session in stored:
        if session.start <= timestamp:
            previous = session
        elif following is None:
            following = session

    matched = [
        s
        for s in (previous, following)
        if s is not None and s.start - gap_ms <= timestamp < s.end + gap_ms
    ]

    event = (timestamp, arrival, seq)
    if len(matched) == 2:
        earlier, later = matched
        stored.remove(earlier)
        stored.remove(later)
        landed = Session(
            start=min(earlier.start, timestamp),
            end=max(later.end, timestamp + 1),
            events=earlier.events + [event] + later.events,
        )
        stored.append(landed)
    elif len(matched) == 1:
        landed = matched[0]
        landed.start = min(landed.start, timestamp)
        landed.end = max(landed.end, timestamp + 1)
        landed.events.append(event)
    else:
        landed = Session(start=timestamp, end=timestamp + 1, events=[event])
        stored.append(landed)

    stored.sort(key=lambda session: session.start)
    return landed


def expected_sessions(
    events: list[dict], gap_ms: int, grace_ms: int, scope: str
) -> OracleResult:
    """Derive the sessions the SDK must emit for one closing strategy.

    :param events: input-topic records carrying `_partition`, `_offset`, `_key`,
        `_timestamp` and `seq`.
    :param gap_ms: the inactivity gap, `G`.
    :param grace_ms: the grace period, `g`.
    :param scope: `"key"` or `"partition"` - the watermark and the R3 sweep scope.
    :return: the final-mode multiset, the current-mode updates and the late drops.
    """
    # In key mode the outcome depends only on the per-key order, in partition mode
    # only on the per-partition order; both are the (partition, offset) order.
    ordered = sorted(events, key=lambda r: (r["_partition"], r["_offset"]))

    sessions: dict[tuple[int, str], list[Session]] = {}
    key_watermark: dict[tuple[int, str], int] = {}
    partition_watermark: dict[int, int] = {}
    finals: Counter = Counter()
    updates: dict[str, list[tuple[int, int, int, int, int]]] = {}
    late_drops: list[tuple[str, int]] = []

    for arrival, record in enumerate(ordered):
        partition = record["_partition"]
        key = record["_key"]
        timestamp = record["_timestamp"]
        scoped = (partition, key)

        if scope == "partition":
            watermark = max(
                partition_watermark.get(partition, 0),
                timestamp,
                key_watermark.get(scoped, 0),
            )
            partition_watermark[partition] = watermark
        else:
            watermark = max(timestamp, key_watermark.get(scoped, 0))

        late_before = watermark - gap_ms - grace_ms
        close_before = late_before - gap_ms

        if timestamp < late_before:
            late_drops.append((key, timestamp))
            continue

        landed = _join(
            sessions.setdefault(scoped, []), timestamp, arrival, record["seq"], gap_ms
        )
        key_watermark[scoped] = max(key_watermark.get(scoped, 0), timestamp)
        updates.setdefault(key, []).append(
            (landed.start, landed.end, landed.count, landed.seqs[0], landed.seqs[-1])
        )

        # R3. The sweep scope is the whole point of the two strategies, and no
        # expiry cursor gates it: every due session closes, every time.
        swept = (
            [scoped_key for scoped_key in sessions if scoped_key[0] == partition]
            if scope == "partition"
            else [scoped]
        )
        for scoped_key in swept:
            open_sessions = sessions[scoped_key]
            name = scoped_key[1]
            for s in open_sessions:
                if s.end <= close_before:
                    finals[(name, s.start, s.end, s.count, s.seqs)] += 1
            sessions[scoped_key] = [s for s in open_sessions if s.end > close_before]

    return OracleResult(finals=finals, updates=updates, late_drops=late_drops)


def group_finals(finals: Counter) -> dict[str, list[tuple]]:
    """Regroup the oracle's multiset into one list of records per key."""
    grouped: dict[str, list[tuple]] = {}
    for record, multiplicity in finals.items():
        key, rest = record[0], tuple(record[1:])
        grouped.setdefault(key, []).extend([rest] * multiplicity)
    return grouped


def observed_finals(records: list[dict]) -> dict[str, list[tuple]]:
    """(start, end, count, seqs) per key, from a `.final()` probe's output topic."""
    grouped: dict[str, list[tuple]] = {}
    for r in records:
        grouped.setdefault(r["key"], []).append(
            (r["start"], r["end"], r["count"], tuple(r["seqs"]))
        )
    return grouped


def observed_updates(records: list[dict]) -> dict[str, list[tuple]]:
    """Ordered (start, end, count, first_seq, last_seq) per key, current probe."""
    grouped: dict[str, list[tuple]] = {}
    for r in sorted(records, key=lambda x: (x["_partition"], x["_offset"])):
        grouped.setdefault(r["key"], []).append(
            (r["start"], r["end"], r["count"], r["first_seq"], r["last_seq"])
        )
    return grouped


def judge(expected: list[tuple], observed: list[tuple], ordered: bool) -> dict:
    """Compare one (probe, key) cell as a multiset, and as a sequence if ordered."""
    expected_counts = Counter(expected)
    observed_counts = Counter(observed)
    surplus = observed_counts - expected_counts
    findings = {
        "missing": sorted((expected_counts - observed_counts).elements()),
        "unexpected": sorted(
            record for record in surplus.elements() if record not in expected_counts
        ),
        "duplicate": sorted(
            record for record in surplus.elements() if record in expected_counts
        ),
        "order": [],
    }
    if ordered and not any(findings.values()) and observed != expected:
        findings["order"] = [tuple(observed)]
    return findings


def print_findings(probe: str, key: str, findings: dict) -> None:
    for label in ("missing", "unexpected", "duplicate", "order"):
        for record in findings[label]:
            print(f"{label.upper():<11}{probe:<11}{key:<10}{record}")


def build_summary(
    run_id: str,
    gap_ms: int,
    grace_ms: int,
    inputs: list[dict],
    key_partitions: dict[str, int],
    sessions_per_probe: dict[str, int],
    verdicts: dict[str, str],
) -> dict:
    return {
        "run_id": run_id,
        "probe": "summary",
        "verdict": "PASS" if set(verdicts.values()) == {"PASS"} else "FAIL",
        "input_events": len(inputs),
        "keys": len(key_partitions),
        "partitions": len(set(key_partitions.values())),
        "key_partition_map": key_partitions,
        "instances": sorted({record["instance_id"] for record in inputs}),
        "base_ms": sorted({record["base_ms"] for record in inputs}),
        "gap_ms": gap_ms,
        "grace_ms": grace_ms,
        "sessions_per_probe": sessions_per_probe,
    }


def report(
    run_id: str,
    gap_ms: int,
    grace_ms: int,
    inputs: list[dict],
    outputs: dict[str, list[dict]],
    read_counts: dict[str, int],
) -> tuple[int, list[dict]]:
    """Print the whole verdict and return (failing checks, records to publish)."""
    by_key = expected_sessions(inputs, gap_ms, grace_ms, "key")
    by_partition = expected_sessions(inputs, gap_ms, grace_ms, "partition")

    key_partitions = {record["_key"]: record["_partition"] for record in inputs}
    closed_keys = {record["_key"] for record in inputs if record["seq"] == -1}
    idle_keys = sorted(set(key_partitions) - closed_keys)

    print(f"run_id={run_id} gap_ms={gap_ms} grace_ms={grace_ms}")
    for topic, count in read_counts.items():
        print(f"  read {count:>7} from {topic}")
    print(f"  instance_id values: {sorted({r['instance_id'] for r in inputs})}")
    print(f"  base_ms values:     {sorted({r['base_ms'] for r in inputs})}")
    print(f"  idle keys (no closer): {idle_keys}")
    print()

    print("key -> partition")
    for key in sorted(key_partitions):
        print(f"  {key} -> {key_partitions[key]}")
    partitions: dict[int, list[str]] = {}
    for key, partition in sorted(key_partitions.items()):
        partitions.setdefault(partition, []).append(key)
    for partition, keys in sorted(partitions.items()):
        if len(keys) < 2:
            has_idle = any(key in idle_keys for key in keys)
            print(
                f"PARTITION SHAPE partition={partition} holds {len(keys)} key(s) "
                f"{keys} idle_key_present={has_idle}"
            )
    if len(partitions) < 2:
        print(f"PARTITION SHAPE only {len(partitions)} partition(s) carry traffic")
    print()

    expected_by_probe = {
        "key": group_finals(by_key.finals),
        "partition": group_finals(by_partition.finals),
        "current": by_key.updates,
    }
    observed_by_probe = {
        "key": observed_finals(outputs["key"]),
        "partition": observed_finals(outputs["partition"]),
        "current": observed_updates(outputs["current"]),
    }

    failures = 0
    rows: list[tuple[str, str, int, int, str]] = []
    details: list[tuple[str, str, dict]] = []
    published: list[dict] = []
    verdicts: dict[str, str] = {}
    for probe in PROBES:
        expected = expected_by_probe[probe]
        observed = observed_by_probe[probe]
        probe_verdict = "PASS"
        for key in sorted(set(expected) | set(observed)):
            expected_records = expected.get(key, [])
            observed_records = observed.get(key, [])
            findings = judge(
                expected_records, observed_records, ordered=probe == "current"
            )
            verdict = "FAIL" if any(findings.values()) else "PASS"
            rows.append(
                (probe, key, len(expected_records), len(observed_records), verdict)
            )
            if verdict == "FAIL":
                failures += 1
                probe_verdict = "FAIL"
                details.append((probe, key, findings))
            published.append(
                {
                    "run_id": run_id,
                    "probe": probe,
                    "key": key,
                    "verdict": verdict,
                    "expected": len(expected_records),
                    "observed": len(observed_records),
                    **{
                        label: [list(record) for record in findings[label]]
                        for label in ("missing", "unexpected", "duplicate", "order")
                    },
                }
            )
        verdicts[probe] = probe_verdict

    header = f"{'probe':<11}{'key':<12}{'expected':>9}{'observed':>9}  verdict"
    print(header)
    print("-" * len(header))
    for probe, key, n_expected, n_observed, verdict in rows:
        print(f"{probe:<11}{key:<12}{n_expected:>9}{n_observed:>9}  {verdict}")
    print()

    if details:
        for probe, key, findings in details:
            print_findings(probe, key, findings)
    else:
        print("no missing, unexpected, duplicate or out-of-order records")
    print()

    sessions_per_probe = {
        probe: sum(len(records) for records in observed_by_probe[probe].values())
        for probe in PROBES
    }
    for probe in PROBES:
        expected_total = sum(
            len(records) for records in expected_by_probe[probe].values()
        )
        print(
            f"{probe:<11} expected={expected_total:<6} "
            f"observed={sessions_per_probe[probe]:<6} verdict={verdicts[probe]}"
        )
    per_key = [len(records) for records in expected_by_probe["key"].values()]
    if per_key:
        print(
            f"sessions per key (key probe, expected): min={min(per_key)} "
            f"median={statistics.median(per_key)} max={max(per_key)}"
        )
        print(
            f"keys with 1 session: {sum(1 for n in per_key if n == 1)}  "
            f"keys with >= 2: {sum(1 for n in per_key if n >= 2)}"
        )
    print(f"late drops predicted by the oracle: {len(by_key.late_drops)}")
    accepted = len(inputs) - len(by_key.late_drops)
    expected_updates = sum(len(v) for v in by_key.updates.values())
    print(
        f"current updates: expected={expected_updates} "
        f"observed={sessions_per_probe['current']} accepted_input={accepted}"
    )
    print()

    summary = build_summary(
        run_id, gap_ms, grace_ms, inputs, key_partitions, sessions_per_probe, verdicts
    )
    published.append(summary)
    print(f"{'PASS' if failures == 0 else 'FAIL'}: {failures} failing check(s)")
    return failures, published


def publish(app: Application, topic, run_id: str, records: list[dict]) -> None:
    """One record per (probe, key) plus the summary, onto the verdict topic."""
    with app.get_producer() as producer:
        for record in records:
            key = f"{run_id}-{record['probe']}-{record.get('key', 'summary')}"
            message = topic.serialize(key=key, value=record)
            producer.produce(topic=topic.name, key=message.key, value=message.value)


def spec_events() -> list[dict]:
    """The 27 records of spec-v2 section 7.1, with the section 7.3 partition map."""
    partitions = {"r3-k000": 0, "r3-k001": 1, "r3-k002": 0, "r3-k003": 1}
    all_keys = ["r3-k000", "r3-k001", "r3-k002", "r3-k003"]
    active = all_keys[:3]
    emitted: list[tuple[str, int, int]] = []
    for timestamp, seq in ((0, 0), (60000, 1), (120000, 2)):
        emitted.extend((key, timestamp, seq) for key in all_keys)
    for timestamp, seq in ((270000, 3), (330000, 4), (390000, 5)):
        emitted.extend((key, timestamp, seq) for key in active)
    emitted.extend((key, 750000, -1) for key in active)

    offsets: dict[int, int] = {}
    records = []
    for key, timestamp, seq in emitted:
        partition = partitions[key]
        offset = offsets.get(partition, 0)
        offsets[partition] = offset + 1
        records.append(
            {
                "_partition": partition,
                "_offset": offset,
                "_key": key,
                "_timestamp": timestamp,
                "seq": seq,
            }
        )
    return records


def selftest() -> int:
    """Replay spec-v2 section 7.3 through the oracle and assert its stated values."""
    events = spec_events()
    by_key = expected_sessions(events, 120000, 0, "key")
    by_partition = expected_sessions(events, 120000, 0, "partition")

    stream = [(0, 120001, 3, (0, 1, 2)), (270000, 390001, 3, (3, 4, 5))]
    expected_key = Counter(
        (key, *record) for key in ("r3-k000", "r3-k001", "r3-k002") for record in stream
    )
    idle_record = ("r3-k003", 0, 120001, 3, (0, 1, 2))
    expected_partition = expected_key + Counter({idle_record: 1})
    active_updates = [
        (0, 1, 1, 0, 0),
        (0, 60001, 2, 0, 1),
        (0, 120001, 3, 0, 2),
        (270000, 270001, 1, 3, 3),
        (270000, 330001, 2, 3, 4),
        (270000, 390001, 3, 3, 5),
        (750000, 750001, 1, -1, -1),
    ]
    expected_updates = {k: active_updates for k in ("r3-k000", "r3-k001", "r3-k002")}
    expected_updates["r3-k003"] = active_updates[:3]

    for label, finals in (("key", by_key.finals), ("partition", by_partition.finals)):
        print(f"{label} probe - {sum(finals.values())} records")
        for record in sorted(finals.elements()):
            print(f"  {record}")
    print(f"current probe - {sum(len(v) for v in by_key.updates.values())} updates")
    for key in sorted(by_key.updates):
        print(f"  {key}")
        for update in by_key.updates[key]:
            print(f"    {update}")
    print(f"late drops: {by_key.late_drops}")

    assert by_key.finals == expected_key, by_key.finals
    assert by_partition.finals == expected_partition, by_partition.finals
    assert by_key.updates == expected_updates, by_key.updates
    assert sum(len(v) for v in by_key.updates.values()) == 24
    assert by_key.late_drops == []
    print("\nSELFTEST PASS: 6 key, 7 partition, 24 current - spec-v2 7.2/7.3/7.4")
    return 0


def main() -> int:
    loglevel = os.environ["LOGLEVEL"]
    logging.basicConfig(
        level=loglevel,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    run_id = os.environ["RUN_ID"]
    gap_ms = int(os.environ["GAP_MS"])
    grace_ms = int(os.environ["GRACE_MS"])

    app = Application(
        consumer_group=os.environ["CONSUMER_GROUP"],
        auto_offset_reset="earliest",
        loglevel=loglevel,
    )
    input_topic = app.topic(
        os.environ["input"], value_deserializer="json", key_deserializer="str"
    )
    probe_topics = {
        probe: app.topic(
            os.environ[f"{probe}_input"],
            value_deserializer="json",
            key_deserializer="str",
        )
        for probe in PROBES
    }
    verdict_topic = app.topic(
        os.environ["output"], value_serializer="json", key_serializer="str"
    )
    for topic in (input_topic, *probe_topics.values()):
        app.dataframe(topic)

    logger.info("Starting Session Verdict V2")
    logger.info("  quixstreams:  %s", version("quixstreams"))
    logger.info("  input topic:  %s", input_topic.name)
    for probe, topic in probe_topics.items():
        logger.info("  %-9s probe topic: %s", probe, topic.name)
    logger.info("  output topic: %s", verdict_topic.name)
    logger.info("  RUN_ID=%s GAP_MS=%d GRACE_MS=%d", run_id, gap_ms, grace_ms)

    # `timeout` is an idle timeout over all four topics at once. The probes emit
    # only in response to input, so "input quiet and all three outputs quiet" means
    # either the probes are caught up or one is stuck - and a stuck probe shows up
    # as MISSING records with its lag visible in the per-topic read counts.
    records = app.run(
        timeout=int(os.environ["IDLE_TIMEOUT_S"]),
        count=int(os.environ["MAX_RECORDS"]),
        metadata=True,
    )

    read_counts = {input_topic.name: 0} | {t.name: 0 for t in probe_topics.values()}
    inputs: list[dict] = []
    outputs: dict[str, list[dict]] = {probe: [] for probe in PROBES}
    for record in records:
        read_counts[record["_topic"]] = read_counts.get(record["_topic"], 0) + 1
        if record["run_id"] != run_id:
            continue
        if record["_topic"] == input_topic.name:
            inputs.append(record)
        else:
            outputs[record["probe"]].append(record)

    failures, published = report(run_id, gap_ms, grace_ms, inputs, outputs, read_counts)
    publish(app, verdict_topic, run_id, published)
    print(json.dumps(published[-1], default=str))
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Session-window v2 verdict")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run the oracle against spec-v2 section 7.3 and exit",
    )
    sys.exit(selftest() if parser.parse_args().selftest else main())
