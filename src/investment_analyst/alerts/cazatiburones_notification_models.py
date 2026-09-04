"""Strict contracts for the isolated Cazatiburones local notification outbox."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

NotificationFamily = Literal["activity", "institutional"]

_NOTIFICATION_NAMESPACE = UUID("a2d1f8f1-7b70-5b4d-9d69-9bd1bb5a4c1e")
_ACKNOWLEDGEMENT_NAMESPACE = UUID("d8c0d6c8-4b41-5f57-9c82-38fd1df8b6d4")


class _Strict(ContractModel):
    """Frozen strict base for notification contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )


def notification_id(family: NotificationFamily, candidate_id: UUID) -> UUID:
    """Return the stable identity for one family/candidate pair."""
    return uuid5(_NOTIFICATION_NAMESPACE, f"{family}|v1|{candidate_id}")


def acknowledgement_id(
    identifier: UUID,
    recorded_at: datetime,
    actor: Literal["local_user"] = "local_user",
) -> UUID:
    """Return the stable identity for one explicit local acknowledgement."""
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("recorded_at must be timezone-aware")
    recorded_utc = recorded_at.astimezone(UTC)
    return uuid5(
        _ACKNOWLEDGEMENT_NAMESPACE,
        f"{identifier}|pending|acknowledged|{recorded_utc.isoformat()}|{actor}",
    )


class CazatiburonesNotification(_Strict):
    """Immutable local delivery item for one persisted descriptive candidate."""

    schema_version: Literal["cazatiburones-notification-v1"] = "cazatiburones-notification-v1"
    channel: Literal["local_app"] = "local_app"
    family: NotificationFamily
    notification_id: UUID
    candidate_id: UUID
    event_id: UUID
    metric_result_id: UUID
    snapshot_id: UUID
    asset_id: NonEmptyStr
    rule_id: NonEmptyStr
    metric_key: NonEmptyStr
    algorithm_version: NonEmptyStr
    unit: NonEmptyStr
    value: Decimal
    available_at: UTCDateTime
    created_at: UTCDateTime
    input_observation_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100)
    manager_cik: NonEmptyStr | None = None
    report_period: NonEmptyStr | None = None
    prior_report_period: NonEmptyStr | None = None
    cusip: NonEmptyStr | None = None
    title_of_class: NonEmptyStr | None = None
    put_call: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "CazatiburonesNotification":
        if self.notification_id != notification_id(self.family, self.candidate_id):
            raise ValueError("notification_id is not deterministic")
        if self.created_at != self.available_at:
            raise ValueError("created_at must equal event available_at")
        institutional_fields = (
            self.manager_cik,
            self.report_period,
            self.prior_report_period,
            self.cusip,
            self.title_of_class,
        )
        if self.family == "institutional" and any(value is None for value in institutional_fields):
            raise ValueError("institutional notification requires institutional provenance")
        if self.family == "activity" and any(value is not None for value in institutional_fields):
            raise ValueError("activity notification must not carry institutional provenance")
        if len(set(self.input_observation_ids)) != len(self.input_observation_ids):
            raise ValueError("input_observation_ids must be unique")
        return self


class CazatiburonesNotificationAcknowledgement(_Strict):
    """Append-only acknowledgement transition for one local notification."""

    schema_version: Literal["cazatiburones-notification-acknowledgement-v1"] = (
        "cazatiburones-notification-acknowledgement-v1"
    )
    acknowledgement_id: UUID
    notification_id: UUID
    from_status: Literal["pending"] = "pending"
    to_status: Literal["acknowledged"] = "acknowledged"
    recorded_at: UTCDateTime
    actor: Literal["local_user"] = "local_user"

    @model_validator(mode="after")
    def validate_identity(self) -> "CazatiburonesNotificationAcknowledgement":
        if self.acknowledgement_id != acknowledgement_id(
            self.notification_id, self.recorded_at, self.actor
        ):
            raise ValueError("acknowledgement_id is not deterministic")
        return self


class CazatiburonesNotificationState(_Strict):
    """Versioned append-only state for the Cazatiburones local outbox."""

    schema_version: Literal["cazatiburones-notification-outbox-state-v1"] = (
        "cazatiburones-notification-outbox-state-v1"
    )
    items: tuple[CazatiburonesNotification, ...] = Field(default=(), max_length=250_000)
    acknowledgements: tuple[CazatiburonesNotificationAcknowledgement, ...] = Field(
        default=(), max_length=250_000
    )

    @model_validator(mode="after")
    def validate_state(self) -> "CazatiburonesNotificationState":
        identity_pairs = tuple((item.family, item.candidate_id) for item in self.items)
        if len(identity_pairs) != len(set(identity_pairs)):
            raise ValueError(
                "notification items contain duplicate identities for a family/candidate pair"
            )
        notification_ids = tuple(item.notification_id for item in self.items)
        if len(notification_ids) != len(set(notification_ids)):
            raise ValueError("notification items contain duplicate identities")
        known_ids = set(notification_ids)
        acknowledgement_targets = tuple(item.notification_id for item in self.acknowledgements)
        if any(identifier not in known_ids for identifier in acknowledgement_targets):
            raise ValueError("acknowledgement references an unknown notification")
        if len(acknowledgement_targets) != len(set(acknowledgement_targets)):
            raise ValueError("notifications can only be acknowledged once")
        expected_items = tuple(
            sorted(self.items, key=lambda item: (item.created_at, str(item.notification_id)))
        )
        if self.items != expected_items:
            raise ValueError("notification items are not deterministically ordered")
        expected_acknowledgements = tuple(
            sorted(
                self.acknowledgements,
                key=lambda item: (item.recorded_at, str(item.acknowledgement_id)),
            )
        )
        if self.acknowledgements != expected_acknowledgements:
            raise ValueError("acknowledgements are not deterministically ordered")
        return self


class CazatiburonesNotificationReconciliationSummary(_Strict):
    """Compact result of one pure projection and append-only enqueue pass."""

    schema_version: Literal["cazatiburones-notification-reconciliation-summary-v1"] = (
        "cazatiburones-notification-reconciliation-summary-v1"
    )
    activity_snapshots: int = Field(ge=0)
    institutional_snapshots: int = Field(ge=0)
    projected_items: int = Field(ge=0)
    created_items: int = Field(ge=0)
    reused_items: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> "CazatiburonesNotificationReconciliationSummary":
        if self.projected_items != self.created_items + self.reused_items:
            raise ValueError("projected_items must equal created_items plus reused_items")
        return self


class CazatiburonesNotificationAcknowledgementResult(_Strict):
    """Compact CLI/application result for an acknowledgement attempt."""

    schema_version: Literal["cazatiburones-notification-acknowledgement-result-v1"] = (
        "cazatiburones-notification-acknowledgement-result-v1"
    )
    item: CazatiburonesNotification
    acknowledgement: CazatiburonesNotificationAcknowledgement
    created: bool


# Descriptive aliases keep the contract discoverable without introducing parallel schemas.
CazatiburonesNotificationItem = CazatiburonesNotification
CazatiburonesNotificationOutboxState = CazatiburonesNotificationState
