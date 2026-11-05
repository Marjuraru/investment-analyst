"""Integration checks for the sealed volatility artifact and offline CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from investment_analyst.core.models.enums import AssetClass
from investment_analyst.research import volatility_export
from investment_analyst.research.historical_archive import (
    build_historical_archive,
    seal_historical_archive,
)
from investment_analyst.research.historical_models import (
    DECIMAL34,
    HistoricalArchive,
    HistoricalArchiveRequest,
    HistoricalAssetIdentity,
    HistoricalCandle,
    assumed_available_instant,
    bar_start_instant,
    historical_bar_content_id,
)
from investment_analyst.research.volatility_evaluation import VolatilityEvaluationError
from investment_analyst.research.volatility_export import (
    VolatilityExportConflictError,
    VolatilityExportPathError,
    VolatilityRetentionError,
    evaluate_volatility_experiment,
    read_volatility_artifact,
)
from investment_analyst.research.volatility_models import (
    ALL_MODELS,
    ARCHIVE_REQUEST_END,
    ARCHIVE_REQUEST_START,
    ASSET_ID,
    MANIFEST_FILE_NAME,
    PREDICTIONS_FILE_NAME,
    PROGRESS_FILE_NAME,
    PROTOCOL_FILE_NAME,
    REPORT_FILE_NAME,
    SOURCE_ID,
)

_RETRIEVED = datetime(2026, 11, 1, 2, 37, 32, tzinfo=UTC)
_ROOT = Path(__file__).resolve().parents[3]
_SCOPE = HistoricalAssetIdentity(
    asset_id=ASSET_ID,
    source_id=SOURCE_ID,
    product_id="BTC-USD",
    asset_class=AssetClass.CRYPTO,
    quote_currency="USD",
    crypto_profile="bitcoin",
    exchange="COINBASE",
    granularity_seconds=86_400,
)
_REQUEST = HistoricalArchiveRequest(
    asset_id=ASSET_ID,
    start_date=ARCHIVE_REQUEST_START,
    end_date=ARCHIVE_REQUEST_END,
)


def _bar(start: datetime, open_value: Decimal, close: Decimal) -> HistoricalCandle:
    high = (max(open_value, close) * Decimal("1.01")).quantize(Decimal("0.01"))
    low = (min(open_value, close) * Decimal("0.99")).quantize(Decimal("0.01"))
    volume = Decimal("100")
    raw = (str(open_value), str(high), str(low), str(close), str(volume), "0")
    return HistoricalCandle(
        bar_content_id=historical_bar_content_id(
            asset_id=_SCOPE.asset_id,
            source_id=_SCOPE.source_id,
            bar_start=start,
            open_value=open_value,
            high=high,
            low=low,
            close=close,
            volume=volume,
            raw_values=raw,
        ),
        bar_start=start,
        bar_end=start + timedelta(days=1),
        open=open_value,
        high=high,
        low=low,
        close=close,
        volume=volume,
        raw_values=raw,
        assumed_available_at=assumed_available_instant(start + timedelta(days=1)),
        retrieved_at=_RETRIEVED,
    )


def _build_archive(
    *,
    multiplier: Decimal = Decimal(1),
    present_days: int = 2557,
) -> HistoricalArchive:
    candles: list[HistoricalCandle] = []
    day = ARCHIVE_REQUEST_START
    with localcontext(DECIMAL34):
        for index in range(present_days):
            base = (Decimal(10000) * (Decimal("1.0005") ** index) * multiplier).quantize(
                Decimal("0.01")
            )
            close = (base * (Decimal(1) + Decimal((index % 7) - 3) / Decimal(1000))).quantize(
                Decimal("0.01")
            )
            candles.append(_bar(bar_start_instant(day), base, close))
            day += timedelta(days=1)
    return build_historical_archive(
        _SCOPE,
        _REQUEST,
        request_urls=("https://example.invalid/candles",),
        retrieved_at=_RETRIEVED,
        candles=tuple(candles),
    )


def _seal(archive: HistoricalArchive, root: Path) -> Path:
    receipt = seal_historical_archive(archive, output_dir=root)
    return Path(receipt.archive_dir)


@pytest.fixture(scope="module")
def baseline_archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _seal(_build_archive(), tmp_path_factory.mktemp("baseline-archive"))


@pytest.fixture(scope="module")
def baseline_artifact(
    baseline_archive: Path, tmp_path_factory: pytest.TempPathFactory
) -> tuple[object, Path]:
    output = tmp_path_factory.mktemp("baseline-output")
    receipt = evaluate_volatility_experiment(archive_dir=baseline_archive, output_dir=output)
    return receipt, output


def _copy_run(source: Path, root: Path) -> Path:
    target = root / source.name
    shutil.copytree(source, target)
    return target


def _file_names(path: Path) -> set[str]:
    return {entry.name for entry in path.iterdir()}


def test_artifact_is_complete_bounded_and_reused_without_a_fit(
    baseline_archive: Path, baseline_artifact: tuple[object, Path]
) -> None:
    receipt, output = baseline_artifact
    run_dir = Path(receipt.artifact_dir)  # type: ignore[attr-defined]
    loaded = read_volatility_artifact(run_dir)

    assert receipt.status.value == "evaluated"  # type: ignore[attr-defined]
    assert receipt.report_status.value == "completed"  # type: ignore[attr-defined]
    assert loaded.report.status.value == "completed"
    assert loaded.report.decision is not None
    assert loaded.manifest is not None
    assert _file_names(run_dir) == {
        PROTOCOL_FILE_NAME,
        REPORT_FILE_NAME,
        PREDICTIONS_FILE_NAME,
        MANIFEST_FILE_NAME,
    }
    assert receipt.artifact_bytes <= 8 * 1024 * 1024  # type: ignore[attr-defined]
    assert (run_dir / PREDICTIONS_FILE_NAME).stat().st_size <= 5 * 1024 * 1024
    assert loaded.report.prediction_rows == len(
        (run_dir / PREDICTIONS_FILE_NAME).read_text(encoding="utf-8").splitlines()
    )
    assert loaded.report.holdout_status == "not_evaluated"
    assert loaded.report.holdout_fits == 0
    assert loaded.report.target_evaluations_reserved == 0
    assert loaded.report.promoted is False
    assert loaded.report.counts.reserved_target_constructions == 0
    hashes = {
        name: volatility_export._sha256_file(run_dir / name)
        for name in (
            PROTOCOL_FILE_NAME,
            REPORT_FILE_NAME,
            PREDICTIONS_FILE_NAME,
            MANIFEST_FILE_NAME,
        )
    }

    reused = evaluate_volatility_experiment(
        archive_dir=baseline_archive,
        output_dir=output,
    )
    assert reused.status.value == "reused"
    assert reused.fits_executed_this_invocation == 0
    assert reused.run_id == receipt.run_id  # type: ignore[attr-defined]
    assert {name: volatility_export._sha256_file(run_dir / name) for name in hashes} == hashes


def test_tampering_with_any_sealed_file_is_rejected(
    baseline_artifact: tuple[object, Path], tmp_path: Path
) -> None:
    receipt, _output = baseline_artifact
    run_dir = Path(receipt.artifact_dir)  # type: ignore[attr-defined]
    for name in (REPORT_FILE_NAME, PREDICTIONS_FILE_NAME, MANIFEST_FILE_NAME):
        copied = _copy_run(run_dir, tmp_path / name.replace(".", "-"))
        target = copied / name
        payload = target.read_bytes()
        target.write_bytes(payload[:-2] + b"X\n")
        with pytest.raises(VolatilityExportConflictError):
            read_volatility_artifact(copied)


def test_path_guards_reject_overlap_symlinks_and_foreign_experiments(
    baseline_archive: Path, tmp_path: Path
) -> None:
    with pytest.raises(VolatilityExportPathError):
        evaluate_volatility_experiment(
            archive_dir=baseline_archive,
            output_dir=baseline_archive / "inside",
        )
    with pytest.raises(VolatilityExportPathError):
        evaluate_volatility_experiment(
            archive_dir=baseline_archive,
            output_dir=_ROOT / "volatility-output",
        )
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(VolatilityExportPathError):
        evaluate_volatility_experiment(
            archive_dir=baseline_archive,
            output_dir=link,
        )
    foreign = Path("~/.local/share/investment-analyst/research/predictive-historical-data/guard")
    with pytest.raises(VolatilityExportPathError):
        evaluate_volatility_experiment(
            archive_dir=baseline_archive,
            output_dir=foreign.expanduser(),
        )
    polluted = _copy_run(baseline_archive, tmp_path / "polluted")
    (polluted / "extra.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(VolatilityExportPathError):
        evaluate_volatility_experiment(
            archive_dir=polluted,
            output_dir=tmp_path / "polluted-output",
        )


def test_retention_declares_superseded_and_rejects_a_third_run(tmp_path: Path) -> None:
    output = tmp_path / "retention"
    first = _seal(_build_archive(multiplier=Decimal("1.1")), tmp_path / "archives-a")
    second = _seal(_build_archive(multiplier=Decimal("1.2")), tmp_path / "archives-b")
    third = _seal(_build_archive(multiplier=Decimal("1.3")), tmp_path / "archives-c")

    receipt_a = evaluate_volatility_experiment(archive_dir=first, output_dir=output)
    receipt_b = evaluate_volatility_experiment(archive_dir=second, output_dir=output)
    assert receipt_a.superseded_run_ids == ()
    assert receipt_b.superseded_run_ids == (receipt_a.run_id,)
    assert set(receipt_b.retained_run_ids) == {receipt_a.run_id, receipt_b.run_id}

    with pytest.raises(VolatilityRetentionError):
        evaluate_volatility_experiment(archive_dir=third, output_dir=output)

    reused = evaluate_volatility_experiment(archive_dir=first, output_dir=output)
    assert reused.status.value == "reused"
    assert reused.superseded_run_ids == (receipt_b.run_id,)


def test_interruption_keeps_progress_and_resumes_without_refitting_completed_folds(
    baseline_archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "resume"
    original = volatility_export.evaluate_fold
    calls: list[int] = []

    def flaky(plan: object, protocol: object) -> object:
        calls.append(plan.fold)  # type: ignore[attr-defined]
        if plan.fold == 2:  # type: ignore[attr-defined]
            raise VolatilityEvaluationError("synthetic interruption")
        return original(plan, protocol)  # type: ignore[arg-type]

    monkeypatch.setattr(volatility_export, "evaluate_fold", flaky)
    first = evaluate_volatility_experiment(archive_dir=baseline_archive, output_dir=output)
    run_dir = Path(first.artifact_dir)
    assert first.report_status.value == "error"
    assert first.fits_executed_this_invocation == 2
    assert calls == [1, 2]
    assert (run_dir / PROGRESS_FILE_NAME).is_file()
    loaded = read_volatility_artifact(run_dir)
    assert loaded.report.status.value == "error"

    monkeypatch.setattr(volatility_export, "evaluate_fold", original)
    second = evaluate_volatility_experiment(archive_dir=baseline_archive, output_dir=output)
    assert second.report_status.value == "completed"
    assert second.fits_executed_this_invocation == 2
    assert second.resumed_from_progress is True
    assert not (run_dir / PROGRESS_FILE_NAME).exists()
    assert read_volatility_artifact(run_dir).report.status.value == "completed"


def test_insufficient_sample_seals_no_predictions_or_manifest(tmp_path: Path) -> None:
    sparse = _seal(_build_archive(present_days=600), tmp_path / "sparse")
    output = tmp_path / "sparse-output"
    receipt = evaluate_volatility_experiment(archive_dir=sparse, output_dir=output)
    run_dir = Path(receipt.artifact_dir)
    loaded = read_volatility_artifact(run_dir)

    assert receipt.report_status.value == "insufficient_sample"
    assert receipt.decision is None
    assert receipt.manifest_sha256 is None
    assert receipt.predictions_sha256 is None
    assert receipt.fits_executed_this_invocation == 0
    assert _file_names(run_dir) == {PROTOCOL_FILE_NAME, REPORT_FILE_NAME}
    assert loaded.report.decision_reason is not None

    reused = evaluate_volatility_experiment(archive_dir=sparse, output_dir=output)
    assert reused.status.value == "reused"
    assert reused.fits_executed_this_invocation == 0


def test_cli_help_and_reader_do_not_require_optional_numerical_dependencies(
    baseline_artifact: tuple[object, Path], tmp_path: Path
) -> None:
    receipt, _output = baseline_artifact
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    for name in ("numpy", "sklearn", "scipy", "threadpoolctl"):
        (blocker / f"{name}.py").write_text(
            f'raise ImportError("blocked {name}")\n', encoding="utf-8"
        )
    environment = {
        "PYTHONPATH": f"{_ROOT / 'src'}{':'}{blocker}",
        "PATH": "/usr/bin:/bin",
    }
    help_result = subprocess.run(
        [sys.executable, "scripts/evaluate_volatility_baseline.py", "--help"],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--archive-dir" in help_result.stdout
    assert "--output-dir" in help_result.stdout

    run_dir = Path(receipt.artifact_dir)  # type: ignore[attr-defined]
    reader = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path;"
            "from investment_analyst.research.volatility_export import read_volatility_artifact;"
            "print(read_volatility_artifact(Path(sys.argv[1])).report.status.value)",
            str(run_dir),
        ],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert reader.returncode == 0, reader.stderr
    assert reader.stdout.strip() == "completed"


def test_cli_json_reports_counts_and_limits(baseline_archive: Path, tmp_path: Path) -> None:
    output = tmp_path / "cli-output"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/evaluate_volatility_baseline.py",
            "--archive-dir",
            str(baseline_archive),
            "--output-dir",
            str(output),
            "--json",
        ],
        cwd=_ROOT,
        env={"PYTHONPATH": str(_ROOT / "src"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "evaluated"
    assert payload["report_status"] == "completed"
    assert payload["counts"]["reserved_target_constructions"] == 0
    assert payload["counts"]["planned_decisions"] == 2186
    assert "pooled_comparisons" in payload
    assert payload["decision"] is not None
    run_dir = Path(payload["artifact_dir"])
    assert _file_names(run_dir) == {
        PROTOCOL_FILE_NAME,
        REPORT_FILE_NAME,
        PREDICTIONS_FILE_NAME,
        MANIFEST_FILE_NAME,
    }
    loaded = read_volatility_artifact(run_dir)
    assert {metric.model for metric in loaded.report.pooled_scores} == set(ALL_MODELS)
