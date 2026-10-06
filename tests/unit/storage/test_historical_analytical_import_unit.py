"""Unit coverage for resumable historical metric and diagnostic import."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from investment_analyst.core.models import DiagnosticResult, MetricResult
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.historical_analytical_import import (
    HistoricalAnalyticalImporter,
    HistoricalAnalyticalImportError,
    HistoricalAnalyticalImportState,
)
from investment_analyst.storage.observation_v2_import import ObservationV2Importer
from investment_analyst.storage.raw_v2 import RawV2Staging
from investment_analyst.storage.raw_v2_import import RawV2Importer, RawV2ImportError
from investment_analyst.workspace.service import WorkspaceService

from .conftest import (
    make_diagnostic_result,
    make_metric_result,
    make_observation,
    make_raw_record,
)


@contextmanager
def _prepared(
    tmp_path: Path,
    *,
    failpoint: str | None = None,
) -> Iterator[
    tuple[
        LocalStorage,
        RawV2Staging,
        tuple[MetricResult, ...],
        tuple[DiagnosticResult, ...],
        Callable[[str], None],
    ]
]:
    initialization = WorkspaceService().initialize(tmp_path / "source-workspace")
    source_paths = StoragePaths.from_root(initialization.paths.storage_root)
    raw = make_raw_record()
    observation = make_observation(raw_record_id=raw.record_id)
    seed = make_metric_result(observation_id=observation.observation_id)
    derived = make_metric_result(
        observation_id=observation.observation_id,
        as_of=seed.as_of,
    ).model_copy(update={"result_id": uuid4(), "input_metric_result_ids": [seed.result_id]})
    second = make_metric_result(
        observation_id=observation.observation_id,
        as_of=seed.as_of,
    )
    metrics = (seed, derived, second)
    diagnostic = make_diagnostic_result(metric_result_id=derived.result_id)
    diagnostics = (diagnostic,)
    with LocalStorage(source_paths) as writer:
        writer.raw_records.save(raw)
        writer.observations.save(observation)
        writer.metric_results.save_many(metrics)
        writer.diagnostics.save_many(diagnostics)
    source = LocalStorage(source_paths, read_only=True).open()
    destination = (tmp_path / "staging").absolute()
    destination.mkdir()
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    staging = RawV2Staging(destination, connection).open()
    fingerprint = "source-corpus-fingerprint-v1"
    source_workspace_id = str(initialization.manifest.workspace_id)
    try:
        raw_summary = RawV2Importer(
            source,
            staging,
            source_workspace_id=source_workspace_id,
            source_fingerprint=fingerprint,
        ).run(page_limit=2)
        ObservationV2Importer(
            source,
            staging,
            source_workspace_id=source_workspace_id,
            source_fingerprint=fingerprint,
            raw_digest=raw_summary.corpus_digest,
        ).run(page_limit=2)

        def inject(point: str) -> None:
            if point == failpoint:
                raise HistoricalAnalyticalImportError(f"simulated interruption at {point}")

        yield source, staging, metrics, diagnostics, inject
    finally:
        staging.close()
        source.close()


def _importer(
    source: LocalStorage,
    staging: RawV2Staging,
    inject: Callable[[str], None] | None = None,
):
    return HistoricalAnalyticalImporter(
        source,
        staging,
        page_limit=2,
        clock=lambda: datetime(2026, 10, 5, tzinfo=UTC),
        failure_injector=inject,
    )


def test_import_preserves_rows_links_and_is_idempotent(tmp_path: Path) -> None:
    with _prepared(tmp_path) as (source, staging, metrics, diagnostics, _):
        summary = _importer(source, staging).run()
        archive = staging.historical_analytical_archive()

        assert summary.complete is True
        assert summary.metric_count == len(metrics)
        assert summary.diagnostic_count == len(diagnostics)
        assert summary.metric_created_count == len(metrics)
        assert summary.diagnostic_created_count == len(diagnostics)
        assert archive.get_metrics([item.result_id for item in metrics]) == {
            item.result_id: source.metric_results.get(item.result_id) for item in metrics
        }
        assert archive.get_diagnostics([item.diagnostic_id for item in diagnostics]) == {
            item.diagnostic_id: source.diagnostics.get(item.diagnostic_id) for item in diagnostics
        }
        assert archive.verify_complete().complete is True

        rerun = _importer(source, staging).run()

        assert rerun.metric_count == len(metrics)
        assert rerun.diagnostic_count == len(diagnostics)
        assert rerun.metric_created_count == 0
        assert rerun.diagnostic_created_count == 0


@pytest.mark.parametrize(
    ("failpoint", "expected_metric_count"),
    (
        ("before_metric_page_write", 0),
        ("after_metric_page_commit_before_state", 2),
        ("after_metric_page_state", 2),
        ("during_final_verification", 3),
    ),
)
def test_import_resumes_across_durable_write_and_state_boundaries(
    tmp_path: Path, failpoint: str, expected_metric_count: int
) -> None:
    with _prepared(tmp_path, failpoint=failpoint) as (
        source,
        staging,
        metrics,
        diagnostics,
        inject,
    ):
        with pytest.raises(HistoricalAnalyticalImportError, match="simulated interruption"):
            _importer(source, staging, inject).run()
        state_path = staging.destination / "historical-analytical-import-state.json"
        state = HistoricalAnalyticalImportState.model_validate_json(
            state_path.read_text(encoding="utf-8")
        )
        assert state.metric_count <= len(metrics)
        assert staging.historical_analytical_archive().count_metrics() == expected_metric_count

        recovered = _importer(source, staging).run()

        assert recovered.complete is True
        assert recovered.metric_count == len(metrics)
        assert recovered.diagnostic_count == len(diagnostics)
        assert staging.historical_analytical_archive().verify_complete().complete is True


@pytest.mark.parametrize(
    ("failpoint", "expected_diagnostic_count"),
    (
        ("before_diagnostic_page_write", 0),
        ("after_diagnostic_page_commit_before_state", 1),
        ("after_diagnostic_page_state", 1),
    ),
)
def test_diagnostic_phase_resumes_across_durable_write_and_state_boundaries(
    tmp_path: Path, failpoint: str, expected_diagnostic_count: int
) -> None:
    with _prepared(tmp_path, failpoint=failpoint) as (
        source,
        staging,
        _metrics,
        diagnostics,
        inject,
    ):
        with pytest.raises(HistoricalAnalyticalImportError, match="simulated interruption"):
            _importer(source, staging, inject).run()
        archive = staging.historical_analytical_archive()
        assert archive.count_diagnostics() == expected_diagnostic_count

        resumed = _importer(source, staging).run()

        assert resumed.complete is True
        assert resumed.diagnostic_count == len(diagnostics)
        assert archive.verify_complete().complete is True


def test_import_rejects_a_raw_checkpoint_that_only_looks_complete(tmp_path: Path) -> None:
    with _prepared(tmp_path) as (source, staging, _, _, _):
        staging._connection.execute("DELETE FROM raw_v2_index")
        raw_state = HistoricalAnalyticalImporter(
            source,
            staging,
            page_limit=2,
        )

        with pytest.raises((RawV2ImportError, HistoricalAnalyticalImportError)):
            raw_state.run()
