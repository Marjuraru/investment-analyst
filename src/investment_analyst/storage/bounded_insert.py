"""Bounded JSON transport for rows already serialized by storage contracts.

The JSON document exists only as one execution parameter. Table and column
names and casts come from this module's fixed registry; callers can supply
values, but cannot select SQL structure.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from duckdb import DuckDBPyConnection

MAX_BOUNDED_INSERT_ROWS = 256


class BoundedInsertTable(StrEnum):
    """Approved table targets for the current v1/v2 persistence contract."""

    RAW_RECORD_INDEX = "raw_record_index"
    NORMALIZED_OBSERVATIONS = "normalized_observations"
    METRIC_RESULTS = "metric_results"
    DIAGNOSTIC_RESULTS = "diagnostic_results"
    OBSERVATIONS_V2 = "normalized_observations_v2"
    METRICS_V2 = "metric_results_v2"
    METRIC_OBSERVATION_LINKS_V2 = "metric_v2_observation_links"
    METRIC_METRIC_LINKS_V2 = "metric_v2_metric_links"
    DIAGNOSTICS_V2 = "diagnostic_results_v2"
    DIAGNOSTIC_COMPONENTS_V2 = "diagnostic_v2_components"
    DIAGNOSTIC_COMPONENT_METRIC_LINKS_V2 = "diagnostic_v2_component_metric_links"
    DIAGNOSTIC_EVIDENCE_V2 = "diagnostic_v2_evidence"
    SNAPSHOTS_V2 = "analysis_snapshots_v2"
    SNAPSHOT_METRIC_LINKS_V2 = "analysis_snapshot_v2_metric_links"
    SNAPSHOT_DIAGNOSTIC_LINKS_V2 = "analysis_snapshot_v2_diagnostic_links"
    EVIDENCE_SEGMENTS_V2 = "evidence_segments_v2"
    EVIDENCE_SETS_V2 = "evidence_sets_v2"
    EVIDENCE_SET_MEMBERS_V2 = "evidence_set_v2_members"
    RAW_V2_INDEX = "raw_v2_index"
    DAILY_PREFIXES_V2 = "market_daily_prefixes_v2"
    DAILY_PREFIX_OBSERVATION_LINKS_V2 = "market_daily_prefix_observation_links_v2"
    MARKET_CHECKPOINTS_V2 = "market_recursive_checkpoints_v2"
    CHECKPOINT_METRIC_LINKS_V2 = "market_recursive_checkpoint_metric_links_v2"


_COLUMN_TYPES: dict[BoundedInsertTable, tuple[tuple[str, str], ...]] = {
    BoundedInsertTable.RAW_RECORD_INDEX: (
        ("record_id", "VARCHAR"),
        ("asset_id", "VARCHAR"),
        ("source_id", "VARCHAR"),
        ("event_time", "TIMESTAMPTZ"),
        ("available_at", "TIMESTAMPTZ"),
        ("received_at", "TIMESTAMPTZ"),
        ("relative_path", "VARCHAR"),
        ("checksum_sha256", "VARCHAR"),
        ("schema_version", "VARCHAR"),
        ("document_json", "VARCHAR"),
    ),
    BoundedInsertTable.NORMALIZED_OBSERVATIONS: (
        ("observation_id", "VARCHAR"),
        ("raw_record_id", "VARCHAR"),
        ("asset_id", "VARCHAR"),
        ("field_name", "VARCHAR"),
        ("frequency", "VARCHAR"),
        ("observed_at", "TIMESTAMPTZ"),
        ("period_end", "TIMESTAMPTZ"),
        ("available_at", "TIMESTAMPTZ"),
        ("quality", "VARCHAR"),
        ("document_json", "VARCHAR"),
    ),
    BoundedInsertTable.METRIC_RESULTS: (
        ("result_id", "VARCHAR"),
        ("asset_id", "VARCHAR"),
        ("metric_key", "VARCHAR"),
        ("as_of", "TIMESTAMPTZ"),
        ("available_at", "TIMESTAMPTZ"),
        ("computed_at", "TIMESTAMPTZ"),
        ("quality", "VARCHAR"),
        ("document_json", "VARCHAR"),
    ),
    BoundedInsertTable.DIAGNOSTIC_RESULTS: (
        ("diagnostic_id", "VARCHAR"),
        ("asset_id", "VARCHAR"),
        ("mode", "VARCHAR"),
        ("verdict", "VARCHAR"),
        ("as_of", "TIMESTAMPTZ"),
        ("available_at", "TIMESTAMPTZ"),
        ("computed_at", "TIMESTAMPTZ"),
        ("quality", "VARCHAR"),
        ("document_json", "VARCHAR"),
    ),
    BoundedInsertTable.OBSERVATIONS_V2: (
        ("observation_id", "VARCHAR"),
        ("raw_record_id", "VARCHAR"),
        ("asset_id", "VARCHAR"),
        ("field_name", "VARCHAR"),
        ("value_text", "VARCHAR"),
        ("unit", "VARCHAR"),
        ("frequency", "VARCHAR"),
        ("observed_at", "VARCHAR"),
        ("period_start", "VARCHAR"),
        ("period_end", "VARCHAR"),
        ("available_at", "VARCHAR"),
        ("normalized_at", "VARCHAR"),
        ("source_id", "VARCHAR"),
        ("source_record_key", "VARCHAR"),
        ("source_retrieved_at", "VARCHAR"),
        ("source_raw_uri", "VARCHAR"),
        ("source_checksum_sha256", "VARCHAR"),
        ("quality", "VARCHAR"),
        ("transformation_version", "VARCHAR"),
    ),
    BoundedInsertTable.METRICS_V2: (
        ("result_id", "VARCHAR"),
        ("asset_id", "VARCHAR"),
        ("metric_key", "VARCHAR"),
        ("value_text", "VARCHAR"),
        ("unit", "VARCHAR"),
        ("as_of", "VARCHAR"),
        ("available_at", "VARCHAR"),
        ("computed_at", "VARCHAR"),
        ("parameters_json", "VARCHAR"),
        ("evidence_set_id", "VARCHAR"),
        ("algorithm_version", "VARCHAR"),
        ("quality", "VARCHAR"),
    ),
    BoundedInsertTable.METRIC_OBSERVATION_LINKS_V2: (
        ("result_id", "VARCHAR"),
        ("position", "INTEGER"),
        ("observation_id", "VARCHAR"),
    ),
    BoundedInsertTable.METRIC_METRIC_LINKS_V2: (
        ("result_id", "VARCHAR"),
        ("position", "INTEGER"),
        ("input_result_id", "VARCHAR"),
    ),
    BoundedInsertTable.DIAGNOSTICS_V2: (
        ("diagnostic_id", "VARCHAR"),
        ("asset_id", "VARCHAR"),
        ("mode", "VARCHAR"),
        ("verdict", "VARCHAR"),
        ("final_score_text", "VARCHAR"),
        ("confidence_text", "VARCHAR"),
        ("as_of", "VARCHAR"),
        ("available_at", "VARCHAR"),
        ("computed_at", "VARCHAR"),
        ("algorithm_version", "VARCHAR"),
        ("summary", "VARCHAR"),
        ("quality", "VARCHAR"),
    ),
    BoundedInsertTable.DIAGNOSTIC_COMPONENTS_V2: (
        ("diagnostic_id", "VARCHAR"),
        ("position", "INTEGER"),
        ("component_key", "VARCHAR"),
        ("score_text", "VARCHAR"),
        ("weight_text", "VARCHAR"),
        ("weighted_contribution_text", "VARCHAR"),
        ("explanation", "VARCHAR"),
    ),
    BoundedInsertTable.DIAGNOSTIC_COMPONENT_METRIC_LINKS_V2: (
        ("diagnostic_id", "VARCHAR"),
        ("component_position", "INTEGER"),
        ("link_position", "INTEGER"),
        ("metric_result_id", "VARCHAR"),
    ),
    BoundedInsertTable.DIAGNOSTIC_EVIDENCE_V2: (
        ("diagnostic_id", "VARCHAR"),
        ("position", "INTEGER"),
        ("metric_result_id", "VARCHAR"),
        ("direction", "VARCHAR"),
        ("contribution_text", "VARCHAR"),
        ("reason", "VARCHAR"),
    ),
    BoundedInsertTable.SNAPSHOTS_V2: (
        ("snapshot_id", "VARCHAR"),
        ("asset_id", "VARCHAR"),
        ("domain", "VARCHAR"),
        ("known_at", "VARCHAR"),
        ("policy_version", "VARCHAR"),
        ("evidence_set_digest", "VARCHAR"),
        ("created_at", "VARCHAR"),
    ),
    BoundedInsertTable.SNAPSHOT_METRIC_LINKS_V2: (
        ("snapshot_id", "VARCHAR"),
        ("position", "INTEGER"),
        ("metric_result_id", "VARCHAR"),
    ),
    BoundedInsertTable.SNAPSHOT_DIAGNOSTIC_LINKS_V2: (
        ("snapshot_id", "VARCHAR"),
        ("position", "INTEGER"),
        ("diagnostic_id", "VARCHAR"),
    ),
    BoundedInsertTable.EVIDENCE_SEGMENTS_V2: (
        ("segment_id", "VARCHAR"),
        ("asset_id", "VARCHAR"),
        ("source_id", "VARCHAR"),
        ("field_name", "VARCHAR"),
        ("day", "VARCHAR"),
        ("observation_ids_json", "VARCHAR"),
        ("available_at", "VARCHAR"),
        ("canonical_hash", "VARCHAR"),
    ),
    BoundedInsertTable.EVIDENCE_SETS_V2: (
        ("evidence_set_id", "VARCHAR"),
        ("asset_id", "VARCHAR"),
        ("source_id", "VARCHAR"),
        ("field_name", "VARCHAR"),
        ("input_count", "INTEGER"),
        ("head_offset", "INTEGER"),
        ("inline_observation_ids_json", "VARCHAR"),
        ("inline_available_at", "VARCHAR"),
        ("first_observed_at", "VARCHAR"),
        ("first_observation_id", "VARCHAR"),
        ("last_observed_at", "VARCHAR"),
        ("last_observation_id", "VARCHAR"),
        ("available_at", "VARCHAR"),
        ("canonical_hash", "VARCHAR"),
    ),
    BoundedInsertTable.EVIDENCE_SET_MEMBERS_V2: (
        ("evidence_set_id", "VARCHAR"),
        ("position", "INTEGER"),
        ("segment_id", "VARCHAR"),
    ),
    BoundedInsertTable.RAW_V2_INDEX: (
        ("record_id", "VARCHAR"),
        ("relative_path", "VARCHAR"),
        ("checksum_sha256", "VARCHAR"),
        ("asset_id", "VARCHAR"),
        ("source_id", "VARCHAR"),
        ("event_time", "VARCHAR"),
        ("available_at", "VARCHAR"),
        ("received_at", "VARCHAR"),
        ("schema_version", "VARCHAR"),
        ("projected_manager_cik", "VARCHAR"),
        ("projected_report_id", "VARCHAR"),
    ),
    BoundedInsertTable.DAILY_PREFIXES_V2: (
        ("prefix_id", "VARCHAR"),
        ("policy_version", "VARCHAR"),
        ("asset_id", "VARCHAR"),
        ("source_id", "VARCHAR"),
        ("frequency", "VARCHAR"),
        ("field_group", "VARCHAR"),
        ("timestamp", "VARCHAR"),
        ("observation_digest", "VARCHAR"),
        ("parent_prefix_id", "VARCHAR"),
        ("parent_hash", "VARCHAR"),
        ("length", "INTEGER"),
        ("available_at", "VARCHAR"),
        ("quality", "VARCHAR"),
        ("prefix_hash", "VARCHAR"),
    ),
    BoundedInsertTable.DAILY_PREFIX_OBSERVATION_LINKS_V2: (
        ("prefix_id", "VARCHAR"),
        ("position", "INTEGER"),
        ("observation_id", "VARCHAR"),
    ),
    BoundedInsertTable.MARKET_CHECKPOINTS_V2: (
        ("checkpoint_id", "VARCHAR"),
        ("policy_version", "VARCHAR"),
        ("asset_id", "VARCHAR"),
        ("source_id", "VARCHAR"),
        ("frequency", "VARCHAR"),
        ("family", "VARCHAR"),
        ("algorithm_version", "VARCHAR"),
        ("parameters_json", "VARCHAR"),
        ("seed_start", "VARCHAR"),
        ("as_of", "VARCHAR"),
        ("available_at", "VARCHAR"),
        ("daily_prefix_id", "VARCHAR"),
        ("daily_prefix_hash", "VARCHAR"),
        ("daily_prefix_length", "INTEGER"),
        ("state_json", "VARCHAR"),
    ),
    BoundedInsertTable.CHECKPOINT_METRIC_LINKS_V2: (
        ("checkpoint_id", "VARCHAR"),
        ("position", "INTEGER"),
        ("metric_key", "VARCHAR"),
        ("result_id", "VARCHAR"),
    ),
}


def _encode_value(value: object) -> str | int | bool | None:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        value.encode("utf-8")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("bounded insert does not accept non-finite Decimal values")
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("bounded insert datetimes must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, float):
        raise TypeError("bounded insert does not accept float values")
    raise TypeError(f"bounded insert does not accept {type(value).__name__} values")


def _statement(table: BoundedInsertTable) -> str:
    spec = _COLUMN_TYPES.get(table)
    if spec is None:
        raise ValueError("bounded insert target is not registered")
    columns = ", ".join(column for column, _ in spec)
    projections = ",\n       ".join(
        f"CAST(json_extract_string(input_row, '$[{position}]') AS {column_type}) AS {column}"
        for position, (column, column_type) in enumerate(spec)
    )
    projected_columns = ", ".join(column for column, _ in spec)
    return (
        "WITH source_rows AS (\n"
        "    SELECT CAST(key AS UBIGINT) AS row_position, value AS input_row\n"
        "    FROM json_each(?)\n"
        "), decoded_rows AS (\n"
        f"    SELECT row_position, {projections}\n"
        "    FROM source_rows\n"
        ")\n"
        f"INSERT INTO {table.value} ({columns})\n"
        f"SELECT {projected_columns} FROM decoded_rows ORDER BY row_position"
    )


def insert_bounded(
    connection: DuckDBPyConnection,
    table: BoundedInsertTable,
    rows: Sequence[Sequence[object]],
) -> int:
    """Insert ordered rows in <=256-row SQL executions, one JSON parameter each.

    The return value is the number of SQL executions, which allows callers and
    tests to measure the transport work without adding persistent metadata.
    """
    if not isinstance(table, BoundedInsertTable):
        raise TypeError("bounded insert target must be a registered table")
    spec = _COLUMN_TYPES[table]
    if not rows:
        return 0
    statement = _statement(table)
    for row in rows:
        if len(row) != len(spec):
            raise ValueError(
                f"bounded insert row for {table.value} has {len(row)} values; expected {len(spec)}"
            )
        for value in row:
            _encode_value(value)
    executions = 0
    for start in range(0, len(rows), MAX_BOUNDED_INSERT_ROWS):
        chunk = rows[start : start + MAX_BOUNDED_INSERT_ROWS]
        encoded_rows: list[list[str | int | bool | None]] = []
        for row in chunk:
            encoded_rows.append([_encode_value(value) for value in row])
        payload = json.dumps(
            encoded_rows,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        connection.execute(statement, [payload])
        executions += 1
    return executions


@contextmanager
def write_transaction(
    connection: DuckDBPyConnection,
    *,
    previous_transaction_id: int | None = None,
) -> Iterator[None]:
    """Keep one storage operation atomic without taking over a caller transaction."""
    if previous_transaction_id is None:
        first_transaction_id = connection.execute("SELECT current_transaction_id()").fetchone()
        second_transaction_id = connection.execute("SELECT current_transaction_id()").fetchone()
        owns_transaction = first_transaction_id != second_transaction_id
    else:
        current_transaction_id = connection.execute("SELECT current_transaction_id()").fetchone()
        owns_transaction = current_transaction_id != (previous_transaction_id,)
    if owns_transaction:
        connection.execute("BEGIN TRANSACTION")

    try:
        yield
        if owns_transaction:
            connection.execute("COMMIT")
    except BaseException:
        if owns_transaction:
            connection.execute("ROLLBACK")
        raise


__all__ = [
    "MAX_BOUNDED_INSERT_ROWS",
    "BoundedInsertTable",
    "insert_bounded",
    "write_transaction",
]
