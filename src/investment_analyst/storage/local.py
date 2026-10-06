"""Small facade that assembles all local storage components."""

from types import TracebackType

from investment_analyst.core.interfaces.repositories import (
    AssetRepository,
    DiagnosticResultRepository,
    MetricDefinitionRepository,
    MetricResultRepository,
    ObservationRepository,
    RawRecordRepository,
    SourceDefinitionRepository,
)
from investment_analyst.storage.compact_analytical_v2 import CompactAnalyticalStore
from investment_analyst.storage.document_content import DocumentContentStore
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
from investment_analyst.storage.workspace_v2 import WorkspaceV2Store
from investment_analyst.storage.workspace_v2_repositories import (
    WorkspaceV2DiagnosticResultRepository,
    WorkspaceV2MetricResultRepository,
    WorkspaceV2ObservationRepository,
    WorkspaceV2RawRecordRepository,
)


class LocalStorage:
    """Context-managed facade for local raw files, DuckDB, and Parquet exports."""

    def __init__(self, paths: StoragePaths, *, read_only: bool = False) -> None:
        self.paths = paths
        self.read_only = read_only
        self.store: DuckDBStore | WorkspaceV2Store
        if paths.format_version == 1:
            self.store = DuckDBStore(paths, read_only=read_only)
        elif paths.format_version == 2:
            self.store = WorkspaceV2Store(paths, read_only=read_only)
        else:
            raise ValueError("workspace storage format is unsupported")
        self.assets: AssetRepository
        self.sources: SourceDefinitionRepository
        self.raw_records: RawRecordRepository
        self.observations: ObservationRepository
        self.metric_definitions: MetricDefinitionRepository
        self.metric_results: MetricResultRepository
        self.diagnostics: DiagnosticResultRepository
        self.parquet: ParquetExporter
        self.documents: DocumentContentStore
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
        self.metric_definitions = DuckDBMetricDefinitionRepository(connection)
        if self.paths.format_version == 1:
            self.raw_records = JsonRawRecordRepository(
                self.paths,
                connection,
                read_only=self.read_only,
            )
            self.observations = DuckDBObservationRepository(connection)
            self.metric_results = DuckDBMetricResultRepository(connection)
            self.diagnostics = DuckDBDiagnosticResultRepository(connection)
        else:
            if not isinstance(self.store, WorkspaceV2Store):
                raise StorageError("workspace v2 store was not selected")
            compact = CompactAnalyticalStore(connection)
            self.raw_records = WorkspaceV2RawRecordRepository(self.store.raw_staging, connection)
            self.observations = WorkspaceV2ObservationRepository(self.store.raw_staging, connection)
            self.metric_results = WorkspaceV2MetricResultRepository(compact)
            self.diagnostics = WorkspaceV2DiagnosticResultRepository(compact)
        if self.paths.format_version == 1:
            self.parquet = ParquetExporter(
                self.paths,
                connection,
                read_only=self.read_only,
            )
        else:
            if not isinstance(self.raw_records, WorkspaceV2RawRecordRepository):
                raise StorageError("workspace v2 raw repository was not selected")
            if not isinstance(self.observations, WorkspaceV2ObservationRepository):
                raise StorageError("workspace v2 observation repository was not selected")
            if not isinstance(self.metric_results, WorkspaceV2MetricResultRepository):
                raise StorageError("workspace v2 metric repository was not selected")
            if not isinstance(self.diagnostics, WorkspaceV2DiagnosticResultRepository):
                raise StorageError("workspace v2 diagnostic repository was not selected")
            self.parquet = ParquetExporter(
                self.paths,
                connection,
                read_only=self.read_only,
                raw_records=self.raw_records,
                observations=self.observations,
                metric_results=self.metric_results,
                diagnostics=self.diagnostics,
            )
        self.documents = DocumentContentStore(self.paths, read_only=self.read_only)
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
