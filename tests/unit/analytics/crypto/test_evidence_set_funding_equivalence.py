"""Equivalence between the derivatives engine's funding lineage and the EvidenceSet contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from uuid import NAMESPACE_URL, uuid5

from investment_analyst.analytics.crypto.derivatives_engine import (
    FUNDING_SUM_KEY,
    CryptoDerivativesMetricEngine,
)
from investment_analyst.analytics.evidence_set import (
    EvidenceSegment,
    EvidenceSet,
    build_evidence_segments,
    build_evidence_set,
    canonical_lineage_hash,
    resolve_evidence_set,
)
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    MetricResult,
    NormalizedObservation,
    SourceReference,
)

_ASSET = "crypto:btc-usd"
_FUNDING = "deribit:btc-perpetual:funding-rate-history"
_DVOL = "deribit:btc:dvol:daily"
_SUMMARY = "deribit:btc-perpetual:book-summary"
_START = datetime(2026, 6, 1, tzinfo=UTC)
_DAYS = 60
_END = _START + timedelta(days=_DAYS)
_AVAILABLE = datetime(2026, 9, 1, tzinfo=UTC)
_KNOWN = datetime(2026, 9, 2, tzinfo=UTC)


def _funding_series(days: int = _DAYS) -> tuple[NormalizedObservation, ...]:
    """One hourly funding series of whole UTC days, single asset, source and field."""
    return tuple(
        NormalizedObservation(
            observation_id=uuid5(NAMESPACE_URL, f"funding:{index}"),
            raw_record_id=uuid5(NAMESPACE_URL, f"funding-raw:{index}"),
            asset_id=_ASSET,
            field_name="funding_interest_1h",
            value=Decimal("0.000001") * Decimal(index % 7 + 1),
            unit="ratio",
            frequency=DataFrequency.HOUR_1,
            observed_at=_START + timedelta(hours=index),
            available_at=_AVAILABLE,
            normalized_at=_AVAILABLE + timedelta(minutes=1),
            source=SourceReference(
                source_id=_FUNDING,
                record_key=f"funding:{index}",
                retrieved_at=_AVAILABLE,
            ),
            quality=DataQuality.VALID,
            transformation_version="evidence-set-equivalence-v1",
        )
        for index in range(days * 24)
    )


def _funding_metrics(
    series: tuple[NormalizedObservation, ...],
    *,
    known_at: datetime = _KNOWN,
) -> tuple[MetricResult, ...]:
    computation = CryptoDerivativesMetricEngine().compute(
        tuple(series),
        asset_id=_ASSET,
        funding_source_id=_FUNDING,
        dvol_source_id=_DVOL,
        summary_source_id=_SUMMARY,
        known_at=known_at,
        computed_at=known_at,
        as_of_from=_START,
        as_of_before=_END,
    )
    return tuple(item for item in computation.results if item.metric_key == FUNDING_SUM_KEY)


@lru_cache(maxsize=1)
def _funding_evidence() -> tuple[
    tuple[NormalizedObservation, ...],
    tuple[EvidenceSegment, ...],
    tuple[MetricResult, ...],
]:
    series = _funding_series()
    return series, build_evidence_segments(series), _funding_metrics(series)


@lru_cache(maxsize=1)
def _evidence_by_metric() -> dict[int, EvidenceSet]:
    series, segments, metrics = _funding_evidence()
    by_identifier = {item.observation_id: item for item in series}
    return {
        index: build_evidence_set(
            tuple(by_identifier[identifier] for identifier in metric.input_observation_ids),
            segments=segments,
        )
        for index, metric in enumerate(metrics)
    }


def _canonical_bytes(model: EvidenceSet | EvidenceSegment) -> int:
    document = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return len(document.encode("utf-8"))


def test_every_engine_funding_metric_resolves_to_its_exact_lineage() -> None:
    """A6: every funding metric of the real engine resolves to its exact lineage."""
    series, segments, metrics = _funding_evidence()
    by_identifier = {item.observation_id: item for item in series}

    assert metrics
    assert len(segments) == _DAYS
    for metric in metrics:
        window = tuple(by_identifier[identifier] for identifier in metric.input_observation_ids)
        evidence = build_evidence_set(window, segments=segments)
        resolved = resolve_evidence_set(evidence, segments)

        assert resolved == tuple(metric.input_observation_ids)
        assert evidence.input_count == len(metric.input_observation_ids)
        assert evidence.available_at == metric.available_at
        assert evidence.canonical_hash == canonical_lineage_hash(
            tuple(metric.input_observation_ids)
        )
        assert evidence.first_observation_id == metric.input_observation_ids[0]
        assert evidence.last_observation_id == metric.input_observation_ids[-1]

    assert len(metrics) > 1000


def test_metrics_sharing_a_cut_and_window_share_one_evidence_set() -> None:
    """I1: metrics sharing a cut and window share one evidence set."""
    series, segments, metrics = _funding_evidence()
    evidences = _evidence_by_metric()
    target_as_of = _START + timedelta(hours=500)

    def pick(key: str, known_at: datetime = _KNOWN) -> tuple[int, MetricResult]:
        candidates = _funding_metrics(series, known_at=known_at)
        for index, metric in enumerate(metrics):
            share = candidates[index]
            if (
                metric.metric_key == key
                and metric.parameters["window"] == 24
                and metric.as_of == target_as_of
            ):
                assert share.metric_key == key and share.as_of == target_as_of
                return index, metric
        raise AssertionError(f"no {key} metric at {target_as_of}")

    sum_index, sum_metric = pick(FUNDING_SUM_KEY)
    _, repeated_sum = pick(FUNDING_SUM_KEY, _KNOWN + timedelta(days=1))

    assert tuple(repeated_sum.input_observation_ids) == tuple(sum_metric.input_observation_ids)
    by_identifier = {item.observation_id: item for item in series}
    repeated_evidence = build_evidence_set(
        tuple(by_identifier[identifier] for identifier in repeated_sum.input_observation_ids),
        segments=segments,
    )
    assert repeated_evidence.evidence_set_id == evidences[sum_index].evidence_set_id
    assert repeated_evidence == evidences[sum_index]


def test_lineage_bytes_are_at_most_ten_percent_of_the_repeated_lists() -> None:
    """A8: the segmented lineage costs at most a tenth of the repeated identifier lists."""
    _, segments, metrics = _funding_evidence()
    evidences = _evidence_by_metric()

    repeated_metrics = tuple(
        metric.model_copy(update={"computed_at": _KNOWN + timedelta(days=offset)})
        for offset in range(5)
        for metric in metrics
    )
    baseline = sum(
        len(
            json.dumps(
                [str(identifier) for identifier in metric.input_observation_ids],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        for metric in repeated_metrics
    )
    distinct_sets = {item.evidence_set_id: item for item in evidences.values()}
    representation = sum(_canonical_bytes(segment) for segment in segments) + sum(
        _canonical_bytes(item) for item in distinct_sets.values()
    )

    assert baseline > 10_000_000
    assert len(distinct_sets) < len(repeated_metrics)
    assert len(segments) == _DAYS
    assert representation * 10 <= baseline
