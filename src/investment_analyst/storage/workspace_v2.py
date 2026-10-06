"""Versioned DuckDB connection for the selectable workspace v2 backend."""

from __future__ import annotations

from types import TracebackType

import duckdb
from duckdb import DuckDBPyConnection

from investment_analyst.storage.errors import StorageError, StorageSchemaError
from investment_analyst.storage.paths import StoragePaths
from investment_analyst.storage.raw_v2 import RawV2Staging

_FORMAT_ID = "workspace-analytical-store-v2"
_SCHEMA_VERSION = "1"
_MEMORY_LIMIT = "2GB"
_THREADS = 2
_METADATA = {
    "format_id": _FORMAT_ID,
    "schema_version": _SCHEMA_VERSION,
}


class WorkspaceV2Error(StorageError):
    """The workspace v2 index or its identity cannot be trusted."""


class WorkspaceV2Store:
    """Own one configured connection and the raw v2 writer lock for a workspace."""

    def __init__(self, paths: StoragePaths, *, read_only: bool = False) -> None:
        if paths.format_version != 2 or paths.workspace_id is None:
            raise WorkspaceV2Error("workspace v2 storage requires a versioned workspace path")
        self.paths = paths
        self.read_only = read_only
        self._connection: DuckDBPyConnection | None = None
        self._raw_staging: RawV2Staging | None = None

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    @property
    def connection(self) -> DuckDBPyConnection:
        if self._connection is None:
            raise StorageError("workspace v2 store is not open")
        return self._connection

    @property
    def raw_staging(self) -> RawV2Staging:
        if self._raw_staging is None:
            raise StorageError("workspace v2 raw store is not open")
        return self._raw_staging

    def open(self) -> WorkspaceV2Store:
        if self.is_open:
            return self
        if self.read_only:
            if not self.paths.database_path.is_file():
                raise StorageError("read-only workspace v2 database does not exist")
        else:
            self.paths.create_directories()

        connection = duckdb.connect(str(self.paths.database_path), read_only=self.read_only)
        self._connection = connection
        staging: RawV2Staging | None = None
        try:
            connection.execute(f"SET memory_limit = '{_MEMORY_LIMIT}'")
            connection.execute(f"SET threads = {_THREADS}")
            existing_tables = self._table_names()
            is_new_store = not existing_tables
            if self.read_only:
                connection.execute("BEGIN TRANSACTION READ ONLY")
                self._validate_metadata()
            else:
                has_metadata = "workspace_v2_metadata" in existing_tables
                if not has_metadata and existing_tables:
                    raise WorkspaceV2Error(
                        "workspace v2 database has tables without versioned metadata"
                    )
                if has_metadata:
                    self._validate_metadata()
                    required_staging_tables = {
                        "raw_v2_index",
                        "normalized_observations_v2",
                    }
                    if not required_staging_tables.issubset(existing_tables):
                        raise StorageSchemaError("workspace v2 staging schema is incomplete")

            staging = RawV2Staging(self.paths.root, connection, read_only=self.read_only)
            staging.open()
            self._raw_staging = staging

            if is_new_store and not self.read_only:
                self._initialize_metadata()
            elif not self.read_only:
                self._validate_metadata()

            self._validate_catalog_schema()

            from investment_analyst.storage.compact_analytical_v2 import (
                CompactAnalyticalStore,
            )

            CompactAnalyticalStore(connection).ensure(create=is_new_store and not self.read_only)
        except (duckdb.Error, OSError, StorageError, ValueError):
            if staging is not None:
                staging.close()
            self._raw_staging = None
            self.close()
            raise
        return self

    def close(self) -> None:
        staging = self._raw_staging
        self._raw_staging = None
        if staging is not None:
            staging.close()
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.close()

    def __enter__(self) -> WorkspaceV2Store:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _table_names(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
        return {str(row[0]) for row in rows}

    def _initialize_metadata(self) -> None:
        from importlib.resources import files

        migration = (
            files("investment_analyst.storage.migrations")
            .joinpath("002_workspace_v2.sql")
            .read_text(encoding="utf-8")
        )
        self.connection.execute(migration)
        metadata = {
            **_METADATA,
            "workspace_id": str(self.paths.workspace_id),
        }
        self.connection.executemany(
            "INSERT INTO workspace_v2_metadata (metadata_key, metadata_value) VALUES (?, ?)",
            list(metadata.items()),
        )

    def _validate_metadata(self) -> None:
        try:
            rows = self.connection.execute(
                "SELECT metadata_key, metadata_value FROM workspace_v2_metadata"
            ).fetchall()
        except duckdb.Error as error:
            raise StorageSchemaError("workspace v2 metadata is missing") from error
        values = {str(key): str(value) for key, value in rows}
        expected = {
            **_METADATA,
            "workspace_id": str(self.paths.workspace_id),
        }
        if values != expected:
            raise WorkspaceV2Error("workspace v2 metadata does not match its manifest")

    def _validate_catalog_schema(self) -> None:
        required = {
            "workspace_v2_metadata": {"metadata_key", "metadata_value"},
            "assets": {
                "asset_id",
                "symbol",
                "asset_class",
                "quote_currency",
                "is_active",
                "document_json",
                "inserted_at",
            },
            "source_definitions": {
                "source_id",
                "provider_name",
                "dataset_name",
                "source_type",
                "is_official",
                "document_json",
                "inserted_at",
            },
            "metric_definitions": {
                "metric_key",
                "display_name",
                "category",
                "definition_version",
                "document_json",
                "inserted_at",
            },
            "workspace_raw_json_projections_v2": {
                "record_id",
                "field_name",
                "field_value",
            },
            "raw_v2_index": {
                "record_id",
                "asset_id",
                "source_id",
                "event_time",
                "available_at",
                "received_at",
                "relative_path",
                "checksum_sha256",
                "schema_version",
                "projected_manager_cik",
                "projected_report_id",
                "inserted_at",
            },
            "normalized_observations_v2": {
                "observation_id",
                "raw_record_id",
                "asset_id",
                "field_name",
                "value_text",
                "unit",
                "frequency",
                "observed_at",
                "period_start",
                "period_end",
                "available_at",
                "normalized_at",
                "source_id",
                "source_record_key",
                "source_retrieved_at",
                "source_raw_uri",
                "source_checksum_sha256",
                "quality",
                "transformation_version",
                "inserted_at",
            },
        }
        for table, expected_columns in required.items():
            rows = self.connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'main' AND table_name = ?",
                [table],
            ).fetchall()
            columns = {str(row[0]) for row in rows}
            if columns != expected_columns:
                raise StorageSchemaError(f"workspace v2 table {table!r} is incompatible")


__all__ = ["WorkspaceV2Error", "WorkspaceV2Store"]
