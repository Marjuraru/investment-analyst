"""Real workspace and CLI integration for the isolated inference boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tracemalloc
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from investment_analyst.analytics.analytical_access_models import analytical_content_hash
from investment_analyst.analytics.metric_identity_v2 import metric_result_id_from_model_v2
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models import (
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.core.models.enums import AssetClass, DataFrequency, DataQuality
from investment_analyst.research.feature_preview_models import FeaturePreviewRequest
from investment_analyst.research.inference_models import (
    CONSTANT_TEST_ADAPTER_ID,
    CONSTANT_TEST_ADAPTER_VERSION,
    CONSTANT_TEST_MODEL_ID,
    CONSTANT_TEST_MODEL_VERSION,
    MAX_MODEL_ARTIFACT_BYTES,
    MAX_MODEL_INFERENCE_BYTES,
    MODEL_ARTIFACT_SCHEMA_VERSION,
    MODEL_PURPOSE,
    MODEL_VALIDATION_STATUS,
    ConstantTestParameters,
    ModelArtifact,
    ModelInferenceRequest,
    ModelInferenceResult,
    model_artifact_id,
)
from investment_analyst.research.inference_service import ModelInferenceService
from investment_analyst.research.predictive_dataset import PredictiveDatasetBuilder
from investment_analyst.research.predictive_models import (
    EXPERIMENT_SPEC_ID,
    FEATURE_SET_VERSION,
    MAX_PAGE_SIZE,
    PREDICTIVE_FEATURE_COLUMNS,
    TARGET_VERSION,
    bar_start_instant,
    decision_instant,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "src"
_SCRIPT = _ROOT / "scripts/run_model_inference.py"

_BTC = "crypto:btc-usd"
_BTC_SOURCE = "coinbase-exchange:btc-usd:daily-candles"
_SOL = "crypto:sol-usd"
_SOL_SOURCE = "coinbase-exchange:sol-usd:daily-candles"
_DECISION = date(2026, 8, 10)
_DECISION_AT = decision_instant(_DECISION)
_DAY_START = bar_start_instant(_DECISION)
_ARTIFACT_AVAILABLE = _DECISION_AT - timedelta(days=1)
_RSI_VERSION = "market-rsi-incremental-v2-decimal34"
_MACD_VERSION = "market-macd-incremental-v2-decimal34"


def _raw(
    *,
    asset_id: str,
    source_id: str,
    day: date,
    value: Decimal,
    revision: str,
    available_at: datetime,
) -> RawRecord:
    return RawRecord(
        record_id=uuid5(
            NAMESPACE_URL,
            f"inference-raw|{asset_id}|{source_id}|{day.isoformat()}|{revision}",
        ),
        asset_id=asset_id,
        source=SourceReference(
            source_id=source_id,
            retrieved_at=available_at,
            record_key=f"{day.isoformat()}|{revision}",
        ),
        event_time=bar_start_instant(day),
        available_at=available_at,
        received_at=available_at,
        payload={"close": str(value)},
        schema_version="inference-integration-v1",
    )


def _observation(
    *,
    raw: RawRecord,
    asset_id: str,
    source_id: str,
    day: date,
    value: Decimal,
    available_at: datetime,
) -> NormalizedObservation:
    start = bar_start_instant(day)
    return NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"{raw.record_id}|close"),
        raw_record_id=raw.record_id,
        asset_id=asset_id,
        field_name="close",
        value=value,
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=start,
        period_start=start,
        period_end=start + timedelta(days=1),
        available_at=available_at,
        normalized_at=available_at,
        source=SourceReference(source_id=source_id, retrieved_at=available_at),
        quality=DataQuality.VALID,
        transformation_version="coinbase-candle-normalizer-v1",
    )


def _metric(
    *,
    asset_id: str,
    source_id: str,
    day: date,
    key: str,
    value: Decimal,
    available_at: datetime,
    input_observation_id: UUID,
    seed: str,
    version: str,
    unit: str,
    parameters: dict[str, object],
) -> MetricResult:
    placeholder = MetricResult(
        result_id=uuid5(NAMESPACE_URL, f"inference-metric|{seed}"),
        asset_id=asset_id,
        metric_key=key,
        value=value,
        unit=unit,
        as_of=bar_start_instant(day),
        available_at=available_at,
        computed_at=available_at,
        parameters={"source_id": source_id, **parameters},
        input_observation_ids=[input_observation_id],
        algorithm_version=version,
        quality=DataQuality.VALID,
    )
    return placeholder.model_copy(update={"result_id": metric_result_id_from_model_v2(placeholder)})


def _seed_series(
    writer: object,
    *,
    asset_id: str,
    source_id: str,
    days: tuple[date, ...],
    start_value: int,
    suffix: str,
) -> None:
    raws: list[RawRecord] = []
    observations: list[NormalizedObservation] = []
    metrics: list[MetricResult] = []
    observation_by_day: dict[date, NormalizedObservation] = {}
    for index, day in enumerate(days):
        available = bar_start_instant(day) + timedelta(days=1, hours=1)
        value = Decimal(start_value + index)
        raw = _raw(
            asset_id=asset_id,
            source_id=source_id,
            day=day,
            value=value,
            revision=f"base|{suffix}",
            available_at=available,
        )
        observation = _observation(
            raw=raw,
            asset_id=asset_id,
            source_id=source_id,
            day=day,
            value=value,
            available_at=available,
        )
        raws.append(raw)
        observations.append(observation)
        observation_by_day[day] = observation
    writer.raw_records.save_many(raws)  # type: ignore[attr-defined]
    writer.observations.save_many(observations)  # type: ignore[attr-defined]
    for index, day in enumerate(days):
        decision = day + timedelta(days=1)
        available = decision_instant(decision) - timedelta(hours=11)
        observation = observation_by_day[day]
        metrics.append(
            _metric(
                asset_id=asset_id,
                source_id=source_id,
                day=day,
                key="market.technical.rsi",
                value=Decimal(40 + (index % 20)),
                available_at=available,
                input_observation_id=observation.observation_id,
                seed=f"{asset_id}|rsi|{day.isoformat()}|{suffix}",
                version=_RSI_VERSION,
                unit="index",
                parameters={"window": 14},
            )
        )
        metrics.append(
            _metric(
                asset_id=asset_id,
                source_id=source_id,
                day=day,
                key="market.technical.macd.histogram",
                value=Decimal("2"),
                available_at=available,
                input_observation_id=observation.observation_id,
                seed=f"{asset_id}|macd|{day.isoformat()}|{suffix}",
                version=_MACD_VERSION,
                unit="USD",
                parameters={"fast_window": 12, "slow_window": 26, "signal_window": 9},
            )
        )
    writer.metric_results.save_many(metrics)  # type: ignore[attr-defined]


def _days(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _workspace(tmp_path: Path) -> tuple[WorkspaceService, Path]:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialization = service.initialize(tmp_path / "workspace")
    return service, initialization.paths.root


def _seed(service: WorkspaceService, root: Path) -> None:
    close_days = _days(_DECISION - timedelta(days=2), _DECISION)
    writer = service.open_storage(
        service.resolve(root),
        WorkspaceAccessMode.READ_WRITE,
    )
    try:
        _seed_series(
            writer,
            asset_id=_BTC,
            source_id=_BTC_SOURCE,
            days=close_days,
            start_value=100,
            suffix="btc",
        )
        _seed_series(
            writer,
            asset_id=_SOL,
            source_id=_SOL_SOURCE,
            days=close_days,
            start_value=200,
            suffix="sol-1x",
        )
    finally:
        writer.close()


def _artifact(*, available_at: datetime = _ARTIFACT_AVAILABLE) -> ModelArtifact:
    draft = ModelArtifact.model_construct(
        schema_version=MODEL_ARTIFACT_SCHEMA_VERSION,
        artifact_id=UUID(int=0),
        artifact_sha256="0" * 64,
        model_id=CONSTANT_TEST_MODEL_ID,
        model_version=CONSTANT_TEST_MODEL_VERSION,
        adapter_id=CONSTANT_TEST_ADAPTER_ID,
        adapter_version=CONSTANT_TEST_ADAPTER_VERSION,
        purpose=MODEL_PURPOSE,
        validation_status=MODEL_VALIDATION_STATUS,
        experiment_spec_id=EXPERIMENT_SPEC_ID,
        target_version=TARGET_VERSION,
        feature_set_version=FEATURE_SET_VERSION,
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        frequency=DataFrequency.DAY_1,
        feature_columns=PREDICTIVE_FEATURE_COLUMNS,
        parameters=ConstantTestParameters(),
        available_at=available_at,
        provenance="synthetic_fixture_not_trained",
    )
    payload = draft.material_payload()
    return ModelArtifact.model_validate(
        {
            **draft.model_dump(mode="python"),
            "artifact_id": model_artifact_id(payload),
            "artifact_sha256": analytical_content_hash(payload),
        }
    )


def _artifact_file(tmp_path: Path, artifact: ModelArtifact) -> Path:
    path = tmp_path / "artifact.json"
    path.write_text(
        json.dumps(
            artifact.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    assert path.stat().st_size <= MAX_MODEL_ARTIFACT_BYTES
    return path


def _tree_state(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _run_cli(
    *,
    workspace: Path,
    artifact: Path,
    asset_id: str,
    known_at: datetime,
    cwd: Path,
    blocker_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    pythonpath = str(_SRC)
    if blocker_dir is not None:
        pythonpath = os.pathsep.join((pythonpath, str(blocker_dir)))
    environment = os.environ.copy()
    environment["PYTHONPATH"] = pythonpath
    environment["http_proxy"] = "http://127.0.0.1:9"
    environment["https_proxy"] = "http://127.0.0.1:9"
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--workspace",
            str(workspace),
            "--model-artifact",
            str(artifact),
            "--asset-id",
            asset_id,
            "--decision-date",
            _DECISION.isoformat(),
            "--known-at",
            known_at.isoformat(),
            "--json",
        ],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def _ml_blocker(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sitecustomize.py").write_text(
        "import sys\n"
        "\n"
        "class _BlockOptionalMl:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname.split('.')[0] in {'sklearn', 'scipy', 'threadpoolctl'}:\n"
        "            raise ImportError('optional ML dependency blocked by integration smoke')\n"
        "        return None\n"
        "\n"
        "sys.meta_path.insert(0, _BlockOptionalMl())\n",
        encoding="utf-8",
    )
    return directory


def _preview_and_infer(
    service: WorkspaceService,
    root: Path,
    artifact: ModelArtifact,
) -> tuple[object, ModelInferenceResult, tuple[int, int, int, int], list[MetricResult], list[int]]:
    runtime = ApplicationRuntime.create_default(workspace_service=service)
    hydrated: list[MetricResult] = []
    pages: list[int] = []
    with runtime.open_storage(
        StorageLocationRequest(workspace=root),
        access_mode=WorkspaceAccessMode.READ_ONLY,
    ) as storage:
        repository = storage.metric_results
        original_get_many = repository.get_many
        original_page = repository.list_metric_index_page

        def recording_get_many(identifiers: tuple[UUID, ...]) -> dict[UUID, MetricResult]:
            result = original_get_many(identifiers)
            hydrated.extend(result.values())
            return result

        def recording_page(query: object) -> tuple[object, ...]:
            result = original_page(query)
            pages.append(len(result))
            return result

        repository.get_many = recording_get_many  # type: ignore[method-assign]
        repository.list_metric_index_page = recording_page  # type: ignore[method-assign]
        builder = PredictiveDatasetBuilder(
            storage,
            provider_resolver=runtime.provider_resolver,
            catalog=runtime.catalog,
        )
        preview = builder.preview_features(
            FeaturePreviewRequest(
                asset_id=_BTC,
                decision_date=_DECISION,
                known_at=_DECISION_AT,
            )
        )
        pages_after_preview = len(pages)
        hydrated_after_preview = len(hydrated)
        result = ModelInferenceService().infer(
            ModelInferenceRequest(artifact=artifact, preview=preview)
        )
        counts = (
            pages_after_preview,
            hydrated_after_preview,
            len(pages),
            len(hydrated),
        )
    return preview, result, counts, hydrated, pages


def test_cli_returns_test_output_from_another_cwd_without_optional_ml(tmp_path: Path) -> None:
    service, root = _workspace(tmp_path)
    _seed(service, root)
    artifact = _artifact()
    artifact_path = _artifact_file(tmp_path, artifact)
    blocker = _ml_blocker(tmp_path / "blocker")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    before = _tree_state(root)

    btc = _run_cli(
        workspace=root,
        artifact=artifact_path,
        asset_id=_BTC,
        known_at=_DECISION_AT,
        cwd=elsewhere,
        blocker_dir=blocker,
    )
    assert btc.returncode == 0, btc.stderr
    assert btc.stderr == ""
    btc_payload = json.loads(btc.stdout)
    assert btc_payload["status"] == "test_output"
    assert btc_payload["test_probability"] == "0.5"
    assert btc_payload["inference_id"]
    assert btc_payload["preview_id"]
    assert btc_payload["explanation"]["method"] == "constant_test_no_attribution"
    assert len(btc.stdout.encode("utf-8")) <= MAX_MODEL_INFERENCE_BYTES

    sol = _run_cli(
        workspace=root,
        artifact=artifact_path,
        asset_id=_SOL,
        known_at=_DECISION_AT,
        cwd=elsewhere,
        blocker_dir=blocker,
    )
    assert sol.returncode == 0, sol.stderr
    sol_payload = json.loads(sol.stdout)
    assert sol_payload["status"] == "test_output"
    assert sol_payload["asset"]["asset_id"] == _SOL
    assert sol_payload["inference_id"] != btc_payload["inference_id"]

    repeated = _run_cli(
        workspace=root,
        artifact=artifact_path,
        asset_id=_BTC,
        known_at=_DECISION_AT,
        cwd=elsewhere,
        blocker_dir=blocker,
    )
    assert repeated.returncode == 0, repeated.stderr
    assert repeated.stdout == btc.stdout

    assert _tree_state(root) == before
    assert _tree_bytes(root) < 32 * 1024 * 1024


def test_cli_reports_unavailable_inputs_with_exit_zero(tmp_path: Path) -> None:
    service, root = _workspace(tmp_path)
    _seed(service, root)
    artifact_path = _artifact_file(tmp_path, _artifact())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    completed = _run_cli(
        workspace=root,
        artifact=artifact_path,
        asset_id=_BTC,
        known_at=_DAY_START,
        cwd=elsewhere,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "inputs_unavailable"
    assert payload["test_probability"] is None
    assert payload["reasons"]
    assert all(reason["code"] == "feature_input_unavailable" for reason in payload["reasons"])


def test_cli_rejects_amd_profile_and_an_invalid_artifact(tmp_path: Path) -> None:
    service, root = _workspace(tmp_path)
    _seed(service, root)
    artifact_path = _artifact_file(tmp_path, _artifact())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    amd = _run_cli(
        workspace=root,
        artifact=artifact_path,
        asset_id="equity:us:amd",
        known_at=_DECISION_AT,
        cwd=elsewhere,
    )
    assert amd.returncode == 2
    assert amd.stdout == ""
    assert "crypto spot" in amd.stderr

    invalid_path = tmp_path / "invalid-artifact.json"
    invalid_path.write_text(
        json.dumps(
            _artifact().model_dump(mode="json") | {"artifact_sha256": "0" * 64},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    invalid = _run_cli(
        workspace=root,
        artifact=invalid_path,
        asset_id=_BTC,
        known_at=_DECISION_AT,
        cwd=elsewhere,
    )
    assert invalid.returncode == 2
    assert invalid.stdout == ""
    assert "artifact" in invalid.stderr


def test_inference_adds_no_queries_and_foreign_corpus_does_not_change_identity(
    tmp_path: Path,
) -> None:
    service, root = _workspace(tmp_path)
    _seed(service, root)
    artifact = _artifact()

    first_preview, first_result, first_counts, first_hydrated, first_pages = _preview_and_infer(
        service, root, artifact
    )
    assert first_result.status.value == "test_output"
    assert first_counts[0] == first_counts[2]
    assert first_counts[1] == first_counts[3]
    assert first_counts[1] > 0
    assert all(metric.asset_id == _BTC for metric in first_hydrated)
    assert all(metric.parameters.get("source_id") == _BTC_SOURCE for metric in first_hydrated)
    assert first_pages and all(0 < size <= MAX_PAGE_SIZE for size in first_pages)

    writer = service.open_storage(
        service.resolve(root),
        WorkspaceAccessMode.READ_WRITE,
    )
    try:
        for index in range(9):
            _seed_series(
                writer,
                asset_id=_SOL,
                source_id=_SOL_SOURCE,
                days=_days(_DECISION - timedelta(days=2), _DECISION),
                start_value=300 + index * 100,
                suffix=f"sol-10x-{index}",
            )
    finally:
        writer.close()

    after_seed = _tree_state(root)
    tracemalloc.start()
    started = time.perf_counter()
    second_preview, second_result, second_counts, second_hydrated, second_pages = (
        _preview_and_infer(service, root, artifact)
    )
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert second_counts[0] == second_counts[2]
    assert second_counts[1] == second_counts[3]
    assert second_counts[1] > 0
    assert first_preview.preview_id == second_preview.preview_id  # type: ignore[attr-defined]
    assert first_result.inference_id == second_result.inference_id
    assert first_result.to_json_dict() == second_result.to_json_dict()
    assert all(metric.asset_id == _BTC for metric in second_hydrated)
    assert all(metric.parameters.get("source_id") == _BTC_SOURCE for metric in second_hydrated)
    assert second_pages and all(0 < size <= MAX_PAGE_SIZE for size in second_pages)
    assert _tree_state(root) == after_seed
    assert elapsed < 60
    assert 0 < peak < 512 * 1024 * 1024
