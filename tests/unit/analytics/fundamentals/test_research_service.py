"""Tests for the read-only point-in-time fundamental research engine."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from investment_analyst.analytics.fundamentals.research_models import (
    AaplFundamentalResearchRequest,
)
from investment_analyst.analytics.fundamentals.research_service import (
    AaplFundamentalResearchService,
    AmbiguousFundamentalResearchRevisionError,
    MalformedFundamentalResearchObservationError,
)
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    SourceReference,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SOURCE_ID,
    TRANSFORMATION_VERSION,
    SecFactPeriodType,
    get_sec_fact_definition,
)

_KNOWN_AT = datetime(2026, 1, 1, tzinfo=UTC)
_PERIOD_END = date(2025, 9, 27)
_AVAILABLE_AT = datetime(2025, 10, 31, 20, tzinfo=UTC)
_VALUES = {
    "fundamental.revenue": "1000",
    "fundamental.net_income": "200",
    "fundamental.assets": "2000",
    "fundamental.stockholders_equity": "800",
    "fundamental.diluted_earnings_per_share": "5.25",
    "fundamental.weighted_average_diluted_shares": "100",
    "fundamental.shares_outstanding": "95",
    "fundamental.gross_profit": "400",
    "fundamental.operating_income": "250",
    "fundamental.operating_cash_flow": "300",
    "fundamental.capital_expenditures": "100",
    "fundamental.share_based_compensation": "20",
    "fundamental.dividends_paid": "30",
    "fundamental.share_repurchases": "120",
    "fundamental.research_and_development": "80",
    "fundamental.selling_general_and_administrative": "60",
    "fundamental.cash_and_cash_equivalents": "200",
    "fundamental.current_assets": "500",
    "fundamental.current_liabilities": "250",
    "fundamental.commercial_paper": "50",
    "fundamental.long_term_debt_current": "20",
    "fundamental.long_term_debt_noncurrent": "180",
    "fundamental.marketable_securities_current": "100",
    "fundamental.marketable_securities_noncurrent": "50",
    "fundamental.interest_expense": "25",
    "fundamental.income_before_tax": "250",
    "fundamental.income_tax_expense": "50",
    "fundamental.property_plant_and_equipment_net": "400",
    "fundamental.operating_lease_liability_current": "10",
    "fundamental.operating_lease_liability_noncurrent": "40",
    "fundamental.finance_lease_liability_current": "5",
    "fundamental.finance_lease_liability_noncurrent": "15",
}


class _ObservationRepository:
    def __init__(self, observations: list[NormalizedObservation]) -> None:
        self._observations = observations
        self.calls = 0

    def list(self, *, asset_id: str | None = None) -> list[NormalizedObservation]:
        self.calls += 1
        return [
            item for item in self._observations if asset_id is None or item.asset_id == asset_id
        ]


class _ForbiddenRawRepository:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"raw repository access is forbidden: {name}")


class _Storage:
    def __init__(self, observations: list[NormalizedObservation]) -> None:
        self.observations = _ObservationRepository(observations)
        self.raw_records = _ForbiddenRawRepository()
        self.is_open = True

    def require_open(self) -> None:
        assert self.is_open


def _observation(
    field_name: str,
    value: str,
    *,
    period_end: date = _PERIOD_END,
    available_at: datetime = _AVAILABLE_AT,
    observation_id: UUID | None = None,
    raw_record_id: UUID | None = None,
    unit: str | None = None,
    record_key_updates: dict[str, object] | None = None,
) -> NormalizedObservation:
    definition = get_sec_fact_definition(field_name)
    resolved_unit = unit or definition.unit
    raw_id = raw_record_id or uuid4()
    period_start = (
        datetime.combine(
            period_end - timedelta(days=363),
            datetime.min.time(),
            tzinfo=UTC,
        )
        if definition.period_type is SecFactPeriodType.DURATION
        else None
    )
    key: dict[str, object] = {
        "accession_number": "0000320193-25-000001",
        "taxonomy": definition.taxonomy,
        "tag": definition.tag,
        "unit": resolved_unit,
        "period": period_end.isoformat(),
        "companyfacts_record_id": str(raw_id),
        "submissions_record_id": str(uuid4()),
        "form": "10-K",
        "fiscal_year": "2025",
        "fiscal_period": "FY",
    }
    if record_key_updates:
        key.update(record_key_updates)
    return NormalizedObservation(
        observation_id=observation_id or uuid4(),
        raw_record_id=raw_id,
        asset_id=ASSET_ID,
        field_name=field_name,
        value=Decimal(value),
        unit=resolved_unit,
        frequency=DataFrequency.ANNUAL,
        observed_at=datetime.combine(period_end, datetime.min.time(), tzinfo=UTC),
        period_start=period_start,
        period_end=datetime.combine(period_end, datetime.min.time(), tzinfo=UTC),
        available_at=available_at,
        normalized_at=max(available_at, datetime(2025, 11, 1, tzinfo=UTC)),
        source=SourceReference(
            source_id=COMPANYFACTS_SOURCE_ID,
            record_key=json.dumps(key, separators=(",", ":"), sort_keys=True),
            retrieved_at=max(available_at, datetime(2025, 11, 1, tzinfo=UTC)),
        ),
        quality=DataQuality.VALID,
        transformation_version=TRANSFORMATION_VERSION,
    )


def _request(
    known_at: datetime = _KNOWN_AT,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int | None = None,
) -> AaplFundamentalResearchRequest:
    return AaplFundamentalResearchRequest(
        known_at=known_at,
        frequency=DataFrequency.ANNUAL,
        start_period_end=start,
        end_period_end=end,
        limit=limit,
    )


def _metric_values(result: object) -> dict[str, Decimal]:
    periods = result.periods
    return {item.metric_key: item.value for item in periods[0].metrics}


def test_all_published_formulas_use_exact_traceable_inputs() -> None:
    observations = [_observation(field_name, value) for field_name, value in _VALUES.items()]
    storage = _Storage(observations)

    result = AaplFundamentalResearchService(storage).query(_request())  # type: ignore[arg-type]
    values = _metric_values(result)

    assert values == {
        "fundamental.research.asset_turnover": Decimal("0.5"),
        "fundamental.research.capex_to_operating_cash_flow": Decimal(
            "0.3333333333333333333333333333333333"
        ),
        "fundamental.research.cash_ratio": Decimal("0.8"),
        "fundamental.research.current_financial_debt": Decimal("70"),
        "fundamental.research.current_financial_debt_share": Decimal("0.28"),
        "fundamental.research.current_ratio": Decimal("2"),
        "fundamental.research.diluted_eps": Decimal("5.25"),
        "fundamental.research.diluted_shares": Decimal("100"),
        "fundamental.research.effective_tax_rate": Decimal("0.2"),
        "fundamental.research.financial_debt": Decimal("250"),
        "fundamental.research.financial_debt_to_assets": Decimal("0.125"),
        "fundamental.research.financial_debt_to_equity": Decimal("0.3125"),
        "fundamental.research.financial_debt_to_free_cash_flow": Decimal("1.25"),
        "fundamental.research.fixed_asset_turnover": Decimal("2.5"),
        "fundamental.research.free_cash_flow": Decimal("200"),
        "fundamental.research.free_cash_flow_margin": Decimal("0.2"),
        "fundamental.research.free_cash_flow_per_diluted_share": Decimal("2"),
        "fundamental.research.free_cash_flow_to_net_income": Decimal("1"),
        "fundamental.research.gross_margin": Decimal("0.4"),
        "fundamental.research.interest_coverage": Decimal("10"),
        "fundamental.research.lease_liabilities": Decimal("70"),
        "fundamental.research.net_debt": Decimal("-100"),
        "fundamental.research.net_debt_to_free_cash_flow": Decimal("-0.5"),
        "fundamental.research.net_liquid_assets": Decimal("150"),
        "fundamental.research.net_margin": Decimal("0.2"),
        "fundamental.research.operating_cash_flow_margin": Decimal("0.3"),
        "fundamental.research.operating_cash_flow_to_net_income": Decimal("1.5"),
        "fundamental.research.operating_margin": Decimal("0.25"),
        "fundamental.research.research_and_development_to_revenue": Decimal("0.08"),
        "fundamental.research.return_on_assets_ending_balance": Decimal("0.1"),
        "fundamental.research.return_on_equity_ending_balance": Decimal("0.25"),
        "fundamental.research.return_on_invested_capital_ending_balance": Decimal(
            "0.2857142857142857142857142857142857"
        ),
        "fundamental.research.revenue_per_diluted_share": Decimal("10"),
        "fundamental.research.selling_general_and_administrative_to_revenue": Decimal("0.06"),
        "fundamental.research.share_based_compensation_to_revenue": Decimal("0.02"),
        "fundamental.research.shareholder_distributions": Decimal("150"),
        "fundamental.research.shareholder_distributions_to_free_cash_flow": Decimal("0.75"),
        "fundamental.research.shares_outstanding": Decimal("95"),
        "fundamental.research.total_financial_obligations": Decimal("320"),
        "fundamental.research.working_capital": Decimal("250"),
    }
    assert result.coverage.metrics_returned == 40
    assert result.coverage.observations_selected == len(_VALUES)
    assert result.traceability_verified
    assert storage.observations.calls == 1
    assert all(
        metric.available_at == max(item.available_at for item in metric.inputs)
        for metric in result.periods[0].metrics
    )
    assert all(
        item.observation_id in {observation.observation_id for observation in observations}
        for metric in result.periods[0].metrics
        for item in metric.inputs
    )


def test_revision_selection_is_point_in_time_and_range_is_inclusive() -> None:
    prior_period = date(2024, 9, 28)
    amendment_at = _AVAILABLE_AT + timedelta(days=10)
    observations = [
        _observation("fundamental.current_assets", "400", period_end=prior_period),
        _observation("fundamental.current_liabilities", "200", period_end=prior_period),
        _observation("fundamental.current_assets", "500"),
        _observation("fundamental.current_assets", "600", available_at=amendment_at),
        _observation("fundamental.current_liabilities", "250"),
    ]
    service = AaplFundamentalResearchService(_Storage(observations))  # type: ignore[arg-type]

    before = service.query(
        _request(
            known_at=amendment_at - timedelta(seconds=1),
            start=_PERIOD_END,
            end=_PERIOD_END,
        )
    )
    after = service.query(_request(known_at=amendment_at, limit=1))

    assert _metric_values(before)["fundamental.research.current_ratio"] == Decimal("2")
    assert _metric_values(after)["fundamental.research.current_ratio"] == Decimal("2.4")
    assert before.coverage.observations_superseded == 0
    assert after.coverage.observations_superseded == 1
    assert [period.period_end.date() for period in after.periods] == [_PERIOD_END]


def test_equally_available_conflicting_revisions_fail() -> None:
    observations = [
        _observation("fundamental.current_assets", "500"),
        _observation("fundamental.current_assets", "501"),
        _observation("fundamental.current_liabilities", "250"),
    ]

    with pytest.raises(AmbiguousFundamentalResearchRevisionError):
        AaplFundamentalResearchService(_Storage(observations)).query(  # type: ignore[arg-type]
            _request()
        )


def test_non_positive_denominators_are_visible_and_safe() -> None:
    observations = [
        _observation("fundamental.cash_and_cash_equivalents", "100"),
        _observation("fundamental.current_assets", "500"),
        _observation("fundamental.current_liabilities", "0"),
    ]

    result = AaplFundamentalResearchService(_Storage(observations)).query(  # type: ignore[arg-type]
        _request()
    )

    assert _metric_values(result) == {"fundamental.research.working_capital": Decimal("500")}
    assert (
        result.coverage.skipped_counts["fundamental.research.cash_ratio:non_positive_denominator"]
        == 1
    )
    assert (
        result.coverage.skipped_counts[
            "fundamental.research.current_ratio:non_positive_denominator"
        ]
        == 1
    )


def test_invalid_tax_rate_skips_effective_rate_and_roic_without_hiding_other_returns() -> None:
    observations = [
        _observation("fundamental.assets", "2000"),
        _observation("fundamental.net_income", "200"),
        _observation("fundamental.income_before_tax", "100"),
        _observation("fundamental.income_tax_expense", "120"),
        _observation("fundamental.operating_income", "250"),
        _observation("fundamental.stockholders_equity", "800"),
        _observation("fundamental.commercial_paper", "50"),
        _observation("fundamental.long_term_debt_current", "20"),
        _observation("fundamental.long_term_debt_noncurrent", "180"),
        _observation("fundamental.cash_and_cash_equivalents", "200"),
        _observation("fundamental.marketable_securities_current", "100"),
        _observation("fundamental.marketable_securities_noncurrent", "50"),
    ]

    result = AaplFundamentalResearchService(_Storage(observations)).query(  # type: ignore[arg-type]
        _request()
    )

    values = _metric_values(result)
    assert values["fundamental.research.return_on_assets_ending_balance"] == Decimal("0.1")
    assert "fundamental.research.effective_tax_rate" not in values
    assert "fundamental.research.return_on_invested_capital_ending_balance" not in values
    assert (
        result.coverage.skipped_counts[
            "fundamental.research.effective_tax_rate:tax_rate_out_of_range"
        ]
        == 1
    )


def test_non_positive_net_income_skips_conversion_but_preserves_other_metrics() -> None:
    observations = [
        _observation("fundamental.revenue", "1000"),
        _observation("fundamental.net_income", "0"),
        _observation("fundamental.operating_cash_flow", "300"),
        _observation("fundamental.capital_expenditures", "100"),
    ]

    result = AaplFundamentalResearchService(_Storage(observations)).query(  # type: ignore[arg-type]
        _request()
    )

    assert _metric_values(result) == {
        "fundamental.research.capex_to_operating_cash_flow": Decimal(
            "0.3333333333333333333333333333333333"
        ),
        "fundamental.research.free_cash_flow": Decimal("200"),
        "fundamental.research.free_cash_flow_margin": Decimal("0.2"),
        "fundamental.research.net_margin": Decimal("0"),
        "fundamental.research.operating_cash_flow_margin": Decimal("0.3"),
    }
    assert (
        result.coverage.skipped_counts[
            "fundamental.research.free_cash_flow_to_net_income:non_positive_denominator"
        ]
        == 1
    )
    assert (
        result.coverage.skipped_counts[
            "fundamental.research.operating_cash_flow_to_net_income:non_positive_denominator"
        ]
        == 1
    )


def test_zero_diluted_shares_skip_per_share_calculations_without_infinity() -> None:
    observations = [
        _observation("fundamental.revenue", "1000"),
        _observation("fundamental.operating_cash_flow", "300"),
        _observation("fundamental.capital_expenditures", "100"),
        _observation("fundamental.weighted_average_diluted_shares", "0"),
    ]

    result = AaplFundamentalResearchService(_Storage(observations)).query(  # type: ignore[arg-type]
        _request()
    )

    assert "fundamental.research.revenue_per_diluted_share" not in _metric_values(result)
    assert "fundamental.research.free_cash_flow_per_diluted_share" not in _metric_values(result)
    assert (
        result.coverage.skipped_counts[
            "fundamental.research.revenue_per_diluted_share:non_positive_denominator"
        ]
        == 1
    )


def test_malformed_evidence_is_rejected_before_calculation() -> None:
    observation = _observation(
        "fundamental.current_assets",
        "500",
        record_key_updates={"tag": "WrongTag"},
    )

    with pytest.raises(MalformedFundamentalResearchObservationError, match="tag"):
        AaplFundamentalResearchService(_Storage([observation])).query(  # type: ignore[arg-type]
            _request()
        )

    wrong_unit = _observation(
        "fundamental.diluted_earnings_per_share",
        "2.01",
        unit="USD",
    )
    with pytest.raises(MalformedFundamentalResearchObservationError, match="unit"):
        AaplFundamentalResearchService(_Storage([wrong_unit])).query(  # type: ignore[arg-type]
            _request()
        )
