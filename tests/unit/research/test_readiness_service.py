"""Readiness projection: partitions, overlapping reasons, determinism and limits."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.core.models import MetricResult, NormalizedObservation
from investment_analyst.core.models.enums import DataFrequency, DataQuality
from investment_analyst.core.models.source import SourceReference
from investment_analyst.research import readiness_service as service_module
from investment_analyst.research.predictive_models import (
    CLOSE_RETURN_COLUMN,
    MACD_HISTOGRAM_COLUMN,
    MACD_HISTOGRAM_METRIC_KEY,
    RSI_COLUMN,
    RSI_METRIC_KEY,
    FeatureState,
    PredictiveDatasetCounts,
    PredictiveDatasetRequest,
    ReasonCode,
    bar_start_instant,
    decision_instant,
)
from investment_analyst.research.readiness_models import MAX_READINESS_VIEW_BYTES
from investment_analyst.research.readiness_service import (
    ReadinessAssetUnsupportedError,
    ReadinessIntegrityError,
    ReadinessService,
    ReadinessServiceError,
)

_ASSET_ID = "crypto:btc-usd"
_SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"
_WORKSPACE_UUID = UUID("70c64668-1738-4dfe-ba1f-7e72026bb7fe")
_DECISION = date(2026, 8, 10)
_KNOWN_AT = datetime(2026, 8, 20, 3, 0, tzinfo=UTC)
_AVAILABLE = decision_instant(_DECISION) - timedelta(hours=11)


def _uuid8(seed: str) -> UUID:
    raw = bytearray(hashlib.sha256(seed.encode("utf-8")).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x80
    raw[8] = (raw[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(raw))


def _observation(day: date, value: Decimal) -> NormalizedObservation:
    start = bar_start_instant(day)
    available = start + timedelta(days=1, hours=1)
    raw_record_id = uuid5(NAMESPACE_URL, f"{_SOURCE_ID}|{day.isoformat()}")
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
            record_key=day.isoformat(),
        ),
        quality=DataQuality.VALID,
        transformation_version="coinbase-candle-normalizer-v1",
    )


def _metric(
    day: date,
    *,
    key: str,
    value: Decimal,
    unit: str,
    version: str,
    parameters: dict[str, object],
    seed: str,
) -> MetricResult:
    return MetricResult(
        result_id=_uuid8(seed),
        asset_id=_ASSET_ID,
        metric_key=key,
        value=value,
        unit=unit,
        as_of=bar_start_instant(day),
        available_at=_AVAILABLE,
        computed_at=_AVAILABLE,
        parameters={"source_id": _SOURCE_ID, **parameters},
        input_observation_ids=[uuid5(NAMESPACE_URL, f"input|{seed}")],
        algorithm_version=version,
        quality=DataQuality.VALID,
    )


class _Paths:
    workspace_id = _WORKSPACE_UUID


class _Observations:
    def __init__(self, rows: list[NormalizedObservation]) -> None:
        self.rows = rows

    def list(self, **filters: object) -> list[NormalizedObservation]:
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

    def list_metric_index_page(self, query: object) -> tuple[object, ...]:
        from investment_analyst.analytics.analytical_access_models import MetricIndexEntry

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
        mapping = {metric.result_id: metric for metric in self.metrics}
        return {identifier: mapping[identifier] for identifier in identifiers}


class _Storage:
    def __init__(
        self,
        observations: list[NormalizedObservation],
        metrics: list[MetricResult],
        *,
        read_only: bool = True,
    ) -> None:
        self.paths = _Paths()
        self.observations = _Observations(observations)
        self.metric_results = _Metrics(metrics)
        self.read_only = read_only

    def require_open(self) -> None:
        return None


def _service(
    observations: list[NormalizedObservation],
    metrics: list[MetricResult],
    *,
    read_only: bool = True,
) -> ReadinessService:
    runtime = ApplicationRuntime.create_default()
    return ReadinessService(
        _Storage(observations, metrics, read_only=read_only),  # type: ignore[arg-type]
        provider_resolver=runtime.provider_resolver,
        catalog=runtime.catalog,
    )


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


def _available_inputs() -> tuple[list[NormalizedObservation], list[MetricResult]]:
    closes = [
        _observation(_DECISION + timedelta(days=offset), Decimal(100 + offset))
        for offset in range(-2, 6)
    ]
    metrics = [
        _metric(
            _DECISION - timedelta(days=1),
            key=RSI_METRIC_KEY,
            value=Decimal("55"),
            unit="index",
            version="market-rsi-incremental-v2-decimal34",
            parameters={"window": 14},
            seed="rsi",
        ),
        _metric(
            _DECISION - timedelta(days=1),
            key=MACD_HISTOGRAM_METRIC_KEY,
            value=Decimal("2"),
            unit="USD",
            version="market-macd-incremental-v2-decimal34",
            parameters={"fast_window": 12, "slow_window": 26, "signal_window": 9},
            seed="macd",
        ),
    ]
    return closes, metrics


def test_every_column_partitions_the_requested_decisions() -> None:
    closes, metrics = _available_inputs()
    view = _service(closes, metrics).query(_request())
    payload = view.to_json_dict()
    assert payload["schema_version"] == "research-pit-readiness-view-v1"
    assert payload["workspace_uuid"] == str(_WORKSPACE_UUID)
    assert [column["column_name"] for column in payload["feature_columns"]] == [
        CLOSE_RETURN_COLUMN,
        RSI_COLUMN,
        MACD_HISTOGRAM_COLUMN,
    ]
    for column in payload["feature_columns"]:
        assert column["requested"] == 1
        assert [entry["state"] for entry in column["states"]] == [
            "available",
            "missing",
            "not_yet_available",
            "incompatible",
            "ambiguous",
        ]
        assert sum(entry["samples"] for entry in column["states"]) == column["requested"]
        assert column["reasons"] == []
    assert payload["reason_scope"] == "column_reason_occurrences"


def test_reason_incidences_may_overlap_beyond_the_requested_count() -> None:
    view = _service([], []).query(_request(_DECISION, _DECISION + timedelta(days=2)))
    payload = view.to_json_dict()
    assert payload["summary"]["counts"]["requested"] == 3
    close_return = payload["feature_columns"][0]
    assert close_return["states"][2]["state"] == "not_yet_available"
    assert close_return["states"][2]["samples"] == 0
    missing_reasons = {reason["code"]: reason["occurrences"] for reason in close_return["reasons"]}
    assert missing_reasons == {ReasonCode.CLOSE_MISSING.value: 6}
    assert missing_reasons[ReasonCode.CLOSE_MISSING.value] > close_return["requested"]
    assert [reason["code"] for reason in close_return["reasons"]] == sorted(missing_reasons)


def test_projection_never_exposes_rows_values_or_heldout_targets() -> None:
    closes, metrics = _available_inputs()
    view = _service(closes, metrics).query(_request())
    payload = view.to_json_dict()
    assert set(payload) == {
        "schema_version",
        "workspace_uuid",
        "summary",
        "feature_columns",
        "reason_scope",
        "limitations",
    }
    serialized = json.dumps(payload)
    assert "samples" not in payload
    assert "rows" not in payload
    assert "manifest" not in payload
    assert "entry_close" not in serialized
    assert "return_5d" not in serialized
    for split in payload["summary"]["splits"]:
        if split["split"] != "train":
            assert split["positive"] is None
            assert split["negative"] is None


def test_projection_is_deterministic_across_instances() -> None:
    closes, metrics = _available_inputs()
    first = _service(list(closes), list(metrics)).query(_request()).to_json_dict()
    second = _service(list(closes), list(metrics)).query(_request()).to_json_dict()
    assert first == second
    assert first["summary"]["dataset_id"] == second["summary"]["dataset_id"]
    assert first["summary"]["dataset_content_sha256"] == second["summary"]["dataset_content_sha256"]


def test_budget_is_enforced_without_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    closes, metrics = _available_inputs()
    monkeypatch.setattr(service_module, "MAX_READINESS_VIEW_BYTES", 1)
    with pytest.raises(ReadinessServiceError, match="response budget"):
        _service(closes, metrics).query(_request())


def test_unsupported_asset_is_rejected_before_projection() -> None:
    with pytest.raises(ReadinessAssetUnsupportedError):
        _service([], []).query(
            PredictiveDatasetRequest(
                asset_id="equity:us:aapl",
                start_date=_DECISION,
                end_date=_DECISION,
                dataset_known_at=_KNOWN_AT,
            )
        )


def test_writable_storage_is_rejected() -> None:
    with pytest.raises(ReadinessServiceError, match="read-only"):
        _service([], [], read_only=False)


def test_incoherent_summary_counts_are_rejected() -> None:
    closes, metrics = _available_inputs()
    service = _service(closes, metrics)
    dataset = service._build(_request())  # noqa: SLF001
    tampered = PredictiveDatasetCounts(
        requested=1,
        feature_available=0,
        feature_missing=1,
        feature_not_yet_available=0,
        feature_incompatible=0,
        feature_ambiguous=0,
        label_available=1,
        label_missing=0,
        label_not_yet_available=0,
        label_incompatible=0,
        label_ambiguous=0,
        eligible_before_split=0,
        purged=0,
    )
    with pytest.raises(ReadinessIntegrityError, match="differ from the summary"):
        ReadinessService._verify_sample_states(dataset.samples, tampered)  # noqa: SLF001


def test_available_and_unavailable_states_are_counted_per_column() -> None:
    closes, metrics = _available_inputs()
    view = _service(closes, metrics).query(_request(_DECISION, _DECISION + timedelta(days=1)))
    payload = view.to_json_dict()
    assert payload["summary"]["counts"]["requested"] == 2
    close_return = payload["feature_columns"][0]
    assert close_return["states"][0] == {"state": "available", "samples": 2}
    assert close_return["reasons"] == []
    rsi = payload["feature_columns"][1]
    assert rsi["states"][0] == {"state": "available", "samples": 1}
    assert rsi["states"][1] == {"state": "missing", "samples": 1}
    assert [reason["code"] for reason in rsi["reasons"]] == [ReasonCode.METRIC_MISSING.value]
    assert payload["summary"]["readiness"]["status"] == "insufficient_pit_sample"
    assert payload["summary"]["readiness"]["unmet_requirements"]
    assert any("research_ready" in limitation for limitation in payload["limitations"])


def test_reason_ordering_and_projection_budget_constant_are_declared() -> None:
    assert MAX_READINESS_VIEW_BYTES == 262_144
    closes, metrics = _available_inputs()
    payload = _service(closes, metrics).query(_request()).to_json_dict()
    codes: Callable[[ReasonCode], str] = lambda code: code.value  # noqa: E731
    for column in payload["feature_columns"]:
        values = [reason["code"] for reason in column["reasons"]]
        assert values == sorted(values, key=codes)
    assert payload["feature_columns"][0]["algorithm_version"] == ("predictive-close-ratio-1d-v1")
    assert payload["feature_columns"][0]["unit"] == "ratio"
    assert FeatureState.AVAILABLE.value == "available"
