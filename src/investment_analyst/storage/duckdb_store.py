"""Persistent DuckDB connection and schema initialization."""

from importlib.resources import files
from types import TracebackType

import duckdb
from duckdb import DuckDBPyConnection

from investment_analyst.storage.errors import StorageError, StorageSchemaError
from investment_analyst.storage.paths import StoragePaths

SCHEMA_VERSION = 1


class DuckDBStore:
    """Own one explicit persistent DuckDB connection."""

    def __init__(self, paths: StoragePaths, *, read_only: bool = False) -> None:
        self.paths = paths
        self.read_only = read_only
        self._connection: DuckDBPyConnection | None = None

    @property
    def is_open(self) -> bool:
        """Return whether this store currently owns an open connection."""
        return self._connection is not None

    @property
    def connection(self) -> DuckDBPyConnection:
        """Return the active connection or fail if the store is closed."""
        if self._connection is None:
            raise StorageError("DuckDBStore is not open")
        return self._connection

    def open(self) -> "DuckDBStore":
        """Open the database in read-write or genuine DuckDB read-only mode."""
        if self._connection is not None:
            return self
        if self.read_only:
            if not self.paths.database_path.is_file():
                raise StorageError("read-only DuckDB database does not exist")
        else:
            self.paths.create_directories()
        self._connection = duckdb.connect(
            str(self.paths.database_path),
            read_only=self.read_only,
        )
        try:
            if self.read_only:
                self._validate_schema()
            else:
                self._initialize_schema()
        except (duckdb.Error, OSError, StorageError):
            self.close()
            raise
        return self

    def close(self) -> None:
        """Close the owned connection."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "DuckDBStore":
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _validate_schema(self) -> None:
        try:
            row = self.connection.execute(
                "SELECT metadata_value FROM storage_metadata WHERE metadata_key = ?",
                ["schema_version"],
            ).fetchone()
        except duckdb.Error as error:
            raise StorageSchemaError("storage schema metadata is missing") from error
        if row is None:
            raise StorageSchemaError("storage schema version is missing")
        if row[0] != str(SCHEMA_VERSION):
            raise StorageSchemaError(
                f"unsupported storage schema version {row[0]!r}; expected {SCHEMA_VERSION}"
            )

    def _initialize_schema(self) -> None:
        connection = self.connection
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS storage_metadata (
                metadata_key VARCHAR PRIMARY KEY,
                metadata_value VARCHAR NOT NULL,
                inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        row = connection.execute(
            "SELECT metadata_value FROM storage_metadata WHERE metadata_key = ?",
            ["schema_version"],
        ).fetchone()
        if row is not None and row[0] != str(SCHEMA_VERSION):
            raise StorageSchemaError(
                f"unsupported storage schema version {row[0]!r}; expected {SCHEMA_VERSION}"
            )

        migration = (
            files("investment_analyst.storage.migrations")
            .joinpath("001_initial.sql")
            .read_text(encoding="utf-8")
        )
        connection.execute(migration)

        row = connection.execute(
            "SELECT metadata_value FROM storage_metadata WHERE metadata_key = ?",
            ["schema_version"],
        ).fetchone()
        if row is None or row[0] != str(SCHEMA_VERSION):
            raise StorageSchemaError("storage schema version 1 was not initialized")
