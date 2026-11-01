"""Sealed ML artifacts: CLI, reuse, corruption, caps, isolation and core-only reader."""

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
from scripts.evaluate_ml_baselines import main as cli_main

from investment_analyst.research import ml_evaluation, ml_export
from investment_analyst.research.historical_archive import (
    build_historical_archive,
    read_historical_archive,
    resolve_historical_scope,
    seal_historical_archive,
)
from investment_analyst.research.historical_dataset import build_historical_dataset
from investment_analyst.research.historical_export import export_historical_dataset
from investment_analyst.research.historical_models import (
    HistoricalArchiveRequest,
    HistoricalCandle,
    HistoricalDecisionRequest,
    assumed_available_instant,
    bar_start_instant,
    historical_bar_content_id,
)
from investment_analyst.research.ml_evaluation import MlEvaluationOutcome, MlPredictionRow
from investment_analyst.research.ml_export import (
    MlExportCapError,
    MlExportConflictError,
    MlExportPathError,
    evaluate_dataset,
    read_ml_baseline_artifact,
)
from investment_analyst.research.ml_models import (
    MANIFEST_FILE_NAME,
    PREDICTIONS_FILE_NAME,
    PROTOCOL_FILE_NAME,
    REPORT_FILE_NAME,
    SELECTION_FILE_NAME,
    MlBaselineReceipt,
    MlHoldoutStatus,
    MlModelState,
    MlRunStatus,
    MlScorer,
)

ROOT = Path(__file__).resolve().parents[3]
_ASSET = "crypto:btc-usd"
_START = date(2019, 1, 1)
_RETRIEVED = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400"


def _price(index: int) -> Decimal:
    return (
        Decimal(100) + Decimal(index) * Decimal("0.37") + Decimal((index % 7) - 3) * Decimal("1.13")
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


def _sealed_dataset(root: Path) -> Path:
    scope = resolve_historical_scope(_ASSET).asset
    candles = tuple(
        _candle(_START + timedelta(days=index), _price(index), source_id=scope.source_id)
        for index in range(2557)
    )
    request = HistoricalArchiveRequest(
        asset_id=_ASSET,
        start_date=candles[0].bar_start.date(),
        end_date=candles[-1].bar_start.date(),
    )
    archive = build_historical_archive(
        scope,
        request,
        request_urls=(_URL,),
        retrieved_at=_RETRIEVED,
        candles=candles,
    )
    seal_historical_archive(archive, output_dir=root / "archives")
    archive = read_historical_archive(root / "archives" / str(archive.archive_id))
    decision = HistoricalDecisionRequest(
        asset_id=_ASSET,
        start_date=date(2020, 1, 1),
        end_date=date(2025, 12, 25),
        known_at=_RETRIEVED,
    )
    dataset = build_historical_dataset(decision, archive)
    receipt = export_historical_dataset(
        dataset,
        archive=archive,
        output_dir=root / "datasets",
        forbidden_roots=(ROOT,),
    )
    return Path(receipt.dataset_dir)


@dataclass(frozen=True, slots=True)
class _Run:
    output_dir: Path
    receipt: MlBaselineReceipt
    protocol_visible_before_fit: tuple[bool, ...]


@pytest.fixture(scope="module")
def sealed_dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _sealed_dataset(tmp_path_factory.mktemp("ml-dataset"))


@pytest.fixture(scope="module")
def run(sealed_dataset: Path, tmp_path_factory: pytest.TempPathFactory) -> _Run:
    output = tmp_path_factory.mktemp("ml-artifacts")
    monkeypatch = pytest.MonkeyPatch()
    observed: list[bool] = []
    original = ml_evaluation.fit_and_predict

    def wrapper(**kwargs: object) -> object:
        observed.append(any(output.glob(f"*/{PROTOCOL_FILE_NAME}")))
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ml_evaluation, "fit_and_predict", wrapper)
    try:
        receipt = evaluate_dataset(dataset_dir=sealed_dataset, output_dir=output)
    finally:
        monkeypatch.undo()
    return _Run(
        output_dir=output,
        receipt=receipt,
        protocol_visible_before_fit=tuple(observed),
    )


def _files(artifact_dir: Path) -> set[str]:
    return {path.name for path in artifact_dir.iterdir() if path.is_file()}


def test_protocol_is_sealed_before_every_fit(run: _Run) -> None:
    assert run.receipt.status is MlRunStatus.EVALUATED
    assert run.receipt.report_status.value == "completed"
    assert len(run.protocol_visible_before_fit) in (6, 7)
    assert all(run.protocol_visible_before_fit)
    artifact_dir = Path(run.receipt.artifact_dir)
    manifest, report = read_ml_baseline_artifact(artifact_dir)
    assert manifest.protocol.name == PROTOCOL_FILE_NAME
    assert report.development is not None
    assert report.development.development_fits == 6
    assert report.domain_writes == 0
    assert report.workspace_opened is False
    assert report.peak_rss_bytes is None or report.peak_rss_bytes > 0
    expected = {
        PROTOCOL_FILE_NAME,
        PREDICTIONS_FILE_NAME,
        REPORT_FILE_NAME,
        MANIFEST_FILE_NAME,
    }
    if report.development.selected_model is not None:
        expected.add(SELECTION_FILE_NAME)
    assert _files(artifact_dir) == expected


def test_cli_rejects_a_missing_dataset(
    run: _Run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact_dir = Path(run.receipt.artifact_dir)
    manifest_before = (artifact_dir / MANIFEST_FILE_NAME).read_bytes()
    exit_code = cli_main(
        [
            "--dataset-dir",
            str(tmp_path / "missing"),
            "--output-dir",
            str(tmp_path / "out"),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error:" in captured.err
    assert (artifact_dir / MANIFEST_FILE_NAME).read_bytes() == manifest_before


def test_cli_json_reuse_receipt(
    sealed_dataset: Path, run: _Run, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli_main(
        ["--dataset-dir", str(sealed_dataset), "--output-dir", str(run.output_dir), "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "reused"
    assert payload["run_id"] == str(run.receipt.run_id)
    assert payload["holdout_accesses"] == run.receipt.holdout_accesses
    assert payload["prediction_rows"] == run.receipt.prediction_rows


def test_reader_rejects_corrupted_predictions_and_manifest(run: _Run, tmp_path: Path) -> None:
    corrupted = tmp_path / "corrupted"
    shutil.copytree(run.receipt.artifact_dir, corrupted)
    predictions = corrupted / PREDICTIONS_FILE_NAME
    payload = bytearray(predictions.read_bytes())
    payload[100] ^= 0xFF
    predictions.write_bytes(payload)
    with pytest.raises(MlExportConflictError):
        read_ml_baseline_artifact(corrupted)

    non_canonical = tmp_path / "non-canonical"
    shutil.copytree(run.receipt.artifact_dir, non_canonical)
    manifest_path = non_canonical / MANIFEST_FILE_NAME
    manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
    with pytest.raises(MlExportConflictError):
        read_ml_baseline_artifact(non_canonical)


def test_resume_rejects_a_foreign_sealed_protocol(
    sealed_dataset: Path, run: _Run, tmp_path: Path
) -> None:
    output = tmp_path / "resume"
    output.mkdir()
    target = output / Path(run.receipt.artifact_dir).name
    shutil.copytree(run.receipt.artifact_dir, target)
    (target / MANIFEST_FILE_NAME).unlink()
    (target / REPORT_FILE_NAME).unlink()
    protocol = target / PROTOCOL_FILE_NAME
    protocol.write_bytes(protocol.read_bytes() + b"\n")
    with pytest.raises(MlExportConflictError):
        evaluate_dataset(dataset_dir=sealed_dataset, output_dir=output)


def test_output_paths_and_input_caps_fail_closed(
    sealed_dataset: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(MlExportPathError):
        evaluate_dataset(dataset_dir=sealed_dataset, output_dir=ROOT / "ml-artifacts")
    with pytest.raises(MlExportPathError):
        evaluate_dataset(dataset_dir=sealed_dataset, output_dir=sealed_dataset)
    with pytest.raises(MlExportPathError):
        evaluate_dataset(dataset_dir=sealed_dataset, output_dir=Path("relative-output"))
    monkeypatch.setattr(ml_export, "MAX_INPUT_FILE_BYTES", 1024)
    with pytest.raises(MlExportCapError):
        evaluate_dataset(dataset_dir=sealed_dataset, output_dir=tmp_path / "cap")


def test_foreign_files_are_untouched_and_no_workspace_is_opened(
    sealed_dataset: Path, run: _Run
) -> None:
    output = run.output_dir
    foreign = output / "foreign"
    foreign.mkdir()
    for index in range(10):
        (foreign / f"foreign-{index}.bin").write_bytes(b"x" * 16)
    before = {path.name: path.read_bytes() for path in foreign.iterdir() if path.is_file()}
    receipt = evaluate_dataset(dataset_dir=sealed_dataset, output_dir=output)
    assert receipt.status is MlRunStatus.REUSED
    after = {path.name: path.read_bytes() for path in foreign.iterdir() if path.is_file()}
    assert after == before
    artifact_dir = Path(receipt.artifact_dir)
    assert _files(artifact_dir) == set(_files(Path(run.receipt.artifact_dir)))
    assert not list(output.rglob("*.duckdb"))
    assert not list(output.rglob("*.wal"))


def test_no_edge_path_never_opens_or_seals_the_holdout(
    sealed_dataset: Path,
    run: _Run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, report = read_ml_baseline_artifact(Path(run.receipt.artifact_dir))
    assert report.development is not None
    no_edge = report.development.model_copy(
        update={
            "decision": MlModelState.NO_DEMONSTRATED_EDGE,
            "selected_model": None,
            "selection_reason": "fixture without an admissible candidate",
        }
    )
    prediction = MlPredictionRow(
        sample_key="synthetic",
        decision_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
        stage="development",
        fold=1,
        scorer=MlScorer.LOGISTIC_REGRESSION,
        label=1,
        probability=0.5,
    )
    outcome = MlEvaluationOutcome(
        development=no_edge,
        predictions=(prediction,),
        fits=6,
        fit_seconds=0.0,
        evaluation_seconds=0.0,
    )
    monkeypatch.setattr(ml_evaluation, "evaluate_development", lambda samples, protocol: outcome)
    receipt = evaluate_dataset(dataset_dir=sealed_dataset, output_dir=tmp_path / "no-edge")
    _, sealed = read_ml_baseline_artifact(Path(receipt.artifact_dir))
    assert sealed.holdout is not None
    assert sealed.holdout.status is MlHoldoutStatus.NOT_EVALUATED
    assert sealed.holdout.scores == ()
    assert sealed.holdout_accesses == 0
    assert sealed.holdout_fits == 0
    assert sealed.selection_sha256 is None
    assert sealed.prediction_rows == 1
    assert not (Path(receipt.artifact_dir) / SELECTION_FILE_NAME).exists()


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
        "from investment_analyst.research.ml_export import read_ml_baseline_artifact\n"
        "manifest, report = read_ml_baseline_artifact(Path(sys.argv[1]))\n"
        "print(manifest.run_id, report.status.value)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", reader, run.receipt.artifact_dir],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == [str(run.receipt.run_id), "completed"]
    helper = blocker + (
        "import runpy, sys\n"
        f"sys.argv = ['evaluate_ml_baselines.py', '--help']\n"
        f"runpy.run_path({str(ROOT / 'scripts' / 'evaluate_ml_baselines.py')!r},"
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
    assert "usage: evaluate_ml_baselines.py" in completed.stdout
    unavailable = blocker + (
        "from investment_analyst.research.ml_adapter import (\n"
        "    MlAdapterUnavailableError,\n"
        "    environment_versions,\n"
        ")\n"
        "try:\n"
        "    environment_versions()\n"
        "except MlAdapterUnavailableError:\n"
        "    print('unavailable')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", unavailable],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "unavailable"
