"""Tests for the deterministic Apple SEC fundamental metric engine."""

from datetime import UTC, date, datetime
from decimal import Context, Decimal, getcontext, localcontext
from uuid import uuid4

import pytest

from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_metric_engine import (
    MalformedSecFundamentalPeriodError,
    SecFundamentalMetricEngine,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SecFundamentalMetricRequest,
)
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    sec_fundamental_metric_result_id,
)
from investment_analyst.providers.fundamentals.sec_query_models import (
    SecFundamentalPeriodView,
    SecFundamentalPointInTimeResult,
    SecFundamentalQuery,
    SecSelectedFundamentalFact,
)

_FIELDS = (
    "fundamental.assets",
    "fundamental.liabilities",
    "fundamental.net_income",
    "fundamental.revenue",
    "fundamental.stockholders_equity",
)


def _fact(
    field_name: str,
    value: str,
    period_end: datetime,
    *,
    frequency: DataFrequency = DataFrequency.ANNUAL,
    fiscal_year: str | None = "2025",
    fiscal_period: str | None = "FY",
    form: str | None = "10-K",
    available_at: datetime | None = None,
    source_id: str = "sec-edgar:aapl:companyfacts",
    unit: str = "USD",
) -> SecSelectedFundamentalFact:
    is_duration = field_name in {"fundamental.revenue", "fundamental.net_income"}
    return SecSelectedFundamentalFact(
        observation_id=uuid4(),
        raw_record_id=uuid4(),
        field_name=field_name,
        value=Decimal(value),
        unit=unit,
        frequency=frequency,
        period_start=(period_end.replace(year=period_end.year - 1) if is_duration else None),
        period_end=period_end,
        available_at=available_at or period_end.replace(month=10, day=31),
        normalized_at=datetime(2026, 1, 1, tzinfo=UTC),
        accession_number=f"0000320193-{fiscal_year or 'none'}-000001",
        taxonomy="us-gaap",
        tag=field_name.rsplit(".", 1)[-1],
        form=form,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        source_id=source_id,
        record_key="{}",
        superseded_count=0,
    )


def _period(
    period_end: datetime,
    values: dict[str, str],
    *,
    frequency: DataFrequency = DataFrequency.ANNUAL,
    fiscal_year: str | None = "2025",
    fiscal_period: str | None = "FY",
    form: str | None = "10-K",
) -> SecFundamentalPeriodView:
    facts = tuple(
        sorted(
            (
                _fact(
                    field_name,
                    value,
                    period_end,
                    frequency=frequency,
                    fiscal_year=fiscal_year,
                    fiscal_period=fiscal_period,
                    form=form,
                )
                for field_name, value in values.items()
            ),
            key=lambda item: item.field_name,
        )
    )
    available = tuple(fact.field_name for fact in facts)
    missing = tuple(field for field in _FIELDS if field not in set(available))
    return SecFundamentalPeriodView(
        period_end=period_end,
        frequency=frequency,
        facts=facts,
        missing_fields=missing,
        available_fields=available,
        is_complete=not missing,
        latest_available_at=max(fact.available_at for fact in facts),
    )


def _result(
    periods: tuple[SecFundamentalPeriodView, ...],
    *,
    frequency: DataFrequency = DataFrequency.ANNUAL,
    known_at: datetime = datetime(2026, 12, 31, tzinfo=UTC),
) -> SecFundamentalPointInTimeResult:
    query = SecFundamentalQuery(known_at=known_at, frequency=frequency)
    return SecFundamentalPointInTimeResult(
        query=query,
        periods=periods,
        observations_examined=sum(len(period.facts) for period in periods),
        observations_eligible=sum(len(period.facts) for period in periods),
        observations_selected=sum(len(period.facts) for period in periods),
        observations_superseded=0,
        periods_returned=len(periods),
        earliest_period_end=periods[0].period_end if periods else None,
        latest_period_end=periods[-1].period_end if periods else None,
        latest_period_complete=periods[-1].is_complete if periods else False,
        traceability_verified=True,
    )


def _annual_history() -> tuple[SecFundamentalPeriodView, ...]:
    previous = _period(
        datetime(2024, 9, 28, tzinfo=UTC),
        {
            "fundamental.revenue": "100",
            "fundamental.net_income": "20",
            "fundamental.assets": "200",
            "fundamental.liabilities": "80",
            "fundamental.stockholders_equity": "120",
        },
        fiscal_year="2024",
    )
    current = _period(
        datetime(2025, 9, 27, tzinfo=UTC),
        {
            "fundamental.revenue": "125",
            "fundamental.net_income": "30",
            "fundamental.assets": "250",
            "fundamental.liabilities": "100",
            "fundamental.stockholders_equity": "150",
        },
        fiscal_year="2025",
    )
    return previous, current


def _compute(
    periods: tuple[SecFundamentalPeriodView, ...],
    **request_overrides: object,
):
    values: dict[str, object] = {
        "known_at": datetime(2026, 12, 31, tzinfo=UTC),
        "frequency": periods[0].frequency if periods else DataFrequency.ANNUAL,
    }
    values.update(request_overrides)
    request = SecFundamentalMetricRequest.model_validate(values)
    return SecFundamentalMetricEngine().compute(
        request,
        _result(periods, frequency=request.frequency, known_at=request.known_at),
        computed_at=datetime(2027, 1, 1, tzinfo=UTC),
    )


def test_exact_same_period_and_yoy_formulas() -> None:
    computation = _compute(_annual_history())
    current = {
        candidate.metric_name: candidate
        for candidate in computation.candidates
        if candidate.period_end.year == 2025
    }

    assert current["fundamental.net_margin"].value == Decimal("0.24")
    assert current["fundamental.liabilities_to_assets"].value == Decimal("0.4")
    with localcontext(Context(prec=34)):
        assert current["fundamental.liabilities_to_equity"].value == (Decimal("2") / Decimal("3"))
    assert current["fundamental.revenue_yoy_growth"].value == Decimal("0.25")
    assert current["fundamental.net_income_yoy_change_rate"].value == Decimal("0.5")
    assert computation.traceability_verified


def test_negative_previous_net_income_uses_absolute_denominator() -> None:
    previous, current = _annual_history()
    facts = tuple(
        fact.model_copy(update={"value": Decimal("-20")})
        if fact.field_name == "fundamental.net_income"
        else fact
        for fact in previous.facts
    )
    previous = previous.model_copy(
        update={
            "facts": facts,
            "latest_available_at": max(fact.available_at for fact in facts),
        }
    )

    computation = _compute((previous, current))
    metric = next(
        item
        for item in computation.candidates
        if item.metric_name == "fundamental.net_income_yoy_change_rate"
        and item.period_end.year == 2025
    )

    assert metric.value == Decimal("2.5")


@pytest.mark.parametrize(
    ("field", "value", "skip_key", "metric_name"),
    [
        (
            "fundamental.revenue",
            "0",
            "non_positive_revenue",
            "fundamental.net_margin",
        ),
        (
            "fundamental.assets",
            "-1",
            "non_positive_assets",
            "fundamental.liabilities_to_assets",
        ),
        (
            "fundamental.stockholders_equity",
            "0",
            "non_positive_equity",
            "fundamental.liabilities_to_equity",
        ),
    ],
)
def test_non_positive_current_denominators_skip_only_affected_metric(
    field: str,
    value: str,
    skip_key: str,
    metric_name: str,
) -> None:
    period = _annual_history()[1]
    facts = tuple(
        fact.model_copy(update={"value": Decimal(value)}) if fact.field_name == field else fact
        for fact in period.facts
    )
    period = period.model_copy(
        update={"facts": facts, "latest_available_at": max(f.available_at for f in facts)}
    )

    computation = _compute((period,))

    assert computation.skipped_counts[skip_key] == 1
    assert metric_name not in {item.metric_name for item in computation.candidates}
    assert computation.candidates


def test_incomplete_period_calculates_independent_metrics() -> None:
    period = _period(
        datetime(2025, 9, 27, tzinfo=UTC),
        {
            "fundamental.revenue": "125",
            "fundamental.net_income": "30",
        },
        fiscal_year="2025",
    )

    computation = _compute((period,))

    assert [item.metric_name for item in computation.candidates] == ["fundamental.net_margin"]
    assert computation.skipped_counts["missing_current_input"] == 2


def test_quarterly_yoy_joins_same_fiscal_quarter_not_previous_date() -> None:
    q1_previous = _period(
        datetime(2024, 3, 30, tzinfo=UTC),
        {"fundamental.revenue": "100", "fundamental.net_income": "10"},
        frequency=DataFrequency.QUARTERLY,
        fiscal_year="2024",
        fiscal_period="Q1",
        form="10-Q",
    )
    q2_previous = _period(
        datetime(2024, 6, 29, tzinfo=UTC),
        {"fundamental.revenue": "500", "fundamental.net_income": "50"},
        frequency=DataFrequency.QUARTERLY,
        fiscal_year="2024",
        fiscal_period="Q2",
        form="10-Q",
    )
    q1_current = _period(
        datetime(2025, 3, 29, tzinfo=UTC),
        {"fundamental.revenue": "120", "fundamental.net_income": "12"},
        frequency=DataFrequency.QUARTERLY,
        fiscal_year="2025",
        fiscal_period="Q1",
        form="10-Q",
    )

    computation = _compute(
        (q1_previous, q2_previous, q1_current),
        frequency=DataFrequency.QUARTERLY,
    )
    growth = next(
        item
        for item in computation.candidates
        if item.metric_name == "fundamental.revenue_yoy_growth" and item.period_end.year == 2025
    )

    assert growth.value == Decimal("0.2")


def test_comparator_outside_requested_range_remains_available() -> None:
    computation = _compute(
        _annual_history(),
        start_period_end=date(2025, 1, 1),
        end_period_end=date(2025, 12, 31),
        limit=1,
    )

    assert computation.target_periods == (datetime(2025, 9, 27, tzinfo=UTC),)
    assert "fundamental.revenue_yoy_growth" in {item.metric_name for item in computation.candidates}


def test_missing_fiscal_metadata_preserves_same_period_metrics_and_skips_yoy() -> None:
    period = _period(
        datetime(2025, 9, 27, tzinfo=UTC),
        {
            "fundamental.revenue": "125",
            "fundamental.net_income": "30",
            "fundamental.assets": "250",
            "fundamental.liabilities": "100",
            "fundamental.stockholders_equity": "150",
        },
        fiscal_year=None,
        fiscal_period=None,
        form=None,
    )

    computation = _compute((period,))

    assert len(computation.candidates) == 3
    assert computation.skipped_counts["missing_fiscal_metadata"] == 2


def test_input_roles_and_availability_are_deterministic() -> None:
    computation = _compute(_annual_history())
    candidate = next(
        item
        for item in computation.candidates
        if item.metric_name == "fundamental.revenue_yoy_growth" and item.period_end.year == 2025
    )

    assert tuple(item.role for item in candidate.input_roles) == (
        "current_revenue",
        "previous_revenue",
    )
    all_facts = {
        fact.observation_id: fact
        for period in computation.source_result.periods
        for fact in period.facts
    }
    assert candidate.available_at == max(
        all_facts[item.observation_id].available_at for item in candidate.input_roles
    )


def test_uuid_is_stable_across_computed_at_and_known_at_when_inputs_match() -> None:
    periods = _annual_history()
    first = _compute(periods)
    second = _compute(
        periods,
        known_at=datetime(2027, 1, 15, tzinfo=UTC),
    )
    first_ids = {sec_fundamental_metric_result_id(item) for item in first.candidates}
    second_ids = {sec_fundamental_metric_result_id(item) for item in second.candidates}

    assert first_ids == second_ids


def test_decimal_context_is_not_modified() -> None:
    original_precision = getcontext().prec

    _compute(_annual_history())

    assert getcontext().prec == original_precision


def test_future_or_wrong_source_fact_is_rejected() -> None:
    period = _annual_history()[1]
    bad_fact = period.facts[0].model_copy(update={"source_id": "other:source"})
    period = period.model_copy(update={"facts": (bad_fact, *period.facts[1:])})

    with pytest.raises(MalformedSecFundamentalPeriodError, match="another source"):
        _compute((period,))
