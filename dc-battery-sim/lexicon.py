"""Loads the plant's lexicon from the two documents D9 splits it into.

CLAUDE.md D9 publishes the lexicon to DCM as two independently versioned
configurations — `sil-signals` and `sil-parameters` — and keeps the source of
truth in the repo in the same shape, so no transform sits between the on-disk
form and the DCM form. This module is the on-disk reader for that pair.

It exists as its own file rather than inline in `main.py` for two reasons: the
loading grew a real responsibility (pair coherence) that has nothing to do with
the simulation, and `main.py` is already at the ~500-line ceiling. It holds no
state and reads no environment at import time — every value comes from a call
argument — which is what makes it safe next to the `fresh_main` fixture in
`tests/conftest.py`: that fixture pops only `"main"` from `sys.modules`, so a
module caching env-derived state here would survive the re-import and silently
defeat every `monkeypatch.setenv` in the suite.
"""

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent


def lexicon_path(env_value: str | None, default: str) -> Path:
    """Resolve one document path, relative values landing next to this package.

    A relative path resolves against the source directory rather than the
    working directory, so the container and pytest read the same files.
    """
    path = Path(env_value or default)
    return path if path.is_absolute() else HERE / path


def load_lexicon(signals_path: Path, parameters_path: Path) -> dict[str, Any]:
    """Read both documents and return one merged lexicon.

    Everything downstream asks "what does this plant expose", not "which
    configuration was it published as", so the two documents are merged once
    here and the rest of the service sees the single combined shape it always
    saw.

    Both files ship in the same image, so a `model.name` disagreement between
    them is a build error, not a runtime condition: it means somebody updated
    one document and not the other. Raising at import is the whole point —
    merging a mismatched pair would hand the simulation a parameter set
    belonging to a different plant, and nothing downstream could tell.
    """
    signals_doc = json.loads(signals_path.read_text(encoding="utf-8"))
    parameters_doc = json.loads(parameters_path.read_text(encoding="utf-8"))

    signals_model = signals_doc["model"]["name"]
    parameters_model = parameters_doc["model"]["name"]
    if signals_model != parameters_model:
        raise ValueError(
            f"lexicon pair disagrees on the model: {signals_path} says "
            f"{signals_model!r} but {parameters_path} says {parameters_model!r}. "
            "Both documents ship in this image, so one of them is stale."
        )

    return {
        "lexicon_version": signals_doc["lexicon_version"],
        "model": signals_doc["model"],
        "signals": signals_doc["signals"],
        "parameters": parameters_doc["parameters"],
    }
