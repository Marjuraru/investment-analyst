"""Typed contracts for the optional local Windows toast delivery channel."""

from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_DELIVERY_NAMESPACE = UUID("560cb13a-f672-56ae-b412-76d63cd2d916")

DESKTOP_NOTIFICATION_CHANNEL = "windows_toast"
DESKTOP_NOTIFICATION_VERSION = "v1"
MAX_RETAINED_DELIVERIES = 10_000
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_BATCH_DELIVERIES = 5
MIN_BATCH_INTERVAL_SECONDS = 60
MAX_SEND_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (60, 300)


class DesktopNotificationDeliveryStatus(StrEnum):
    """Explicit transport lifecycle for one candidate notification."""

    PENDING = "pending"
    ATTEMPTING = "attempting"
    ACCEPTED = "accepted"
    RETRY_WAIT = "retry_wait"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DesktopNotificationReason(StrEnum):
    """Closed vocabulary for transport outcomes and cancellation causes."""

    CHANNEL_DISABLED = "channel_disabled"
    CANDIDATE_MISSING = "candidate_missing"
    CANDIDATE_NOT_NEW = "candidate_not_new"
    CANDIDATE_ACKNOWLEDGED = "candidate_acknowledged"
    CANDIDATE_EVIDENCE_MISMATCH = "candidate_evidence_mismatch"
    NOTIFICATION_MISSING = "notification_missing"
    NOTIFICATION_EVIDENCE_MISMATCH = "notification_evidence_mismatch"
    RESULT_MISSING = "result_missing"
    RULE_CONFIGURATION_MISSING = "rule_configuration_missing"
    RULE_NOT_ACTIVE_AT_ACTIVATION = "rule_not_active_at_activation"
    RULE_NOT_ACTIVE_NOW = "rule_not_active_now"
    RULE_FINGERPRINT_CHANGED = "rule_fingerprint_changed"
    NOTIFICATION_BEFORE_ACTIVATION = "notification_before_activation"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTERRUPTED_AFTER_INVOCATION = "interrupted_after_invocation"
    BRIDGE_UNAVAILABLE = "bridge_unavailable"
    POWERSHELL_MISSING = "powershell_missing"
    SCRIPT_MISSING = "script_missing"
    SESSION_UNAVAILABLE = "session_unavailable"
    WINRT_UNAVAILABLE = "winrt_unavailable"
    REGISTRATION_FAILED = "registration_failed"
    TIMEOUT = "timeout"
    INVALID_RESULT = "invalid_result"
    PROCESS_EXIT = "process_exit"
    OUTPUT_LIMIT = "output_limit"
    SEND_ERROR = "send_error"
    PROBE_FAILED = "probe_failed"


class DesktopNotificationChannelConfig(ContractModel):
    """Persisted channel configuration and last verified capability evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["desktop-notification-config-v1"] = "desktop-notification-config-v1"
    channel: Literal["windows_toast"] = "windows_toast"
    enabled: bool = False
    enabled_at: UTCDateTime | None = None
    updated_at: UTCDateTime | None = None
    last_probe_at: UTCDateTime | None = None
    last_probe_available: bool | None = None
    last_probe_reason: DesktopNotificationReason | None = None
    last_test_at: UTCDateTime | None = None
    last_test_accepted: bool | None = None
    last_test_reason: DesktopNotificationReason | None = None
    last_batch_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def validate_configuration(self) -> "DesktopNotificationChannelConfig":
        """Keep activation and capability fields internally consistent."""
        if self.enabled != (self.enabled_at is not None):
            raise ValueError("enabled must match the activation boundary")
        if (self.last_probe_at is None) != (self.last_probe_available is None):
            raise ValueError("probe availability must match its timestamp")
        if self.last_probe_available is True and self.last_probe_reason is not None:
            raise ValueError("an available probe cannot declare a reason")
        if self.last_probe_available is False and self.last_probe_reason is None:
            raise ValueError("an unavailable probe must declare a reason")
        if (self.last_test_at is None) != (self.last_test_accepted is None):
            raise ValueError("test acceptance must match its timestamp")
        if self.last_test_accepted is True and self.last_test_reason is not None:
            raise ValueError("an accepted test cannot declare a reason")
        if self.last_test_accepted is False and self.last_test_reason is None:
            raise ValueError("a failed test must declare a reason")
        if (
            self.updated_at is not None
            and self.enabled_at is not None
            and self.enabled_at > self.updated_at
        ):
            raise ValueError("activation cannot follow the last configuration update")
        return self


class DesktopNotificationDelivery(ContractModel):
    """Compact append-only evidence for one candidate notification delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["desktop-notification-delivery-v1"] = "desktop-notification-delivery-v1"
    channel: Literal["windows_toast"] = "windows_toast"
    delivery_id: UUID
    workspace_uuid: UUID
    notification_id: UUID
    candidate_id: UUID
    activation_result_id: UUID
    rule_id: NonEmptyStr
    asset_id: NonEmptyStr
    title_es: NonEmptyStr = Field(max_length=160)
    body_es: NonEmptyStr = Field(max_length=240)
    notification_created_at: UTCDateTime
    fingerprint: NonEmptyStr = Field(pattern=r"^[0-9a-f]{64}$")
    status: DesktopNotificationDeliveryStatus
    attempt_count: int = Field(ge=0, le=MAX_SEND_ATTEMPTS)
    created_at: UTCDateTime
    updated_at: UTCDateTime
    next_attempt_at: UTCDateTime | None = None
    reason: DesktopNotificationReason | None = None
    windows_tag: NonEmptyStr = Field(pattern=r"^[0-9a-f]{16}$")
    windows_group: NonEmptyStr = Field(pattern=r"^[0-9a-f]{16}$")

    @model_validator(mode="after")
    def validate_delivery(self) -> "DesktopNotificationDelivery":
        """Verify deterministic identity, Windows identity, and lifecycle coherence."""
        if self.delivery_id != desktop_notification_delivery_id(
            self.workspace_uuid, self.notification_id
        ):
            raise ValueError("delivery_id is not deterministic")
        if self.windows_tag != self.delivery_id.hex[:16]:
            raise ValueError("windows_tag is not derived from the delivery identity")
        if self.windows_group != self.delivery_id.hex[16:32]:
            raise ValueError("windows_group is not derived from the delivery identity")
        if self.updated_at < self.created_at:
            raise ValueError("delivery update cannot predate admission")
        if self.notification_created_at > self.created_at:
            raise ValueError("delivery cannot precede its notification")
        if self.status is DesktopNotificationDeliveryStatus.PENDING:
            if (
                self.attempt_count != 0
                or self.next_attempt_at is not None
                or self.reason is not None
            ):
                raise ValueError("pending deliveries cannot contain an attempt outcome")
        elif self.status is DesktopNotificationDeliveryStatus.ATTEMPTING:
            if (
                self.attempt_count < 1
                or self.next_attempt_at is not None
                or self.reason is not None
            ):
                raise ValueError("attempting deliveries require one declared attempt")
        elif self.status is DesktopNotificationDeliveryStatus.ACCEPTED:
            if (
                self.attempt_count < 1
                or self.next_attempt_at is not None
                or self.reason is not None
            ):
                raise ValueError("accepted deliveries require only attempt evidence")
        elif self.status is DesktopNotificationDeliveryStatus.RETRY_WAIT:
            if self.attempt_count < 1 or self.attempt_count >= MAX_SEND_ATTEMPTS:
                raise ValueError("retry_wait requires remaining attempts")
            if self.next_attempt_at is None or self.reason is None:
                raise ValueError("retry_wait requires a delay and a reason")
            if self.next_attempt_at < self.updated_at:
                raise ValueError("retry_wait cannot be due before its transition")
        elif self.status is DesktopNotificationDeliveryStatus.UNKNOWN:
            if self.attempt_count < 1 or self.next_attempt_at is not None or self.reason is None:
                raise ValueError("unknown deliveries require a reason and no retry")
        elif self.status is DesktopNotificationDeliveryStatus.CANCELLED:
            if self.next_attempt_at is not None or self.reason is None:
                raise ValueError("cancelled deliveries require a reason and no retry")
        else:
            if self.attempt_count < 1:
                raise ValueError("failed deliveries require at least one attempt")
            if self.next_attempt_at is not None or self.reason is None:
                raise ValueError("failed deliveries require a reason and no retry")
        return self


class DesktopNotificationCapability(ContractModel):
    """Last known transport capability without probing on read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool | None = None
    reason: DesktopNotificationReason | None = None
    checked_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def validate_capability(self) -> "DesktopNotificationCapability":
        """Keep the tri-state capability and its evidence aligned."""
        if (self.available is None) != (self.checked_at is None):
            raise ValueError("capability availability must match its check timestamp")
        if self.available is True and self.reason is not None:
            raise ValueError("an available capability cannot declare a reason")
        if self.available is False and self.reason is None:
            raise ValueError("an unavailable capability must declare a reason")
        return self


class DesktopNotificationTestResponse(ContractModel):
    """Explicit transport-test outcome without any synthetic candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["desktop-notification-test-response-v1"] = (
        "desktop-notification-test-response-v1"
    )
    accepted: bool
    reason: DesktopNotificationReason | None = None
    checked_at: UTCDateTime
    capability: DesktopNotificationCapability

    @model_validator(mode="after")
    def validate_test(self) -> "DesktopNotificationTestResponse":
        """Acceptance is exactly the absence of a failure reason."""
        if self.accepted != (self.reason is None):
            raise ValueError("test acceptance must match its reason")
        return self


class DesktopNotificationStatus(ContractModel):
    """Bounded read-only channel summary plus one stable delivery page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["desktop-notification-status-v1"] = "desktop-notification-status-v1"
    channel: Literal["windows_toast"] = "windows_toast"
    enabled: bool
    enabled_at: UTCDateTime | None
    updated_at: UTCDateTime | None
    capability: DesktopNotificationCapability
    last_test_at: UTCDateTime | None
    last_test_accepted: bool | None
    last_test_reason: DesktopNotificationReason | None
    total: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    attempting_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    retry_wait_count: int = Field(ge=0)
    unknown_count: int = Field(ge=0)
    cancelled_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    state_bytes: int = Field(ge=0)
    budget_bytes: int = Field(ge=0)
    retained_limit: int = Field(ge=1)
    budget_exceeded: bool
    next_attempt_at: UTCDateTime | None
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    returned: int = Field(ge=0)
    truncated: bool
    deliveries: tuple[DesktopNotificationDelivery, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_status(self) -> "DesktopNotificationStatus":
        """Keep counters, pagination, and the delivery page aligned."""
        counts = (
            self.pending_count
            + self.attempting_count
            + self.accepted_count
            + self.retry_wait_count
            + self.unknown_count
            + self.cancelled_count
            + self.failed_count
        )
        if counts != self.total:
            raise ValueError("delivery counters must match the total")
        if self.returned != len(self.deliveries):
            raise ValueError("returned must match the delivery page")
        if self.returned > self.limit:
            raise ValueError("delivery page cannot exceed its limit")
        if self.budget_exceeded != (
            self.state_bytes >= self.budget_bytes or self.total >= self.retained_limit
        ):
            raise ValueError("budget_exceeded must match the declared budget")
        return self


def desktop_notification_delivery_id(workspace_uuid: UUID, notification_id: UUID) -> UUID:
    """Return the stable delivery identity for one workspace notification."""
    return uuid5(
        _DELIVERY_NAMESPACE,
        (
            f"{DESKTOP_NOTIFICATION_CHANNEL}|{DESKTOP_NOTIFICATION_VERSION}|"
            f"{workspace_uuid}|{notification_id}"
        ),
    )


__all__ = [
    "DESKTOP_NOTIFICATION_CHANNEL",
    "DESKTOP_NOTIFICATION_VERSION",
    "MAX_BATCH_DELIVERIES",
    "MAX_RETAINED_DELIVERIES",
    "MAX_SEND_ATTEMPTS",
    "MAX_STATE_BYTES",
    "MIN_BATCH_INTERVAL_SECONDS",
    "RETRY_DELAYS_SECONDS",
    "DesktopNotificationCapability",
    "DesktopNotificationChannelConfig",
    "DesktopNotificationDelivery",
    "DesktopNotificationDeliveryStatus",
    "DesktopNotificationReason",
    "DesktopNotificationStatus",
    "DesktopNotificationTestResponse",
    "desktop_notification_delivery_id",
]
