"""Real workspace and CLI integration for the bounded shadow observation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from investment_analyst.analytics.analytical_access_models import analytical_content_hash
from investment_analyst.analytics.metric_identity_v2 import metric_result_id_from_model_v2
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.core.models import (
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.core.models.enums import AssetClass, DataFrequency, DataQuality
from investment_analyst.research.inference_models import (
    CONSTANT_TEST_ADAPTER_ID,
    CONSTANT_TEST_ADAPTER_VERSION,
    CONSTANT_TEST_MODEL_ID,
    CONSTANT_TEST_MODEL_VERSION,
    MAX_MODEL_ARTIFACT_BYTES,
    MODEL_ARTIFACT_SCHEMA_VERSION,
    MODEL_PURPOSE,
    MODEL_VALIDATION_STATUS,
    ConstantTestParameters,
    ModelArtifact,
    model_artifact_id,
)
from investment_analyst.research.predictive_models import (
    EXPERIMENT_SPEC_ID,
    FEATURE_SET_VERSION,
    PREDICTIVE_FEATURE_COLUMNS,
    TARGET_VERSION,
    bar_start_instant,
    decision_instant,
)
from investment_analyst.research.shadow_models import ShadowRunRequest
from investment_analyst.research.shadow_service import (
    ModelShadowRequestError,
    ModelShadowService,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "src"
_SCRIPT = _ROOT / "scripts/run_model_shadow.py"

_BTC = "crypto:btc-usd"
_BTC_SOURCE = "coinbase-exchange:btc-usd:daily-candles"
_SOL = "crypto:sol-usd"
_SOL_SOURCE = "coinbase-exchange:sol-usd:daily-candles"
_FOREIGN = "crypto:ada-usd"
_FOREIGN_SOURCE = "coinbase-exchange:ada-usd:daily-candles"
_DECISION = date(2026, 8, 10)
_DECISION_AT = decision_instant(_DECISION)
_KNOWN_AT = _DECISION_AT - timedelta(hours=1)
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
            f"shadow-raw|{asset_id}|{source_id}|{day.isoformat()}|{revision}",
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
        schema_version="shadow-integration-v1",
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
        result_id=uuid5(NAMESPACE_URL, f"shadow-metric|{seed}"),
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
    writer = service.open_storage(service.resolve(root), WorkspaceAccessMode.READ_WRITE)
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
            suffix="sol",
        )
    finally:
        writer.close()


def _artifact() -> ModelArtifact:
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
        available_at=_ARTIFACT_AVAILABLE,
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


def _artifact_file(tmp_path: Path) -> Path:
    path = tmp_path / "artifact.json"
    path.write_text(
        json.dumps(
            _artifact().model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    assert path.stat().st_size <= MAX_MODEL_ARTIFACT_BYTES
    return path


def _tree_state(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        str(path.relative_to(root)): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _run_state(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        str(path.relative_to(root)): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _request(
    tmp_path: Path,
    root: Path,
    artifact: Path,
    *,
    assets: tuple[str, ...] = (_SOL, _BTC),
) -> ShadowRunRequest:
    return ShadowRunRequest(
        workspace=root,
        model_artifact=artifact,
        asset_ids=assets,
        decision_date=_DECISION,
        known_at=_KNOWN_AT,
        output_root=tmp_path / "model-shadow-v1",
    )


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_SRC)
    environment["http_proxy"] = "http://127.0.0.1:9"
    environment["https_proxy"] = "http://127.0.0.1:9"
    return environment


def _cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *arguments],
        cwd=_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def _enabled_cli_arguments(
    root: Path, artifact: Path, *, assets: tuple[str, ...] = (_SOL, _BTC)
) -> list[str]:
    values = [
        "--workspace",
        str(root),
        "--model-artifact",
        str(artifact),
        "--decision-date",
        _DECISION.isoformat(),
        "--known-at",
        _KNOWN_AT.isoformat(),
        "--output-root",
        str(root.parent / "model-shadow-v1"),
        "--enabled",
        "--json",
    ]
    for asset_id in assets:
        values.extend(("--asset-id", asset_id))
    return values


def test_shadow_opens_read_only_once_and_reuses_an_identical_bundle(tmp_path: Path) -> None:
    service, root = _workspace(tmp_path)
    _seed(service, root)
    artifact = _artifact_file(tmp_path)
    runtime = ApplicationRuntime.create_default(workspace_service=service)
    calls: list[WorkspaceAccessMode] = []
    original = runtime.open_storage

    def counted(request, *, access_mode):
        calls.append(access_mode)
        return original(request, access_mode=access_mode)

    runtime.open_storage = counted  # type: ignore[method-assign]
    before = _tree_state(root)
    started = time.perf_counter()
    summary = ModelShadowService(runtime=runtime).run(_request(tmp_path, root, artifact))
    elapsed = time.perf_counter() - started
    after = _tree_state(root)

    assert summary.status == "created"
    assert calls == [WorkspaceAccessMode.READ_ONLY]
    assert before == after
    assert elapsed < 60
    assert tuple(asset.asset_id for asset in summary.assets) == (_BTC, _SOL)
    assert all(asset.status.value == "test_output" for asset in summary.assets)
    assert all(asset.test_probability == Decimal("0.5") for asset in summary.assets)
    run_dir = tmp_path / "model-shadow-v1" / str(summary.run_id)
    assert {item.name for item in run_dir.iterdir()} == {
        "manifest.json",
        *(f"{asset.inference_id}.json" for asset in summary.assets),
    }
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["purpose"] == "integration_test"
    assert manifest["validation_status"] == "test_only"
    assert manifest["limitations"]
    assert "updated_at" not in manifest
    for asset in manifest["assets"]:
        result = json.loads((run_dir / asset["result_file"]).read_text(encoding="utf-8"))
        assert result["validation_status"] == "test_only"
        assert result["test_probability"] == "0.5"
        assert result["explanation"]["limitations"]

    first_state = _run_state(run_dir)
    repeated = ModelShadowService(runtime=runtime).run(_request(tmp_path, root, artifact))
    assert repeated.status == "reused"
    assert repeated.run_id == summary.run_id
    assert repeated.manifest_sha256 == summary.manifest_sha256
    assert _run_state(run_dir) == first_state
    assert _tree_state(root) == before


def test_foreign_corpus_never_changes_the_shadow_identity(tmp_path: Path) -> None:
    service, root = _workspace(tmp_path)
    _seed(service, root)
    artifact = _artifact_file(tmp_path)
    request = _request(tmp_path, root, artifact)
    first = ModelShadowService().run(request)
    first_manifest = tmp_path / "model-shadow-v1" / str(first.run_id) / "manifest.json"
    first_bytes = first_manifest.read_bytes()
    before = _run_state(tmp_path / "model-shadow-v1")

    writer = service.open_storage(service.resolve(root), WorkspaceAccessMode.READ_WRITE)
    try:
        for index in range(10):
            _seed_series(
                writer,
                asset_id=_FOREIGN,
                source_id=_FOREIGN_SOURCE,
                days=_days(_DECISION - timedelta(days=2), _DECISION),
                start_value=500 + index * 100,
                suffix=f"ada-10x-{index}",
            )
    finally:
        writer.close()

    second = ModelShadowService().run(request)
    assert second.status == "reused"
    assert second.run_id == first.run_id
    assert second.manifest_sha256 == first.manifest_sha256
    assert first_manifest.read_bytes() == first_bytes
    assert _run_state(tmp_path / "model-shadow-v1") == before


def test_disabled_cli_reports_without_touching_workspace_artifact_or_root(
    tmp_path: Path,
) -> None:
    service, root = _workspace(tmp_path)
    _seed(service, root)
    artifact = _artifact_file(tmp_path)
    before = _tree_state(root)
    artifact_before = (artifact.stat().st_mtime_ns, artifact.read_bytes())
    completed = _cli(
        "--workspace",
        str(root),
        "--model-artifact",
        str(artifact),
        "--asset-id",
        _BTC,
        "--decision-date",
        _DECISION.isoformat(),
        "--known-at",
        _KNOWN_AT.isoformat(),
        "--output-root",
        str(tmp_path / "model-shadow-v1"),
    )
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["enabled"] is False
    assert payload["status"] == "disabled"
    assert completed.stderr == ""
    assert not (tmp_path / "model-shadow-v1").exists()
    assert _tree_state(root) == before
    assert (artifact.stat().st_mtime_ns, artifact.read_bytes()) == artifact_before


def test_amd_is_rejected_before_the_shadow_root_is_created(tmp_path: Path) -> None:
    service, root = _workspace(tmp_path)
    _seed(service, root)
    artifact = _artifact_file(tmp_path)
    request = _request(tmp_path, root, artifact, assets=("equity:us:amd",))
    before = _tree_state(root)
    with pytest.raises(ModelShadowRequestError, match="crypto spot USD"):
        ModelShadowService().run(request)
    assert not (tmp_path / "model-shadow-v1").exists()
    assert _tree_state(root) == before


def test_enabled_cli_publishes_the_same_run_identity(tmp_path: Path) -> None:
    service, root = _workspace(tmp_path)
    _seed(service, root)
    artifact = _artifact_file(tmp_path)
    completed = _cli(*_enabled_cli_arguments(root, artifact))
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["enabled"] is True
    assert payload["status"] == "created"
    assert tuple(asset["asset_id"] for asset in payload["assets"]) == (_BTC, _SOL)
    manifest_path = Path(payload["manifest_path"])
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["run_id"] == payload["run_id"]
    repeated = _cli(*_enabled_cli_arguments(root, artifact))
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["status"] == "reused"


def test_enabled_cli_requires_every_runtime_argument(tmp_path: Path) -> None:
    completed = _cli("--enabled", "--json")
    assert completed.returncode == 2
    payload = json.loads(completed.stderr)
    assert "--workspace" in payload["error"]
    assert not (tmp_path / "model-shadow-v1").exists()
