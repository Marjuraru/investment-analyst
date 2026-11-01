"""Atomic export, readback, offline consumption and CLI of the reconstructed dataset."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from scripts.build_historical_research_dataset import main as cli_main

from investment_analyst.providers.http import UrlLibHttpTransport
from investment_analyst.research import historical_archive as archive_module
from investment_analyst.research import historical_export as export_module
from investment_analyst.research import historical_features, historical_models
from investment_analyst.research.historical_archive import (
    build_historical_archive,
    read_historical_archive,
    resolve_historical_scope,
    seal_historical_archive,
)
from investment_analyst.research.historical_dataset import build_historical_dataset
from investment_analyst.research.historical_export import (
    MANIFEST_FILE_NAME,
    ROWS_FILE_NAME,
    HistoricalExportConflictError,
    HistoricalExportPathError,
    export_historical_dataset,
    read_historical_dataset_view,
    read_historical_export,
)
from investment_analyst.research.historical_models import (
    HistoricalArchiveRequest,
    HistoricalCandle,
    HistoricalDecisionRequest,
    assumed_available_instant,
    bar_start_instant,
    historical_bar_content_id,
)

_START = date(2019, 1, 1)
_RETRIEVED = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
_ASSET = "crypto:btc-usd"


def _price(index: int) -> Decimal:
    return (
        Decimal(100) + Decimal(index) * Decimal("0.37") + Decimal((index % 5) - 2) * Decimal("1.11")
    )


def _candle(day: date, price: Decimal, *, source_id: str) -> HistoricalCandle:
    start = bar_start_instant(day)
    end = start + timedelta(days=1)
    raw = (str(int(start.timestamp())), str(price), str(price + 1), str(price), str(price), "4.5")
    return HistoricalCandle(
        bar_content_id=historical_bar_content_id(
            asset_id=_ASSET,
            source_id=source_id,
            bar_start=start,
            open_value=price,
            high=price + 1,
            low=price - 1,
            close=price,
            volume=Decimal("4.5"),
            raw_values=raw,
        ),
        bar_start=start,
        bar_end=end,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=Decimal("4.5"),
        raw_values=raw,
        assumed_available_at=assumed_available_instant(end),
        retrieved_at=_RETRIEVED,
    )


def _sealed_archive(root: Path, *, days: int = 120):
    scope = resolve_historical_scope(_ASSET).asset
    candles = tuple(
        _candle(_START + timedelta(days=index), _price(index), source_id=scope.source_id)
        for index in range(days)
    )
    request = HistoricalArchiveRequest(
        asset_id=_ASSET,
        start_date=candles[0].bar_start.date(),
        end_date=candles[-1].bar_start.date(),
    )
    archive = build_historical_archive(
        scope,
        request,
        request_urls=("https://api.exchange.coinbase.com/products/BTC-USD/candles",),
        retrieved_at=_RETRIEVED,
        candles=candles,
    )
    seal_historical_archive(archive, output_dir=root)
    return read_historical_archive(root / str(archive.archive_id))


def _dataset(archive, *, start_offset: int = 40, days: int = 40):
    start = _START + timedelta(days=start_offset)
    request = HistoricalDecisionRequest(
        asset_id=_ASSET,
        start_date=start,
        end_date=start + timedelta(days=days - 1),
        known_at=_RETRIEVED,
    )
    return build_historical_dataset(request, archive)


def _forbidden(tmp_path: Path) -> tuple[Path, ...]:
    return (tmp_path / "protected",)


def test_export_round_trip_readback_and_offline_feature_separation(tmp_path: Path) -> None:
    archive = _sealed_archive(tmp_path / "archives")
    dataset = _dataset(archive)
    receipt = export_historical_dataset(
        dataset,
        archive=archive,
        output_dir=tmp_path / "export",
        forbidden_roots=_forbidden(tmp_path),
    )
    assert receipt.status == "exported"
    assert receipt.row_count == len(dataset.samples)
    dataset_dir = Path(receipt.dataset_dir)
    assert (dataset_dir / ROWS_FILE_NAME).is_file()
    assert (dataset_dir / MANIFEST_FILE_NAME).is_file()

    manifest = read_historical_export(dataset_dir)
    assert manifest.dataset.dataset_id == dataset.dataset_id
    assert manifest.dataset.strict_pit_verified is False
    assert manifest.archive.reference == (
        f"content-addressed:sha256:{archive.manifest.semantic_sha256}"
    )
    assert manifest.archive.candles_bytes == archive.manifest.candles_bytes

    view = read_historical_dataset_view(dataset_dir)
    assert len(view.features) == len(view.targets) == len(view.metadata) == receipt.row_count
    sample = dataset.samples[0]
    feature_row = next(row for row in view.features if row.sample_key == str(sample.sample_key))
    assert feature_row.close_return_1d == sample.features[0].value
    assert feature_row.rsi_14_fraction == sample.features[1].value
    assert feature_row.macd_12_26_9_histogram_close_ratio == sample.features[2].value
    target_row = next(row for row in view.targets if row.sample_key == str(sample.sample_key))
    assert target_row.label == sample.label.label
    assert target_row.return_5d == sample.label.return_5d
    metadata_row = next(row for row in view.metadata if row.sample_key == str(sample.sample_key))
    assert metadata_row.split == sample.split.value
    assert metadata_row.eligible is sample.eligible
    assert all(isinstance(row.decision_at, datetime) for row in view.features)


def test_export_is_idempotent_without_rewrite(tmp_path: Path) -> None:
    archive = _sealed_archive(tmp_path / "archives")
    dataset = _dataset(archive)
    first = export_historical_dataset(
        dataset,
        archive=archive,
        output_dir=tmp_path / "export",
        forbidden_roots=_forbidden(tmp_path),
    )
    rows_path = Path(first.dataset_dir) / ROWS_FILE_NAME
    before = rows_path.stat().st_mtime_ns
    second = export_historical_dataset(
        dataset,
        archive=archive,
        output_dir=tmp_path / "export",
        forbidden_roots=_forbidden(tmp_path),
    )
    assert second.status == "reused"
    assert second.rows_sha256 == first.rows_sha256
    assert second.manifest_sha256 == first.manifest_sha256
    assert rows_path.stat().st_mtime_ns == before


def test_interrupted_export_leaves_no_sealed_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _sealed_archive(tmp_path / "archives")
    dataset = _dataset(archive)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(export_module, "_write_parquet", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        export_historical_dataset(
            dataset,
            archive=archive,
            output_dir=tmp_path / "export",
            forbidden_roots=_forbidden(tmp_path),
        )
    root = tmp_path / "export"
    assert not (root / str(dataset.dataset_id)).exists()
    assert [path for path in root.iterdir() if path.name.startswith(".")] == []


def test_corrupted_or_foreign_destination_fails_without_overwrite(tmp_path: Path) -> None:
    archive = _sealed_archive(tmp_path / "archives")
    dataset = _dataset(archive)
    receipt = export_historical_dataset(
        dataset,
        archive=archive,
        output_dir=tmp_path / "export",
        forbidden_roots=_forbidden(tmp_path),
    )
    dataset_dir = Path(receipt.dataset_dir)
    rows_path = dataset_dir / ROWS_FILE_NAME
    original = rows_path.read_bytes()
    rows_path.write_bytes(original[:-1])
    with pytest.raises(HistoricalExportConflictError, match="differs from its manifest"):
        read_historical_export(dataset_dir)
    rows_path.write_bytes(original)
    assert read_historical_export(dataset_dir).dataset.dataset_id == dataset.dataset_id

    other = _dataset(archive, start_offset=80, days=20)
    other_receipt = export_historical_dataset(
        other,
        archive=archive,
        output_dir=tmp_path / "export-other",
        forbidden_roots=_forbidden(tmp_path),
    )
    shutil.rmtree(dataset_dir)
    shutil.copytree(other_receipt.dataset_dir, dataset_dir)
    with pytest.raises(HistoricalExportConflictError, match="different dataset"):
        export_historical_dataset(
            dataset,
            archive=archive,
            output_dir=tmp_path / "export",
            forbidden_roots=_forbidden(tmp_path),
        )


def test_unsafe_destinations_are_rejected_before_writing(tmp_path: Path) -> None:
    archive = _sealed_archive(tmp_path / "archives")
    dataset = _dataset(archive)
    with pytest.raises(HistoricalExportPathError, match="absolute"):
        export_historical_dataset(
            dataset,
            archive=archive,
            output_dir=Path("relative-export"),
            forbidden_roots=_forbidden(tmp_path),
        )
    with pytest.raises(HistoricalExportPathError, match="protected root"):
        export_historical_dataset(
            dataset,
            archive=archive,
            output_dir=tmp_path / "protected" / "export",
            forbidden_roots=_forbidden(tmp_path),
        )
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(HistoricalExportPathError, match="symbolic link"):
        export_historical_dataset(
            dataset,
            archive=archive,
            output_dir=link / "export",
            forbidden_roots=_forbidden(tmp_path),
        )
    assert not (tmp_path / "protected").exists()
    assert not (real / "export").exists()


def test_foreign_corpus_does_not_change_ids_ordering_or_decimals(tmp_path: Path) -> None:
    archive = _sealed_archive(tmp_path / "archives")
    dataset = _dataset(archive)
    receipt = export_historical_dataset(
        dataset,
        archive=archive,
        output_dir=tmp_path / "export",
        forbidden_roots=_forbidden(tmp_path),
    )
    dataset_dir = Path(receipt.dataset_dir)
    baseline = read_historical_dataset_view(dataset_dir)
    foreign_root = tmp_path / "scratch-foreign"
    foreign_root.mkdir()
    for index in range(10):
        shutil.copyfile(
            dataset_dir / ROWS_FILE_NAME,
            foreign_root / f"foreign-{index}.parquet",
        )
    after = read_historical_dataset_view(dataset_dir)
    assert after == baseline
    assert [row.sample_key for row in after.features] == [
        row.sample_key for row in baseline.features
    ]
    assert read_historical_export(dataset_dir).artifact.rows_sha256 == receipt.rows_sha256


def test_prefix_digest_work_is_linear_in_bars(tmp_path: Path) -> None:
    archive = _sealed_archive(tmp_path / "archives")
    calls = {"count": 0}
    original = historical_features.historical_prefix_digest

    def counting(previous, **kwargs):
        calls["count"] += 1
        return original(previous, **kwargs)

    historical_features.historical_prefix_digest = counting
    try:
        historical_features.reconstruct_feature_series(archive.candles)
    finally:
        historical_features.historical_prefix_digest = original
    assert calls["count"] == len(archive.candles)


def test_cli_build_without_output_writes_nothing_and_uses_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = _sealed_archive(tmp_path / "archives")
    archive_dir = tmp_path / "archives" / str(archive.archive_id)

    def forbidden_get(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("CLI build must not use the network")

    monkeypatch.setattr(UrlLibHttpTransport, "get", forbidden_get)
    exit_code = cli_main(
        [
            "build",
            "--archive-dir",
            str(archive_dir),
            "--start-date",
            (_START + timedelta(days=40)).isoformat(),
            "--end-date",
            (_START + timedelta(days=50)).isoformat(),
            "--known-at",
            _RETRIEVED.isoformat(),
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dataset"]["readiness"]["strict_pit_verified"] is False
    assert "export" not in payload
    assert list(tmp_path.iterdir()) == [tmp_path / "archives"]


def test_cli_export_and_reuse_match_the_python_api(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = _sealed_archive(tmp_path / "archives")
    archive_dir = tmp_path / "archives" / str(archive.archive_id)
    arguments = [
        "build",
        "--archive-dir",
        str(archive_dir),
        "--start-date",
        (_START + timedelta(days=40)).isoformat(),
        "--end-date",
        (_START + timedelta(days=60)).isoformat(),
        "--known-at",
        _RETRIEVED.isoformat(),
        "--output-dir",
        str(tmp_path / "export"),
        "--json",
    ]
    assert cli_main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["export"]["status"] == "exported"
    assert cli_main(arguments) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["export"]["status"] == "reused"
    assert second["export"]["rows_sha256"] == first["export"]["rows_sha256"]


def test_cli_rejects_invalid_ranges_with_json_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = _sealed_archive(tmp_path / "archives")
    exit_code = cli_main(
        [
            "build",
            "--archive-dir",
            str(tmp_path / "archives" / str(archive.archive_id)),
            "--start-date",
            (_START + timedelta(days=200)).isoformat(),
            "--end-date",
            (_START + timedelta(days=210)).isoformat(),
            "--known-at",
            _RETRIEVED.isoformat(),
            "--json",
        ]
    )
    assert exit_code == 2
    assert "error" in json.loads(capsys.readouterr().err)
    assert not (tmp_path / "export").exists()


def test_exporter_never_opens_storage_or_workspace() -> None:
    source = inspect_source(export_module)
    assert "investment_analyst.storage" not in source
    assert "investment_analyst.workspace" not in source
    assert "ApplicationRuntime" not in source
    assert "HistoricalArchive" in inspect_source(archive_module)
    assert historical_models.HISTORICAL_ROWS_SCHEMA


def inspect_source(module: object) -> str:
    import inspect

    return inspect.getsource(module)
