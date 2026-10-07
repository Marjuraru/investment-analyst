"""Deterministic access contract over independent in-memory read adapters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from investment_analyst.analytics.analysis_snapshot import build_analysis_snapshot
from investment_analyst.analytics.analytical_access_models import (
    FeatureSetSpec,
    MetricIndexEntry,
    MetricSeriesQuery,
)
from investment_analyst.analytics.analytical_access_service import (
    AnalyticalAccessError,
    AnalyticalAccessService,
)
from investment_analyst.core.models import (
    DataQuality,
    MetricCategory,
    MetricDefinition,
    MetricResult,
)

_START = datetime(2026, 9, 1, tzinfo=UTC)
_CUT = datetime(2026, 9, 10, tzinfo=UTC)
_KEY = "market.technical.ema"


def _metrics(*, legacy_parameters: bool, computed_offset: int) -> tuple[MetricResult, ...]:
    output: list[MetricResult] = []
    for index in range(3):
        available_at = _START + timedelta(days=index)
        parameters = {"source_id": "source:test", "frequency": "1Day"}
        if legacy_parameters:
            parameters["known_at"] = _CUT.isoformat()
        output.append(
            MetricResult(
                result_id=UUID(int=10_000 + index),
                asset_id="equity:us:aapl",
                metric_key=_KEY,
                value=Decimal("125.3400") + Decimal(index),
                unit="USD",
                as_of=available_at,
                available_at=available_at,
                computed_at=available_at + timedelta(hours=computed_offset),
                parameters=parameters,
                input_observation_ids=[UUID(int=20_000 + index)],
                algorithm_version="ema-decimal34-v2",
                quality=DataQuality.VALID,
            )
        )
    return tuple(output)


def _definition() -> MetricDefinition:
    return MetricDefinition(
        metric_key=_KEY,
        display_name="Exponential moving average",
        category=MetricCategory.MARKET,
        description="Persisted descriptive moving average.",
        formula="EMA(close, window)",
        unit="USD",
        default_parameters={"window": 20},
        limitations=["Historical market coverage is source-specific."],
        references=["official-market-bars"],
        definition_version="ema-definition-v2",
    )


class _DictionaryReadPort:
    """Index-map adapter with ID lookups, independent of DuckDB and SQL."""

    def __init__(self, metrics: tuple[MetricResult, ...]) -> None:
        self.metrics = {item.result_id: item for item in metrics}
        self.hydrated: list[UUID] = []

    def validate_snapshot(self, snapshot) -> None:
        assert set(snapshot.metric_ids).issubset(self.metrics)

    def list_metric_index_page(self, query: MetricSeriesQuery) -> tuple[MetricIndexEntry, ...]:
        entries = tuple(
            sorted(
                (
                    _index_entry(item)
                    for item in self.metrics.values()
                    if item.asset_id == query.asset_id
                    and item.metric_key in query.metric_keys
                    and item.available_at <= query.known_at
                    and item.parameters.get("source_id") == query.source_id
                    and item.parameters.get("frequency") == query.frequency
                    and (query.as_of_from is None or item.as_of >= query.as_of_from)
                    and (query.as_of_before is None or item.as_of < query.as_of_before)
                ),
                key=lambda item: (item.available_at, str(item.result_id)),
            )
        )
        if query.after is not None:
            entries = tuple(
                item
                for item in entries
                if (item.available_at, str(item.result_id))
                > (query.after.available_at, str(query.after.result_id))
            )
        return entries[: query.limit]

    def get_metric_results(self, result_ids) -> dict[UUID, MetricResult]:
        identifiers = tuple(result_ids)
        self.hydrated.extend(identifiers)
        return {identifier: self.metrics[identifier] for identifier in identifiers}

    def get_diagnostic_results(self, diagnostic_ids):
        return {}

    def get_metric_definition(self, metric_key: str) -> MetricDefinition:
        assert metric_key == _KEY
        return _definition()


class _SequenceReadPort:
    """Sequence-backed adapter with cursor filtering, independent of the index-map adapter."""

    def __init__(self, metrics: tuple[MetricResult, ...]) -> None:
        self.rows = metrics
        self.hydrated: list[UUID] = []

    def validate_snapshot(self, snapshot) -> None:
        row_ids = {row.result_id for row in self.rows}
        assert row_ids.issuperset(snapshot.metric_ids)

    def list_metric_index_page(self, query: MetricSeriesQuery) -> tuple[MetricIndexEntry, ...]:
        page: list[MetricIndexEntry] = []
        for row in sorted(self.rows, key=lambda item: (item.available_at, str(item.result_id))):
            source = row.parameters.get("source_id")
            frequency = row.parameters.get("frequency")
            legacy_cut = row.parameters.get("known_at")
            if row.asset_id != query.asset_id or row.metric_key not in query.metric_keys:
                continue
            if row.available_at > query.known_at or source != query.source_id:
                continue
            if frequency != query.frequency:
                continue
            if query.as_of_from and row.as_of < query.as_of_from:
                continue
            if query.as_of_before and row.as_of >= query.as_of_before:
                continue
            if legacy_cut is not None and datetime.fromisoformat(str(legacy_cut)) > query.known_at:
                continue
            if query.after and (row.available_at, str(row.result_id)) <= (
                query.after.available_at,
                str(query.after.result_id),
            ):
                continue
            page.append(_index_entry(row))
            if len(page) == query.limit:
                break
        return tuple(page)

    def get_metric_results(self, result_ids) -> dict[UUID, MetricResult]:
        identifiers = tuple(result_ids)
        self.hydrated.extend(identifiers)
        selected = set(identifiers)
        return {item.result_id: item for item in self.rows if item.result_id in selected}

    def get_diagnostic_results(self, diagnostic_ids):
        return {}

    def get_metric_definition(self, metric_key: str) -> MetricDefinition:
        assert metric_key == _KEY
        return _definition()


def _index_entry(metric: MetricResult) -> MetricIndexEntry:
    legacy_cut = metric.parameters.get("known_at")
    return MetricIndexEntry(
        result_id=metric.result_id,
        asset_id=metric.asset_id,
        metric_key=metric.metric_key,
        as_of=metric.as_of,
        available_at=metric.available_at,
        source_id=str(metric.parameters["source_id"]),
        frequency=str(metric.parameters["frequency"]),
        legacy_known_at=str(legacy_cut) if legacy_cut is not None else None,
    )


def _snapshot(metrics: tuple[MetricResult, ...]):
    return build_analysis_snapshot(
        asset_id="equity:us:aapl",
        domain="market",
        known_at=_CUT,
        policy_version="access-test-v1",
        metric_ids=tuple(item.result_id for item in metrics),
        created_at=datetime(2026, 9, 11, tzinfo=UTC),
    )


def test_independent_adapters_produce_identical_features_explanations_and_hashes() -> None:
    legacy_rows = _metrics(legacy_parameters=True, computed_offset=1)
    compact_rows = _metrics(legacy_parameters=False, computed_offset=5)
    snapshot = _snapshot(legacy_rows)
    feature_set = FeatureSetSpec(
        feature_set_id="daily-market-features",
        version="2",
        domain="market",
        metric_keys=(_KEY,),
    )
    legacy_port = _DictionaryReadPort(legacy_rows)
    compact_port = _SequenceReadPort(compact_rows)
    legacy = AnalyticalAccessService(legacy_port)
    compact = AnalyticalAccessService(compact_port)

    legacy_features = legacy.features(snapshot, feature_set)
    compact_features = compact.features(snapshot, feature_set)
    assert legacy_features == compact_features
    assert legacy_features.schema_hash == compact_features.schema_hash
    assert legacy_features.content_hash == compact_features.content_hash

    legacy_series = legacy.metric_series(
        MetricSeriesQuery(
            asset_id=snapshot.asset_id,
            domain=snapshot.domain,
            known_at=snapshot.known_at,
            metric_keys=(_KEY,),
            source_id="source:test",
            frequency="1Day",
            limit=2,
        )
    )
    compact_series = compact.metric_series(
        MetricSeriesQuery(
            asset_id=snapshot.asset_id,
            domain=snapshot.domain,
            known_at=snapshot.known_at,
            metric_keys=(_KEY,),
            source_id="source:test",
            frequency="1Day",
            limit=2,
        )
    )
    assert legacy_series == compact_series
    assert legacy_series.truncated
    assert legacy_series.next_cursor is not None
    assert [item.result_id for item in legacy_series.items] == [
        item.result_id for item in compact_series.items
    ]

    legacy_explanation = legacy.explain(snapshot, (legacy_rows[0].result_id,))
    compact_explanation = compact.explain(snapshot, (compact_rows[0].result_id,))
    assert legacy_explanation == compact_explanation
    assert legacy_explanation.content_hash == compact_explanation.content_hash

    legacy_evidence = legacy.evidence(snapshot, metric_result_ids=(legacy_rows[0].result_id,))
    compact_evidence = compact.evidence(snapshot, metric_result_ids=(compact_rows[0].result_id,))
    assert legacy_evidence == compact_evidence
    assert legacy_evidence.content_hash == compact_evidence.content_hash


def test_metric_index_future_scope_is_rejected_before_metric_hydration() -> None:
    rows = _metrics(legacy_parameters=False, computed_offset=1)
    port = _DictionaryReadPort(rows)
    future_entry = _index_entry(rows[0]).model_copy(
        update={"available_at": _CUT + timedelta(days=1)}
    )
    port.list_metric_index_page = lambda query: (future_entry,)
    service = AnalyticalAccessService(port)

    with pytest.raises(AnalyticalAccessError, match="future result"):
        service.metric_series(
            MetricSeriesQuery(
                asset_id="equity:us:aapl",
                domain="market",
                known_at=_CUT,
                metric_keys=(_KEY,),
                source_id="source:test",
                frequency="1Day",
            )
        )
    assert port.hydrated == []
