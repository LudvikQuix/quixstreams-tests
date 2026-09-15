"""Load-time validation for the two lexicon documents, plus their error types.

Split out of `lexicon.py` when D9 turned one configuration into two: the rules
are now applied to two documents by two independent caches and to two bundled
seed files, so they are no longer one module's private business. `LexiconError`
and `LexiconMissing` come with them, because a module that raises them from four
call sites should not have to import them back from the module it serves.

The rules themselves are unchanged — they are the six load rules of
parameter-contract spec section 6.1. What D9 changes is where rule 6 runs: a
document can only check itself for duplicates, so the cross-collection half of
it moved to `pair_problems`, which runs at merge time when both documents are
actually in hand.
"""

from __future__ import annotations

from typing import Any

NUMERIC_TYPES = ("uint", "int", "float")
DESCRIPTOR_KEYS = (
    "name",
    "label",
    "description",
    "datatype",
    "unit",
    "default",
    "min",
    "max",
    "enum",
    "direction",
    "tunable",
)


class LexiconError(Exception):
    """The document could not be fetched, parsed, or validated."""


class LexiconMissing(LexiconError):
    """The DCM answered 404: this configuration does not exist yet.

    The one failure the bundled copy is allowed to answer. Every other failure -
    403, 500, a connection error, a malformed document - means the store may
    well hold a document, so writing the bundle over it is never right.
    """


def _problem(problems: list[str], entry: dict[str, Any], message: str) -> None:
    problems.append(f"{entry.get('name', '<unnamed>')}: {message}")


def _validate_range(entry: dict[str, Any], problems: list[str]) -> None:
    """Load rules 1, 2, 3 and 5 - min/max/enum shape follows from the datatype."""
    datatype = entry["datatype"]
    if datatype == "enum":
        if not isinstance(entry["enum"], list) or not entry["enum"]:
            _problem(
                problems, entry, "load rule 1: enum datatype needs a non-empty enum"
            )
        if entry["min"] is not None or entry["max"] is not None:
            _problem(
                problems, entry, "load rule 1: enum datatype must have null min/max"
            )
        return
    if datatype in NUMERIC_TYPES:
        low, high = entry["min"], entry["max"]
        numeric = isinstance(low, (int, float)) and isinstance(high, (int, float))
        if not numeric:
            _problem(
                problems, entry, "load rule 2: numeric datatype needs numeric min/max"
            )
        elif low > high:
            _problem(problems, entry, "load rule 2: min > max")
        elif datatype == "uint" and low < 0:
            _problem(problems, entry, "load rule 5: uint min must be >= 0")
        if entry["enum"] is not None:
            _problem(
                problems, entry, "load rule 2: numeric datatype must have null enum"
            )
        return
    if any(entry[key] is not None for key in ("min", "max", "enum")):
        _problem(problems, entry, "load rule 3: bool must have null min/max/enum")


def _validate_default(entry: dict[str, Any], problems: list[str]) -> None:
    """Load rule 4 - the startup default must be a value the plant would accept."""
    datatype = entry["datatype"]
    default = entry["default"]
    if datatype == "enum" and isinstance(entry["enum"], list):
        members = [m.get("value") for m in entry["enum"] if isinstance(m, dict)]
        if default not in members:
            _problem(
                problems, entry, "load rule 4: default is not an enum member value"
            )
        return
    if datatype in NUMERIC_TYPES and isinstance(entry["min"], (int, float)):
        if isinstance(default, bool) or not isinstance(default, (int, float)):
            _problem(problems, entry, "load rule 4: default is not numeric")
        elif not entry["min"] <= default <= entry["max"]:
            _problem(problems, entry, "load rule 4: default outside min/max")


def _validate_descriptor(entry: Any, collection: str, problems: list[str]) -> None:
    if not isinstance(entry, dict):
        problems.append(f"{collection}: entry is not an object")
        return
    missing = [key for key in DESCRIPTOR_KEYS if key not in entry]
    if missing:
        _problem(problems, entry, f"missing keys {missing}")
        return
    if entry["datatype"] not in ("bool", "uint", "int", "float", "enum"):
        _problem(problems, entry, f"unknown datatype {entry['datatype']!r}")
        return

    _validate_range(entry, problems)
    _validate_default(entry, problems)

    if collection == "signals":
        if entry["direction"] not in ("input", "output"):
            _problem(problems, entry, "signal needs direction input|output")
        if entry["tunable"] is not None:
            _problem(problems, entry, "signal tunable must be null")
        return
    if entry["direction"] is not None:
        _problem(problems, entry, "parameter direction must be null")
    if not isinstance(entry["tunable"], bool):
        _problem(problems, entry, "parameter tunable must be a boolean")


def _header(document: Any, collection: str) -> list[Any]:
    """The part both documents share: version 1, a model block, one collection.

    `model` is mandatory in both, and that is load-bearing rather than tidy:
    D9 makes it the only way to tell that the two configurations describe the
    same plant, and a document without one cannot be paired with anything.
    """
    if not isinstance(document, dict):
        raise LexiconError(f"{collection} content is not a JSON object")

    version = str(document.get("lexicon_version", ""))
    if version.split(".")[0] != "1":
        raise LexiconError(f"unsupported lexicon_version {version!r}; major must be 1")

    model = document.get("model")
    if not isinstance(model, dict) or not model.get("name"):
        raise LexiconError(f"{collection} document has no model.name")

    entries = document.get(collection)
    if not isinstance(entries, list):
        raise LexiconError(f"{collection} document needs a `{collection}` array")
    return entries


def _validate(document: Any, collection: str) -> None:
    entries = _header(document, collection)
    problems: list[str] = []
    for entry in entries:
        _validate_descriptor(entry, collection, problems)

    names: list[Any]
    if collection == "signals":
        # Load rule 6, the half a signal document can check alone: signals are
        # unique on (name, direction), so an input and an output may share a
        # name (the sim echoes two of its setpoints on the output topic).
        names = [
            (s.get("name"), s.get("direction")) for s in entries if isinstance(s, dict)
        ]
    else:
        names = [p.get("name") for p in entries if isinstance(p, dict)]
    if len(set(names)) != len(names):
        problems.append(f"load rule 6: duplicate entry key in {collection}")

    if problems:
        raise LexiconError("; ".join(problems))


def validate_signals(document: Any) -> None:
    """Validate a `sil-signals` document. Raises `LexiconError` on any problem."""
    _validate(document, "signals")


def validate_parameters(document: Any) -> None:
    """Validate a `sil-parameters` document. Raises `LexiconError` on any problem."""
    _validate(document, "parameters")


def pair_problems(
    signals_doc: dict[str, Any], parameters_doc: dict[str, Any]
) -> list[str]:
    """What is wrong with this *pair*, given that each half is valid on its own.

    Only reachable when both documents loaded. Two configurations that version
    independently can drift into describing different plants, and D9 puts
    `model` in both precisely so that is detectable instead of silently merged:
    a dashboard built from one plant's signals and another's parameters would
    render controls that write fields the running plant has never heard of.

    The second check is the other half of load rule 6. A name in both
    collections is ambiguous to anything that resolves by name alone, and
    neither document can see it alone.
    """
    problems: list[str] = []

    signals_model = str(signals_doc["model"]["name"])
    parameters_model = str(parameters_doc["model"]["name"])
    if signals_model != parameters_model:
        problems.append(
            f"model mismatch: signals describe {signals_model!r} but parameters "
            f"describe {parameters_model!r}"
        )

    signal_names = {
        s.get("name") for s in signals_doc["signals"] if isinstance(s, dict)
    }
    parameter_names = {
        p.get("name") for p in parameters_doc["parameters"] if isinstance(p, dict)
    }
    overlap = signal_names & parameter_names
    if overlap:
        problems.append(f"load rule 6: name in both collections: {sorted(overlap)}")

    return problems
