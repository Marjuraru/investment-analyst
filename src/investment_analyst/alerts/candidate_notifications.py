"""Durable local delivery receipts, independent from analytical candidate lifecycle."""

import json
import os
import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.alerts.analytical_models import (
    AnalyticalConditionState,
    AnalyticalScreeningResult,
)
from investment_analyst.alerts.analytical_state import (
    AnalyticalCandidateStatus,
    AnalyticalScreeningStateStore,
)
from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.core.models.base import ContractModel, UTCDateTime

_NAMESPACE = UUID("d1eb17a2-25e8-5fcf-bcbc-e0f726db499f")
_TRANSITION_NAMESPACE = UUID("4a1ca89e-c067-58fb-9120-54e785e2d4a5")


class CandidateNotificationStatus(StrEnum):
    """Projected delivery state for the local channel."""

    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"


class CandidateNotificationCondition(ContractModel):
    """Compact immutable evidence retained for the local notification center."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    condition_id: str = Field(min_length=1)
    state: AnalyticalConditionState
    metric_key: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    metric_result_id: UUID | None = None
    as_of: UTCDateTime | None = None
    explanation_es: str = Field(min_length=1)


class CandidateNotificationPayload(ContractModel):
    """Small display payload derived exactly once from the activation result."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    schema_version: Literal["candidate-notification-payload-v1"] = (
        "candidate-notification-payload-v1"
    )
    rule_name_es: str = Field(min_length=1)
    explanation_es: str = Field(min_length=1)
    conditions: tuple[CandidateNotificationCondition, ...] = Field(min_length=1, max_length=20)


class CandidateNotification(ContractModel):
    """Immutable local delivery payload for one candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["candidate-notification-v1"] = "candidate-notification-v1"
    channel: Literal["local_app"] = "local_app"
    notification_id: UUID
    candidate_id: UUID
    activation_result_id: UUID
    asset_id: str
    rule_id: str
    as_of: UTCDateTime
    created_at: UTCDateTime
    payload: CandidateNotificationPayload

    @model_validator(mode="after")
    def validate_identity(self) -> "CandidateNotification":
        if self.notification_id != notification_id(self.candidate_id):
            raise ValueError("notification_id is not deterministic")
        return self


class CandidateNotificationTransition(ContractModel):
    """Append-only acknowledgement evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    transition_id: UUID
    notification_id: UUID
    from_status: Literal[CandidateNotificationStatus.PENDING] = CandidateNotificationStatus.PENDING
    to_status: Literal[CandidateNotificationStatus.ACKNOWLEDGED] = (
        CandidateNotificationStatus.ACKNOWLEDGED
    )
    recorded_at: UTCDateTime
    actor: Literal["local_user"] = "local_user"

    @model_validator(mode="after")
    def validate_identity(self) -> "CandidateNotificationTransition":
        if self.transition_id != notification_transition_id(
            self.notification_id, self.recorded_at, self.actor
        ):
            raise ValueError("notification transition_id is not deterministic")
        return self


class CandidateNotificationState(ContractModel):
    """Versioned deterministic outbox state."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["candidate-notification-outbox-state-v1"] = (
        "candidate-notification-outbox-state-v1"
    )
    items: tuple[CandidateNotification, ...] = Field(default=(), max_length=250_000)
    transitions: tuple[CandidateNotificationTransition, ...] = Field(default=(), max_length=250_000)

    @model_validator(mode="after")
    def validate_state(self) -> "CandidateNotificationState":
        identifiers = tuple(item.notification_id for item in self.items)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("notification items contain duplicate identities")
        known = set(identifiers)
        if any(item.notification_id not in known for item in self.transitions):
            raise ValueError("notification transition references an unknown item")
        if len(self.transitions) != len({item.notification_id for item in self.transitions}):
            raise ValueError("notification transitions are not sequential")
        if tuple((item.created_at, str(item.notification_id)) for item in self.items) != tuple(
            sorted((item.created_at, str(item.notification_id)) for item in self.items)
        ):
            raise ValueError("notification items are not deterministically ordered")
        if tuple((item.recorded_at, str(item.transition_id)) for item in self.transitions) != tuple(
            sorted((item.recorded_at, str(item.transition_id)) for item in self.transitions)
        ):
            raise ValueError("notification transitions are not deterministically ordered")
        return self


def notification_id(candidate_id: UUID) -> UUID:
    return uuid5(_NAMESPACE, f"local_app|v1|{candidate_id}")


def notification_transition_id(
    identifier: UUID, recorded_at: datetime, actor: Literal["local_user"] = "local_user"
) -> UUID:
    return uuid5(
        _TRANSITION_NAMESPACE,
        f"{identifier}|pending|acknowledged|{recorded_at.astimezone(UTC).isoformat()}|{actor}",
    )


class CandidateNotificationStore:
    """Atomically persist and acknowledge local delivery items."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve(strict=False)
        self._lock = threading.RLock()

    def load(self) -> CandidateNotificationState:
        with self._lock:
            if not self._path.exists():
                return CandidateNotificationState()
            try:
                return CandidateNotificationState.model_validate_json(self._path.read_text("utf-8"))
            except (OSError, UnicodeError, ValueError) as error:
                raise AaplOperationalStateError(
                    "candidate notification outbox is malformed"
                ) from error

    def enqueue(self, item: CandidateNotification) -> tuple[CandidateNotification, bool]:
        with self._lock:
            state = self.load()
            existing = next(
                (value for value in state.items if value.notification_id == item.notification_id),
                None,
            )
            if existing is not None:
                if existing != item:
                    raise AaplOperationalStateError("notification identity changed semantics")
                return existing, False
            self._write(
                CandidateNotificationState(
                    items=tuple(
                        sorted(
                            (*state.items, item),
                            key=lambda value: (value.created_at, str(value.notification_id)),
                        )
                    ),
                    transitions=state.transitions,
                )
            )
            return item, True

    def acknowledge(
        self, identifier: UUID, *, recorded_at: datetime
    ) -> tuple[CandidateNotification, bool]:
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        with self._lock:
            state = self.load()
            item = next(
                (value for value in state.items if value.notification_id == identifier), None
            )
            if item is None:
                raise ValueError("notification does not exist")
            if any(value.notification_id == identifier for value in state.transitions):
                return item, False
            transition = CandidateNotificationTransition(
                transition_id=notification_transition_id(identifier, recorded_at),
                notification_id=identifier,
                recorded_at=recorded_at.astimezone(UTC),
            )
            self._write(
                CandidateNotificationState(
                    items=state.items,
                    transitions=tuple(
                        sorted(
                            (*state.transitions, transition),
                            key=lambda value: (value.recorded_at, str(value.notification_id)),
                        )
                    ),
                )
            )
            return item, True

    def _write(self, state: CandidateNotificationState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state.model_dump(mode="json"), separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self._path)


class CandidateNotificationMonitor:
    """Enqueue persisted new candidates without evaluating rules or touching providers."""

    def __init__(
        self, store: CandidateNotificationStore, analytical_store: AnalyticalScreeningStateStore
    ) -> None:
        self._store = store
        self._analytical_store = analytical_store

    def __call__(self, _attempt: object) -> None:
        self.reconcile()

    def reconcile(self) -> None:
        state = self._analytical_store.load()
        results = {item.result_id: item for item in state.results}
        for candidate in state.candidates:
            if candidate.status is not AnalyticalCandidateStatus.NEW:
                continue
            self._store.enqueue(
                CandidateNotification(
                    notification_id=notification_id(candidate.candidate_id),
                    candidate_id=candidate.candidate_id,
                    activation_result_id=candidate.activation_result_id,
                    asset_id=candidate.asset_id,
                    rule_id=candidate.rule_id,
                    as_of=candidate.as_of,
                    created_at=candidate.activated_at,
                    payload=_notification_payload(results[candidate.activation_result_id]),
                )
            )


def _notification_payload(result: AnalyticalScreeningResult) -> CandidateNotificationPayload:
    return CandidateNotificationPayload(
        rule_name_es=result.rule.name_es,
        explanation_es=result.explanation_es,
        conditions=tuple(
            CandidateNotificationCondition(
                condition_id=condition.condition_id,
                state=condition.state,
                metric_key=condition.metric_key,
                unit=condition.unit,
                metric_result_id=condition.metric_result_id,
                as_of=condition.as_of,
                explanation_es=condition.explanation_es,
            )
            for condition in result.conditions
        ),
    )
