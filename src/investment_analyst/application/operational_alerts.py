"""Deterministic silent screening and local operational alert persistence."""

import hashlib
import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4, uuid5

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.application.multi_asset_scheduler import (
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
)
from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_ALERT_NAMESPACE = UUID("a3b19316-f265-52c2-9239-b653be7d8c1d")
_MAX_ALERT_RECORDS = 250_000


class ScreeningConditionState(StrEnum):
    """Explicit tri-valued condition result."""

    MET = "met"
    NOT_MET = "not_met"
    NOT_EVALUABLE = "not_evaluable"


class OperationalAlertEventStatus(StrEnum):
    """Lifecycle states reserved for the local inbox and future channels."""

    NEW = "new"
    SEEN = "seen"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"
    SILENCED = "silenced"


class OperationalRuleId(StrEnum):
    """Versioned operational rules enabled in the first silent monitor."""

    JOB_FAILED = "operation.job_failed"
    JOB_INTERRUPTED = "operation.job_interrupted"
    JOB_SKIPPED = "operation.job_skipped"
    JOB_COVERAGE_INCOMPLETE = "operation.job_coverage_incomplete"


class OperationalConditionResult(ContractModel):
    """One tri-valued rule condition tied to exact scheduler evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: ScreeningConditionState
    observed_status: ScheduledJobAttemptStatus
    observed_category: NonEmptyStr | None = None
    explanation: NonEmptyStr = Field(max_length=500)


class OperationalScreeningResult(ContractModel):
    """Reproducible screening result for one rule and one job attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["operational-screening-result-v1"] = "operational-screening-result-v1"
    result_id: UUID
    rule_id: OperationalRuleId
    rule_version: Literal["1.0"] = "1.0"
    job_id: NonEmptyStr
    asset_id: NonEmptyStr | None = None
    provider: NonEmptyStr
    domain: NonEmptyStr
    evidence_attempt_id: UUID
    known_at: UTCDateTime
    computed_at: UTCDateTime
    condition: OperationalConditionResult
    activated: bool
    explanation: NonEmptyStr = Field(max_length=500)

    @model_validator(mode="after")
    def validate_result(self) -> "OperationalScreeningResult":
        """Keep activation and deterministic identity aligned with the condition."""
        if self.activated != (self.condition.state is ScreeningConditionState.MET):
            raise ValueError("screening activation must match the tri-valued condition")
        expected = operational_screening_result_id(
            self.rule_id,
            self.rule_version,
            self.evidence_attempt_id,
        )
        if self.result_id != expected:
            raise ValueError("screening result_id is not deterministic")
        if self.computed_at < self.known_at:
            raise ValueError("computed_at must not predate known_at")
        return self

    def semantic_fingerprint(self) -> str:
        """Hash reproducible semantics while excluding recomputation time."""
        payload = self.model_dump(mode="json", exclude={"computed_at"})
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()

    def to_json_dict(self) -> dict[str, object]:
        """Return exact JSON primitives for audit and replay."""
        return self.model_dump(mode="json")


class OperationalAlertEvent(ContractModel):
    """One deduplicated local-inbox event caused by an activated result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["operational-alert-event-v1"] = "operational-alert-event-v1"
    alert_id: UUID
    result_id: UUID
    deduplication_key: NonEmptyStr
    rule_id: OperationalRuleId
    job_id: NonEmptyStr
    asset_id: NonEmptyStr | None = None
    provider: NonEmptyStr
    domain: NonEmptyStr
    first_activated_at: UTCDateTime
    last_activated_at: UTCDateTime
    status: OperationalAlertEventStatus = OperationalAlertEventStatus.NEW
    title: NonEmptyStr = Field(max_length=160)
    message: NonEmptyStr = Field(max_length=500)

    @model_validator(mode="after")
    def validate_event(self) -> "OperationalAlertEvent":
        """Keep time ordering and deterministic deduplication identity coherent."""
        expected_key = f"{self.rule_id.value}:{self.job_id}:{self.result_id}"
        if self.deduplication_key != expected_key:
            raise ValueError("alert deduplication_key is inconsistent")
        expected_id = uuid5(_ALERT_NAMESPACE, self.deduplication_key)
        if self.alert_id != expected_id:
            raise ValueError("alert_id is not deterministic")
        if self.last_activated_at < self.first_activated_at:
            raise ValueError("last_activated_at must not predate first_activated_at")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return bounded local-inbox JSON."""
        return self.model_dump(mode="json")

    def semantic_fingerprint(self) -> str:
        """Hash immutable activation semantics independently from inbox state."""
        payload = self.model_dump(mode="json", exclude={"status"})
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()


class OperationalAlertTransition(ContractModel):
    """Append-only audit record for one explicit local inbox transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["operational-alert-transition-v1"] = "operational-alert-transition-v1"
    transition_id: UUID
    alert_id: UUID
    from_status: OperationalAlertEventStatus
    to_status: OperationalAlertEventStatus
    recorded_at: UTCDateTime
    actor: Literal["local_user", "system_recovery"] = "local_user"

    @model_validator(mode="after")
    def validate_transition(self) -> "OperationalAlertTransition":
        """Reject no-op or non-deterministically identified transitions."""
        if self.from_status is self.to_status:
            raise ValueError("alert transitions must change status")
        if self.transition_id != _alert_transition_id(
            self.alert_id,
            self.from_status,
            self.to_status,
            self.recorded_at,
        ):
            raise ValueError("alert transition_id is not deterministic")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return exact audit primitives."""
        return self.model_dump(mode="json")


class OperationalAlertState(ContractModel):
    """Append-only analytical results and deduplicated alert events."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["operational-alert-state-v1"] = "operational-alert-state-v1"
    screenings: tuple[OperationalScreeningResult, ...] = Field(max_length=_MAX_ALERT_RECORDS)
    events: tuple[OperationalAlertEvent, ...] = Field(max_length=_MAX_ALERT_RECORDS)
    transitions: tuple[OperationalAlertTransition, ...] = Field(
        default=(),
        max_length=_MAX_ALERT_RECORDS,
    )

    @model_validator(mode="after")
    def validate_history(self) -> "OperationalAlertState":
        """Require unique identities, deterministic ordering, and valid references."""
        result_ids = tuple(item.result_id for item in self.screenings)
        event_ids = tuple(item.alert_id for item in self.events)
        transition_ids = tuple(item.transition_id for item in self.transitions)
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("operational screening history contains duplicate identities")
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("operational alert history contains duplicate identities")
        if len(transition_ids) != len(set(transition_ids)):
            raise ValueError("operational alert transition history contains duplicate identities")
        if tuple((item.known_at, str(item.result_id)) for item in self.screenings) != tuple(
            sorted((item.known_at, str(item.result_id)) for item in self.screenings)
        ):
            raise ValueError("operational screening history is not deterministically ordered")
        if tuple((item.first_activated_at, str(item.alert_id)) for item in self.events) != tuple(
            sorted((item.first_activated_at, str(item.alert_id)) for item in self.events)
        ):
            raise ValueError("operational alert history is not deterministically ordered")
        transition_order = tuple(
            (item.recorded_at, str(item.transition_id)) for item in self.transitions
        )
        if transition_order != tuple(sorted(transition_order)):
            raise ValueError("operational alert transitions are not deterministically ordered")
        known_results = set(result_ids)
        if any(item.result_id not in known_results for item in self.events):
            raise ValueError("operational alert references an unknown screening result")
        events_by_id = {item.alert_id: item for item in self.events}
        if any(item.alert_id not in events_by_id for item in self.transitions):
            raise ValueError("operational transition references an unknown alert")
        projected = {item.alert_id: OperationalAlertEventStatus.NEW for item in self.events}
        for transition in self.transitions:
            if projected[transition.alert_id] is not transition.from_status:
                raise ValueError("operational alert transition history is not sequential")
            projected[transition.alert_id] = transition.to_status
        if any(
            events_by_id[alert_id].status is not status for alert_id, status in projected.items()
        ):
            raise ValueError("operational alert status differs from its transition history")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return the complete recoverable monitor state."""
        return {
            "schema_version": self.schema_version,
            "screenings": [item.to_json_dict() for item in self.screenings],
            "events": [item.to_json_dict() for item in self.events],
            "transitions": [item.to_json_dict() for item in self.transitions],
        }


class OperationalAlertInboxStatus(ContractModel):
    """Compact read-only monitor state exposed with the operational overview."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["operational-alert-inbox-status-v1"] = (
        "operational-alert-inbox-status-v1"
    )
    enabled: Literal[True] = True
    silent_mode: Literal[True] = True
    screening_results: int = Field(ge=0)
    alert_count: int = Field(ge=0)
    new_count: int = Field(ge=0)
    latest_alert_at: UTCDateTime | None = None

    def to_json_dict(self) -> dict[str, object]:
        """Return compact counts without loading evidence in the frontend."""
        return self.model_dump(mode="json")


class OperationalAlertInbox(ContractModel):
    """Bounded newest-first local inbox response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["operational-alert-inbox-v1"] = "operational-alert-inbox-v1"
    silent_mode: Literal[True] = True
    total: int = Field(ge=0)
    events: tuple[OperationalAlertEvent, ...] = Field(max_length=200)

    def to_json_dict(self) -> dict[str, object]:
        """Return bounded local-inbox events."""
        return self.model_dump(mode="json")


class OperationalAlertStateStore:
    """Atomically persist deterministic screening and local-inbox events."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve(strict=False)
        self._lock = threading.RLock()
        self._reconciliation_state: OperationalAlertState | None = None
        self._reconciliation_result_ids: set[UUID] | None = None

    def load(self) -> OperationalAlertState:
        """Load valid monitor state without creating a missing file."""
        with self._lock:
            if not self._path.exists():
                return OperationalAlertState(screenings=(), events=(), transitions=())
            try:
                return OperationalAlertState.model_validate_json(
                    self._path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError) as error:
                raise AaplOperationalStateError(
                    "operational alert state is malformed or unreadable"
                ) from error

    def begin_reconciliation(self) -> None:
        """Validate once and retain the startup snapshot until reconciliation completes."""
        with self._lock:
            self._reconciliation_state = self.load()
            self._reconciliation_result_ids = {
                item.result_id for item in self._reconciliation_state.screenings
            }

    def end_reconciliation(self) -> None:
        """Discard the ephemeral startup index after the ordered replay."""
        with self._lock:
            self._reconciliation_state = None
            self._reconciliation_result_ids = None

    def is_attempt_processed(self, attempt: ScheduledJobAttempt) -> bool:
        """Require all four deterministic rule results before skipping an attempt."""
        with self._lock:
            state = self._reconciliation_state or self.load()
            known = self._reconciliation_result_ids or {item.result_id for item in state.screenings}
            return all(
                operational_screening_result_id(rule_id, "1.0", attempt.attempt_id) in known
                for rule_id in OperationalRuleId
            )

    def needs_recovery(self, attempt: ScheduledJobAttempt) -> bool:
        """Keep an interrupted post-screening recovery visible as pending work."""
        if (
            attempt.status is not ScheduledJobAttemptStatus.SUCCEEDED
            or attempt.execution is None
            or not attempt.execution.coverage_complete
            or attempt.completed_at is None
        ):
            return False
        with self._lock:
            state = self._reconciliation_state or self.load()
            return any(
                event.job_id == attempt.definition.job_id
                and event.first_activated_at < attempt.completed_at
                and event.status is not OperationalAlertEventStatus.RESOLVED
                for event in state.events
            )

    def record(
        self,
        screenings: tuple[OperationalScreeningResult, ...],
        events: tuple[OperationalAlertEvent, ...],
    ) -> tuple[int, int]:
        """Persist only new identities and reject contradictory recomputations."""
        with self._lock:
            state = self._reconciliation_state or self.load()
            by_result = {item.result_id: item for item in state.screenings}
            by_alert = {item.alert_id: item for item in state.events}
            created_results = 0
            created_events = 0
            for result in screenings:
                existing = by_result.get(result.result_id)
                if existing is not None:
                    if existing.semantic_fingerprint() != result.semantic_fingerprint():
                        raise AaplOperationalStateError(
                            "screening recomputation changed deterministic semantics"
                        )
                    continue
                by_result[result.result_id] = result
                created_results += 1
            for event in events:
                existing = by_alert.get(event.alert_id)
                if existing is not None:
                    if existing.semantic_fingerprint() != event.semantic_fingerprint():
                        raise AaplOperationalStateError(
                            "alert recomputation changed deterministic semantics"
                        )
                    continue
                if event.result_id not in by_result:
                    raise AaplOperationalStateError(
                        "alert event cannot be stored without its screening result"
                    )
                by_alert[event.alert_id] = event
                created_events += 1
            if created_results or created_events:
                snapshot = OperationalAlertState(
                    screenings=tuple(
                        sorted(
                            by_result.values(),
                            key=lambda item: (item.known_at, str(item.result_id)),
                        )
                    ),
                    events=tuple(
                        sorted(
                            by_alert.values(),
                            key=lambda item: (
                                item.first_activated_at,
                                str(item.alert_id),
                            ),
                        )
                    ),
                    transitions=state.transitions,
                )
                self._write(snapshot)
                self._reconciliation_state = snapshot
                self._reconciliation_result_ids = {item.result_id for item in snapshot.screenings}
            return created_results, created_events

    def transition(
        self,
        alert_id: UUID,
        to_status: OperationalAlertEventStatus,
        *,
        recorded_at: datetime,
        actor: Literal["local_user", "system_recovery"] = "local_user",
    ) -> tuple[OperationalAlertEvent, bool]:
        """Apply one idempotent allowed transition and append its audit evidence."""
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        recorded_at = recorded_at.astimezone(UTC)
        if to_status is OperationalAlertEventStatus.NEW:
            raise ValueError("an alert cannot transition back to new")
        with self._lock:
            state = self._reconciliation_state or self.load()
            events = {item.alert_id: item for item in state.events}
            current = events.get(alert_id)
            if current is None:
                raise ValueError("operational alert does not exist")
            if current.status is to_status:
                return current, False
            allowed = {
                OperationalAlertEventStatus.NEW: {
                    OperationalAlertEventStatus.SEEN,
                    OperationalAlertEventStatus.DISMISSED,
                    OperationalAlertEventStatus.RESOLVED,
                    OperationalAlertEventStatus.SILENCED,
                },
                OperationalAlertEventStatus.SEEN: {
                    OperationalAlertEventStatus.DISMISSED,
                    OperationalAlertEventStatus.RESOLVED,
                    OperationalAlertEventStatus.SILENCED,
                },
                OperationalAlertEventStatus.DISMISSED: {
                    OperationalAlertEventStatus.RESOLVED,
                },
                OperationalAlertEventStatus.SILENCED: {
                    OperationalAlertEventStatus.RESOLVED,
                },
                OperationalAlertEventStatus.RESOLVED: set(),
            }
            if to_status not in allowed[current.status]:
                raise ValueError("operational alert transition is not allowed")
            transition = OperationalAlertTransition(
                transition_id=_alert_transition_id(
                    alert_id,
                    current.status,
                    to_status,
                    recorded_at,
                ),
                alert_id=alert_id,
                from_status=current.status,
                to_status=to_status,
                recorded_at=recorded_at,
                actor=actor,
            )
            updated = current.model_copy(update={"status": to_status})
            events[alert_id] = updated
            transitions = tuple(
                sorted(
                    (*state.transitions, transition),
                    key=lambda item: (item.recorded_at, str(item.transition_id)),
                )
            )
            snapshot = OperationalAlertState(
                screenings=state.screenings,
                events=tuple(
                    sorted(
                        events.values(),
                        key=lambda item: (
                            item.first_activated_at,
                            str(item.alert_id),
                        ),
                    )
                ),
                transitions=transitions,
            )
            self._write(snapshot)
            self._reconciliation_state = snapshot
            return updated, True

    def resolve_recovered_job(
        self,
        job_id: str,
        *,
        recovered_at: datetime,
        recorded_at: datetime,
    ) -> int:
        """Resolve prior alerts after complete success for the same scheduled job."""
        if not job_id.strip():
            raise ValueError("job_id must not be empty")
        for name, value in (("recovered_at", recovered_at), ("recorded_at", recorded_at)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        recovered_at = recovered_at.astimezone(UTC)
        recorded_at = recorded_at.astimezone(UTC)
        if recorded_at < recovered_at:
            raise ValueError("recorded_at must not predate recovered_at")
        with self._lock:
            state = self._reconciliation_state or self.load()
            recoverable = tuple(
                item
                for item in state.events
                if item.job_id == job_id
                and item.first_activated_at < recovered_at
                and item.status is not OperationalAlertEventStatus.RESOLVED
            )
            if not recoverable:
                return 0
            if state.transitions and recorded_at < state.transitions[-1].recorded_at:
                raise AaplOperationalStateError(
                    "operational recovery time predates the latest alert transition"
                )
            events = {item.alert_id: item for item in state.events}
            transitions = list(state.transitions)
            for current in recoverable:
                transitions.append(
                    OperationalAlertTransition(
                        transition_id=_alert_transition_id(
                            current.alert_id,
                            current.status,
                            OperationalAlertEventStatus.RESOLVED,
                            recorded_at,
                        ),
                        alert_id=current.alert_id,
                        from_status=current.status,
                        to_status=OperationalAlertEventStatus.RESOLVED,
                        recorded_at=recorded_at,
                        actor="system_recovery",
                    )
                )
                events[current.alert_id] = current.model_copy(
                    update={"status": OperationalAlertEventStatus.RESOLVED}
                )
            snapshot = OperationalAlertState(
                screenings=state.screenings,
                events=tuple(
                    sorted(
                        events.values(),
                        key=lambda item: (
                            item.first_activated_at,
                            str(item.alert_id),
                        ),
                    )
                ),
                transitions=tuple(
                    sorted(
                        transitions,
                        key=lambda item: (item.recorded_at, str(item.transition_id)),
                    )
                ),
            )
            self._write(snapshot)
            self._reconciliation_state = snapshot
            return len(recoverable)

    def status(self) -> OperationalAlertInboxStatus:
        """Return monitor counts without any provider or analytical query."""
        state = self.load()
        return OperationalAlertInboxStatus(
            screening_results=len(state.screenings),
            alert_count=len(state.events),
            new_count=sum(item.status is OperationalAlertEventStatus.NEW for item in state.events),
            latest_alert_at=(
                max(item.last_activated_at for item in state.events) if state.events else None
            ),
        )

    def inbox(self, *, limit: int = 50) -> OperationalAlertInbox:
        """Return a bounded newest-first local inbox."""
        if isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("alert inbox limit must be between 1 and 200")
        state = self.load()
        ordered = tuple(
            sorted(
                state.events,
                key=lambda item: (item.last_activated_at, str(item.alert_id)),
                reverse=True,
            )
        )
        return OperationalAlertInbox(total=len(ordered), events=ordered[:limit])

    def _write(self, state: OperationalAlertState) -> None:
        document = (
            json.dumps(
                state.to_json_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        descriptor: int | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            directory = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            raise AaplOperationalStateError(
                "operational alert state could not be written"
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


class OperationalAlertEngine:
    """Replay operational rules over one completed scheduler attempt."""

    def evaluate(
        self,
        attempt: ScheduledJobAttempt,
        *,
        computed_at: datetime,
    ) -> tuple[OperationalScreeningResult, ...]:
        """Evaluate all v1 operational rules without providers or workspace reads."""
        if attempt.status is ScheduledJobAttemptStatus.RUNNING or attempt.completed_at is None:
            raise ValueError("operational screening requires a completed attempt")
        if computed_at.tzinfo is None or computed_at.utcoffset() is None:
            raise ValueError("computed_at must be timezone-aware")
        computed_at = computed_at.astimezone(UTC)
        return tuple(
            self._evaluate_rule(rule_id, attempt, computed_at) for rule_id in OperationalRuleId
        )

    def events_for(
        self,
        results: tuple[OperationalScreeningResult, ...],
    ) -> tuple[OperationalAlertEvent, ...]:
        """Create at most one deterministic silent event per activated result."""
        return tuple(self._event(result) for result in results if result.activated)

    def _evaluate_rule(
        self,
        rule_id: OperationalRuleId,
        attempt: ScheduledJobAttempt,
        computed_at: datetime,
    ) -> OperationalScreeningResult:
        category = attempt.failure.category if attempt.failure else None
        state = ScreeningConditionState.NOT_MET
        explanation = "La ejecución terminó sin activar esta condición operativa."
        if rule_id is OperationalRuleId.JOB_FAILED:
            if attempt.status is ScheduledJobAttemptStatus.FAILED and category != "interrupted_job":
                state = ScreeningConditionState.MET
                explanation = "La actualización automática terminó con un fallo seguro."
        elif rule_id is OperationalRuleId.JOB_INTERRUPTED:
            if category == "interrupted_job":
                state = ScreeningConditionState.MET
                explanation = "La actualización anterior fue interrumpida antes de completarse."
        elif rule_id is OperationalRuleId.JOB_SKIPPED:
            if attempt.status is ScheduledJobAttemptStatus.SKIPPED:
                state = ScreeningConditionState.MET
                explanation = "La actualización automática fue omitida y requiere revisión."
        elif (
            attempt.status is ScheduledJobAttemptStatus.SUCCEEDED
            and attempt.execution is not None
            and not attempt.execution.coverage_complete
        ):
            state = ScreeningConditionState.MET
            explanation = "La actualización terminó, pero dejó cobertura pendiente."
        if attempt.failure is None and attempt.status not in {
            ScheduledJobAttemptStatus.SUCCEEDED,
        }:
            state = ScreeningConditionState.NOT_EVALUABLE
            explanation = "La ejecución no contiene evidencia suficiente para evaluar la regla."
        result_id = operational_screening_result_id(
            rule_id,
            "1.0",
            attempt.attempt_id,
        )
        return OperationalScreeningResult(
            result_id=result_id,
            rule_id=rule_id,
            job_id=attempt.definition.job_id,
            asset_id=attempt.definition.asset_id,
            provider=attempt.definition.provider,
            domain=attempt.definition.domain.value,
            evidence_attempt_id=attempt.attempt_id,
            known_at=attempt.completed_at,
            computed_at=computed_at,
            condition=OperationalConditionResult(
                state=state,
                observed_status=attempt.status,
                observed_category=category,
                explanation=explanation,
            ),
            activated=state is ScreeningConditionState.MET,
            explanation=explanation,
        )

    @staticmethod
    def _event(result: OperationalScreeningResult) -> OperationalAlertEvent:
        deduplication_key = f"{result.rule_id.value}:{result.job_id}:{result.result_id}"
        titles = {
            OperationalRuleId.JOB_FAILED: "Actualización automática fallida",
            OperationalRuleId.JOB_INTERRUPTED: "Actualización automática interrumpida",
            OperationalRuleId.JOB_SKIPPED: "Actualización automática omitida",
            OperationalRuleId.JOB_COVERAGE_INCOMPLETE: "Cobertura automática incompleta",
        }
        return OperationalAlertEvent(
            alert_id=uuid5(_ALERT_NAMESPACE, deduplication_key),
            result_id=result.result_id,
            deduplication_key=deduplication_key,
            rule_id=result.rule_id,
            job_id=result.job_id,
            asset_id=result.asset_id,
            provider=result.provider,
            domain=result.domain,
            first_activated_at=result.known_at,
            last_activated_at=result.known_at,
            title=titles[result.rule_id],
            message=(
                f"{result.explanation} Trabajo: {result.job_id}. "
                "Revisa cobertura y evidencia antes de continuar."
            ),
        )


class OperationalAlertMonitor:
    """Persist silent operational screening after every completed scheduler job."""

    def __init__(
        self,
        store: OperationalAlertStateStore,
        *,
        engine: OperationalAlertEngine | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._engine = engine or OperationalAlertEngine()
        self._clock = clock

    def __call__(self, attempt: ScheduledJobAttempt) -> None:
        """Evaluate one new evidence attempt and persist idempotently."""
        already_processed = self._store.is_attempt_processed(attempt)
        recovery_pending = self._store.needs_recovery(attempt)
        if already_processed and not recovery_pending:
            return
        computed_at = self._clock()
        if not isinstance(computed_at, datetime):
            raise ValueError("alert monitor clock must return a datetime")
        if not already_processed:
            results = self._engine.evaluate(attempt, computed_at=computed_at)
            self._store.record(results, self._engine.events_for(results))
        if (
            attempt.status is ScheduledJobAttemptStatus.SUCCEEDED
            and attempt.execution is not None
            and attempt.execution.coverage_complete
            and attempt.completed_at is not None
        ):
            self._store.resolve_recovered_job(
                attempt.definition.job_id,
                recovered_at=attempt.completed_at,
                recorded_at=computed_at,
            )

    def reconcile(self, attempts: tuple[ScheduledJobAttempt, ...]) -> None:
        """Backfill missing monitor results from durable completed scheduler attempts."""
        self._store.begin_reconciliation()
        try:
            for attempt in attempts:
                if attempt.status is not ScheduledJobAttemptStatus.RUNNING:
                    self(attempt)
        finally:
            self._store.end_reconciliation()


def operational_screening_result_id(
    rule_id: OperationalRuleId,
    rule_version: str,
    evidence_attempt_id: UUID,
) -> UUID:
    """Return the stable identity for one rule/evidence evaluation."""
    return uuid5(
        _ALERT_NAMESPACE,
        f"{rule_id.value}:{rule_version}:{evidence_attempt_id}",
    )


def _alert_transition_id(
    alert_id: UUID,
    from_status: OperationalAlertEventStatus,
    to_status: OperationalAlertEventStatus,
    recorded_at: datetime,
) -> UUID:
    return uuid5(
        _ALERT_NAMESPACE,
        (
            f"transition:{alert_id}:{from_status.value}:{to_status.value}:"
            f"{recorded_at.astimezone(UTC).isoformat()}"
        ),
    )


__all__ = [
    "OperationalAlertEngine",
    "OperationalAlertEvent",
    "OperationalAlertEventStatus",
    "OperationalAlertInbox",
    "OperationalAlertInboxStatus",
    "OperationalAlertMonitor",
    "OperationalAlertState",
    "OperationalAlertStateStore",
    "OperationalAlertTransition",
    "OperationalConditionResult",
    "OperationalRuleId",
    "OperationalScreeningResult",
    "ScreeningConditionState",
    "operational_screening_result_id",
]
