"""Focused append-only persistence tests for corporate valuation."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from investment_analyst.analytics.valuation import (
    CorporateValuationPersistencePipeline,
    CorporateValuationRequest,
    CorporateValuationSnapshot,
    ValuationCoverage,
    ValuationMetricDefinition,
    ValuationMetricValue,
    ValuationSecurityBasis,
    ValuationSnapshotStatus,
    ValuationStatus,
)
from investment_analyst.core.models import MetricDefinition, MetricResult

_KNOWN_AT = datetime(2026, 2, 1, tzinfo=UTC)
_AS_OF = datetime(2026, 1, 30, tzinfo=UTC)
_AVAILABLE_AT = datetime(2026, 1, 31, tzinfo=UTC)
_PERIOD_END = datetime(2025, 9, 27, tzinfo=UTC)
_INPUT_ID = UUID("11111111-1111-1111-1111-111111111111")


class _Definitions:
    def __init__(self) -> None:
        self.rows: list[MetricDefinition] = []

    def list_all(self) -> list[MetricDefinition]:
        return list(self.rows)

    def upsert(self, definition: MetricDefinition) -> MetricDefinition:
        self.rows.append(definition)
        return definition


class _Results:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.rows: list[MetricResult] = []
        self.fail_after = fail_after

    def list(self, *, asset_id: str | None = None) -> list[MetricResult]:
        return [row for row in self.rows if asset_id is None or row.asset_id == asset_id]

    def save(self, result: MetricResult) -> MetricResult:
        if self.fail_after is not None and len(self.rows) >= self.fail_after:
            raise RuntimeError("simulated compact storage failure without SECRET content")
        self.rows.append(result)
        return result


class _Storage:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.metric_definitions = _Definitions()
        self.metric_results = _Results(fail_after=fail_after)

    def require_open(self) -> None:
        return None


def _definition(key: str) -> ValuationMetricDefinition:
    return ValuationMetricDefinition(
        metric_key=key,
        display_name_es=key,
        formula="close_price * shares_outstanding",
        input_roles=("close_price", "shares_outstanding"),
        unit="USD",
        algorithm_version="valuation-test-v1",
        definition_version="valuation-definition-test-v1",
    )


def _snapshot(*keys: str) -> CorporateValuationSnapshot:
    ordered = tuple(sorted(keys))
    definitions = tuple(_definition(key) for key in ordered)
    metrics = tuple(
        ValuationMetricValue(
            metric_key=key,
            status=ValuationStatus.EVALUATED,
            value=Decimal(index + 1),
            result_id=UUID(int=index + 10),
            available_at=_AVAILABLE_AT,
            input_observation_ids=(_INPUT_ID,),
        )
        for index, key in enumerate(ordered)
    )
    from investment_analyst.analytics.valuation.models import ValuationInput
    from investment_analyst.core.models import DataFrequency

    inputs = (
        ValuationInput(
            role="close_price",
            observation_id=_INPUT_ID,
            raw_record_id=UUID(int=100),
            source_id="alpaca-market-data:iex:aapl:daily-bars:adjustment-all",
            value=Decimal("1"),
            unit="USD",
            frequency=DataFrequency.DAY_1,
            observed_at=_AS_OF,
            available_at=_AVAILABLE_AT,
        ),
    )
    return CorporateValuationSnapshot(
        asset_id="equity:us:aapl",
        request=CorporateValuationRequest(
            asset_id="equity:us:aapl",
            known_at=_KNOWN_AT,
            valuation_date=date(2026, 1, 30),
        ),
        status=ValuationSnapshotStatus.EVALUATED,
        valuation_as_of=_AS_OF,
        known_at=_KNOWN_AT,
        computed_at=datetime(2026, 2, 2, tzinfo=UTC),
        available_at=_AVAILABLE_AT,
        annual_period_end=_PERIOD_END,
        security_basis=ValuationSecurityBasis(
            basis="reported_common_share",
            market_units_per_reported_share=Decimal("1"),
            market_adjustment="all",
            contract_version="security-unit-basis-v1",
        ),
        inputs=inputs,
        definitions=definitions,
        metrics=metrics,
        coverage=ValuationCoverage(
            total=len(metrics),
            evaluated=len(metrics),
            not_evaluable=0,
            not_applicable=0,
        ),
    )


def test_persistence_is_deterministic_and_idempotent() -> None:
    storage = _Storage()
    pipeline = CorporateValuationPersistencePipeline(storage)
    snapshot = _snapshot("valuation.corporate.market_cap")

    first = pipeline.persist(snapshot)
    second = pipeline.persist(
        snapshot.model_copy(update={"computed_at": datetime(2026, 2, 3, tzinfo=UTC)})
    )

    assert first.metric_results_created == 1
    assert first.metric_results_reused == 0
    assert second.metric_results_created == 0
    assert second.metric_results_reused == 1
    assert len(storage.metric_results.rows) == 1
    assert storage.metric_results.rows[0].input_observation_ids == [_INPUT_ID]
    assert storage.metric_results.rows[0].parameters["category"] == "valuation"


def test_late_metric_failure_preserves_earlier_append_only_progress() -> None:
    storage = _Storage(fail_after=1)
    pipeline = CorporateValuationPersistencePipeline(storage)

    with pytest.raises(RuntimeError, match="simulated compact storage failure"):
        pipeline.persist(
            _snapshot(
                "valuation.corporate.enterprise_value",
                "valuation.corporate.market_cap",
            )
        )

    assert len(storage.metric_results.rows) == 1
    assert storage.metric_results.rows[0].metric_key == ("valuation.corporate.enterprise_value")
