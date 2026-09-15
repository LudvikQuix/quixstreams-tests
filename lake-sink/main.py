"""Sink the enriched output of both lookup-sink arms into the lakehouse.

The rig's claim is a row-level paired join across two arms over thousands of rows -
"for every (device_id, seq) in the pre-seed window, did the buffered arm resolve where
the control arm did not". That is a SQL question, and the Portal's message viewer cannot
answer it. This deployment exists so the verification queries can.

It is a SEPARATE deployment from lookup-sink on purpose. The service under test must
not carry a blobStorage bind: if the bind fails, a broken lake arm would block the
experiment itself instead of just its reporting surface. The output topic stays as the
ten-second liveness check, long before the first parquet flush.

The sink measures nothing: dwell_ms is computed in-process inside lookup-sink from
ingest_ms and emit_ms, so no sink latency contaminates the result.

HIVE_COLUMNS, TIMESTAMP_COLUMN and SORT_COLUMN are not runtime tweaks. The sink
validates the partition set against the catalog spec AND the on-disk Hive paths at
setup() and raises on a mismatch, and table properties are written only at CREATE.
Changing any of them means a new table - bump TABLE_VERSION.
"""

import logging
import os
import re

from quixstreams import Application
from quixstreams.sinks.core.quix_ts_datalake_sink import QuixTSDataLakeSink

TIMESERIES_PREFIX = "data-lake/time-series"

_TABLE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _envstr(env_var: str, default: str = "") -> str:
    """Read an env var, treating a blank Portal value as a request for the default.

    The Quix Portal stores a variable with an empty value happily, and
    os.getenv(name, "10") then returns "" rather than the default, so a bare int() or a
    bare .lower() == "true" misreads Portal config at import time.
    """
    raw = os.getenv(env_var, "").strip()
    return raw if raw else default


def _envstr_unsettable(env_var: str, default: str) -> str:
    """Resolve a var whose EMPTY value is meaningful, not a request for the default.

    Absent -> `default`. Present but blank -> "" (explicitly unset). Present with a
    value -> that value, stripped. Two variables need this reading: SORT_COLUMN (blank
    = no sort column, fall back to the timestamp column) and TABLE_VERSION (blank = no
    table-name suffix).
    """
    raw = os.getenv(env_var)
    if raw is None:
        return default
    return raw.strip()


def _envflag(env_var: str, default: str = "0") -> bool:
    return _envstr(env_var, default).lower() in ("1", "true", "yes", "on")


def _positive_int(env_var: str, default: str) -> int:
    raw = _envstr(env_var, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{env_var} must be a positive integer, got '{raw}'")
    if value <= 0:
        raise ValueError(f"{env_var} must be a positive integer, got {value}")
    return value


def parse_columns(columns_str: str) -> list:
    """Split a comma-separated column list, stripping whitespace only.

    A leading `~` (the virtual-partition marker) survives intact - splitting physical
    from virtual entries is the sink's own job. Also used for STATS_COLUMNS, which has
    the same shape.
    """
    if not columns_str or columns_str.strip() == "":
        return []
    return [col.strip() for col in columns_str.split(",") if col.strip()]


logging.basicConfig(
    level=_envstr("LOGLEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Application(
    consumer_group=_envstr("CONSUMER_GROUP", "pr1110_lake_v1"),
    auto_offset_reset=_envstr("AUTO_OFFSET_RESET", "earliest"),
    commit_interval=_positive_int("COMMIT_INTERVAL", "30"),
    commit_every=_positive_int("BATCH_SIZE", "1000"),
)

# arm is the primary analysis slice and has cardinality 2. run_id is deliberately NOT
# partitioned: its cardinality grows with every run, which would shatter the table into
# tiny parquet files, and it is cheap to filter as a plain column.
hive_columns = parse_columns(_envstr("HIVE_COLUMNS", "year,month,day,hour,arm"))
auto_discover = _envflag("AUTO_DISCOVER", "true")
# Absolute epoch-ms event time from the generator. If the escape hatch is ever used
# (TIMESTAMP_SKEW_MS > 0 on data-generator), switch this to produced_ms or every row
# lands in a future hour partition.
timestamp_column = _envstr("TIMESTAMP_COLUMN", "timestamp")
# Emission order is the natural read order for this table. Deliberately not equal to
# timestamp_column, because an unset sort_column falls back to the timestamp column and
# setting the two equal would be indistinguishable from unset.
sort_column = _envstr_unsettable("SORT_COLUMN", "emit_ms") or None
# Blank means "unset" -> None -> per-file min/max for every numeric and timestamp
# column, which here includes dwell_ms, the column the verification queries prune on.
stats_columns = parse_columns(_envstr("STATS_COLUMNS")) or None
catalog_url = os.getenv("Quix__Lakehouse__Catalog__Url") or os.getenv("CATALOG_URL")

table_name = _envstr("TABLE_NAME", "pr1110_grace")
table_version = _envstr_unsettable("TABLE_VERSION", "v1")
if table_version:
    table_name = f"{table_name}_{table_version}"
if not _TABLE_NAME_PATTERN.match(table_name):
    raise ValueError(
        f"Invalid table name '{table_name}'. Table names must start with a letter or "
        f"digit and may only contain letters, digits, dots (.), hyphens (-), and "
        f"underscores (_)."
    )

workspace_id = os.getenv("Quix__Workspace__Id", "")

# Blob credentials are never passed explicitly: quixportal reads
# Quix__BlobStorage__Connection__Json and extracts the bucket itself, which is what
# blobStorage: {bind: true} injects. The Portal injects the catalog URL under both the
# Quix name and the PyIceberg one; the auth token only under the Quix name.
blob_sink = QuixTSDataLakeSink(
    s3_prefix=TIMESERIES_PREFIX,
    table_name=table_name,
    workspace_id=workspace_id,
    hive_columns=hive_columns,
    timestamp_column=timestamp_column,
    sort_column=sort_column,
    catalog_url=catalog_url,
    catalog_auth_token=os.getenv("Quix__Lakehouse__Catalog__AuthToken"),
    auto_discover=auto_discover,
    namespace=_envstr("CATALOG_NAMESPACE", "default"),
    auto_create_bucket=True,
    max_workers=_positive_int("MAX_WRITE_WORKERS", "10"),
    stats_columns=stats_columns,
    on_client_connect_success=lambda: print("CONNECTED!"),
    on_client_connect_failure=lambda e: print(f"ERROR! {e}"),
)

sdf = app.dataframe(topic=app.topic(os.environ["input"]))
sdf.sink(blob_sink)

storage_path = (
    f"{workspace_id}/{TIMESERIES_PREFIX}" if workspace_id else TIMESERIES_PREFIX
)
logger.info("Starting Lake Sink")
logger.info("  Input topic:      %s", os.environ["input"])
logger.info("  Storage path:     %s/%s", storage_path, table_name)
logger.info("  Partitioning:     %s", hive_columns or "none")
logger.info("  Timestamp column: %s", timestamp_column)
logger.info("  Sort column:      %s", sort_column or f"unset -> {timestamp_column}")
logger.info(
    "  Stats columns:    %s",
    stats_columns or "unset -> every numeric/timestamp column",
)
logger.info(
    "  Catalog URL:      %s",
    "resolved" if catalog_url else "NOT RESOLVED (check blobStorage.bind)",
)

if __name__ == "__main__":
    app.run()
