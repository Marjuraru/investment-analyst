import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from investment_analyst.analytics.cazatiburones.activity_metric_definitions import ALGORITHM_VERSION
from investment_analyst.analytics.cazatiburones.activity_metric_engine import (
    calculate_beneficial_activity_metrics,
    calculate_insider_activity_metrics,
)
from investment_analyst.core.models.enums import DataFrequency, DataQuality
from investment_analyst.core.models.observation import NormalizedObservation
from investment_analyst.core.models.source import SourceReference
from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BENEFICIAL_OWNERSHIP_SOURCE_ID,
)
from investment_analyst.evidence.sec_declared_activity_observations.normalizer import (
    expected_observation_id,
)
from investment_analyst.evidence.sec_ownership.models import OWNERSHIP_SOURCE_ID, OwnershipEntry

_ASSET_ID = "equity:us:aapl"
_OWNER_CIK = "0000000001"


def _insider_statement(
    *, statement_id, entry_id, available_at, transaction_date, following, asset_id=_ASSET_ID
):
    entry = OwnershipEntry.model_construct(
        entry_id=entry_id,
        table="non_derivative",
        kind="transaction",
        ordinal=0,
        owner_cik=_OWNER_CIK,
        security_title="Common Stock",
        transaction_date=transaction_date,
        acquired_disposed="A",
        shares=Decimal("10"),
        shares_owned_following=following,
    )
    return SimpleNamespace(
        statement_id=statement_id,
        asset_id=asset_id,
        form="4",
        period_of_report=transaction_date,
        available_at=available_at,
        entries=(entry,),
    )


def _observation(
    *,
    source_id,
    statement_id,
    entry_id,
    field_name,
    value,
    available_at,
    date_attribute,
    quality=DataQuality.VALID,
) -> NormalizedObservation:
    observed_at = (
        available_at
        if date_attribute.endswith("transaction_date") or date_attribute.endswith("event_date")
        else None
    )
    period_end = None if observed_at is not None else available_at
    record_key = json.dumps(
        {
            "statement_id": str(statement_id),
            "entry_id": str(entry_id) if entry_id is not None else None,
            "field_name": field_name,
            "date_attribute": date_attribute,
            "transformation_version": "sec-declared-activity-observation-normalizer-v1",
        },
        sort_keys=True,
    )
    return NormalizedObservation(
        observation_id=expected_observation_id(
            source_id=source_id, statement_id=statement_id, entry_id=entry_id, field_name=field_name
        ),
        raw_record_id=uuid4(),
        asset_id=_ASSET_ID,
        field_name=field_name,
        value=value,
        unit="shares" if "shares" in field_name else "percent",
        frequency=DataFrequency.EVENT,
        observed_at=observed_at,
        period_end=period_end,
        available_at=available_at,
        normalized_at=available_at,
        source=SourceReference(
            source_id=source_id, record_key=record_key, retrieved_at=available_at
        ),
        quality=quality,
        transformation_version="sec-declared-activity-observation-normalizer-v1",
    )


def test_insider_engine_computes_ratio_and_flags_first_declaration() -> None:
    first_id, second_id = uuid4(), uuid4()
    entry1, entry2 = uuid4(), uuid4()
    first_at = datetime(2025, 1, 13, tzinfo=UTC)
    second_at = datetime(2025, 1, 23, tzinfo=UTC)
    first = _insider_statement(
        statement_id=first_id,
        entry_id=entry1,
        available_at=first_at,
        transaction_date=date(2025, 1, 10),
        following=Decimal("100"),
    )
    second = _insider_statement(
        statement_id=second_id,
        entry_id=entry2,
        available_at=second_at,
        transaction_date=date(2025, 1, 20),
        following=Decimal("125"),
    )
    observations = {
        o.observation_id: o
        for o in (
            _observation(
                source_id=OWNERSHIP_SOURCE_ID,
                statement_id=first_id,
                entry_id=entry1,
                field_name="insider_shares_owned_following",
                value=Decimal("100"),
                available_at=first_at,
                date_attribute="entry.transaction_date",
            ),
            _observation(
                source_id=OWNERSHIP_SOURCE_ID,
                statement_id=second_id,
                entry_id=entry2,
                field_name="insider_shares_owned_following",
                value=Decimal("125"),
                available_at=second_at,
                date_attribute="entry.transaction_date",
            ),
        )
    }

    result = calculate_insider_activity_metrics(
        (first, second), observations=observations, known_at=second_at
    )

    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "not_evaluable_no_precedent"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.metric_key == "cazatiburones.insider.holding_delta_ratio"
    assert candidate.value == Decimal("0.25")
    assert candidate.unit == "ratio"
    assert candidate.algorithm_version == ALGORITHM_VERSION
    assert candidate.available_at == second_at
    assert candidate.input_observation_ids == (
        expected_observation_id(
            source_id=OWNERSHIP_SOURCE_ID,
            statement_id=first_id,
            entry_id=entry1,
            field_name="insider_shares_owned_following",
        ),
        expected_observation_id(
            source_id=OWNERSHIP_SOURCE_ID,
            statement_id=second_id,
            entry_id=entry2,
            field_name="insider_shares_owned_following",
        ),
    )
    assert candidate.parameters["participant_cik"] == _OWNER_CIK
    assert candidate.parameters["security_title"] == "Common Stock"
    assert candidate.parameters["decimal_precision"] == 28


def test_insider_engine_flags_zero_prior_holding() -> None:
    first_id, second_id = uuid4(), uuid4()
    entry1, entry2 = uuid4(), uuid4()
    first_at = datetime(2025, 1, 13, tzinfo=UTC)
    second_at = datetime(2025, 1, 23, tzinfo=UTC)
    first = _insider_statement(
        statement_id=first_id,
        entry_id=entry1,
        available_at=first_at,
        transaction_date=date(2025, 1, 10),
        following=Decimal("0"),
    )
    second = _insider_statement(
        statement_id=second_id,
        entry_id=entry2,
        available_at=second_at,
        transaction_date=date(2025, 1, 20),
        following=Decimal("5"),
    )
    observations = {
        o.observation_id: o
        for o in (
            _observation(
                source_id=OWNERSHIP_SOURCE_ID,
                statement_id=first_id,
                entry_id=entry1,
                field_name="insider_shares_owned_following",
                value=Decimal("0"),
                available_at=first_at,
                date_attribute="entry.transaction_date",
            ),
            _observation(
                source_id=OWNERSHIP_SOURCE_ID,
                statement_id=second_id,
                entry_id=entry2,
                field_name="insider_shares_owned_following",
                value=Decimal("5"),
                available_at=second_at,
                date_attribute="entry.transaction_date",
            ),
        )
    }

    result = calculate_insider_activity_metrics(
        (first, second), observations=observations, known_at=second_at
    )

    assert not result.candidates
    reasons = [skip.reason for skip in result.skipped]
    assert reasons == ["not_evaluable_no_precedent", "not_evaluable_zero_prior"]


def test_insider_engine_flags_missing_observation() -> None:
    first_id, second_id = uuid4(), uuid4()
    entry1, entry2 = uuid4(), uuid4()
    first_at = datetime(2025, 1, 13, tzinfo=UTC)
    second_at = datetime(2025, 1, 23, tzinfo=UTC)
    first = _insider_statement(
        statement_id=first_id,
        entry_id=entry1,
        available_at=first_at,
        transaction_date=date(2025, 1, 10),
        following=Decimal("100"),
    )
    second = _insider_statement(
        statement_id=second_id,
        entry_id=entry2,
        available_at=second_at,
        transaction_date=date(2025, 1, 20),
        following=Decimal("125"),
    )

    result = calculate_insider_activity_metrics(
        (first, second), observations={}, known_at=second_at
    )

    assert not result.candidates
    reasons = [skip.reason for skip in result.skipped]
    assert reasons == ["not_evaluable_no_precedent", "missing_input_observation"]


def test_insider_engine_rejects_cross_asset_input() -> None:
    first = _insider_statement(
        statement_id=uuid4(),
        entry_id=uuid4(),
        available_at=datetime(2025, 1, 13, tzinfo=UTC),
        transaction_date=date(2025, 1, 10),
        following=Decimal("100"),
    )
    second = _insider_statement(
        statement_id=uuid4(),
        entry_id=uuid4(),
        available_at=datetime(2025, 1, 23, tzinfo=UTC),
        transaction_date=date(2025, 1, 20),
        following=Decimal("110"),
        asset_id="equity:us:msft",
    )

    with pytest.raises(ValueError, match="one asset"):
        calculate_insider_activity_metrics(
            (first, second), observations={}, known_at=datetime(2025, 1, 23, tzinfo=UTC)
        )


def _beneficial_statement(*, statement_id, available_at, event_date, form="SC 13G"):
    return SimpleNamespace(
        statement_id=statement_id,
        asset_id=_ASSET_ID,
        form=form,
        subject_cik="0000320193",
        reporting_person_cik="0000102909",
        event_date=event_date,
        available_at=available_at,
    )


def test_beneficial_engine_computes_deltas_and_flags_first_declaration() -> None:
    first_id, second_id = uuid4(), uuid4()
    first_at = datetime(2025, 2, 1, tzinfo=UTC)
    second_at = datetime(2025, 2, 10, tzinfo=UTC)
    first = _beneficial_statement(
        statement_id=first_id, available_at=first_at, event_date=date(2025, 1, 20)
    )
    second = _beneficial_statement(
        statement_id=second_id, available_at=second_at, event_date=date(2025, 2, 2)
    )
    observations = {}
    for field_name, first_value, second_value in (
        ("beneficial_percent_of_class", Decimal("4"), Decimal("5")),
        ("beneficial_shares_owned", Decimal("100"), Decimal("150")),
    ):
        for statement_id, at, value in (
            (first_id, first_at, first_value),
            (second_id, second_at, second_value),
        ):
            obs = _observation(
                source_id=BENEFICIAL_OWNERSHIP_SOURCE_ID,
                statement_id=statement_id,
                entry_id=None,
                field_name=field_name,
                value=value,
                available_at=at,
                date_attribute="statement.event_date",
            )
            observations[obs.observation_id] = obs

    result = calculate_beneficial_activity_metrics(
        (first, second), observations=observations, known_at=second_at
    )

    assert len(result.skipped) == 2
    assert all(skip.reason == "not_evaluable_no_precedent" for skip in result.skipped)
    by_key = {c.metric_key: c for c in result.candidates}
    assert by_key["cazatiburones.beneficial.delta_percent_of_class"].value == Decimal("1")
    assert by_key["cazatiburones.beneficial.delta_percent_of_class"].unit == "percentage_points"
    assert by_key["cazatiburones.beneficial.delta_shares_beneficially_owned"].value == Decimal("50")
    assert by_key["cazatiburones.beneficial.delta_shares_beneficially_owned"].unit == "shares"
    for candidate in result.candidates:
        assert candidate.parameters["subject_cik"] == "0000320193"
        assert candidate.parameters["reporting_person_cik"] == "0000102909"


def test_beneficial_engine_flags_missing_observation() -> None:
    first = _beneficial_statement(
        statement_id=uuid4(),
        available_at=datetime(2025, 2, 1, tzinfo=UTC),
        event_date=date(2025, 1, 20),
    )
    second = _beneficial_statement(
        statement_id=uuid4(),
        available_at=datetime(2025, 2, 10, tzinfo=UTC),
        event_date=date(2025, 2, 2),
    )

    result = calculate_beneficial_activity_metrics(
        (first, second), observations={}, known_at=datetime(2025, 2, 10, tzinfo=UTC)
    )

    assert not result.candidates
    reasons = sorted(skip.reason for skip in result.skipped)
    assert reasons == [
        "missing_input_observation",
        "missing_input_observation",
        "not_evaluable_no_precedent",
        "not_evaluable_no_precedent",
    ]


def test_beneficial_engine_rejects_cross_asset_input() -> None:
    first = _beneficial_statement(
        statement_id=uuid4(),
        available_at=datetime(2025, 2, 1, tzinfo=UTC),
        event_date=date(2025, 1, 20),
    )
    second = SimpleNamespace(
        statement_id=uuid4(),
        asset_id="equity:us:msft",
        form="SC 13G",
        subject_cik="0000320193",
        reporting_person_cik="0000102909",
        event_date=date(2025, 2, 2),
        available_at=datetime(2025, 2, 10, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="one asset"):
        calculate_beneficial_activity_metrics(
            (first, second), observations={}, known_at=datetime(2025, 2, 10, tzinfo=UTC)
        )
