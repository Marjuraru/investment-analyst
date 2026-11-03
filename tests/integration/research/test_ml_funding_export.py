"""Sealed funding artifacts: CLI, reuse, corruption, caps and the core-only reader."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from scripts.evaluate_ml_funding import main as cli_main

from investment_analyst.core.models.enums import AssetClass
from investment_analyst.research import ml_funding_evaluation
from investment_analyst.research.funding_archive import (
    _page_bytes,
    _sha256_bytes,
    build_funding_archive,
)
from investment_analyst.research.funding_export import (
    export_funding_bundle,
    load_base_dataset,
)
from investment_analyst.research.funding_features import build_funding_bundle
from investment_analyst.research.funding_models import (
    FundingArchivePage,
    FundingAssetIdentity,
    FundingPoint,
    assumed_funding_available_instant,
    funding_archive_request,
    funding_point_id,
)
from investment_analyst.research.historical_archive import build_historical_archive
from investment_analyst.research.historical_dataset import build_historical_dataset
from investment_analyst.research.historical_export import export_historical_dataset
from investment_analyst.research.historical_models import (
    HistoricalArchiveRequest,
    HistoricalAssetIdentity,
    HistoricalCandle,
    HistoricalDecisionRequest,
    assumed_available_instant,
    bar_start_instant,
    historical_bar_content_id,
)
from investment_analyst.research.ml_funding_export import (
    FundingMlExportConflictError,
    FundingMlExportPathError,
    evaluate_funding_experiment,
    read_funding_ml_artifact,
)
from investment_analyst.research.ml_funding_models import (
    MANIFEST_FILE_NAME,
    PREDICTIONS_FILE_NAME,
    PROTOCOL_FILE_NAME,
    REPORT_FILE_NAME,
    FundingArtifactStatus,
    FundingIncrementDecision,
    FundingRunStatus,
)

ROOT = Path(__file__).resolve().parents[3]
_ASSET_ID = "crypto:btc-usd"
_FUNDING_SOURCE = "deribit:btc-perpetual:funding-rate-history"
_SPOT_SOURCE = "coinbase-exchange:btc-usd:daily-candles"
_DECISION_START = date(2020, 1, 1)
_ROWS = 1200
_DECISION_END = _DECISION_START + timedelta(days=_ROWS - 1)
_HISTORICAL_RETRIEVED = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
_HISTORICAL_KNOWN = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
_FUNDING_RETRIEVED = datetime(2026, 11, 1, 0, 5, tzinfo=UTC)
_KNOWN = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)


def _historical_asset() -> HistoricalAssetIdentity:
    return HistoricalAssetIdentity(
        asset_id=_ASSET_ID,
        source_id=_SPOT_SOURCE,
        product_id="BTC-USD",
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        crypto_profile="bitcoin",
        exchange="COINBASE",
        granularity_seconds=86_400,
    )


def _price(offset: int) -> Decimal:
    return Decimal(100) + Decimal(offset % 11 - 5) + Decimal(offset % 3) * Decimal("0.5")


def _candle(day: date, close: Decimal) -> HistoricalCandle:
    start = bar_start_instant(day)
    end = start + timedelta(days=1)
    raw_values = (
        str(int(start.timestamp())),
        str(close),
        str(close + 1),
        str(close - 1),
        str(close),
        "10",
    )
    return HistoricalCandle(
        bar_content_id=historical_bar_content_id(
            asset_id=_ASSET_ID,
            source_id=_SPOT_SOURCE,
            bar_start=start,
            open_value=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=Decimal("10"),
            raw_values=raw_values,
        ),
        bar_start=start,
        bar_end=end,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=Decimal("10"),
        raw_values=raw_values,
        assumed_available_at=assumed_available_instant(end),
        retrieved_at=_HISTORICAL_RETRIEVED,
    )


def _build_base_dataset(root: Path) -> Path:
    archive_start = _DECISION_START - timedelta(days=40)
    archive_end = _DECISION_END + timedelta(days=6)
    archive_request = HistoricalArchiveRequest(
        asset_id=_ASSET_ID,
        start_date=archive_start,
        end_date=archive_end,
    )
    days = (archive_end - archive_start).days + 1
    candles = tuple(
        _candle(archive_start + timedelta(days=offset), _price(offset)) for offset in range(days)
    )
    archive = build_historical_archive(
        _historical_asset(),
        archive_request,
        request_urls=("https://example.invalid/candles",),
        retrieved_at=_HISTORICAL_RETRIEVED,
        candles=candles,
    )
    request = HistoricalDecisionRequest(
        asset_id=_ASSET_ID,
        start_date=_DECISION_START,
        end_date=_DECISION_END,
        known_at=_HISTORICAL_KNOWN,
    )
    dataset = build_historical_dataset(request, archive)
    receipt = export_historical_dataset(
        dataset,
        archive=archive,
        output_dir=root / "base",
        forbidden_roots=(),
    )
    return Path(receipt.dataset_dir)


def _funding_asset() -> FundingAssetIdentity:
    return FundingAssetIdentity(
        asset_id=_ASSET_ID,
        funding_source_id=_FUNDING_SOURCE,
        currency="BTC",
        instrument_name="BTC-PERPETUAL",
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        crypto_profile="bitcoin",
    )


def _raw(timestamp: datetime) -> dict[str, str]:
    return {
        "timestamp": str(int(timestamp.timestamp() * 1_000)),
        "index_price": "100.5",
        "prev_index_price": "100",
        "interest_1h": "0.000001",
        "interest_8h": "0.000008",
    }


def _point(timestamp: datetime, page_index: int) -> FundingPoint:
    raw = _raw(timestamp)
    return FundingPoint(
        point_id=funding_point_id(
            funding_source_id=_FUNDING_SOURCE,
            timestamp=timestamp,
            index_price=Decimal(raw["index_price"]),
            prev_index_price=Decimal(raw["prev_index_price"]),
            interest_1h=Decimal(raw["interest_1h"]),
            interest_8h=Decimal(raw["interest_8h"]),
            raw_payload=raw,
        ),
        page_index=page_index,
        timestamp=timestamp,
        index_price=Decimal(raw["index_price"]),
        prev_index_price=Decimal(raw["prev_index_price"]),
        interest_1h=Decimal(raw["interest_1h"]),
        interest_8h=Decimal(raw["interest_8h"]),
        raw_payload=raw,
        assumed_available_at=assumed_funding_available_instant(timestamp),
    )


def _funding_archive(base_dataset_dir: Path):
    loaded = load_base_dataset(base_dataset_dir)
    request = funding_archive_request(
        asset_id=_ASSET_ID,
        decision_start_date=loaded.reference.start_date,
        decision_end_date=loaded.reference.end_date,
    )
    pages = []
    points = []
    cursor = request.requested_start
    page_index = 0
    while cursor < request.requested_end:
        page_end = min(cursor + timedelta(days=31), request.requested_end)
        page_points = tuple(
            _point(cursor + timedelta(hours=offset), page_index)
            for offset in range(int((page_end - cursor).total_seconds() // 3_600))
        )
        payload = _page_bytes(page_points)
        pages.append(
            FundingArchivePage(
                page_index=page_index,
                requested_start=cursor,
                requested_end=page_end,
                request_url=f"https://example.invalid/funding/{page_index}",
                started_at=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
                finished_at=datetime(2026, 11, 1, 0, 10, tzinfo=UTC),
                elapsed_milliseconds=10_000,
                retrieved_at=_FUNDING_RETRIEVED,
                point_count=len(page_points),
                points_sha256=_sha256_bytes(payload),
                points_bytes=len(payload),
            )
        )
        points.extend(page_points)
        cursor = page_end
        page_index += 1
    return build_funding_archive(
        asset=_funding_asset(),
        request=request,
        pages=tuple(pages),
        points=tuple(points),
        retrieved_at=_FUNDING_RETRIEVED,
    )


@dataclass(frozen=True, slots=True)
class _Inputs:
    dataset_dir: Path
    bundle_dir: Path


@pytest.fixture(scope="module")
def inputs(tmp_path_factory: pytest.TempPathFactory) -> _Inputs:
    root = tmp_path_factory.mktemp("ml-funding-inputs")
    base_dir = _build_base_dataset(root)
    loaded = load_base_dataset(base_dir)
    archive = _funding_archive(base_dir)
    bundle = build_funding_bundle(
        base_dataset=loaded.reference,
        decisions=loaded.decisions,
        funding_asset=archive.manifest.asset,
        archive=archive,
        known_at=_KNOWN,
    )
    receipt = export_funding_bundle(
        bundle,
        archive=archive,
        output_dir=root / "bundles",
        forbidden_roots=(root / "base",),
        exported_at=_KNOWN,
    )
    return _Inputs(dataset_dir=base_dir, bundle_dir=Path(receipt.bundle_dir))


@dataclass(frozen=True, slots=True)
class _Run:
    output_dir: Path
    artifact_dir: Path
    protocol_visible_before_fit: tuple[bool, ...]
    prediction_rows: int
    evaluation_decisions: int


@pytest.fixture(scope="module")
def run(inputs: _Inputs, tmp_path_factory: pytest.TempPathFactory) -> _Run:
    output = tmp_path_factory.mktemp("ml-funding-artifacts")
    observed: list[bool] = []
    original = ml_funding_evaluation.fit_and_predict
    monkeypatch = pytest.MonkeyPatch()

    def wrapper(**kwargs: object) -> object:
        observed.append(any(output.glob(f"*/{PROTOCOL_FILE_NAME}")))
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ml_funding_evaluation, "fit_and_predict", wrapper)
    try:
        receipt = evaluate_funding_experiment(
            dataset_dir=inputs.dataset_dir,
            bundle_dir=inputs.bundle_dir,
            output_dir=output,
        )
    finally:
        monkeypatch.undo()
    assert receipt.status is FundingRunStatus.EVALUATED
    assert receipt.report_status is FundingArtifactStatus.COMPLETED, receipt
    loaded = read_funding_ml_artifact(Path(receipt.artifact_dir))
    decisions = sum(fold.evaluation_rows for fold in loaded.report.folds)
    return _Run(
        output_dir=output,
        artifact_dir=Path(receipt.artifact_dir),
        protocol_visible_before_fit=tuple(observed),
        prediction_rows=receipt.prediction_rows,
        evaluation_decisions=decisions,
    )


def _files(directory: Path) -> set[str]:
    return {path.name for path in directory.iterdir() if path.is_file()}


def test_the_protocol_is_sealed_before_every_fit_and_the_holdout_stays_closed(run: _Run) -> None:
    assert len(run.protocol_visible_before_fit) == 6
    assert all(run.protocol_visible_before_fit)
    loaded = read_funding_ml_artifact(run.artifact_dir)
    assert loaded.manifest is not None
    assert loaded.report.status is FundingArtifactStatus.COMPLETED
    assert loaded.report.development_fits == 6
    assert loaded.report.holdout.accesses == 0
    assert loaded.report.holdout.status.value == "not_evaluated"
    assert loaded.report.workspace_opened is False
    assert loaded.report.provider_requests == 0
    assert loaded.report.prediction_rows == run.prediction_rows
    assert run.prediction_rows == 4 * run.evaluation_decisions
    assert loaded.report.decision in (
        FundingIncrementDecision.INCREMENTAL_EVIDENCE_OBSERVED,
        FundingIncrementDecision.NO_DEMONSTRATED_INCREMENT,
    )
    assert _files(run.artifact_dir) == {
        PROTOCOL_FILE_NAME,
        PREDICTIONS_FILE_NAME,
        REPORT_FILE_NAME,
        MANIFEST_FILE_NAME,
    }


def test_an_identical_run_is_reused_without_any_fit(
    inputs: _Inputs, run: _Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = {name: (run.artifact_dir / name).read_bytes() for name in _files(run.artifact_dir)}

    def forbidden(**kwargs: object) -> object:
        raise AssertionError("a reused run must not fit a model")

    monkeypatch.setattr(ml_funding_evaluation, "fit_and_predict", forbidden)
    receipt = evaluate_funding_experiment(
        dataset_dir=inputs.dataset_dir,
        bundle_dir=inputs.bundle_dir,
        output_dir=run.output_dir,
    )
    assert receipt.status is FundingRunStatus.REUSED
    after = {name: (run.artifact_dir / name).read_bytes() for name in _files(run.artifact_dir)}
    assert after == before


def test_a_non_evaluable_run_is_recorded_without_a_manifest(
    inputs: _Inputs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def insufficient(samples: object, protocol: object) -> object:
        raise ml_funding_evaluation.FundingInsufficientSampleError("fold 1 below the floor")

    monkeypatch.setattr(ml_funding_evaluation, "evaluate_funding_development", insufficient)
    output = tmp_path / "insufficient"
    receipt = evaluate_funding_experiment(
        dataset_dir=inputs.dataset_dir,
        bundle_dir=inputs.bundle_dir,
        output_dir=output,
    )
    assert receipt.report_status is FundingArtifactStatus.INSUFFICIENT_SAMPLE
    assert receipt.manifest_sha256 is None
    artifact = Path(receipt.artifact_dir)
    assert _files(artifact) == {PROTOCOL_FILE_NAME, REPORT_FILE_NAME}
    loaded = read_funding_ml_artifact(artifact)
    assert loaded.manifest is None
    assert loaded.report.decision is None
    assert loaded.report.disposition_reason == "fold 1 below the floor"


def test_reader_rejects_corrupted_predictions_protocol_and_manifest(
    run: _Run, tmp_path: Path
) -> None:
    corrupted = tmp_path / "corrupted"
    shutil.copytree(run.artifact_dir, corrupted)
    predictions = corrupted / PREDICTIONS_FILE_NAME
    payload = bytearray(predictions.read_bytes())
    payload[100] ^= 0xFF
    predictions.write_bytes(payload)
    with pytest.raises(FundingMlExportConflictError):
        read_funding_ml_artifact(corrupted)

    non_canonical = tmp_path / "non-canonical"
    shutil.copytree(run.artifact_dir, non_canonical)
    manifest_path = non_canonical / MANIFEST_FILE_NAME
    manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
    with pytest.raises(FundingMlExportConflictError):
        read_funding_ml_artifact(non_canonical)

    wrong_protocol = tmp_path / "wrong-protocol"
    shutil.copytree(run.artifact_dir, wrong_protocol)
    protocol_path = wrong_protocol / PROTOCOL_FILE_NAME
    protocol_path.write_bytes(protocol_path.read_bytes() + b"\n")
    with pytest.raises(FundingMlExportConflictError):
        read_funding_ml_artifact(wrong_protocol)


def test_a_foreign_sealed_protocol_fails_without_rewriting(
    inputs: _Inputs, run: _Run, tmp_path: Path
) -> None:
    output = tmp_path / "resume"
    output.mkdir()
    target = output / run.artifact_dir.name
    shutil.copytree(run.artifact_dir, target)
    (target / MANIFEST_FILE_NAME).unlink()
    (target / REPORT_FILE_NAME).unlink()
    protocol_path = target / PROTOCOL_FILE_NAME
    protocol_path.write_bytes(protocol_path.read_bytes() + b"\n")
    before = protocol_path.read_bytes()
    with pytest.raises(FundingMlExportConflictError, match="differs"):
        evaluate_funding_experiment(
            dataset_dir=inputs.dataset_dir,
            bundle_dir=inputs.bundle_dir,
            output_dir=output,
        )
    assert protocol_path.read_bytes() == before


def test_output_paths_fail_closed(inputs: _Inputs, tmp_path: Path) -> None:
    with pytest.raises(FundingMlExportPathError, match="absolute"):
        evaluate_funding_experiment(
            dataset_dir=inputs.dataset_dir,
            bundle_dir=inputs.bundle_dir,
            output_dir=Path("relative-output"),
        )
    with pytest.raises(FundingMlExportPathError, match="protected root"):
        evaluate_funding_experiment(
            dataset_dir=inputs.dataset_dir,
            bundle_dir=inputs.bundle_dir,
            output_dir=ROOT / "ml-funding-artifacts",
        )
    with pytest.raises(FundingMlExportPathError, match="overlap"):
        evaluate_funding_experiment(
            dataset_dir=inputs.dataset_dir,
            bundle_dir=inputs.bundle_dir,
            output_dir=inputs.dataset_dir / "artifacts",
        )
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(FundingMlExportPathError, match="symbolic link"):
        evaluate_funding_experiment(
            dataset_dir=inputs.dataset_dir,
            bundle_dir=inputs.bundle_dir,
            output_dir=link / "artifacts",
        )


def test_foreign_output_entries_are_untouched(inputs: _Inputs, run: _Run) -> None:
    foreign = run.output_dir / "foreign"
    foreign.mkdir(exist_ok=True)
    for index in range(5):
        (foreign / f"foreign-{index}.bin").write_bytes(b"x" * 16)
    before = {path.name: path.read_bytes() for path in foreign.iterdir() if path.is_file()}
    receipt = evaluate_funding_experiment(
        dataset_dir=inputs.dataset_dir,
        bundle_dir=inputs.bundle_dir,
        output_dir=run.output_dir,
    )
    assert receipt.status is FundingRunStatus.REUSED
    after = {path.name: path.read_bytes() for path in foreign.iterdir() if path.is_file()}
    assert after == before
    assert not list(run.output_dir.rglob("*.duckdb"))
    assert not list(run.output_dir.rglob("*.wal"))


def test_cli_evaluate_and_read_are_stable(
    inputs: _Inputs, run: _Run, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli_main(
        [
            "evaluate",
            "--dataset-dir",
            str(inputs.dataset_dir),
            "--funding-bundle-dir",
            str(inputs.bundle_dir),
            "--output-dir",
            str(run.output_dir),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "reused"
    assert payload["holdout_accesses"] == 0
    assert payload["prediction_rows"] == run.prediction_rows

    exit_code = cli_main(["read", "--artifact-dir", str(run.artifact_dir), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["status"] == "completed"
    assert report["holdout"]["accesses"] == 0
    assert len(report["pooled_comparisons"]) == 3

    exit_code = cli_main(["read", "--artifact-dir", str(run.artifact_dir)])
    human = capsys.readouterr().out
    assert exit_code == 0
    assert "holdout=not_evaluated accesses=0" in human
    assert "comparison=training_class_prior" in human


def test_reader_and_help_work_without_ml_dependencies(run: _Run) -> None:
    blocker = (
        "import importlib.abc, sys\n"
        "class Blocker(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in {'sklearn','numpy','scipy','threadpoolctl'}:\n"
        "            raise ImportError('blocked ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
    )
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    reader = blocker + (
        "from pathlib import Path\n"
        "from investment_analyst.research.ml_funding_export import read_funding_ml_artifact\n"
        "loaded = read_funding_ml_artifact(Path(sys.argv[1]))\n"
        "print(loaded.report.run_id, loaded.report.status.value)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", reader, str(run.artifact_dir)],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split()[1] == "completed"
    helper = blocker + (
        "import runpy, sys\n"
        "sys.argv = ['evaluate_ml_funding.py', '--help']\n"
        f"runpy.run_path({str(ROOT / 'scripts' / 'evaluate_ml_funding.py')!r},"
        " run_name='__main__')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", helper],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage: evaluate_ml_funding.py" in completed.stdout
