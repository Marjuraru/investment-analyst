"""Unit tests for the isolated Cazatiburones notification contracts."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.alerts.cazatiburones_notification_models import (
    CazatiburonesNotification,
    CazatiburonesNotificationAcknowledgement,
    CazatiburonesNotificationState,
    acknowledgement_id,
    notification_id,
)


def _activity_item(*, candidate_id_value=None) -> CazatiburonesNotification:
    candidate = candidate_id_value or uuid4()
    available_at = datetime(2026, 1, 1, 12, tzinfo=UTC)
    return CazatiburonesNotification(
        family="activity",
        notification_id=notification_id("activity", candidate),
        candidate_id=candidate,
        event_id=uuid4(),
        metric_result_id=uuid4(),
        snapshot_id=uuid4(),
        asset_id="equity:us:aapl",
        rule_id="insider-holding-increased",
        metric_key="cazatiburones.insider.holding_delta_ratio",
        algorithm_version="cazatiburones-persisted-activity-events-v1",
        unit="ratio",
        value=Decimal("1.250000000000000001"),
        available_at=available_at,
        created_at=available_at,
        input_observation_ids=(uuid4(), uuid4()),
    )


def test_notification_contract_is_frozen_strict_and_decimal_exact() -> None:
    item = _activity_item()

    assert item.model_config["frozen"] is True
    assert item.model_config["strict"] is True
    assert isinstance(item.value, Decimal)
    assert item.value == Decimal("1.250000000000000001")
    with pytest.raises(ValidationError):
        item.value = Decimal("2")


def test_notification_identity_and_acknowledgement_are_deterministic() -> None:
    item = _activity_item()
    first_time = datetime(2026, 1, 2, 12, tzinfo=UTC)
    equivalent_time = first_time.astimezone(UTC)

    assert item.notification_id == notification_id("activity", item.candidate_id)
    assert acknowledgement_id(item.notification_id, first_time) == acknowledgement_id(
        item.notification_id, equivalent_time
    )
    acknowledgement = CazatiburonesNotificationAcknowledgement(
        acknowledgement_id=acknowledgement_id(item.notification_id, first_time),
        notification_id=item.notification_id,
        recorded_at=first_time,
    )
    assert acknowledgement.to_status == "acknowledged"


def test_family_provenance_is_mandatory_and_verifiable() -> None:
    item = _activity_item()

    with pytest.raises(ValueError, match="institutional notification requires"):
        CazatiburonesNotification(
            **item.model_dump(exclude={"family", "notification_id"}),
            family="institutional",
            notification_id=notification_id("institutional", item.candidate_id),
        )
    with pytest.raises(ValueError, match="activity notification must not"):
        CazatiburonesNotification(
            **item.model_dump(exclude={"manager_cik"}),
            manager_cik="0001350694",
        )


def test_state_validates_unique_identities_and_deterministic_order() -> None:
    first = _activity_item()
    second = _activity_item()
    ordered = tuple(
        sorted(
            (first, second),
            key=lambda value: (value.created_at, str(value.notification_id)),
        )
    )
    state = CazatiburonesNotificationState(items=ordered)

    assert state.items == ordered
    with pytest.raises(ValueError, match="duplicate identities"):
        CazatiburonesNotificationState(items=(first, first))
    with pytest.raises(ValueError, match="deterministically ordered"):
        CazatiburonesNotificationState(items=tuple(reversed(ordered)))


def test_recorded_at_must_be_timezone_aware() -> None:
    item = _activity_item()
    with pytest.raises(ValueError, match="timezone-aware"):
        acknowledgement_id(item.notification_id, datetime(2026, 1, 2))
    with pytest.raises(ValueError, match="created_at"):
        CazatiburonesNotification(
            **item.model_dump(exclude={"created_at"}),
            created_at=item.available_at + timedelta(seconds=1),
        )
