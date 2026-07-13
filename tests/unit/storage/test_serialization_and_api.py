"""Tests for canonical serialization, public imports, and dependency boundaries."""

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from investment_analyst.core.interfaces import (
    AssetRepository,
    DiagnosticResultRepository,
    MetricDefinitionRepository,
    MetricResultRepository,
    ObservationRepository,
    RawRecordRepository,
    SourceDefinitionRepository,
)
from investment_analyst.storage import (
    DuckDBStore,
    JsonRawRecordRepository,
    LocalStorage,
    ParquetExporter,
    StoragePaths,
)
from investment_analyst.storage.serialization import canonical_json_bytes

from .conftest import make_metric_result


def test_canonical_json_is_reproducible_and_rejects_non_finite_values() -> None:
    result = make_metric_result(observation_id=__import__("uuid").uuid4())

    first = canonical_json_bytes(result)
    second = canonical_json_bytes(result)

    assert first == second
    assert b'"algorithm_version"' in first
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json_bytes(result.model_copy(update={"value": Decimal("NaN")}))


def test_public_storage_and_repository_symbols_are_importable() -> None:
    assert all(
        symbol is not None
        for symbol in (
            AssetRepository,
            DiagnosticResultRepository,
            DuckDBStore,
            JsonRawRecordRepository,
            LocalStorage,
            MetricDefinitionRepository,
            MetricResultRepository,
            ObservationRepository,
            ParquetExporter,
            RawRecordRepository,
            SourceDefinitionRepository,
            StoragePaths,
        )
    )


def test_storage_modules_do_not_import_forbidden_persistence_libraries() -> None:
    storage_root = Path(__file__).parents[3] / "src" / "investment_analyst" / "storage"
    forbidden = {"pandas", "polars", "pyarrow", "sqlalchemy", "sqlite3"}
    imported: set[str] = set()

    for path in storage_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

    assert imported.isdisjoint(forbidden)
