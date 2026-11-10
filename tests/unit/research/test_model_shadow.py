"""Bounded shadow contracts, atomic store, idempotence and fail-closed boundaries."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.analytical_access_models import (
    analytical_content_hash,
    canonical_analytical_json,
)
from investment_analyst.core.models.enums import AssetClass, DataFrequency, DataQuality
from investment_analyst.research.feature_preview_models import (
    FEATURE_PREVIEW_SCHEMA_VERSION,
    FeaturePreviewCloseReference,
    FeaturePreviewRequest,
    FeaturePreviewStatus,
    ResearchFeaturePreview,
    feature_preview_id,
)
from investment_analyst.research.inference_models import (
    CONSTANT_TEST_ADAPTER_ID,
    CONSTANT_TEST_ADAPTER_VERSION,
    CONSTANT_TEST_MODEL_ID,
    CONSTANT_TEST_MODEL_VERSION,
    MODEL_ARTIFACT_SCHEMA_VERSION,
    MODEL_PURPOSE,
    MODEL_VALIDATION_STATUS,
    ConstantTestParameters,
    ModelArtifact,
    ModelInferenceRequest,
    model_artifact_id,
)
from investment_analyst.research.inference_service import ModelInferenceService
from investment_analyst.research.predictive_models import (
    CLOSE_RETURN_ALGORITHM_VERSION,
    EXPERIMENT_SPEC_ID,
    FEATURE_RATIO_UNIT,
    FEATURE_SET_VERSION,
    PREDICTIVE_FEATURE_COLUMNS,
    TARGET_VERSION,
    EvidenceReason,
    FeatureState,
    PredictiveAssetIdentity,
    PredictiveFeatureEvidence,
    ReasonCode,
    aggregate_feature_state,
    bar_start_instant,
    decision_instant,
    predictive_feature_hash,
    predictive_sample_key,
)
from investment_analyst.research.shadow_models import (
    SHADOW_LIMITATIONS,
    ShadowAssetManifest,
    ShadowRunManifest,
    ShadowRunRequest,
    shadow_run_id,
)
from investment_analyst.research.shadow_service import ModelShadowService
from investment_analyst.research.shadow_store import (
    ShadowStore,
    ShadowStoreBudgetError,
    ShadowStoreConflictError,
    ShadowStoreError,
    ShadowStoreLockError,
)

_ASSET_ID = "crypto:btc-usd"
_SOL_ASSET_ID = "crypto:sol-usd"
_SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"
_SOL_SOURCE_ID = "coinbase-exchange:sol-usd:daily-candles"
_WORKSPACE_UUID = UUID("70c64668-1738-4dfe-ba1f-7e72026bb7fe")
_DECISION = date(2026, 8, 10)
_DECISION_AT = decision_instant(_DECISION)
_KNOWN_AT = _DECISION_AT - timedelta(hours=1)
_ARTIFACT_AVAILABLE = _DECISION_AT - timedelta(days=1)
_PREVIEW_LIMITATIONS = ("Proyección de prueba sin labels.",)


def _asset(
    asset_id: str = _ASSET_ID,
    source_id: str = _SOURCE_ID,
) -> PredictiveAssetIdentity:
    return PredictiveAssetIdentity(
        asset_id=asset_id,
        source_id=source_id,
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        crypto_profile="spot",
        exchange="coinbase",
    )


def _available_feature(specification, value: Decimal) -> PredictiveFeatureEvidence:
    algorithm = (
        CLOSE_RETURN_ALGORITHM_VERSION
        if specification.transform == "close_ratio_1d"
        else specification.algorithm_version
    )
    return PredictiveFeatureEvidence(
        column_name=specification.column_name,
        state=FeatureState.AVAILABLE,
        value=value,
        as_of=bar_start_instant(_DECISION - timedelta(days=1)),
        available_at=_DECISION_AT - timedelta(hours=1),
        computed_at=_DECISION_AT - timedelta(hours=2),
        algorithm_version=algorithm,
        unit=FEATURE_RATIO_UNIT,
        quality=DataQuality.VALID,
    )


def _features(values=(Decimal("0.01"), Decimal("0.55"), Decimal("0.02"))):
    return tuple(
        _available_feature(specification, value)
        for specification, value in zip(PREDICTIVE_FEATURE_COLUMNS, values, strict=True)
    )


def _missing_features():
    return tuple(
        PredictiveFeatureEvidence(
            column_name=specification.column_name,
            state=FeatureState.MISSING,
            algorithm_version=(
                CLOSE_RETURN_ALGORITHM_VERSION
                if specification.transform == "close_ratio_1d"
                else specification.algorithm_version
            ),
            unit=FEATURE_RATIO_UNIT,
            reasons=(
                EvidenceReason(
                    code=ReasonCode.CLOSE_MISSING,
                    detail=f"{specification.column_name} is missing at the cut",
                ),
            ),
        )
        for specification in PREDICTIVE_FEATURE_COLUMNS
    )


def _close_references() -> tuple[FeaturePreviewCloseReference, ...]:
    references = []
    for offset in (-2, -1):
        start = bar_start_instant(_DECISION + timedelta(days=offset))
        references.append(
            FeaturePreviewCloseReference(
                observation_id=uuid5(NAMESPACE_URL, f"close-reference|{start.isoformat()}"),
                period_start=start,
                period_end=start + timedelta(days=1),
                available_at=start + timedelta(days=1, hours=1),
            )
        )
    return tuple(references)


def _preview(
    *,
    asset_id: str = _ASSET_ID,
    source_id: str = _SOURCE_ID,
    features=None,
) -> ResearchFeaturePreview:
    asset = _asset(asset_id, source_id)
    request = FeaturePreviewRequest(
        asset_id=asset_id,
        decision_date=_DECISION,
        known_at=_KNOWN_AT,
    )
    selected = features if features is not None else _features()
    status = (
        FeaturePreviewStatus.FEATURES_AVAILABLE
        if aggregate_feature_state(selected) is FeatureState.AVAILABLE
        else FeaturePreviewStatus.INPUTS_UNAVAILABLE
    )
    sample_key = predictive_sample_key(
        experiment_spec_id=EXPERIMENT_SPEC_ID,
        asset_id=asset_id,
        source_id=source_id,
        decision_at=_DECISION_AT,
    )
    feature_hash = predictive_feature_hash(
        sample_key=sample_key,
        asset_id=asset_id,
        source_id=source_id,
        decision_at=_DECISION_AT,
        features=selected,
    )
    draft = ResearchFeaturePreview.model_construct(
        schema_version=FEATURE_PREVIEW_SCHEMA_VERSION,
        preview_id=UUID(int=0),
        workspace_uuid=_WORKSPACE_UUID,
        asset=asset,
        experiment_spec_id=EXPERIMENT_SPEC_ID,
        feature_set_version=FEATURE_SET_VERSION,
        request=request,
        decision_at=_DECISION_AT,
        status=status,
        sample_key=sample_key,
        feature_hash=feature_hash,
        features=selected,
        close_references=_close_references(),
        limitations=_PREVIEW_LIMITATIONS,
    )
    preview_id = feature_preview_id(
        workspace_uuid=_WORKSPACE_UUID,
        asset=asset,
        request=request,
        decision_at=_DECISION_AT,
        material_payload=draft.material_payload(),
    )
    return ResearchFeaturePreview.model_validate(
        {**draft.model_dump(mode="python"), "preview_id": preview_id}
    )


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


def _request(tmp_path: Path, *, assets: tuple[str, ...] = (_ASSET_ID,)) -> ShadowRunRequest:
    return ShadowRunRequest(
        workspace=tmp_path / "workspace",
        model_artifact=tmp_path / "artifact.json",
        asset_ids=assets,
        decision_date=_DECISION,
        known_at=_KNOWN_AT,
        output_root=tmp_path / "model-shadow-v1",
    )


def _result(asset_id: str = _ASSET_ID, source_id: str = _SOURCE_ID, *, missing=False):
    preview = _preview(
        asset_id=asset_id,
        source_id=source_id,
        features=_missing_features() if missing else None,
    )
    return ModelInferenceService().infer(
        ModelInferenceRequest(artifact=_artifact(), preview=preview)
    )


def _encoded(result) -> bytes:
    return canonical_analytical_json(result.to_json_dict())


def _manifest(tmp_path: Path, results, *, assets=None):
    request = _request(
        tmp_path,
        assets=assets or tuple(result.asset.asset_id for result in results),
    )
    return ModelShadowService._manifest(
        request=request,
        artifact=_artifact(),
        workspace_uuid=_WORKSPACE_UUID,
        results=tuple((result, _encoded(result)) for result in results),
    )


def _store(tmp_path: Path) -> ShadowStore:
    return ShadowStore(
        tmp_path / "model-shadow-v1",
        workspace_root=tmp_path / "workspace",
        protected_roots=(tmp_path / "repo",),
    )


def _publish(store: ShadowStore, manifest: ShadowRunManifest, results):
    return store.publish(
        manifest,
        {result.inference_id: _encoded(result) for result in results},
    )


def test_request_rejects_ambiguous_scope_and_cut() -> None:
    base = {
        "workspace": Path("/tmp/workspace"),
        "model_artifact": Path("/tmp/artifact.json"),
        "asset_ids": (_ASSET_ID,),
        "decision_date": _DECISION,
        "known_at": _KNOWN_AT,
        "output_root": Path("/tmp/model-shadow-v1"),
    }
    with pytest.raises(ValidationError, match="one or two"):
        ShadowRunRequest(**{**base, "asset_ids": (_ASSET_ID,) * 3})
    with pytest.raises(ValidationError, match="repeat"):
        ShadowRunRequest(**{**base, "asset_ids": (_ASSET_ID, _ASSET_ID)})
    with pytest.raises(ValidationError, match="absolute"):
        ShadowRunRequest(**{**base, "workspace": Path("relative")})
    with pytest.raises(ValidationError, match="12:00"):
        ShadowRunRequest(**{**base, "known_at": _DECISION_AT + timedelta(seconds=1)})
    with pytest.raises(ValidationError, match="00:00"):
        ShadowRunRequest(
            **{
                **base,
                "known_at": datetime(2026, 8, 10, tzinfo=UTC) - timedelta(seconds=1),
            }
        )
    with pytest.raises(ValidationError, match="dedicated"):
        ShadowRunRequest(**{**base, "output_root": Path("/")})
    with pytest.raises(ValidationError, match="must not include a time"):
        ShadowRunRequest(**{**base, "decision_date": datetime(2026, 8, 10, tzinfo=UTC)})


def test_manifest_is_deterministic_ordered_and_preserves_test_only_limits(
    tmp_path: Path,
) -> None:
    btc = _result()
    sol = _result(_SOL_ASSET_ID, _SOL_SOURCE_ID)
    manifest = _manifest(tmp_path, (sol, btc))
    payload = manifest.material_payload()
    assert tuple(item.asset_id for item in manifest.assets) == (_ASSET_ID, _SOL_ASSET_ID)
    assert manifest.run_id == shadow_run_id(payload)
    assert "updated_at" not in manifest.model_dump()
    assert "duration_seconds" not in manifest.model_dump()
    assert set(SHADOW_LIMITATIONS).issubset(manifest.limitations)
    assert manifest.purpose == "integration_test"
    assert manifest.validation_status == "test_only"
    assert len(canonical_analytical_json(manifest.model_dump(mode="json"))) <= 16_384


def test_manifest_rejects_unordered_assets_and_tampered_identity(tmp_path: Path) -> None:
    btc = _result()
    sol = _result(_SOL_ASSET_ID, _SOL_SOURCE_ID)
    manifest = _manifest(tmp_path, (btc, sol))
    values = manifest.model_dump(mode="python")
    values["assets"] = tuple(reversed(values["assets"]))
    with pytest.raises(ValidationError, match="canonical order"):
        ShadowRunManifest.model_validate(values)
    values = manifest.model_dump(mode="python")
    values["run_id"] = uuid5(NAMESPACE_URL, "other")
    with pytest.raises(ValidationError, match="run_id"):
        ShadowRunManifest.model_validate(values)
    values = manifest.model_dump(mode="python")
    values["limitations"] = ("short",)
    with pytest.raises(ValidationError, match="mandatory shadow limitation"):
        ShadowRunManifest.model_validate(values)


def test_store_publishes_atomically_and_reuses_identical_runs(tmp_path: Path) -> None:
    result = _result()
    manifest = _manifest(tmp_path, (result,))
    store = _store(tmp_path)
    first = _publish(store, manifest, (result,))
    assert first.created is True
    run_dir = tmp_path / "model-shadow-v1" / str(manifest.run_id)
    assert {item.name for item in run_dir.iterdir()} == {
        "manifest.json",
        f"{result.inference_id}.json",
    }
    before = {
        item.name: (item.stat().st_ino, item.stat().st_mtime_ns, item.read_bytes())
        for item in run_dir.iterdir()
    }
    second = _publish(store, manifest, (result,))
    after = {
        item.name: (item.stat().st_ino, item.stat().st_mtime_ns, item.read_bytes())
        for item in run_dir.iterdir()
    }
    assert second.created is False
    assert second.manifest_sha256 == first.manifest_sha256
    assert before == after
    assert not (tmp_path / "model-shadow-v1" / ".shadow.lock").exists()
    assert not any(
        item.name.startswith(".staging-") for item in (tmp_path / "model-shadow-v1").iterdir()
    )


@pytest.mark.parametrize("tamper", ["manifest", "result", "missing", "extra"])
def test_store_fails_closed_on_corruption(tmp_path: Path, tamper: str) -> None:
    result = _result()
    manifest = _manifest(tmp_path, (result,))
    store = _store(tmp_path)
    _publish(store, manifest, (result,))
    run_dir = tmp_path / "model-shadow-v1" / str(manifest.run_id)
    result_path = run_dir / f"{result.inference_id}.json"
    if tamper == "manifest":
        (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
    elif tamper == "result":
        data = bytearray(result_path.read_bytes())
        data[-2] = data[-2] ^ 1
        result_path.write_bytes(bytes(data))
    elif tamper == "missing":
        result_path.unlink()
    else:
        (run_dir / "extra.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ShadowStoreConflictError):
        _publish(store, manifest, (result,))


def test_store_rejects_lock_mixed_root_and_unsafe_paths(tmp_path: Path) -> None:
    result = _result()
    manifest = _manifest(tmp_path, (result,))
    store = _store(tmp_path)
    store.root.mkdir()
    (store.root / ".shadow.lock").write_text("orphan", encoding="utf-8")
    with pytest.raises(ShadowStoreLockError, match="owner must resolve"):
        _publish(store, manifest, (result,))
    (store.root / ".shadow.lock").unlink()
    (store.root / "historical-study").mkdir()
    with pytest.raises(ShadowStoreError, match="mixes"):
        _publish(store, manifest, (result,))
    (store.root / "historical-study").rmdir()
    with pytest.raises(ShadowStoreError):
        ShadowStore(
            tmp_path / "workspace" / "shadow",
            workspace_root=tmp_path / "workspace",
        )
    with pytest.raises(ShadowStoreError):
        ShadowStore(Path("/"), workspace_root=tmp_path / "workspace")
    real = tmp_path / "real-shadow"
    real.mkdir()
    link = tmp_path / "linked-shadow"
    link.symlink_to(real)
    with pytest.raises(ShadowStoreError):
        ShadowStore(link, workspace_root=tmp_path / "workspace")


def test_store_enforces_count_and_byte_budgets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("investment_analyst.research.shadow_store.MAX_SHADOW_RUNS", 1)
    first = _result()
    second = _result(_SOL_ASSET_ID, _SOL_SOURCE_ID)
    first_manifest = _manifest(tmp_path, (first,))
    second_manifest = _manifest(tmp_path, (second,))
    store = _store(tmp_path)
    _publish(store, first_manifest, (first,))
    with pytest.raises(ShadowStoreBudgetError, match="run budget"):
        _publish(store, second_manifest, (second,))
    assert _publish(store, first_manifest, (first,)).created is False
    monkeypatch.setattr("investment_analyst.research.shadow_store.MAX_SHADOW_RUNS", 8)
    monkeypatch.setattr("investment_analyst.research.shadow_store.MAX_SHADOW_STORE_BYTES", 64)
    with pytest.raises(ShadowStoreBudgetError, match="byte budget"):
        _publish(_store(tmp_path), second_manifest, (second,))


def test_store_rejects_oversized_manifest_and_result(tmp_path: Path, monkeypatch) -> None:
    result = _result()
    manifest = _manifest(tmp_path, (result,))
    monkeypatch.setattr("investment_analyst.research.shadow_store.MAX_SHADOW_MANIFEST_BYTES", 64)
    with pytest.raises(ShadowStoreBudgetError, match="manifest exceeds"):
        _publish(_store(tmp_path), manifest, (result,))
    monkeypatch.undo()
    monkeypatch.setattr("investment_analyst.research.shadow_store.MAX_SHADOW_RESULT_BYTES", 64)
    with pytest.raises(ShadowStoreBudgetError, match="result exceeds"):
        _publish(_store(tmp_path), manifest, (result,))


def test_store_cleans_temporary_directory_after_publish_failure(
    tmp_path: Path, monkeypatch
) -> None:
    result = _result()
    manifest = _manifest(tmp_path, (result,))

    def fail_rename(source, destination):
        raise OSError("rename failed")

    monkeypatch.setattr("investment_analyst.research.shadow_store.os.rename", fail_rename)
    with pytest.raises(OSError, match="rename failed"):
        _publish(_store(tmp_path), manifest, (result,))
    root = tmp_path / "model-shadow-v1"
    assert list(root.iterdir()) == []


def test_abstention_is_preserved_without_probability(tmp_path: Path) -> None:
    result = _result(missing=True)
    assert result.test_probability is None
    manifest = _manifest(tmp_path, (result,))
    assert manifest.assets[0].status.value == "inputs_unavailable"
    assert manifest.assets[0].test_probability is None
    receipt = _publish(_store(tmp_path), manifest, (result,))
    assert receipt.created is True
    payload = json.loads(
        (
            tmp_path / "model-shadow-v1" / str(manifest.run_id) / manifest.assets[0].result_file
        ).read_text(encoding="utf-8")
    )
    assert payload["test_probability"] is None
    assert payload["status"] == "inputs_unavailable"

    values = manifest.model_dump(mode="python")
    with pytest.raises(ValidationError, match="probability"):
        ShadowAssetManifest(
            **{
                **values["assets"][0],
                "test_probability": Decimal("0.5"),
            }
        )


def test_publish_requires_exact_result_identity(tmp_path: Path) -> None:
    result = _result()
    manifest = _manifest(tmp_path, (result,))
    with pytest.raises(ShadowStoreConflictError, match="manifest identities"):
        _store(tmp_path).publish(manifest, {uuid5(NAMESPACE_URL, "other"): _encoded(result)})


def test_store_does_not_rewrite_a_complete_run_even_when_saturated(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("investment_analyst.research.shadow_store.MAX_SHADOW_RUNS", 1)
    result = _result()
    manifest = _manifest(tmp_path, (result,))
    store = _store(tmp_path)
    receipt = _publish(store, manifest, (result,))
    run_dir = tmp_path / "model-shadow-v1" / str(manifest.run_id)
    manifest_path = run_dir / "manifest.json"
    before = (manifest_path.stat().st_ino, manifest_path.stat().st_mtime_ns)
    assert _publish(store, manifest, (result,)).created is False
    assert (manifest_path.stat().st_ino, manifest_path.stat().st_mtime_ns) == before
    assert receipt.created is True
