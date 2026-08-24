"""Public local storage API."""

from investment_analyst.storage.document_content import DocumentContentStore
from investment_analyst.storage.duckdb_store import DuckDBStore
from investment_analyst.storage.errors import (
    RecordConflictError,
    RecordNotFoundError,
    StorageError,
    StorageSchemaError,
)
from investment_analyst.storage.local import LocalStorage
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

__all__ = [
    "DuckDBAssetRepository",
    "DuckDBDiagnosticResultRepository",
    "DuckDBMetricDefinitionRepository",
    "DuckDBMetricResultRepository",
    "DuckDBObservationRepository",
    "DuckDBSourceDefinitionRepository",
    "DuckDBStore",
    "DocumentContentStore",
    "JsonRawRecordRepository",
    "LocalStorage",
    "ParquetExporter",
    "RecordConflictError",
    "RecordNotFoundError",
    "StorageError",
    "StoragePaths",
    "StorageSchemaError",
]
