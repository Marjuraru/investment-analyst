"""Controlled Parquet exports from DuckDB tables."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from duckdb import DuckDBPyConnection

from investment_analyst.storage.errors import RecordConflictError, StorageError
from investment_analyst.storage.paths import StoragePaths
from investment_analyst.storage.serialization import canonical_json_text
from investment_analyst.storage.workspace_v2_repositories import (
    WorkspaceV2DiagnosticResultRepository,
    WorkspaceV2MetricResultRepository,
    WorkspaceV2ObservationRepository,
    WorkspaceV2RawRecordRepository,
)

_ALLOWED_EXPORTS = {
    "assets": "asset_id",
    "source_definitions": "source_id",
    "raw_record_index": "record_id",
    "normalized_observations": "observation_id",
    "metric_definitions": "metric_key",
    "metric_results": "result_id",
    "diagnostic_results": "diagnostic_id",
}


class ParquetExporter:
    """Export a closed set of storage tables to Parquet."""

    def __init__(
        self,
        paths: StoragePaths,
        connection: DuckDBPyConnection,
        *,
        read_only: bool = False,
        raw_records: WorkspaceV2RawRecordRepository | None = None,
        observations: WorkspaceV2ObservationRepository | None = None,
        metric_results: WorkspaceV2MetricResultRepository | None = None,
        diagnostics: WorkspaceV2DiagnosticResultRepository | None = None,
    ) -> None:
        self._paths = paths
        self._connection = connection
        self._read_only = read_only
        self._raw_records = raw_records
        self._observations = observations
        self._metric_results = metric_results
        self._diagnostics = diagnostics

    def export_table(
        self,
        table_name: str,
        destination: Path | None = None,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Export one allowed table without silently replacing an existing file."""
        if self._read_only:
            raise StorageError("Parquet cannot be exported through read-only storage")
        order_column = _ALLOWED_EXPORTS.get(table_name)
        if order_column is None:
            raise StorageError(f"table {table_name!r} is not allowed for Parquet export")
        output = (
            Path(destination)
            if destination is not None
            else (self._paths.exports_dir / f"{table_name}.parquet")
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists() and not overwrite:
            raise RecordConflictError(f"Parquet export already exists: {output}")

        if self._paths.format_version == 2 and table_name in {
            "raw_record_index",
            "normalized_observations",
            "metric_results",
            "diagnostic_results",
        }:
            return self._export_v2_table(table_name, output, overwrite=overwrite)

        temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
        try:
            query = f"SELECT * FROM {table_name} ORDER BY {order_column}"
            self._connection.execute(
                f"COPY ({query}) TO ? (FORMAT PARQUET)",  # noqa: S608
                [str(temporary)],
            )
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        return output

    def _export_v2_table(self, table_name: str, output: Path, *, overwrite: bool) -> Path:
        schemas = {
            "raw_record_index": (
                "record_id VARCHAR, asset_id VARCHAR, source_id VARCHAR, event_time TIMESTAMPTZ, "
                "available_at TIMESTAMPTZ, received_at TIMESTAMPTZ, relative_path VARCHAR, "
                "checksum_sha256 VARCHAR, schema_version VARCHAR, document_json VARCHAR, "
                "inserted_at TIMESTAMPTZ"
            ),
            "normalized_observations": (
                "observation_id VARCHAR, raw_record_id VARCHAR, asset_id VARCHAR, "
                "field_name VARCHAR, frequency VARCHAR, observed_at TIMESTAMPTZ, "
                "period_end TIMESTAMPTZ, available_at TIMESTAMPTZ, quality VARCHAR, "
                "document_json VARCHAR, inserted_at TIMESTAMPTZ"
            ),
            "metric_results": (
                "result_id VARCHAR, asset_id VARCHAR, metric_key VARCHAR, as_of TIMESTAMPTZ, "
                "available_at TIMESTAMPTZ, computed_at TIMESTAMPTZ, quality VARCHAR, "
                "document_json VARCHAR, inserted_at TIMESTAMPTZ"
            ),
            "diagnostic_results": (
                "diagnostic_id VARCHAR, asset_id VARCHAR, mode VARCHAR, verdict VARCHAR, "
                "as_of TIMESTAMPTZ, available_at TIMESTAMPTZ, computed_at TIMESTAMPTZ, "
                "quality VARCHAR, document_json VARCHAR, inserted_at TIMESTAMPTZ"
            ),
        }
        temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
        table = f"v2_export_{uuid4().hex}"
        self._connection.execute(f"CREATE TEMP TABLE {table} ({schemas[table_name]})")
        try:
            if table_name == "raw_record_index":
                self._fill_raw_export(table)
            elif table_name == "normalized_observations":
                self._fill_observation_export(table)
            elif table_name == "metric_results":
                self._fill_metric_export(table)
            else:
                self._fill_diagnostic_export(table)
            order_column = _ALLOWED_EXPORTS[table_name]
            query = f"SELECT * FROM {table} ORDER BY {order_column}"
            self._connection.execute(f"COPY ({query}) TO ? (FORMAT PARQUET)", [str(temporary)])
            if output.exists() and not overwrite:
                raise RecordConflictError(f"Parquet export already exists: {output}")
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
            self._connection.execute(f"DROP TABLE IF EXISTS {table}")
        return output

    def _fill_raw_export(self, table: str) -> None:
        if self._raw_records is None:
            raise StorageError("workspace v2 raw export repository is unavailable")
        cursor_at: datetime | None = None
        cursor_id = None
        while True:
            ids = self._raw_records.list_import_page(
                limit=256,
                after_received_at=cursor_at,
                after_record_id=cursor_id,
            )
            if not ids:
                return
            models = self._raw_records.get_many(ids)
            rows = self._indexed_rows(
                "raw_v2_index", "record_id", "relative_path, checksum_sha256, inserted_at", ids
            )
            inserted: list[tuple[object, ...]] = []
            for identifier in ids:
                model = models[identifier]
                relative_path, checksum, inserted_at = rows[str(identifier)]
                inserted.append(
                    (
                        str(identifier),
                        model.asset_id,
                        model.source.source_id,
                        model.event_time,
                        model.available_at,
                        model.received_at,
                        relative_path,
                        checksum,
                        model.schema_version,
                        canonical_json_text(model),
                        self._parse_inserted_at(inserted_at),
                    )
                )
            self._insert_export_rows(table, inserted)
            last = models[ids[-1]]
            cursor_at, cursor_id = last.received_at, last.record_id

    def _fill_observation_export(self, table: str) -> None:
        if self._observations is None:
            raise StorageError("workspace v2 observation export repository is unavailable")
        cursor_at: datetime | None = None
        cursor_id = None
        while True:
            ids = self._observations.list_observation_import_page(
                limit=256,
                after_available_at=cursor_at,
                after_observation_id=cursor_id,
            )
            if not ids:
                return
            models = self._observations.get_many(ids)
            inserted = self._indexed_rows(
                "normalized_observations_v2", "observation_id", "inserted_at", ids
            )
            rows = [
                (
                    str(identifier),
                    str(models[identifier].raw_record_id),
                    models[identifier].asset_id,
                    models[identifier].field_name,
                    models[identifier].frequency.value,
                    models[identifier].observed_at,
                    models[identifier].period_end,
                    models[identifier].available_at,
                    models[identifier].quality.value,
                    canonical_json_text(models[identifier]),
                    self._parse_inserted_at(inserted[str(identifier)][0]),
                )
                for identifier in ids
            ]
            self._insert_export_rows(table, rows)
            last = models[ids[-1]]
            cursor_at, cursor_id = last.available_at, last.observation_id

    def _fill_metric_export(self, table: str) -> None:
        if self._metric_results is None:
            raise StorageError("workspace v2 metric export repository is unavailable")
        cursor_at: datetime | None = None
        cursor_id = None
        while True:
            ids = self._metric_results.list_import_page(
                limit=256,
                after_available_at=cursor_at,
                after_result_id=cursor_id,
            )
            if not ids:
                return
            models = self._metric_results.get_many(ids)
            inserted = self._indexed_rows(
                "workspace_metric_results_v2",
                "result_id",
                "CAST(inserted_at AS VARCHAR)",
                ids,
            )
            rows = [
                (
                    str(identifier),
                    models[identifier].asset_id,
                    models[identifier].metric_key,
                    models[identifier].as_of,
                    models[identifier].available_at,
                    models[identifier].computed_at,
                    models[identifier].quality.value,
                    canonical_json_text(models[identifier]),
                    self._parse_inserted_at(inserted[str(identifier)][0]),
                )
                for identifier in ids
            ]
            self._insert_export_rows(table, rows)
            last = models[ids[-1]]
            cursor_at, cursor_id = last.available_at, last.result_id

    def _fill_diagnostic_export(self, table: str) -> None:
        if self._diagnostics is None:
            raise StorageError("workspace v2 diagnostic export repository is unavailable")
        cursor_at: datetime | None = None
        cursor_id = None
        while True:
            ids = self._diagnostics.list_import_page(
                limit=256,
                after_available_at=cursor_at,
                after_diagnostic_id=cursor_id,
            )
            if not ids:
                return
            models = self._diagnostics.get_many(ids)
            inserted = self._indexed_rows(
                "workspace_diagnostic_results_v2",
                "diagnostic_id",
                "CAST(inserted_at AS VARCHAR)",
                ids,
            )
            rows = [
                (
                    str(identifier),
                    models[identifier].asset_id,
                    models[identifier].mode.value,
                    models[identifier].verdict.value,
                    models[identifier].as_of,
                    models[identifier].available_at,
                    models[identifier].computed_at,
                    models[identifier].quality.value,
                    canonical_json_text(models[identifier]),
                    self._parse_inserted_at(inserted[str(identifier)][0]),
                )
                for identifier in ids
            ]
            self._insert_export_rows(table, rows)
            last = models[ids[-1]]
            cursor_at, cursor_id = last.available_at, last.diagnostic_id

    def _indexed_rows(
        self,
        source_table: str,
        key_column: str,
        selected_columns: str,
        identifiers: list[object] | tuple[object, ...],
    ) -> dict[str, tuple[object, ...]]:
        indexed: dict[str, tuple[object, ...]] = {}
        for start in range(0, len(identifiers), 256):
            chunk = identifiers[start : start + 256]
            rows = self._connection.execute(
                f"SELECT {key_column}, {selected_columns} FROM {source_table} "
                f"WHERE {key_column} IN ({', '.join('?' for _ in chunk)})",
                [str(identifier) for identifier in chunk],
            ).fetchall()
            indexed.update({str(row[0]): tuple(row[1:]) for row in rows})
        if len(indexed) != len(identifiers):
            raise StorageError("workspace v2 export index is incomplete")
        return indexed

    def _parse_inserted_at(self, value: object) -> datetime:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise StorageError("workspace v2 inserted_at export timestamp is not timezone-aware")
        return parsed.astimezone(UTC)

    def _insert_export_rows(self, table: str, rows: list[tuple[object, ...]]) -> None:
        if not rows:
            return
        placeholders = ", ".join("?" for _ in rows[0])
        for start in range(0, len(rows), 256):
            self._connection.executemany(
                f"INSERT INTO {table} VALUES ({placeholders})", rows[start : start + 256]
            )
