"""Tests for strict SEC point-in-time query models."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_query_models import (
    SecFundamentalAmbiguityPolicy,
    SecFundamentalPeriodView,
    SecFundamentalPointInTimeResult,
    SecFundamentalQuery,
    SecFundamentalRevisionConflict,
    SecSelectedFundamentalFact,
    allowed_sec_fundamental_fields,
)


def _fact(field_name: str = "fundamental.revenue") -> SecSelectedFundamentalFact:
    period_end = datetime(2025, 9, 27, tzinfo=UTC)
    return SecSelectedFundamentalFact(
        observation_id=uuid4(),
        raw_record_id=uuid4(),
        field_name=field_name,
        value=Decimal("100"),
        unit="USD",
        frequency=DataFrequency.ANNUAL,
        period_start=datetime(2024, 9, 29, tzinfo=UTC),
        period_end=period_end,
        available_at=datetime(2025, 10, 31, 20, tzinfo=UTC),
        normalized_at=datetime(2025, 11, 1, tzinfo=UTC),
        accession_number="0000320193-25-000001",
        taxonomy="us-gaap",
        tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        source_id="sec-edgar:aapl:companyfacts",
        record_key="{}",
        superseded_count=0,
    )


def test_query_normalizes_offset_and_validates_range() -> None:
    query = SecFundamentalQuery(
        known_at=datetime(2026, 1, 1, 3, tzinfo=timezone(timedelta(hours=-5))),
        frequency="annual",
        start_period_end=date(2024, 1, 1),
        end_period_end=date(2025, 12, 31),
        limit=10,
    )

    assert query.known_at == datetime(2026, 1, 1, 8, tzinfo=UTC)
    assert query.frequency is DataFrequency.ANNUAL


def test_query_accepts_an_explicit_sec_issuer_asset() -> None:
    query = SecFundamentalQuery(
        asset_id="equity:us:amd",
        known_at=datetime(2026, 1, 1, tzinfo=UTC),
        frequency=DataFrequency.ANNUAL,
    )

    assert query.asset_id == "equity:us:amd"


@pytest.mark.parametrize(
    "values",
    [
        {"known_at": datetime(2026, 1, 1), "frequency": "annual"},
        {"known_at": datetime(2026, 1, 1, tzinfo=UTC), "frequency": "day_1"},
        {
            "known_at": datetime(2026, 1, 1, tzinfo=UTC),
            "frequency": "annual",
            "start_period_end": date(2026, 1, 2),
            "end_period_end": date(2026, 1, 1),
        },
        {"known_at": datetime(2026, 1, 1, tzinfo=UTC), "frequency": "annual", "limit": 0},
        {
            "known_at": datetime(2026, 1, 1, tzinfo=UTC),
            "frequency": "annual",
            "limit": True,
        },
        {
            "known_at": datetime(2026, 1, 1, tzinfo=UTC),
            "frequency": "annual",
            "limit": 501,
        },
    ],
)
def test_query_rejects_invalid_values(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SecFundamentalQuery.model_validate(values)


def test_selected_fact_preserves_decimal_and_rejects_binary_values() -> None:
    fact = _fact()

    assert fact.value == Decimal("100")
    with pytest.raises(ValidationError):
        _fact().model_copy(update={"value": 1.5}).model_validate(
            {**_fact().model_dump(), "value": 1.5}
        )
    with pytest.raises(ValidationError):
        SecSelectedFundamentalFact.model_validate({**fact.model_dump(), "value": True})


def test_period_and_result_are_deterministic_and_json_serializable() -> None:
    fact = _fact()
    available = (fact.field_name,)
    missing = tuple(sorted(set(allowed_sec_fundamental_fields()) - set(available)))
    period = SecFundamentalPeriodView(
        period_end=fact.period_end,
        frequency=fact.frequency,
        facts=(fact,),
        missing_fields=missing,
        available_fields=available,
        is_complete=False,
        latest_available_at=fact.available_at,
    )
    query = SecFundamentalQuery(
        known_at=datetime(2026, 1, 1, tzinfo=UTC),
        frequency=DataFrequency.ANNUAL,
    )
    result = SecFundamentalPointInTimeResult(
        query=query,
        periods=(period,),
        observations_examined=1,
        observations_eligible=1,
        observations_selected=1,
        observations_superseded=0,
        periods_returned=1,
        earliest_period_end=fact.period_end,
        latest_period_end=fact.period_end,
        latest_period_complete=False,
        traceability_verified=True,
    )

    payload = result.to_json_dict()
    assert payload["periods"][0]["facts"][0]["value"] == "100"
    assert payload["periods"][0]["available_fields"] == ["fundamental.revenue"]


def test_models_reject_extra_fields_and_unsorted_facts() -> None:
    with pytest.raises(ValidationError):
        SecFundamentalQuery(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            frequency=DataFrequency.ANNUAL,
            unknown="value",
        )

    revenue = _fact("fundamental.revenue")
    assets = _fact("fundamental.assets").model_copy(
        update={"period_start": None, "observation_id": uuid4()}
    )
    missing = tuple(
        sorted(set(allowed_sec_fundamental_fields()) - {revenue.field_name, assets.field_name})
    )
    with pytest.raises(ValidationError, match="ordered"):
        SecFundamentalPeriodView(
            period_end=revenue.period_end,
            frequency=DataFrequency.ANNUAL,
            facts=(revenue, assets),
            missing_fields=missing,
            available_fields=(revenue.field_name, assets.field_name),
            is_complete=False,
            latest_available_at=revenue.available_at,
        )


def _conflict() -> SecFundamentalRevisionConflict:
    first = _fact()
    second = _fact().model_copy(
        update={
            "observation_id": uuid4(),
            "value": Decimal("101"),
            "accession_number": "0000320193-25-000002",
        }
    )
    candidates = tuple(
        sorted((first, second), key=lambda item: (item.available_at, item.observation_id))
    )
    return SecFundamentalRevisionConflict(
        asset_id="equity:us:aapl",
        source_id=first.source_id,
        frequency=first.frequency,
        field_name=first.field_name,
        period_end=first.period_end,
        tied_available_at=(first.available_at,),
        candidates=candidates,
    )


def test_conflict_only_period_is_valid_and_bare_empty_period_is_not() -> None:
    conflict = _conflict()
    missing = tuple(sorted(allowed_sec_fundamental_fields()))
    period = SecFundamentalPeriodView(
        period_end=conflict.period_end,
        frequency=conflict.frequency,
        facts=(),
        conflict_fields=(conflict.field_name,),
        missing_fields=missing,
        available_fields=(),
        is_complete=False,
        latest_available_at=conflict.tied_available_at[-1],
    )

    assert period.facts == ()
    assert period.conflict_fields == ("fundamental.revenue",)

    with pytest.raises(ValidationError, match="fact or a conflict"):
        SecFundamentalPeriodView(
            period_end=conflict.period_end,
            frequency=conflict.frequency,
            facts=(),
            missing_fields=missing,
            available_fields=(),
            is_complete=False,
            latest_available_at=conflict.tied_available_at[-1],
        )


def test_conflict_rejects_equivalent_or_unsorted_candidates() -> None:
    conflict = _conflict()
    equivalent = conflict.candidates[0].model_copy(update={"observation_id": uuid4()})
    equivalent_candidates = tuple(
        sorted(
            (conflict.candidates[0], equivalent),
            key=lambda item: (item.available_at, item.observation_id),
        )
    )
    with pytest.raises(ValidationError, match="distinct semantic"):
        SecFundamentalRevisionConflict(
            **{
                **conflict.model_dump(),
                "candidates": equivalent_candidates,
            }
        )
    with pytest.raises(ValidationError, match="ordered by available_at"):
        SecFundamentalRevisionConflict(
            **{
                **conflict.model_dump(),
                "candidates": tuple(reversed(conflict.candidates)),
            }
        )


def test_raise_result_rejects_conflicts_and_report_serializes_them() -> None:
    conflict = _conflict()
    period = SecFundamentalPeriodView(
        period_end=conflict.period_end,
        frequency=conflict.frequency,
        facts=(),
        conflict_fields=(conflict.field_name,),
        missing_fields=tuple(sorted(allowed_sec_fundamental_fields())),
        available_fields=(),
        is_complete=False,
        latest_available_at=conflict.tied_available_at[-1],
    )
    common = {
        "periods": (period,),
        "conflicts": (conflict,),
        "has_conflicts": True,
        "observations_examined": 2,
        "observations_eligible": 2,
        "observations_selected": 0,
        "observations_superseded": 0,
        "periods_returned": 1,
        "earliest_period_end": conflict.period_end,
        "latest_period_end": conflict.period_end,
        "latest_period_complete": False,
        "traceability_verified": True,
    }
    raise_query = SecFundamentalQuery(
        known_at=datetime(2026, 1, 1, tzinfo=UTC),
        frequency=DataFrequency.ANNUAL,
    )
    with pytest.raises(ValidationError, match="raise queries"):
        SecFundamentalPointInTimeResult(query=raise_query, **common)

    report_query = SecFundamentalQuery(
        known_at=datetime(2026, 1, 1, tzinfo=UTC),
        frequency=DataFrequency.ANNUAL,
        ambiguity_policy=SecFundamentalAmbiguityPolicy.REPORT,
    )
    result = SecFundamentalPointInTimeResult(query=report_query, **common)
    payload = result.to_json_dict()

    assert payload["query"]["ambiguity_policy"] == "report"
    assert payload["has_conflicts"] is True
    assert payload["conflicts"][0]["field_name"] == "fundamental.revenue"
    assert payload["periods"][0]["conflict_fields"] == ["fundamental.revenue"]
