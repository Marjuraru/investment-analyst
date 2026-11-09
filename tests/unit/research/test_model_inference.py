"""Isolated model inference: artifact, compatibility, identity and abstention."""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.analytical_access_models import analytical_content_hash
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.core.models.enums import AssetClass, DataFrequency, DataQuality
from investment_analyst.research.feature_preview_models import (
    FEATURE_PREVIEW_SCHEMA_VERSION,
    FeaturePreviewAssetUnsupportedError,
    FeaturePreviewCloseReference,
    FeaturePreviewRequest,
    FeaturePreviewStatus,
    ResearchFeaturePreview,
    feature_preview_id,
)
from investment_analyst.research.inference_adapter import ConstantTestAdapter
from investment_analyst.research.inference_models import (
    CONSTANT_TEST_ADAPTER_ID,
    CONSTANT_TEST_ADAPTER_VERSION,
    CONSTANT_TEST_MODEL_ID,
    CONSTANT_TEST_MODEL_VERSION,
    CONSTANT_TEST_PROBABILITY,
    MAX_MODEL_ARTIFACT_BYTES,
    MODEL_ARTIFACT_SCHEMA_VERSION,
    MODEL_PURPOSE,
    MODEL_TEST_LIMITATIONS,
    MODEL_VALIDATION_STATUS,
    ConstantTestParameters,
    InferenceReasonCode,
    InferenceStatus,
    ModelAdapterOutput,
    ModelArtifact,
    ModelArtifactParseError,
    ModelArtifactValidationError,
    ModelInferenceRequest,
    ModelInferenceResult,
    load_model_artifact,
    model_artifact_id,
    model_inference_id,
    parse_model_artifact_json,
)
from investment_analyst.research.inference_service import (
    ModelInferenceBudgetError,
    ModelInferenceExecutionError,
    ModelInferenceIntegrityError,
    ModelInferenceService,
)
from investment_analyst.research.predictive_dataset import PredictiveDatasetBuilder
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
    PredictiveFeatureColumnSpec,
    PredictiveFeatureEvidence,
    ReasonCode,
    aggregate_feature_state,
    bar_start_instant,
    decision_instant,
    predictive_feature_hash,
    predictive_sample_key,
)

_ASSET_ID = "crypto:btc-usd"
_SOL_ASSET_ID = "crypto:sol-usd"
_SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"
_SOL_SOURCE_ID = "coinbase-exchange:sol-usd:daily-candles"
_WORKSPACE_UUID = UUID("70c64668-1738-4dfe-ba1f-7e72026bb7fe")
_OTHER_WORKSPACE_UUID = UUID("11111111-2222-4333-8444-555555555555")
_DECISION = date(2026, 8, 10)
_DECISION_AT = decision_instant(_DECISION)
_ARTIFACT_AVAILABLE = _DECISION_AT - timedelta(days=1)
_PREVIEW_LIMITATIONS = ("Proyección de prueba sin labels.",)


class _Paths:
    workspace_id = _WORKSPACE_UUID


class _EmptyStorage:
    read_only = True
    paths = _Paths()

    def require_open(self) -> None:
        return None


def _asset(
    *,
    asset_id: str = _ASSET_ID,
    source_id: str = _SOURCE_ID,
    asset_class: AssetClass = AssetClass.CRYPTO,
    quote_currency: str = "USD",
) -> PredictiveAssetIdentity:
    return PredictiveAssetIdentity(
        asset_id=asset_id,
        source_id=source_id,
        asset_class=asset_class,
        quote_currency=quote_currency,
        crypto_profile="spot" if asset_class is AssetClass.CRYPTO else "not_crypto",
        exchange="coinbase" if asset_class is AssetClass.CRYPTO else "alpaca",
    )


def _available_feature(
    specification: PredictiveFeatureColumnSpec,
    value: Decimal,
    *,
    decision_date: date = _DECISION,
    available_at: datetime | None = None,
    unit: str = FEATURE_RATIO_UNIT,
    algorithm_version: str | None = None,
) -> PredictiveFeatureEvidence:
    decision_at = decision_instant(decision_date)
    default_algorithm = (
        CLOSE_RETURN_ALGORITHM_VERSION
        if specification.transform == "close_ratio_1d"
        else specification.algorithm_version
    )
    return PredictiveFeatureEvidence(
        column_name=specification.column_name,
        state=FeatureState.AVAILABLE,
        value=value,
        as_of=bar_start_instant(decision_date - timedelta(days=1)),
        available_at=available_at if available_at is not None else decision_at - timedelta(hours=1),
        computed_at=decision_at - timedelta(hours=2),
        algorithm_version=(
            algorithm_version if algorithm_version is not None else default_algorithm
        ),
        unit=unit,
        quality=DataQuality.VALID,
    )


def _features(
    *,
    decision_date: date = _DECISION,
    values: tuple[Decimal, Decimal, Decimal] = (
        Decimal("0.01"),
        Decimal("0.55"),
        Decimal("0.02"),
    ),
    unit: str = FEATURE_RATIO_UNIT,
    algorithm_override: str | None = None,
    available_at: datetime | None = None,
) -> tuple[PredictiveFeatureEvidence, ...]:
    return tuple(
        _available_feature(
            specification,
            value,
            decision_date=decision_date,
            unit=unit,
            algorithm_version=algorithm_override,
            available_at=available_at,
        )
        for specification, value in zip(
            PREDICTIVE_FEATURE_COLUMNS,
            values,
            strict=True,
        )
    )


def _unavailable_features(
    *,
    decision_date: date = _DECISION,
) -> tuple[PredictiveFeatureEvidence, ...]:
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


def _close_references(
    *,
    decision_date: date = _DECISION,
    known_at: datetime = _DECISION_AT,
) -> tuple[FeaturePreviewCloseReference, ...]:
    references = []
    for offset in (-2, -1):
        start = bar_start_instant(decision_date + timedelta(days=offset))
        references.append(
            FeaturePreviewCloseReference(
                observation_id=uuid5(NAMESPACE_URL, f"close-reference|{start.isoformat()}"),
                period_start=start,
                period_end=start + timedelta(days=1),
                available_at=min(known_at, start + timedelta(days=1, hours=1)),
            )
        )
    return tuple(references)


def _preview(
    *,
    asset_id: str = _ASSET_ID,
    source_id: str = _SOURCE_ID,
    asset_class: AssetClass = AssetClass.CRYPTO,
    quote_currency: str = "USD",
    workspace_uuid: UUID = _WORKSPACE_UUID,
    decision_date: date = _DECISION,
    known_at: datetime = _DECISION_AT,
    features: tuple[PredictiveFeatureEvidence, ...] | None = None,
    close_references: tuple[FeaturePreviewCloseReference, ...] | None = None,
    status: FeaturePreviewStatus | None = None,
) -> ResearchFeaturePreview:
    asset = _asset(
        asset_id=asset_id,
        source_id=source_id,
        asset_class=asset_class,
        quote_currency=quote_currency,
    )
    request = FeaturePreviewRequest(
        asset_id=asset_id,
        decision_date=decision_date,
        known_at=known_at,
    )
    selected = features if features is not None else _features(decision_date=decision_date)
    expected_status = (
        FeaturePreviewStatus.FEATURES_AVAILABLE
        if aggregate_feature_state(selected) is FeatureState.AVAILABLE
        else FeaturePreviewStatus.INPUTS_UNAVAILABLE
    )
    selected_status = status if status is not None else expected_status
    decision_at = decision_instant(decision_date)
    sample_key = predictive_sample_key(
        experiment_spec_id=EXPERIMENT_SPEC_ID,
        asset_id=asset_id,
        source_id=source_id,
        decision_at=decision_at,
    )
    feature_hash = predictive_feature_hash(
        sample_key=sample_key,
        asset_id=asset_id,
        source_id=source_id,
        decision_at=decision_at,
        features=selected,
    )
    references = (
        close_references
        if close_references is not None
        else _close_references(decision_date=decision_date, known_at=known_at)
    )
    draft = ResearchFeaturePreview.model_construct(
        schema_version=FEATURE_PREVIEW_SCHEMA_VERSION,
        preview_id=UUID(int=0),
        workspace_uuid=workspace_uuid,
        asset=asset,
        experiment_spec_id=EXPERIMENT_SPEC_ID,
        feature_set_version=FEATURE_SET_VERSION,
        request=request,
        decision_at=decision_at,
        status=selected_status,
        sample_key=sample_key,
        feature_hash=feature_hash,
        features=selected,
        close_references=references,
        limitations=_PREVIEW_LIMITATIONS,
    )
    preview_id = feature_preview_id(
        workspace_uuid=workspace_uuid,
        asset=asset,
        request=request,
        decision_at=decision_at,
        material_payload=draft.material_payload(),
    )
    return ResearchFeaturePreview.model_validate(
        {**draft.model_dump(mode="python"), "preview_id": preview_id}
    )


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


def _artifact_document(artifact: ModelArtifact) -> str:
    return json.dumps(
        artifact.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _infer(
    *,
    artifact: ModelArtifact | None = None,
    preview: ResearchFeaturePreview | None = None,
    adapter: object | None = None,
) -> ModelInferenceResult:
    request = ModelInferenceRequest.model_construct(
        artifact=artifact if artifact is not None else _artifact(),
        preview=preview if preview is not None else _preview(),
    )
    return ModelInferenceService(adapter=adapter).infer(request)  # type: ignore[arg-type]


class _SpyAdapter:
    adapter_id = CONSTANT_TEST_ADAPTER_ID
    adapter_version = CONSTANT_TEST_ADAPTER_VERSION

    def __init__(self, *, output: object | None = None, error: Exception | None = None) -> None:
        self.calls = 0
        self.inputs: list[object] = []
        self._output = output
        self._error = error

    def predict(self, inference_input: object) -> object:
        self.calls += 1
        self.inputs.append(inference_input)
        if self._error is not None:
            raise self._error
        if self._output is not None:
            return self._output
        return ModelAdapterOutput(
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            output_kind="binary_probability",
            probability=CONSTANT_TEST_PROBABILITY,
        )


def test_artifact_round_trip_preserves_identity_and_rejects_semantic_drift() -> None:
    artifact = _artifact()
    parsed = parse_model_artifact_json(_artifact_document(artifact).encode("utf-8"))
    assert parsed == artifact
    assert parsed.artifact_id == artifact.artifact_id
    assert parsed.artifact_sha256 == artifact.artifact_sha256

    later = _artifact(available_at=_ARTIFACT_AVAILABLE + timedelta(seconds=1))
    assert later.artifact_id != artifact.artifact_id
    assert later.artifact_sha256 != artifact.artifact_sha256


def test_artifact_parser_rejects_duplicate_extras_floats_nonfinite_and_false_identity() -> None:
    document = json.loads(_artifact_document(_artifact()))
    with_extra = {**document, "unexpected": "value"}
    false_hash = {**document, "artifact_sha256": "0" * 64}
    false_id = {**document, "artifact_id": str(UUID(int=1))}
    wrong_probability = {
        **document,
        "parameters": {"family": "constant-test-v1", "test_probability": "0.4"},
    }
    wrong_family = {
        **document,
        "parameters": {"family": "linear-v1", "test_probability": "0.5"},
    }
    unknown_version = {**document, "schema_version": "research-model-artifact-v2"}
    non_finite_string = {
        **document,
        "parameters": {"family": "constant-test-v1", "test_probability": "Infinity"},
    }

    for payload in (
        with_extra,
        false_hash,
        false_id,
        wrong_probability,
        wrong_family,
        unknown_version,
        non_finite_string,
    ):
        with pytest.raises((ModelArtifactParseError, ModelArtifactValidationError)):
            parse_model_artifact_json(json.dumps(payload).encode("utf-8"))

    duplicate = _artifact_document(_artifact()).replace(
        '"schema_version":"research-model-artifact-v1"',
        '"schema_version":"research-model-artifact-v1","schema_version":"research-model-artifact-v1"',
    )
    with pytest.raises(ModelArtifactParseError):
        parse_model_artifact_json(duplicate.encode("utf-8"))

    for invalid in (
        b"[]",
        b'{"test_probability":0.5}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":1e999}',
    ):
        with pytest.raises(ModelArtifactParseError):
            parse_model_artifact_json(invalid)

    with pytest.raises(ModelArtifactParseError):
        parse_model_artifact_json(b" " * (MAX_MODEL_ARTIFACT_BYTES + 1))


def test_artifact_loader_reads_regular_files_only(tmp_path: Path) -> None:
    artifact = _artifact()
    valid = tmp_path / "artifact.json"
    valid.write_text(_artifact_document(artifact), encoding="utf-8")
    assert load_model_artifact(valid) == artifact

    symlink = tmp_path / "artifact-link.json"
    symlink.symlink_to(valid)
    with pytest.raises(ModelArtifactParseError, match="regular file"):
        load_model_artifact(symlink)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (MAX_MODEL_ARTIFACT_BYTES + 1))
    with pytest.raises(ModelArtifactParseError, match="exceeds"):
        load_model_artifact(oversized)


def test_btc_and_sol_share_the_same_class_contract_and_differ_in_identity() -> None:
    artifact = _artifact()
    btc = _infer(artifact=artifact, preview=_preview())
    sol = _infer(
        artifact=artifact,
        preview=_preview(asset_id=_SOL_ASSET_ID, source_id=_SOL_SOURCE_ID),
    )

    for result in (btc, sol):
        assert result.status is InferenceStatus.TEST_OUTPUT
        assert result.test_probability == CONSTANT_TEST_PROBABILITY
        assert result.reasons == ()
        assert set(MODEL_TEST_LIMITATIONS).issubset(result.explanation.limitations)
        assert result.explanation.method == "constant_test_no_attribution"

    assert btc.inference_id != sol.inference_id
    assert btc.asset.asset_id == _ASSET_ID
    assert sol.asset.asset_id == _SOL_ASSET_ID


def test_amd_is_rejected_by_the_preview_profile_and_by_compatibility() -> None:
    storage = _EmptyStorage()
    runtime = ApplicationRuntime.create_default()
    builder = PredictiveDatasetBuilder(
        storage,  # type: ignore[arg-type]
        provider_resolver=runtime.provider_resolver,
        catalog=runtime.catalog,
    )
    with pytest.raises(FeaturePreviewAssetUnsupportedError, match="crypto spot"):
        builder.preview_features(
            FeaturePreviewRequest(
                asset_id="equity:us:amd",
                decision_date=_DECISION,
                known_at=_DECISION_AT,
            )
        )

    equity_preview = _preview(
        asset_id="equity:us:amd",
        source_id="alpaca-market-data:iex:amd:daily-bars:adjustment-all",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
    )
    result = _infer(preview=equity_preview)
    assert result.status is InferenceStatus.INCOMPATIBLE
    assert any(
        reason.code is InferenceReasonCode.ASSET_PROFILE_INCOMPATIBLE for reason in result.reasons
    )
    assert result.test_probability is None


def test_workspace_source_cut_and_feature_change_the_inference_identity() -> None:
    artifact = _artifact()
    baseline = _infer(artifact=artifact, preview=_preview())
    other_workspace = _infer(
        artifact=artifact,
        preview=_preview(workspace_uuid=_OTHER_WORKSPACE_UUID),
    )
    other_source = _infer(
        artifact=artifact,
        preview=_preview(source_id="coinbase-exchange:btc-usd:daily-candles-v2"),
    )
    other_cut = _infer(
        artifact=artifact,
        preview=_preview(known_at=_DECISION_AT - timedelta(hours=1)),
    )
    other_features = _infer(
        artifact=artifact,
        preview=_preview(
            features=_features(
                values=(Decimal("0.02"), Decimal("0.55"), Decimal("0.02")),
            )
        ),
    )
    assert (
        len(
            {
                baseline.inference_id,
                other_workspace.inference_id,
                other_source.inference_id,
                other_cut.inference_id,
                other_features.inference_id,
            }
        )
        == 5
    )


def test_missing_reordered_and_wrong_feature_contracts_are_incompatible() -> None:
    baseline = _preview()
    reordered = baseline.model_copy(
        update={
            "features": (
                baseline.features[1],
                baseline.features[0],
                baseline.features[2],
            )
        }
    )
    missing = baseline.model_copy(update={"features": baseline.features[:2]})
    wrong_unit = _preview(features=_features(unit="USD"))
    wrong_algorithm = _preview(
        features=_features(algorithm_override="market-rsi-wilder-v1-decimal34")
    )

    for preview, code in (
        (reordered, InferenceReasonCode.FEATURE_COLUMNS_MISMATCH),
        (missing, InferenceReasonCode.FEATURE_COLUMNS_MISMATCH),
        (wrong_unit, InferenceReasonCode.FEATURE_CONTRACT_MISMATCH),
        (wrong_algorithm, InferenceReasonCode.FEATURE_CONTRACT_MISMATCH),
    ):
        result = _infer(preview=preview)
        assert result.status is InferenceStatus.INCOMPATIBLE
        assert any(reason.code is code for reason in result.reasons)
        assert result.test_probability is None


def test_evidence_after_the_cut_and_stale_preview_identity_are_incompatible() -> None:
    late_preview = _preview(features=_features(available_at=_DECISION_AT + timedelta(seconds=1)))
    late = _infer(preview=late_preview)
    assert late.status is InferenceStatus.INCOMPATIBLE
    assert any(
        reason.code is InferenceReasonCode.FEATURE_EVIDENCE_AFTER_CUT for reason in late.reasons
    )

    stale = _preview().model_copy(update={"features": _features(values=(Decimal("9"),) * 3)})
    stale_result = _infer(preview=stale)
    assert stale_result.status is InferenceStatus.INCOMPATIBLE
    assert any(
        reason.code is InferenceReasonCode.PREVIEW_IDENTITY_MISMATCH
        for reason in stale_result.reasons
    )


@pytest.mark.parametrize(
    ("preview", "artifact"),
    [
        (_preview(features=_unavailable_features()), _artifact()),
        (_preview(), _artifact(available_at=_DECISION_AT + timedelta(seconds=1))),
        (
            _preview(
                asset_id="equity:us:amd",
                source_id="alpaca-market-data:iex:amd:daily-bars:adjustment-all",
                asset_class=AssetClass.EQUITY,
            ),
            _artifact(),
        ),
    ],
)
def test_abstentions_never_invoke_the_adapter_or_expose_a_probability(
    preview: ResearchFeaturePreview,
    artifact: ModelArtifact,
) -> None:
    spy = _SpyAdapter()
    result = _infer(preview=preview, artifact=artifact, adapter=spy)
    assert result.status is not InferenceStatus.TEST_OUTPUT
    assert result.test_probability is None
    assert result.reasons
    assert spy.calls == 0


def test_inputs_unavailable_preserves_specific_feature_reasons() -> None:
    result = _infer(preview=_preview(features=_unavailable_features()))
    assert result.status is InferenceStatus.INPUTS_UNAVAILABLE
    assert all(
        reason.code is InferenceReasonCode.FEATURE_INPUT_UNAVAILABLE for reason in result.reasons
    )
    assert "close_missing" in result.reasons[0].detail


def test_artifact_after_the_cut_takes_precedence_over_unavailable_inputs() -> None:
    result = _infer(
        preview=_preview(features=_unavailable_features()),
        artifact=_artifact(available_at=_DECISION_AT + timedelta(seconds=1)),
    )
    assert result.status is InferenceStatus.ARTIFACT_NOT_AVAILABLE
    assert result.reasons[0].code is InferenceReasonCode.ARTIFACT_NOT_YET_AVAILABLE


def test_adapter_failure_malformed_or_out_of_contract_outputs_fail_explicitly() -> None:
    cases = [
        _SpyAdapter(error=RuntimeError("boom")),
        _SpyAdapter(output={"probability": "0.5"}),
        _SpyAdapter(
            output=ModelAdapterOutput.model_construct(
                adapter_id=CONSTANT_TEST_ADAPTER_ID,
                adapter_version=CONSTANT_TEST_ADAPTER_VERSION,
                output_kind="binary_probability",
                probability=Decimal("1.5"),
            )
        ),
        _SpyAdapter(
            output=ModelAdapterOutput.model_construct(
                adapter_id=CONSTANT_TEST_ADAPTER_ID,
                adapter_version=CONSTANT_TEST_ADAPTER_VERSION,
                output_kind="binary_probability",
                probability=Decimal("0.7"),
            )
        ),
        _SpyAdapter(
            output=ModelAdapterOutput(
                adapter_id="another-adapter",
                adapter_version=CONSTANT_TEST_ADAPTER_VERSION,
                output_kind="binary_probability",
                probability=CONSTANT_TEST_PROBABILITY,
            )
        ),
    ]
    for spy in cases:
        with pytest.raises(ModelInferenceExecutionError):
            _infer(adapter=spy)
        assert spy.calls == 1


def test_injected_adapter_identity_must_match_the_artifact() -> None:
    class _ForeignAdapter(_SpyAdapter):
        adapter_id = "foreign-adapter"

    spy = _ForeignAdapter()
    with pytest.raises(ModelInferenceExecutionError, match="identity"):
        _infer(adapter=spy)
    assert spy.calls == 0


def test_adapter_receives_only_three_ordered_values_and_compatibility_context() -> None:
    spy = _SpyAdapter()
    _infer(adapter=spy)
    assert spy.calls == 1
    inference_input = spy.inputs[0]
    assert len(inference_input.feature_values) == 3
    assert inference_input.feature_values == (
        Decimal("0.01"),
        Decimal("0.55"),
        Decimal("0.02"),
    )
    assert inference_input.context.asset.asset_id == _ASSET_ID
    assert inference_input.context.preview_id == _preview().preview_id
    assert not hasattr(inference_input, "storage")


def test_repeated_inference_is_byte_identical_and_ignores_the_execution_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact()
    preview = _preview()
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
    first = _infer(artifact=artifact, preview=preview)
    monkeypatch.setattr(time, "time", lambda: 2_000_000.0)
    second = _infer(artifact=artifact, preview=preview)

    assert first.inference_id == second.inference_id
    assert first.to_json_dict() == second.to_json_dict()
    assert json.dumps(first.to_json_dict(), sort_keys=True) == json.dumps(
        second.to_json_dict(), sort_keys=True
    )
    assert "execution_time" not in first.to_json_dict()


def test_result_links_the_explanation_and_recomputes_its_identity() -> None:
    result = _infer()
    assert result.explanation.inference_id == result.inference_id
    assert result.explanation.preview_id == result.preview_id
    assert result.explanation.status is result.status
    expected = model_inference_id(
        artifact_sha256=result.artifact_sha256,
        adapter_version=result.adapter_version,
        preview_id=result.preview_id,
        material_payload=result.material_payload(),
    )
    assert expected == result.inference_id
    with pytest.raises(ValidationError, match="inference_id"):
        ModelInferenceResult.model_validate(
            {**result.model_dump(mode="python"), "inference_id": UUID(int=1)}
        )


def test_json_projection_is_decimal_text_and_contains_no_floats() -> None:
    payload = _infer().to_json_dict()

    def walk(value: object) -> None:
        assert not isinstance(value, float)
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    assert payload["test_probability"] == "0.5"
    features = payload["explanation"]["features"]
    assert isinstance(features, list)
    assert features[0]["value"] == "0.01"
    assert all(isinstance(item["value"], str) for item in features)


def test_result_budget_fails_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    import investment_analyst.research.inference_service as service_module

    monkeypatch.setattr(service_module, "MAX_MODEL_INFERENCE_BYTES", 16)
    with pytest.raises(ModelInferenceBudgetError, match="exceeds 16 bytes"):
        _infer()


def test_request_rejects_a_preview_whose_identity_does_not_match() -> None:
    preview = _preview()
    tampered = preview.model_copy(
        update={"features": _features(values=(Decimal("9"), Decimal("9"), Decimal("9")))}
    )
    with pytest.raises(ValidationError, match="feature_hash"):
        ModelInferenceRequest(artifact=_artifact(), preview=tampered)


def test_artifact_contract_rejects_another_profile_or_parameters() -> None:
    document = json.loads(_artifact_document(_artifact()))
    document["asset_class"] = "equity"
    document["artifact_id"] = str(UUID(int=2))
    document["artifact_sha256"] = "1" * 64
    with pytest.raises(ValidationError):
        ModelArtifact.model_validate(document)

    document = json.loads(_artifact_document(_artifact()))
    document["parameters"] = {"family": "constant-test-v1", "test_probability": "0.6"}
    with pytest.raises(ValidationError):
        ModelArtifact.model_validate(document)


def test_service_rejects_an_artifact_whose_identity_is_tampered() -> None:
    artifact = _artifact()
    tampered = artifact.model_copy(update={"artifact_sha256": "0" * 64})
    with pytest.raises(ModelInferenceIntegrityError, match="artifact hash"):
        _infer(artifact=tampered)


def test_constant_adapter_is_the_only_resolved_engine() -> None:
    from investment_analyst.research.inference_adapter import (
        ModelInferenceAdapterError,
        resolve_model_inference_adapter,
    )

    artifact = _artifact()
    assert isinstance(resolve_model_inference_adapter(artifact), ConstantTestAdapter)
    tampered = artifact.model_copy(update={"adapter_version": "v2"})
    with pytest.raises(ModelInferenceAdapterError):
        resolve_model_inference_adapter(tampered)


def test_inference_result_rejects_abstention_with_probability_and_success_with_reason() -> None:
    success = _infer()
    with pytest.raises(ValidationError):
        ModelInferenceResult.model_validate(
            {
                **success.model_dump(mode="python"),
                "reasons": (
                    {
                        "code": InferenceReasonCode.FEATURE_INPUT_UNAVAILABLE,
                        "detail": "missing",
                    },
                ),
            }
        )
    abstention = _infer(preview=_preview(features=_unavailable_features()))
    with pytest.raises(ValidationError):
        ModelInferenceResult.model_validate(
            {
                **abstention.model_dump(mode="python"),
                "test_probability": CONSTANT_TEST_PROBABILITY,
            }
        )
