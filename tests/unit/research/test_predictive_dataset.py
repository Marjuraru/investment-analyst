"""Predictive dataset builder: PIT windows, states, splits and readiness."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.core.models import MetricResult, NormalizedObservation
from investment_analyst.core.models.enums import DataFrequency, DataQuality
from investment_analyst.core.models.source import SourceReference
from investment_analyst.research.predictive_dataset import (
    PredictiveDatasetBuilder,
    PredictiveDatasetError,
)
from investment_analyst.research.predictive_models import (
    CLOSE_RETURN_COLUMN,
    DECIMAL34,
    FEATURE_COLUMNS,
    MACD_HISTOGRAM_METRIC_KEY,
    PREREGISTERED_EXPERIMENT,
    RSI_METRIC_KEY,
    FeatureState,
    LabelState,
    PredictiveDatasetRequest,
    ReasonCode,
    SampleSplit,
    bar_start_instant,
    decision_instant,
    label_exit_bar_end,
)

_ASSET_ID = "crypto:btc-usd"
_SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"
_WORKSPACE_UUID = UUID("70c64668-1738-4dfe-ba1f-7e72026bb7fe")
_KNOWN_AT = datetime(2026, 8, 20, 3, 0, tzinfo=UTC)
_DECISION = date(2026, 8, 10)
_DECISION_AVAILABLE = decision_instant(_DECISION) - timedelta(hours=11)


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
    asset_id: str = _ASSET_ID,
    source_id: str = _SOURCE_ID,
    quality: DataQuality = DataQuality.VALID,
    unit: str = "USD",
) -> NormalizedObservation:
    start = bar_start_instant(day)
    available = available_at if available_at is not None else start + timedelta(days=1, hours=1)
    raw_record_id = uuid5(NAMESPACE_URL, f"{source_id}|{day}|{revision}")
    return NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"{raw_record_id}|close"),
        raw_record_id=raw_record_id,
        asset_id=asset_id,
        field_name="close",
        value=value,
        unit=unit,
        frequency=DataFrequency.DAY_1,
        observed_at=start,
        period_start=start,
        period_end=start + timedelta(days=1),
        available_at=available,
        normalized_at=available,
        source=SourceReference(
            source_id=source_id,
            retrieved_at=available,
            record_key=f"{day.isoformat()}|{revision}",
        ),
        quality=quality,
        transformation_version="coinbase-candle-normalizer-v1",
    )


def _metric(
    day: date,
    *,
    key: str,
    value: Decimal,
    available_at: datetime,
    computed_at: datetime,
    seed: str,
    version: str,
    unit: str,
    parameters: dict[str, object],
) -> MetricResult:
    return MetricResult(
        result_id=_uuid8(seed),
        asset_id=_ASSET_ID,
        metric_key=key,
        value=value,
        unit=unit,
        as_of=bar_start_instant(day),
        available_at=available_at,
        computed_at=computed_at,
        parameters={"source_id": _SOURCE_ID, **parameters},
        input_observation_ids=[uuid5(NAMESPACE_URL, f"input|{seed}")],
        algorithm_version=version,
        quality=DataQuality.VALID,
    )


def _rsi(
    day: date,
    value: Decimal,
    *,
    available_at: datetime,
    computed_at: datetime | None = None,
    seed: str | None = None,
    version: str = "market-rsi-incremental-v2-decimal34",
    unit: str = "index",
    window: int = 14,
) -> MetricResult:
    return _metric(
        day,
        key=RSI_METRIC_KEY,
        value=value,
        available_at=available_at,
        computed_at=computed_at if computed_at is not None else available_at,
        seed=seed if seed is not None else f"rsi|{day.isoformat()}",
        version=version,
        unit=unit,
        parameters={"window": window},
    )


def _macd(
    day: date,
    value: Decimal,
    *,
    available_at: datetime,
    computed_at: datetime | None = None,
    seed: str | None = None,
) -> MetricResult:
    return _metric(
        day,
        key=MACD_HISTOGRAM_METRIC_KEY,
        value=value,
        available_at=available_at,
        computed_at=computed_at if computed_at is not None else available_at,
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
    *,
    read_only: bool = True,
) -> tuple[PredictiveDatasetBuilder, _Storage]:
    storage = _Storage(observations, metrics)
    storage.read_only = read_only
    runtime = ApplicationRuntime.create_default()
    builder = PredictiveDatasetBuilder(
        storage,  # type: ignore[arg-type]
        provider_resolver=runtime.provider_resolver,
        catalog=runtime.catalog,
    )
    return builder, storage


def _request(
    start: date = _DECISION,
    end: date = _DECISION,
    known_at: datetime = _KNOWN_AT,
) -> PredictiveDatasetRequest:
    return PredictiveDatasetRequest(
        asset_id=_ASSET_ID,
        start_date=start,
        end_date=end,
        dataset_known_at=known_at,
    )


def _seed_closes(
    first_day: date,
    last_day: date,
    *,
    entry_close: Decimal = Decimal("100"),
) -> list[NormalizedObservation]:
    rows = []
    day = first_day
    index = 0
    while day <= last_day:
        rows.append(_observation(day, entry_close + Decimal(index)))
        day += timedelta(days=1)
        index += 1
    return rows


def _seed_metrics(day: date, *, available_at: datetime) -> list[MetricResult]:
    return [
        _rsi(day, Decimal("55"), available_at=available_at),
        _macd(day, Decimal("2"), available_at=available_at),
    ]


def test_builds_exact_feature_and_label_windows() -> None:
    closes = _seed_closes(_DECISION - timedelta(days=2), _DECISION + timedelta(days=5))
    metrics = _seed_metrics(_DECISION - timedelta(days=1), available_at=_DECISION_AVAILABLE)
    builder, storage = _builder(closes, metrics)
    dataset = builder.build(_request())

    assert len(dataset.samples) == 1
    sample = dataset.samples[0]
    assert tuple(item.column_name for item in sample.features) == FEATURE_COLUMNS
    assert sample.feature_state is FeatureState.AVAILABLE
    assert sample.label_state is LabelState.AVAILABLE
    assert sample.eligible is True
    assert sample.split is SampleSplit.HOLDOUT

    close_return = sample.features[0]
    with localcontext(DECIMAL34):
        expected_return = Decimal("101") / Decimal("100") - 1
    assert close_return.value == expected_return
    assert sample.features[1].value == Decimal("0.55")
    with localcontext(DECIMAL34):
        assert sample.features[2].value == Decimal("2") / Decimal("101")

    assert sample.label.entry_close == Decimal("102")
    assert sample.label.exit_close == Decimal("107")
    with localcontext(DECIMAL34):
        assert sample.label.return_5d == Decimal("107") / Decimal("102") - 1
    assert sample.label.label == 1
    assert len(sample.label.bars) == 6
    assert sample.label.bars[0].bar_start == bar_start_instant(_DECISION)
    assert sample.label.bars[-1].bar_start == bar_start_instant(_DECISION + timedelta(days=5))
    assert sample.label.exit_bar_end == label_exit_bar_end(_DECISION)
    assert sample.label.label_available_at == datetime(2026, 8, 16, 1, 0, tzinfo=UTC)

    close_call = storage.observations.calls[0]
    assert close_call["observed_from"] == bar_start_instant(_DECISION - timedelta(days=2))
    assert close_call["observed_before"] == bar_start_instant(_DECISION + timedelta(days=6))
    assert close_call["available_to"] == _KNOWN_AT
    assert close_call["field_name"] == "close"
    assert close_call["frequency"] is DataFrequency.DAY_1
    assert close_call["source_id"] == _SOURCE_ID


def test_metric_available_after_decision_is_not_yet_available() -> None:
    closes = _seed_closes(_DECISION - timedelta(days=2), _DECISION + timedelta(days=5))
    decision_at = decision_instant(_DECISION)
    metrics = [
        _rsi(
            _DECISION - timedelta(days=1),
            Decimal("55"),
            available_at=decision_at + timedelta(hours=1),
        ),
        _macd(
            _DECISION - timedelta(days=1),
            Decimal("2"),
            available_at=decision_at - timedelta(hours=1),
        ),
    ]
    builder, _ = _builder(closes, metrics)
    sample = builder.build(_request()).samples[0]
    assert sample.features[1].state is FeatureState.NOT_YET_AVAILABLE
    assert sample.features[1].reasons[0].code is ReasonCode.METRIC_NOT_YET_AVAILABLE
    assert sample.feature_state is FeatureState.NOT_YET_AVAILABLE
    assert sample.eligible is False


def test_incompatible_metric_version_is_reported() -> None:
    closes = _seed_closes(_DECISION - timedelta(days=2), _DECISION + timedelta(days=5))
    metrics = [
        _rsi(
            _DECISION - timedelta(days=1),
            Decimal("55"),
            available_at=_DECISION_AVAILABLE,
            version="market-rsi-wilder-v1-decimal34",
        ),
        _macd(
            _DECISION - timedelta(days=1),
            Decimal("2"),
            available_at=_DECISION_AVAILABLE,
        ),
    ]
    builder, _ = _builder(closes, metrics)
    sample = builder.build(_request()).samples[0]
    assert sample.features[1].state is FeatureState.INCOMPATIBLE
    assert sample.features[1].reasons[0].code is ReasonCode.METRIC_INCOMPATIBLE
    assert sample.feature_state is FeatureState.INCOMPATIBLE


def test_ambiguous_metric_revisions_fail_closed() -> None:
    closes = _seed_closes(_DECISION - timedelta(days=2), _DECISION + timedelta(days=5))
    decision = _DECISION - timedelta(days=1)
    available = _DECISION_AVAILABLE
    metrics = [
        _rsi(decision, Decimal("55"), available_at=available, seed="rsi-a"),
        _rsi(decision, Decimal("56"), available_at=available, seed="rsi-b"),
        _macd(decision, Decimal("2"), available_at=available),
    ]
    builder, _ = _builder(closes, metrics)
    sample = builder.build(_request()).samples[0]
    assert sample.features[1].state is FeatureState.AMBIGUOUS
    assert sample.features[1].reasons[0].code is ReasonCode.METRIC_AMBIGUOUS
    assert sample.feature_state is FeatureState.AMBIGUOUS
    assert sample.eligible is False


def test_ambiguous_close_revision_fails_closed() -> None:
    closes = _seed_closes(_DECISION - timedelta(days=2), _DECISION + timedelta(days=5))
    available = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    closes.extend(
        [
            _observation(
                _DECISION - timedelta(days=1),
                Decimal("101"),
                available_at=available,
                revision="r1",
            ),
            _observation(
                _DECISION - timedelta(days=1),
                Decimal("102"),
                available_at=available,
                revision="r2",
            ),
        ]
    )
    metrics = _seed_metrics(_DECISION - timedelta(days=1), available_at=_DECISION_AVAILABLE)
    builder, _ = _builder(closes, metrics)
    sample = builder.build(_request()).samples[0]
    assert sample.features[0].state is FeatureState.AMBIGUOUS
    assert sample.features[0].reasons[0].code is ReasonCode.CLOSE_AMBIGUOUS
    assert sample.label_state is LabelState.AVAILABLE
    assert sample.eligible is False


def test_ambiguous_label_bar_revision_fails_closed() -> None:
    closes = _seed_closes(_DECISION - timedelta(days=2), _DECISION + timedelta(days=5))
    available = datetime(2026, 8, 15, 2, 0, tzinfo=UTC)
    closes.extend(
        [
            _observation(
                _DECISION + timedelta(days=2),
                Decimal("104"),
                available_at=available,
                revision="r1",
            ),
            _observation(
                _DECISION + timedelta(days=2),
                Decimal("105"),
                available_at=available,
                revision="r2",
            ),
        ]
    )
    metrics = _seed_metrics(_DECISION - timedelta(days=1), available_at=_DECISION_AVAILABLE)
    builder, _ = _builder(closes, metrics)
    sample = builder.build(_request()).samples[0]
    assert sample.features[0].state is FeatureState.AVAILABLE
    assert sample.label_state is LabelState.AMBIGUOUS
    assert any(reason.code is ReasonCode.LABEL_CLOSE_AMBIGUOUS for reason in sample.label.reasons)
    assert sample.eligible is False


def test_missing_and_unmatured_labels_are_distinguished() -> None:
    closes = _seed_closes(_DECISION, _DECISION + timedelta(days=5))
    closes.extend(_seed_closes(_DECISION - timedelta(days=2), _DECISION - timedelta(days=1)))
    closes = [
        row for row in closes if row.observed_at != bar_start_instant(_DECISION + timedelta(days=3))
    ]
    metrics = _seed_metrics(_DECISION - timedelta(days=1), available_at=_DECISION_AVAILABLE)
    builder, _ = _builder(closes, metrics)
    missing = builder.build(_request()).samples[0]
    assert missing.label_state is LabelState.MISSING
    assert any(reason.code is ReasonCode.LABEL_CLOSE_MISSING for reason in missing.label.reasons)
    assert missing.eligible is False

    early_cut = datetime(2026, 8, 13, 0, 0, tzinfo=UTC)
    builder, _ = _builder(closes, metrics)
    unmatured = builder.build(_request(known_at=early_cut)).samples[0]
    assert unmatured.label_state is LabelState.NOT_YET_AVAILABLE
    assert any(reason.code is ReasonCode.LABEL_NOT_MATURED for reason in unmatured.label.reasons)


def test_label_uses_the_latest_revision_available_at_dataset_cut() -> None:
    closes = _seed_closes(_DECISION, _DECISION + timedelta(days=5))
    closes.extend(_seed_closes(_DECISION - timedelta(days=2), _DECISION - timedelta(days=1)))
    late_available = datetime(2026, 8, 17, 0, 30, tzinfo=UTC)
    closes.append(
        _observation(
            _DECISION + timedelta(days=5),
            Decimal("106"),
            available_at=late_available,
            revision="r1",
        )
    )
    metrics = _seed_metrics(_DECISION - timedelta(days=1), available_at=_DECISION_AVAILABLE)
    builder, _ = _builder(closes, metrics)
    latest = builder.build(_request(known_at=late_available + timedelta(minutes=1))).samples[0]
    assert latest.label.exit_close == Decimal("106")
    assert latest.label.label == 1

    builder, _ = _builder(closes, metrics)
    earlier = builder.build(_request(known_at=late_available - timedelta(minutes=1))).samples[0]
    assert earlier.label.exit_close == Decimal("105")
    assert earlier.label.bars[-1].observation_id != latest.label.bars[-1].observation_id


def test_splits_purge_and_readiness_are_reported() -> None:
    start = date(2026, 8, 3)
    end = date(2026, 8, 12)
    closes = _seed_closes(start - timedelta(days=2), end + timedelta(days=5))
    metrics = []
    day = start - timedelta(days=1)
    while day <= end - timedelta(days=1):
        metrics.extend(_seed_metrics(day, available_at=decision_instant(day + timedelta(days=1))))
        day += timedelta(days=1)
    builder, _ = _builder(closes, metrics)
    dataset = builder.build(_request(start=start, end=end, known_at=_KNOWN_AT))
    summary = dataset.summary

    assert summary.counts.requested == 10
    assert [item.split for item in summary.splits] == [
        SampleSplit.TRAIN,
        SampleSplit.VALIDATION,
        SampleSplit.HOLDOUT,
    ]
    assert [item.proposed for item in summary.splits] == [6, 2, 2]
    assert summary.counts.eligible_before_split == 10
    assert summary.readiness.unmet_requirements
    assert summary.splits[0].positive is not None
    assert summary.splits[1].positive is None
    assert summary.splits[2].negative is None
    assert summary.counts.purged == 8
    assert summary.splits[1].eligible_after_purge == 0


def test_builder_requires_read_only_storage() -> None:
    with pytest.raises(PredictiveDatasetError, match="read-only"):
        _builder([], [], read_only=False)


def test_execution_clock_does_not_change_dataset_identity() -> None:
    closes = _seed_closes(_DECISION - timedelta(days=2), _DECISION + timedelta(days=5))
    decision = _DECISION - timedelta(days=1)
    first_metrics = [
        _rsi(
            decision,
            Decimal("55"),
            available_at=_DECISION_AVAILABLE,
            computed_at=_DECISION_AVAILABLE,
        ),
        _macd(
            decision,
            Decimal("2"),
            available_at=_DECISION_AVAILABLE,
            computed_at=_DECISION_AVAILABLE,
        ),
    ]
    second_metrics = [
        _rsi(
            decision,
            Decimal("55"),
            available_at=_DECISION_AVAILABLE,
            computed_at=_DECISION_AVAILABLE + timedelta(hours=5),
        ),
        _macd(
            decision,
            Decimal("2"),
            available_at=_DECISION_AVAILABLE,
            computed_at=_DECISION_AVAILABLE + timedelta(hours=5),
        ),
    ]
    builder, _ = _builder(closes, first_metrics)
    first = builder.build(_request())
    builder, _ = _builder(closes, second_metrics)
    second = builder.build(_request())
    assert first.dataset_id == second.dataset_id
    assert first.dataset_content_sha256 == second.dataset_content_sha256


def test_metric_pages_are_bounded_and_scoped() -> None:
    closes = _seed_closes(_DECISION - timedelta(days=2), _DECISION + timedelta(days=5))
    decision = _DECISION - timedelta(days=1)
    metrics = []
    for index in range(300):
        metrics.append(
            _rsi(
                decision,
                Decimal("55"),
                available_at=_DECISION_AVAILABLE,
                seed=f"rsi-{index}",
            )
        )
    metrics.append(_macd(decision, Decimal("2"), available_at=_DECISION_AVAILABLE))
    builder, storage = _builder(closes, metrics)
    builder.build(_request())
    assert storage.metric_results.page_calls >= 2
    assert all(size <= 256 for size in storage.metric_results.chunk_sizes)
    assert PREREGISTERED_EXPERIMENT.experiment_spec_id
    assert CLOSE_RETURN_COLUMN in FEATURE_COLUMNS
