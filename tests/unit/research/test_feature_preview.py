"""Pre-decision feature preview: cut, states, identity and bounded projection."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import ValidationError

from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.core.models import MetricResult, NormalizedObservation
from investment_analyst.core.models.enums import DataFrequency, DataQuality
from investment_analyst.core.models.source import SourceReference
from investment_analyst.research import predictive_dataset as predictive_module
from investment_analyst.research.feature_preview_models import (
    FeaturePreviewAssetUnsupportedError,
    FeaturePreviewBudgetError,
    FeaturePreviewRequest,
    FeaturePreviewStatus,
    ResearchFeaturePreview,
)
from investment_analyst.research.predictive_dataset import PredictiveDatasetBuilder
from investment_analyst.research.predictive_models import (
    MACD_HISTOGRAM_METRIC_KEY,
    RSI_METRIC_KEY,
    FeatureState,
    PredictiveDatasetRequest,
    ReasonCode,
    bar_start_instant,
    decision_instant,
)

_ASSET_ID = "crypto:btc-usd"
_SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"
_WORKSPACE_UUID = UUID("70c64668-1738-4dfe-ba1f-7e72026bb7fe")
_DECISION = date(2026, 8, 10)
_DECISION_AT = decision_instant(_DECISION)
_METRIC_AVAILABLE = _DECISION_AT - timedelta(hours=11)
_LATER_KNOWN_AT = datetime(2026, 8, 20, 3, 0, tzinfo=UTC)


def _uuid8(seed: str) -> UUID:
    raw = bytearray(hashlib.sha256(seed.encode("utf-8")).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x80
    raw[8] = (raw[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(raw))


def _observation(
    day: date,
    value: Decimal,
    *,
    available_at: datetime | None = None,
    revision: str = "r0",
) -> NormalizedObservation:
    start = bar_start_instant(day)
    available = available_at if available_at is not None else start + timedelta(days=1, hours=1)
    raw_record_id = uuid5(NAMESPACE_URL, f"{_SOURCE_ID}|{day}|{revision}")
    return NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"{raw_record_id}|close"),
        raw_record_id=raw_record_id,
        asset_id=_ASSET_ID,
        field_name="close",
        value=value,
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=start,
        period_start=start,
        period_end=start + timedelta(days=1),
        available_at=available,
        normalized_at=available,
        source=SourceReference(
            source_id=_SOURCE_ID,
            retrieved_at=available,
            record_key=f"{day.isoformat()}|{revision}",
        ),
        quality=DataQuality.VALID,
        transformation_version="coinbase-candle-normalizer-v1",
    )


def _metric(
    day: date,
    *,
    key: str,
    value: Decimal,
    available_at: datetime,
    seed: str,
    version: str,
    unit: str,
    parameters: dict[str, object] | None = None,
    computed_at: datetime | None = None,
) -> MetricResult:
    return MetricResult(
        result_id=_uuid8(seed),
        asset_id=_ASSET_ID,
        metric_key=key,
        value=value,
        unit=unit,
        as_of=bar_start_instant(day),
        available_at=available_at,
        computed_at=computed_at if computed_at is not None else available_at,
        parameters={"source_id": _SOURCE_ID, **(parameters or {})},
        input_observation_ids=[uuid5(NAMESPACE_URL, f"input|{seed}")],
        algorithm_version=version,
        quality=DataQuality.VALID,
    )


def _rsi(
    day: date,
    value: Decimal,
    *,
    available_at: datetime,
    seed: str | None = None,
    version: str = "market-rsi-incremental-v2-decimal34",
    unit: str = "index",
    window: int = 14,
    legacy_known_at: str | None = None,
) -> MetricResult:
    return _metric(
        day,
        key=RSI_METRIC_KEY,
        value=value,
        available_at=available_at,
        seed=seed if seed is not None else f"rsi|{day.isoformat()}",
        version=version,
        unit=unit,
        parameters={
            "window": window,
            **({"known_at": legacy_known_at} if legacy_known_at is not None else {}),
        },
    )


def _macd(
    day: date,
    value: Decimal,
    *,
    available_at: datetime,
    seed: str | None = None,
) -> MetricResult:
    return _metric(
        day,
        key=MACD_HISTOGRAM_METRIC_KEY,
        value=value,
        available_at=available_at,
        seed=seed if seed is not None else f"macd|{day.isoformat()}",
        version="market-macd-incremental-v2-decimal34",
        unit="USD",
        parameters={"fast_window": 12, "slow_window": 26, "signal_window": 9},
    )


class _Paths:
    workspace_id = _WORKSPACE_UUID


class _Observations:
    def __init__(self, rows: list[NormalizedObservation]) -> None:
        self.rows = rows
        self.calls: list[dict[str, object]] = []

    def list(self, **filters: object) -> list[NormalizedObservation]:
        self.calls.append(dict(filters))
        output = []
        for row in self.rows:
            if row.asset_id != filters.get("asset_id"):
                continue
            if row.source.source_id != filters.get("source_id"):
                continue
            if row.frequency is not filters.get("frequency"):
                continue
            if row.field_name != filters.get("field_name"):
                continue
            observed_from = filters.get("observed_from")
            observed_before = filters.get("observed_before")
            if observed_from is not None and row.observed_at < observed_from:
                continue
            if observed_before is not None and row.observed_at >= observed_before:
                continue
            available_to = filters.get("available_to")
            if available_to is not None and row.available_at > available_to:
                continue
            output.append(row)
        return output


class _Metrics:
    def __init__(self, metrics: list[MetricResult]) -> None:
        self.metrics = metrics
        self.page_calls = 0
        self.chunk_sizes: list[int] = []

    def list_metric_index_page(self, query: object) -> tuple[object, ...]:
        from investment_analyst.analytics.analytical_access_models import MetricIndexEntry

        self.page_calls += 1
        keys = set(query.metric_keys)
        available = [
            metric
            for metric in self.metrics
            if metric.asset_id == query.asset_id
            and metric.metric_key in keys
            and metric.parameters.get("source_id") == query.source_id
            and metric.available_at <= query.known_at
            and metric.as_of >= query.as_of_from
            and metric.as_of < query.as_of_before
        ]
        available.sort(key=lambda item: (item.available_at, str(item.result_id)))
        if query.after is not None:
            available = [
                metric
                for metric in available
                if (metric.available_at, str(metric.result_id))
                > (query.after.available_at, str(query.after.result_id))
            ]
        page = available[: query.limit]
        return tuple(
            MetricIndexEntry(
                result_id=metric.result_id,
                asset_id=metric.asset_id,
                metric_key=metric.metric_key,
                as_of=metric.as_of,
                available_at=metric.available_at,
                source_id=metric.parameters.get("source_id"),
                frequency=metric.parameters.get("frequency"),
                legacy_known_at=metric.parameters.get("known_at"),
            )
            for metric in page
        )

    def get_many(self, identifiers: tuple[UUID, ...]) -> dict[UUID, MetricResult]:
        self.chunk_sizes.append(len(identifiers))
        mapping = {metric.result_id: metric for metric in self.metrics}
        return {identifier: mapping[identifier] for identifier in identifiers}


class _Storage:
    read_only = True

    def __init__(
        self,
        observations: list[NormalizedObservation],
        metrics: list[MetricResult],
    ) -> None:
        self.paths = _Paths()
        self.observations = _Observations(observations)
        self.metric_results = _Metrics(metrics)

    def require_open(self) -> None:
        return None


def _builder(
    observations: list[NormalizedObservation],
    metrics: list[MetricResult],
) -> tuple[PredictiveDatasetBuilder, _Storage]:
    storage = _Storage(observations, metrics)
    runtime = ApplicationRuntime.create_default()
    builder = PredictiveDatasetBuilder(
        storage,  # type: ignore[arg-type]
        provider_resolver=runtime.provider_resolver,
        catalog=runtime.catalog,
    )
    return builder, storage


def _seed_closes(first_day: date, last_day: date) -> list[NormalizedObservation]:
    rows = []
    day = first_day
    index = 0
    while day <= last_day:
        rows.append(_observation(day, Decimal("100") + Decimal(index)))
        day += timedelta(days=1)
        index += 1
    return rows


def _seed_metrics(day: date, *, available_at: datetime) -> list[MetricResult]:
    return [
        _rsi(day, Decimal("55"), available_at=available_at),
        _macd(day, Decimal("2"), available_at=available_at),
    ]


def _preview_request(
    *,
    known_at: datetime = _DECISION_AT,
    decision_date: date = _DECISION,
    asset_id: str = _ASSET_ID,
) -> FeaturePreviewRequest:
    return FeaturePreviewRequest(
        asset_id=asset_id,
        decision_date=decision_date,
        known_at=known_at,
    )


def test_request_accepts_only_an_explicit_cut_inside_the_decision_day() -> None:
    assert _preview_request(known_at=bar_start_instant(_DECISION)).known_at == bar_start_instant(
        _DECISION
    )
    assert _preview_request(known_at=_DECISION_AT).known_at == _DECISION_AT

    for known_at in (
        bar_start_instant(_DECISION) - timedelta(seconds=1),
        _DECISION_AT + timedelta(seconds=1),
        datetime(2026, 8, 10, 10, 0),
    ):
        with pytest.raises(ValidationError):
            _preview_request(known_at=known_at)

    for ambiguous_date in (
        "2026-08-10T00:00:00Z",
        datetime(2026, 8, 10, 0, 0, tzinfo=UTC),
    ):
        with pytest.raises(ValidationError):
            FeaturePreviewRequest(
                asset_id=_ASSET_ID,
                decision_date=ambiguous_date,
                known_at=_DECISION_AT,
            )

    with pytest.raises(ValidationError):
        FeaturePreviewRequest(  # type: ignore[call-arg]
            asset_id=_ASSET_ID,
            decision_date=_DECISION,
            known_at=_DECISION_AT,
            extra="forbidden",
        )


def test_preview_matches_dataset_identity_and_features_at_decision_cut() -> None:
    closes = _seed_closes(_DECISION - timedelta(days=2), _DECISION + timedelta(days=5))
    metrics = _seed_metrics(_DECISION - timedelta(days=1), available_at=_METRIC_AVAILABLE)
    builder, _ = _builder(closes, metrics)
    dataset = builder.build(
        PredictiveDatasetRequest(
            asset_id=_ASSET_ID,
            start_date=_DECISION,
            end_date=_DECISION,
            dataset_known_at=_LATER_KNOWN_AT,
        )
    )
    preview = builder.preview_features(_preview_request())

    sample = dataset.samples[0]
    assert preview.status is FeaturePreviewStatus.FEATURES_AVAILABLE
    assert preview.features == sample.features
    assert preview.sample_key == sample.sample_key
    assert preview.feature_hash == sample.feature_hash
    assert preview.preview_id != dataset.dataset_id
    assert preview.decision_at == _DECISION_AT
    assert len(preview.close_references) == 2
    assert tuple(item.period_start for item in preview.close_references) == (
        bar_start_instant(_DECISION - timedelta(days=2)),
        bar_start_instant(_DECISION - timedelta(days=1)),
    )

    payload = preview.to_json_dict()
    features = payload["features"]
    assert isinstance(features, list)
    assert features[1]["value"] == "0.55"
    assert isinstance(features[1]["value"], str)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert '"label":' not in serialized
    assert '"label_state":' not in serialized
    assert "research_ready" not in serialized
    assert '"row_id":' not in serialized


def test_preview_cut_reveals_only_inputs_available_at_that_instant() -> None:
    decision_day = bar_start_instant(_DECISION)
    closes = [
        _observation(
            _DECISION - timedelta(days=2),
            Decimal("100"),
            available_at=bar_start_instant(_DECISION - timedelta(days=1)) + timedelta(hours=1),
        ),
        _observation(
            _DECISION - timedelta(days=1),
            Decimal("101"),
            available_at=decision_day + timedelta(hours=10, minutes=30),
        ),
    ]
    metrics = _seed_metrics(
        _DECISION - timedelta(days=1),
        available_at=decision_day + timedelta(hours=10, minutes=45),
    )
    builder, _ = _builder(closes, metrics)

    early = builder.preview_features(_preview_request(known_at=decision_day + timedelta(hours=9)))
    assert early.status is FeaturePreviewStatus.INPUTS_UNAVAILABLE
    assert early.features[0].state is FeatureState.MISSING
    assert early.features[0].reasons[0].code is ReasonCode.CLOSE_MISSING
    assert early.features[1].state is FeatureState.MISSING
    assert early.features[2].state is FeatureState.MISSING
    assert len(early.close_references) == 1
    assert early.close_references[0].period_start == bar_start_instant(
        _DECISION - timedelta(days=2)
    )

    late = builder.preview_features(_preview_request(known_at=_DECISION_AT))
    assert late.status is FeaturePreviewStatus.FEATURES_AVAILABLE
    assert all(item.state is FeatureState.AVAILABLE for item in late.features)
    assert late.sample_key == early.sample_key
    assert late.preview_id != early.preview_id

    same = builder.preview_features(_preview_request(known_at=_DECISION_AT))
    assert same.preview_id == late.preview_id
    assert same.to_json_dict() == late.to_json_dict()


def test_preview_reads_only_d_minus_two_and_d_minus_one_closes() -> None:
    closes = _seed_closes(_DECISION - timedelta(days=2), _DECISION + timedelta(days=5))
    metrics = _seed_metrics(_DECISION - timedelta(days=1), available_at=_METRIC_AVAILABLE)
    builder, storage = _builder(closes, metrics)
    builder.preview_features(_preview_request())

    call = storage.observations.calls[0]
    assert call["observed_from"] == bar_start_instant(_DECISION - timedelta(days=2))
    assert call["observed_before"] == bar_start_instant(_DECISION)
    assert call["available_to"] == _DECISION_AT
    assert call["field_name"] == "close"
    assert call["frequency"] is DataFrequency.DAY_1
    assert storage.metric_results.page_calls == 1
    assert all(size <= 256 for size in storage.metric_results.chunk_sizes)


def test_preview_never_builds_the_dataset_or_reads_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closes = _seed_closes(_DECISION - timedelta(days=2), _DECISION + timedelta(days=5))
    metrics = _seed_metrics(_DECISION - timedelta(days=1), available_at=_METRIC_AVAILABLE)
    builder, _ = _builder(closes, metrics)

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("the preview must not build the dataset or read labels")

    monkeypatch.setattr(PredictiveDatasetBuilder, "build", forbidden)
    monkeypatch.setattr(PredictiveDatasetBuilder, "_label", forbidden)
    preview = builder.preview_features(_preview_request())
    assert preview.status is FeaturePreviewStatus.FEATURES_AVAILABLE


def test_preview_reports_missing_close_zero_indicator_and_ambiguous_revision() -> None:
    decision = _DECISION - timedelta(days=1)
    metrics = _seed_metrics(decision, available_at=_METRIC_AVAILABLE)
    closes = [
        _observation(_DECISION - timedelta(days=1), Decimal("101")),
    ]
    builder, _ = _builder(closes, metrics)
    missing = builder.preview_features(_preview_request())
    assert missing.status is FeaturePreviewStatus.INPUTS_UNAVAILABLE
    assert missing.features[0].state is FeatureState.MISSING
    assert missing.features[0].reasons[0].code is ReasonCode.CLOSE_MISSING
    assert len(missing.close_references) == 1

    zero_rsi_metrics = [
        _rsi(decision, Decimal("0"), available_at=_METRIC_AVAILABLE),
        _macd(decision, Decimal("2"), available_at=_METRIC_AVAILABLE),
    ]
    builder, _ = _builder(closes, zero_rsi_metrics)
    zero_rsi = builder.preview_features(_preview_request())
    assert zero_rsi.features[1].state is FeatureState.AVAILABLE
    assert zero_rsi.features[1].value == Decimal("0")

    zero_close = [
        _observation(_DECISION - timedelta(days=2), Decimal("100")),
        _observation(_DECISION - timedelta(days=1), Decimal("0")),
    ]
    builder, _ = _builder(zero_close, metrics)
    invalid = builder.preview_features(_preview_request())
    assert invalid.features[0].state is FeatureState.INCOMPATIBLE
    assert invalid.features[0].reasons[0].code is ReasonCode.CLOSE_INVALID

    ambiguous_available = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    ambiguous = [
        _observation(_DECISION - timedelta(days=2), Decimal("100")),
        _observation(
            _DECISION - timedelta(days=1),
            Decimal("101"),
            available_at=ambiguous_available,
            revision="r1",
        ),
        _observation(
            _DECISION - timedelta(days=1),
            Decimal("102"),
            available_at=ambiguous_available,
            revision="r2",
        ),
    ]
    builder, _ = _builder(ambiguous, metrics)
    ambiguous_preview = builder.preview_features(_preview_request())
    assert ambiguous_preview.features[0].state is FeatureState.AMBIGUOUS
    assert ambiguous_preview.features[0].reasons[0].code is ReasonCode.CLOSE_AMBIGUOUS


def test_preview_reports_incompatible_metric_contract() -> None:
    decision = _DECISION - timedelta(days=1)
    closes = _seed_closes(_DECISION - timedelta(days=2), _DECISION - timedelta(days=1))
    metrics = [
        _rsi(
            decision,
            Decimal("55"),
            available_at=_METRIC_AVAILABLE,
            version="market-rsi-wilder-v1-decimal34",
        ),
        _macd(decision, Decimal("2"), available_at=_METRIC_AVAILABLE),
    ]
    builder, _ = _builder(closes, metrics)
    preview = builder.preview_features(_preview_request())
    assert preview.features[1].state is FeatureState.INCOMPATIBLE
    assert preview.features[1].reasons[0].code is ReasonCode.METRIC_INCOMPATIBLE


def test_preview_respects_legacy_known_at() -> None:
    decision = _DECISION - timedelta(days=1)
    closes = _seed_closes(_DECISION - timedelta(days=2), _DECISION - timedelta(days=1))
    legacy_rsi = _rsi(
        decision,
        Decimal("55"),
        available_at=_METRIC_AVAILABLE,
        legacy_known_at="2026-08-10T12:00:00Z",
    ).model_copy(update={"result_id": uuid5(NAMESPACE_URL, "legacy-rsi")})
    metrics = [
        legacy_rsi,
        _macd(decision, Decimal("2"), available_at=_METRIC_AVAILABLE),
    ]
    builder, _ = _builder(closes, metrics)
    early = builder.preview_features(
        _preview_request(known_at=bar_start_instant(_DECISION) + timedelta(hours=10))
    )
    assert early.features[1].state is FeatureState.MISSING
    late = builder.preview_features(_preview_request())
    assert late.features[1].state is FeatureState.AVAILABLE
    assert late.features[1].value == Decimal("0.55")


def test_preview_rejects_assets_outside_crypto_spot_usd() -> None:
    builder, _ = _builder([], [])
    with pytest.raises(FeaturePreviewAssetUnsupportedError, match="crypto spot"):
        builder.preview_features(_preview_request(asset_id="equity:us:aapl"))


def test_preview_budget_fails_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    closes = _seed_closes(_DECISION - timedelta(days=2), _DECISION - timedelta(days=1))
    metrics = _seed_metrics(_DECISION - timedelta(days=1), available_at=_METRIC_AVAILABLE)
    builder, _ = _builder(closes, metrics)
    monkeypatch.setattr(predictive_module, "MAX_FEATURE_PREVIEW_BYTES", 16)
    with pytest.raises(FeaturePreviewBudgetError, match="exceeds 16 bytes"):
        builder.preview_features(_preview_request())


def test_preview_identity_changes_with_decision_date() -> None:
    closes = _seed_closes(_DECISION - timedelta(days=2), _DECISION + timedelta(days=1))
    metrics = _seed_metrics(_DECISION - timedelta(days=1), available_at=_METRIC_AVAILABLE)
    builder, _ = _builder(closes, metrics)
    first = builder.preview_features(_preview_request())
    later_date = _DECISION + timedelta(days=1)
    builder, _ = _builder(
        _seed_closes(later_date - timedelta(days=2), later_date),
        _seed_metrics(later_date - timedelta(days=1), available_at=decision_instant(later_date)),
    )
    second = builder.preview_features(
        _preview_request(decision_date=later_date, known_at=decision_instant(later_date))
    )
    assert first.sample_key != second.sample_key
    assert first.preview_id != second.preview_id
    assert isinstance(first, ResearchFeaturePreview)
