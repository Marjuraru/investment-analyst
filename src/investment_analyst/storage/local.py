"""Small facade that assembles all local storage components."""

from types import TracebackType

from investment_analyst.storage.duckdb_store import DuckDBStore
from investment_analyst.storage.errors import StorageError
from investment_analyst.storage.parquet import ParquetExporter
from investment_analyst.storage.paths import StoragePaths
from investment_analyst.storage.raw_records import JsonRawRecordRepository
from investment_analyst.storage.repositories import (
    DuckDBAssetRepository,
    DuckDBDiagnosticResultRepository,
    DuckDBMetricDefinitionRepository,
    DuckDBMetricResultRepository,
    DuckDBObservationRepository,
    DuckDBSourceDefinitionRepository,
)


class LocalStorage:
    """Context-managed facade for local raw files, DuckDB, and Parquet exports."""

    def __init__(self, paths: StoragePaths, *, read_only: bool = False) -> None:
        self.paths = paths
        self.read_only = read_only
        self.store = DuckDBStore(paths, read_only=read_only)
        self.assets: DuckDBAssetRepository
        self.sources: DuckDBSourceDefinitionRepository
        self.raw_records: JsonRawRecordRepository
        self.observations: DuckDBObservationRepository
        self.metric_definitions: DuckDBMetricDefinitionRepository
        self.metric_results: DuckDBMetricResultRepository
        self.diagnostics: DuckDBDiagnosticResultRepository
        self.parquet: ParquetExporter
        self._is_open = False

    @property
    def is_open(self) -> bool:
        """Return whether the facade currently owns an open DuckDB connection."""
        return self._is_open

    def open(self) -> "LocalStorage":
        """Initialize DuckDB and expose repository instances."""
        if self._is_open:
            return self
        self.store.open()
        connection = self.store.connection
        self.assets = DuckDBAssetRepository(connection)
        self.sources = DuckDBSourceDefinitionRepository(connection)
        self.raw_records = JsonRawRecordRepository(
            self.paths,
            connection,
            read_only=self.read_only,
        )
        self.observations = DuckDBObservationRepository(connection)
        self.metric_definitions = DuckDBMetricDefinitionRepository(connection)
        self.metric_results = DuckDBMetricResultRepository(connection)
        self.diagnostics = DuckDBDiagnosticResultRepository(connection)
        self.parquet = ParquetExporter(
            self.paths,
            connection,
            read_only=self.read_only,
        )
        self._is_open = True
        return self

    def close(self) -> None:
        """Close the local storage connection."""
        self.store.close()
        self._is_open = False

    def require_open(self) -> None:
        """Raise a storage error when repositories are accessed before opening."""
        if not self._is_open:
            raise StorageError("LocalStorage is not open")

    def __enter__(self) -> "LocalStorage":
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
