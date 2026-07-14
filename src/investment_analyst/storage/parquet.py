"""Controlled Parquet exports from DuckDB tables."""

from pathlib import Path
from uuid import uuid4

from duckdb import DuckDBPyConnection

from investment_analyst.storage.errors import RecordConflictError, StorageError
from investment_analyst.storage.paths import StoragePaths

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
    ) -> None:
        self._paths = paths
        self._connection = connection
        self._read_only = read_only

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
