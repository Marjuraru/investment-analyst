"""Strict contract tests for explicit annual filing selection."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.valuation.filing_models import (
    CorporateValuationFilingOption,
    CorporateValuationFilingOptions,
    CorporateValuationFilingQuery,
    CorporateValuationFilingSelector,
    CorporateValuationSelectedFilingRequest,
    CorporateValuationSelectedFilingSnapshot,
)
from investment_analyst.analytics.valuation.models import (
    ValuationCoverage,
    ValuationInput,
    ValuationMetricDefinition,
    ValuationMetricValue,
    ValuationSnapshotStatus,
    ValuationStatus,
)
from investment_analyst.core.models import DataFrequency

_KNOWN_AT = datetime(2026, 5, 1, 20, tzinfo=UTC)
_ACCEPTED_AT = datetime(2025, 2, 15, 16, tzinfo=UTC)
_PERIOD_END = datetime(2024, 12, 31, tzinfo=UTC)
_METRIC_KEY = "valuation.corporate.market_cap"
_INPUT_ID = UUID("11111111-1111-1111-1111-111111111111")


def _selector() -> CorporateValuationFilingSelector:
    return CorporateValuationFilingSelector(
        annual_period_end=date(2024, 12, 31),
        filing_accession_number="0001193125-25-000001",
        filing_accepted_at=_ACCEPTED_AT,
        filing_form="20-F",
        fiscal_year="2024",
        fiscal_period="FY",
    )


def _request() -> CorporateValuationSelectedFilingRequest:
    return CorporateValuationSelectedFilingRequest(
        asset_id="equity:us:tsm",
        known_at=_KNOWN_AT,
        valuation_date=date(2026, 4, 17),
        annual_period_end=date(2024, 12, 31),
        filing_accession_number="0001193125-25-000001",
        filing_accepted_at=_ACCEPTED_AT,
        filing_form="20-F",
        fiscal_year="2024",
        fiscal_period="FY",
    )


def _definition() -> ValuationMetricDefinition:
    return ValuationMetricDefinition(
        metric_key=_METRIC_KEY,
        display_name_es="Capitalización bursátil",
        formula="close_price * shares_outstanding",
        input_roles=("close_price", "shares_outstanding"),
        unit="USD",
        algorithm_version="corporate-valuation-selected-annual-filing-v1-decimal34",
        definition_version="corporate-valuation-selected-filing-definition-v1",
    )


def _input() -> ValuationInput:
    return ValuationInput(
        role="close_price",
        observation_id=_INPUT_ID,
        raw_record_id=UUID(int=100),
        source_id="alpaca-market-data:iex:tsm:daily-bars:adjustment-all",
        value=Decimal("100"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=datetime(2026, 4, 17, 16, tzinfo=UTC),
        available_at=datetime(2026, 4, 17, 18, tzinfo=UTC),
    )


def _snapshot(**updates: object) -> CorporateValuationSelectedFilingSnapshot:
    metric = ValuationMetricValue(
        metric_key=_METRIC_KEY,
        status=ValuationStatus.EVALUATED,
        value=Decimal("20000"),
        result_id=UUID(f"00000000-0000-8000-8000-{10:012d}"),
        available_at=datetime(2026, 4, 17, 18, tzinfo=UTC),
        input_observation_ids=(_INPUT_ID,),
    )
    payload: dict[str, object] = {
        "asset_id": "equity:us:tsm",
        "request": _request(),
        "status": ValuationSnapshotStatus.EVALUATED,
        "valuation_as_of": datetime(2026, 4, 17, 16, tzinfo=UTC),
        "known_at": _KNOWN_AT,
        "computed_at": datetime(2026, 5, 1, 21, tzinfo=UTC),
        "available_at": datetime(2026, 4, 17, 18, tzinfo=UTC),
        "price_age_days": 0,
        "annual_period_end": _PERIOD_END,
        "filing_date": date(2025, 2, 15),
        "filing_accepted_at": _ACCEPTED_AT,
        "filing_accession_number": "0001193125-25-000001",
        "filing_form": "20-F",
        "fiscal_year": "2024",
        "fiscal_period": "FY",
        "price_currency": "USD",
        "report_currency": "USD",
        "price_source_id": "alpaca-market-data:iex:tsm:daily-bars:adjustment-all",
        "fundamental_source_id": "sec-edgar:tsm:companyfacts",
        "inputs": (_input(),),
        "definitions": (_definition(),),
        "metrics": (metric,),
        "coverage": ValuationCoverage(
            total=1,
            evaluated=1,
            not_evaluable=0,
            not_applicable=0,
        ),
        "limitations": ("Ejercicio anual elegido explícitamente.",),
    }
    payload.update(updates)
    return CorporateValuationSelectedFilingSnapshot.model_validate(payload)


def test_query_bounds_and_ordering_metadata_are_strict() -> None:
    query = CorporateValuationFilingQuery(
        asset_id="equity:us:tsm",
        known_at=_KNOWN_AT,
        valuation_date=date(2026, 4, 17),
        start_date=date(2024, 1, 1),
        end_date=date(2026, 12, 31),
    )
    assert query.offset == 0
    assert query.limit == 50

    with pytest.raises(ValidationError, match="start_date must not be after end_date"):
        CorporateValuationFilingQuery(
            asset_id="equity:us:tsm",
            known_at=_KNOWN_AT,
            valuation_date=date(2026, 4, 17),
            start_date=date(2026, 1, 1),
            end_date=date(2024, 1, 1),
        )
    with pytest.raises(ValidationError, match="less than or equal to 200"):
        CorporateValuationFilingQuery.model_validate({**query.model_dump(), "limit": 201})
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        CorporateValuationFilingQuery.model_validate({**query.model_dump(), "offset": -1})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CorporateValuationFilingQuery.model_validate(
            {
                **query.model_dump(),
                "unexpected": "x",
            }
        )
    with pytest.raises(ValidationError):
        query.asset_id = "equity:us:aapl"


def test_options_require_consistent_pagination_and_sorted_fields() -> None:
    query = CorporateValuationFilingQuery(
        asset_id="equity:us:tsm",
        known_at=_KNOWN_AT,
        valuation_date=date(2026, 4, 17),
        start_date=date(2024, 1, 1),
        end_date=date(2026, 12, 31),
        offset=0,
        limit=50,
    )
    option = CorporateValuationFilingOption(
        selector=_selector(),
        available_field_roles=("net_income", "revenue", "shares_outstanding"),
        taxonomies=("dei", "us-gaap"),
    )
    options = CorporateValuationFilingOptions(
        request=query,
        total=1,
        offset=0,
        limit=50,
        returned=1,
        options=(option,),
    )
    assert options.options[0].selector == _selector()

    with pytest.raises(ValidationError, match="returned must match"):
        CorporateValuationFilingOptions(
            request=query,
            total=1,
            offset=0,
            limit=50,
            returned=0,
            options=(option,),
        )
    with pytest.raises(ValidationError, match="sorted and unique"):
        CorporateValuationFilingOption(
            selector=_selector(),
            available_field_roles=("revenue", "net_income"),
            taxonomies=(),
        )
    with pytest.raises(ValidationError, match="pagination must preserve"):
        CorporateValuationFilingOptions(
            request=query,
            total=1,
            offset=1,
            limit=50,
            returned=0,
            options=(),
        )
    out_of_range = CorporateValuationFilingQuery.model_validate(
        {**query.model_dump(), "offset": 10}
    )
    with pytest.raises(ValidationError, match="exceeds the declared total"):
        CorporateValuationFilingOptions(
            request=out_of_range,
            total=1,
            offset=10,
            limit=50,
            returned=1,
            options=(option,),
        )


def test_selected_request_rejects_impossible_dates_and_partial_basis() -> None:
    request = _request()
    with pytest.raises(ValidationError, match="annual_period_end must not be after"):
        CorporateValuationSelectedFilingRequest.model_validate(
            {**request.model_dump(), "valuation_date": date(2024, 12, 30)}
        )
    with pytest.raises(ValidationError, match="filing_accepted_at must not be after"):
        CorporateValuationSelectedFilingRequest.model_validate(
            {**request.model_dump(), "known_at": datetime(2025, 2, 15, 15, tzinfo=UTC)}
        )
    with pytest.raises(ValidationError, match="Input should be 'selected_annual_filing'"):
        CorporateValuationSelectedFilingRequest.model_validate(
            {**request.model_dump(), "basis": "latest_annual"}
        )


def test_selected_snapshot_binds_the_literal_context() -> None:
    snapshot = _snapshot()
    assert snapshot.to_json_dict()["metrics"][0]["value"] == "20000"
    assert snapshot.request.selector() == _selector()

    with pytest.raises(ValidationError, match="must preserve the request identity"):
        _snapshot(asset_id="equity:us:aapl")
    with pytest.raises(ValidationError, match="selected filing context must match"):
        _snapshot(filing_accession_number="0001193125-25-999999")
    with pytest.raises(ValidationError, match="selected filing period must match"):
        _snapshot(annual_period_end=datetime(2025, 12, 31, tzinfo=UTC))
    with pytest.raises(ValidationError, match="status must match metric coverage"):
        _snapshot(status=ValuationSnapshotStatus.NOT_EVALUABLE)
    with pytest.raises(ValidationError, match="definitions must match metric values"):
        _snapshot(definitions=())
    with pytest.raises(ValidationError, match="must not be after computed_at"):
        _snapshot(available_at=datetime(2026, 6, 1, tzinfo=UTC))
    with pytest.raises(ValidationError, match="price must not be after valuation_date"):
        _snapshot(valuation_as_of=datetime(2026, 5, 1, tzinfo=UTC))
