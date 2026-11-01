"""Atomic predictive dataset export, reuse and corruption rejection."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import duckdb
import pytest
from pydantic import ValidationError

from investment_analyst.analytics.metric_identity_v2 import metric_result_id_from_model_v2
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models import (
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.core.models.enums import DataFrequency, DataQuality
from investment_analyst.research import predictive_export
from investment_analyst.research.predictive_dataset import PredictiveDatasetBuilder
from investment_analyst.research.predictive_export import (
    MANIFEST_FILE_NAME,
    ROWS_FILE_NAME,
    PredictiveExportConflictError,
    PredictiveExportError,
    PredictiveExportPathError,
    export_predictive_dataset,
    read_predictive_export,
)
from investment_analyst.research.predictive_models import (
    PREDICTIVE_ROWS_SCHEMA,
    PredictiveDataset,
    PredictiveDatasetRequest,
    bar_start_instant,
    decision_instant,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_ASSET = "crypto:btc-usd"
_SOURCE = "coinbase-exchange:btc-usd:daily-candles"
_START = date(2026, 8, 3)
_END = date(2026, 8, 7)
_KNOWN_AT = datetime(2026, 8, 12, 3, 0, tzinfo=UTC)
_MISSING_DAY = date(2026, 8, 5)


def _raw(day: date, value: Decimal) -> RawRecord:
    available = bar_start_instant(day) + timedelta(days=1, hours=1)
    return RawRecord(
        record_id=uuid5(NAMESPACE_URL, f"export-raw|{day.isoformat()}"),
        asset_id=_ASSET,
        source=SourceReference(source_id=_SOURCE, retrieved_at=available),
        event_time=bar_start_instant(day),
        available_at=available,
        received_at=available,
        payload={"close": str(value)},
        schema_version="predictive-export-v1",
    )


def _observation(raw: RawRecord, day: date, value: Decimal) -> NormalizedObservation:
    available = bar_start_instant(day) + timedelta(days=1, hours=1)
    return NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"{raw.record_id}|close"),
        raw_record_id=raw.record_id,
        asset_id=_ASSET,
        field_name="close",
        value=value,
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=bar_start_instant(day),
        period_start=bar_start_instant(day),
        period_end=bar_start_instant(day) + timedelta(days=1),
        available_at=available,
        normalized_at=available,
        source=SourceReference(source_id=_SOURCE, retrieved_at=available),
        quality=DataQuality.VALID,
        transformation_version="coinbase-candle-normalizer-v1",
    )


def _metric(
    *,
    day: date,
    key: str,
    value: Decimal,
    available_at: datetime,
    input_observation_id: UUID,
    version: str,
    unit: str,
    parameters: dict[str, object],
) -> MetricResult:
    placeholder = MetricResult(
        result_id=uuid5(NAMESPACE_URL, f"export-metric|{key}|{day.isoformat()}|{value}"),
        asset_id=_ASSET,
        metric_key=key,
        value=value,
        unit=unit,
        as_of=bar_start_instant(day),
        available_at=available_at,
        computed_at=available_at,
        parameters={"source_id": _SOURCE, **parameters},
        input_observation_ids=[input_observation_id],
        algorithm_version=version,
        quality=DataQuality.VALID,
    )
    return placeholder.model_copy(update={"result_id": metric_result_id_from_model_v2(placeholder)})


def _dataset(tmp_path: Path) -> tuple[PredictiveDataset, Path]:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialization = service.initialize(tmp_path / "workspace")
    writer = service.open_storage(
        initialization.paths,
        WorkspaceAccessMode.READ_WRITE,
    )
    observations: dict[date, NormalizedObservation] = {}
    try:
        day = _START - timedelta(days=2)
        while day <= _END + timedelta(days=5):
            if day != _MISSING_DAY:
                raw = _raw(day, Decimal(100 + (day - _START).days))
                observation = _observation(raw, day, Decimal(100 + (day - _START).days))
                writer.raw_records.save(raw)
                writer.observations.save(observation)
                observations[day] = observation
            day += timedelta(days=1)
        decision = _START
        while decision <= _END:
            period = decision - timedelta(days=1)
            observation = observations.get(period)
            if observation is not None:
                available = decision_instant(decision) - timedelta(hours=11)
                writer.metric_results.save_many(
                    [
                        _metric(
                            day=period,
                            key="market.technical.rsi",
                            value=Decimal("55"),
                            available_at=available,
                            input_observation_id=observation.observation_id,
                            version="market-rsi-incremental-v2-decimal34",
                            unit="index",
                            parameters={"window": 14},
                        ),
                        _metric(
                            day=period,
                            key="market.technical.macd.histogram",
                            value=Decimal("2"),
                            available_at=available,
                            input_observation_id=observation.observation_id,
                            version="market-macd-incremental-v2-decimal34",
                            unit="USD",
                            parameters={
                                "fast_window": 12,
                                "slow_window": 26,
                                "signal_window": 9,
                            },
                        ),
                    ]
                )
            decision += timedelta(days=1)
    finally:
        writer.close()

    runtime = ApplicationRuntime.create_default(workspace_service=service)
    request = PredictiveDatasetRequest(
        asset_id=_ASSET,
        start_date=_START,
        end_date=_END,
        dataset_known_at=_KNOWN_AT,
    )
    with runtime.open_storage(
        StorageLocationRequest(workspace=initialization.paths.root),
        access_mode=WorkspaceAccessMode.READ_ONLY,
    ) as storage:
        dataset = PredictiveDatasetBuilder(
            storage,
            provider_resolver=runtime.provider_resolver,
            catalog=runtime.catalog,
        ).build(request)
    return dataset, initialization.paths.root


def _tree_state(root: Path) -> dict[str, tuple[str, int]]:
    return {
        str(path.relative_to(root)): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_export_seals_manifest_and_reads_back_exact_rows(tmp_path: Path) -> None:
    dataset, workspace = _dataset(tmp_path)
    output = tmp_path / "research"
    exported_at = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    receipt = export_predictive_dataset(
        dataset,
        output_dir=output,
        workspace_root=workspace,
        exported_at=exported_at,
    )
    assert receipt.status == "exported"
    assert receipt.row_count == len(dataset.samples)
    dataset_dir = Path(receipt.dataset_dir)
    assert dataset_dir == output / str(dataset.dataset_id)
    assert (dataset_dir / ROWS_FILE_NAME).is_file()
    assert (dataset_dir / MANIFEST_FILE_NAME).is_file()

    manifest = read_predictive_export(dataset_dir)
    assert manifest.schema_version == "predictive-dataset-manifest-v1"
    assert manifest.dataset.dataset_id == dataset.dataset_id
    assert manifest.dataset.dataset_content_sha256 == dataset.dataset_content_sha256
    assert manifest.artifact.exported_at == exported_at
    assert manifest.artifact.rows_sha256 == receipt.rows_sha256
    assert manifest.artifact.rows_bytes == receipt.rows_bytes
    assert tuple(item.name for item in manifest.dataset.artifact_schema.columns) == tuple(
        item.name for item in PREDICTIVE_ROWS_SCHEMA
    )
    assert manifest.dataset.artifact_schema.feature_encoding == "canonical-decimal-text"
    assert manifest.dataset.counts == dataset.summary.counts

    connection = duckdb.connect(":memory:")
    try:
        rows = connection.execute(
            "SELECT sample_key, row_id, decision_at, close_return_1d, label, return_5d,"
            " feature_state, label_state FROM read_parquet(?) ORDER BY decision_at",
            [str(dataset_dir / ROWS_FILE_NAME)],
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == len(dataset.samples)
    ordered = tuple(dataset.samples)
    for row, sample in zip(rows, ordered, strict=True):
        assert row[0] == str(sample.sample_key)
        assert row[1] == str(sample.row_id)
        expected_return = (
            None if sample.features[0].value is None else str(sample.features[0].value)
        )
        assert row[3] == expected_return
        assert row[4] == sample.label.label
        expected_label_return = (
            None if sample.label.return_5d is None else str(sample.label.return_5d)
        )
        assert row[5] == expected_label_return
        assert row[6] == sample.feature_state.value
        assert row[7] == sample.label_state.value
    assert any(sample.label.label is None for sample in dataset.samples)
    assert any(sample.label.label == 1 for sample in dataset.samples)
    assert any(sample.label_state.value == "not_yet_available" for sample in dataset.samples)


def test_repeated_export_reuses_without_rewriting(tmp_path: Path) -> None:
    dataset, workspace = _dataset(tmp_path)
    output = tmp_path / "research"
    first = export_predictive_dataset(
        dataset,
        output_dir=output,
        workspace_root=workspace,
        exported_at=datetime(2026, 10, 31, 12, 0, tzinfo=UTC),
    )
    dataset_dir = Path(first.dataset_dir)
    before = {
        name: (dataset_dir / name).read_bytes() for name in (ROWS_FILE_NAME, MANIFEST_FILE_NAME)
    }
    second = export_predictive_dataset(
        dataset,
        output_dir=output,
        workspace_root=workspace,
        exported_at=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
    )
    after = {
        name: (dataset_dir / name).read_bytes() for name in (ROWS_FILE_NAME, MANIFEST_FILE_NAME)
    }
    assert second.status == "reused"
    assert second.dataset_id == first.dataset_id
    assert second.rows_sha256 == first.rows_sha256
    assert second.manifest_sha256 == first.manifest_sha256
    assert before == after


def test_corrupt_or_incomplete_artifacts_are_rejected(tmp_path: Path) -> None:
    dataset, workspace = _dataset(tmp_path)
    output = tmp_path / "research"
    receipt = export_predictive_dataset(
        dataset,
        output_dir=output,
        workspace_root=workspace,
    )
    dataset_dir = Path(receipt.dataset_dir)
    rows_path = dataset_dir / ROWS_FILE_NAME
    original = rows_path.read_bytes()
    rows_path.write_bytes(original + b"corruption")
    with pytest.raises(PredictiveExportConflictError, match="differs from its manifest"):
        read_predictive_export(dataset_dir)
    with pytest.raises(PredictiveExportConflictError):
        export_predictive_dataset(dataset, output_dir=output, workspace_root=workspace)
    assert rows_path.read_bytes() == original + b"corruption"

    rows_path.write_bytes(original)
    (dataset_dir / MANIFEST_FILE_NAME).unlink()
    with pytest.raises(PredictiveExportConflictError, match="unreadable"):
        read_predictive_export(dataset_dir)
    with pytest.raises(PredictiveExportConflictError):
        export_predictive_dataset(dataset, output_dir=output, workspace_root=workspace)

    (dataset_dir / MANIFEST_FILE_NAME).write_text("{}", encoding="utf-8")
    with pytest.raises(PredictiveExportConflictError, match="malformed"):
        read_predictive_export(dataset_dir)


def test_output_paths_outside_the_workspace_are_enforced(tmp_path: Path) -> None:
    dataset, workspace = _dataset(tmp_path)
    with pytest.raises(PredictiveExportPathError, match="absolute"):
        export_predictive_dataset(
            dataset,
            output_dir=Path("relative-research"),
            workspace_root=workspace,
        )
    with pytest.raises(PredictiveExportPathError, match="outside"):
        export_predictive_dataset(
            dataset,
            output_dir=workspace / "exports",
            workspace_root=workspace,
        )
    link = tmp_path / "workspace-link"
    link.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(PredictiveExportPathError, match="outside"):
        export_predictive_dataset(
            dataset,
            output_dir=link / "research",
            workspace_root=workspace,
        )


def test_export_is_independent_from_the_current_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, workspace = _dataset(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    receipt = export_predictive_dataset(
        dataset,
        output_dir=(tmp_path / "research").resolve(),
        workspace_root=workspace,
    )
    assert receipt.status == "exported"
    assert Path(receipt.dataset_dir).is_dir()


def test_interruption_before_sealing_leaves_no_partial_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, workspace = _dataset(tmp_path)
    output = tmp_path / "research"

    def interrupted(path: Path, rows: tuple[tuple[object, ...], ...]) -> None:
        path.write_bytes(b"partial")
        raise RuntimeError("interrupted before sealing")

    monkeypatch.setattr(predictive_export, "_write_parquet", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        export_predictive_dataset(
            dataset,
            output_dir=output,
            workspace_root=workspace,
        )
    assert not (output / str(dataset.dataset_id)).exists()
    assert not list(output.glob(".*.tmp-*"))


def test_artifact_budget_stops_export_without_partial_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, workspace = _dataset(tmp_path)
    output = tmp_path / "research"
    monkeypatch.setattr(predictive_export, "MAX_ARTIFACT_BYTES", 16)
    with pytest.raises(PredictiveExportError, match="byte budget"):
        export_predictive_dataset(
            dataset,
            output_dir=output,
            workspace_root=workspace,
        )
    assert not (output / str(dataset.dataset_id)).exists()
    assert not list(output.glob(".*.tmp-*"))


def test_export_does_not_write_the_workspace_or_mutate_the_dataset(
    tmp_path: Path,
) -> None:
    dataset, workspace = _dataset(tmp_path)
    before = _tree_state(workspace)
    export_predictive_dataset(
        dataset,
        output_dir=tmp_path / "research",
        workspace_root=workspace,
    )
    assert _tree_state(workspace) == before
    with pytest.raises(ValidationError):
        dataset.dataset_id = uuid5(NAMESPACE_URL, "mutated")  # type: ignore[misc]
