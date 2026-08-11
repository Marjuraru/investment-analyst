"""Provider-independent scheduling for explicit asset and data-domain jobs."""

import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_MAX_ATTEMPTS_RETAINED = 100_000


class ScheduledJobDomain(StrEnum):
    """Independent data or analytical domain updated by one job."""

    MARKET_DAILY = "market_daily"
    MARKET_INTRADAY = "market_intraday"
    CRYPTO_DERIVATIVES = "crypto_derivatives"
    FUNDAMENTALS = "fundamentals"
    CATALOG = "catalog"
    MACRO = "macro"
    EVENTS = "events"


class ScheduledJobAttemptStatus(StrEnum):
    """Durable lifecycle outcome for one bounded job attempt."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ScheduledJobFreshness(StrEnum):
    """Age and coverage state of the latest successful provider check."""

    NEVER_RUN = "never_run"
    CURRENT = "current"
    STALE = "stale"
    INCOMPLETE = "incomplete"


class ScheduledJobFailureCategory(StrEnum):
    """Stable operational categories that determine retry policy without message parsing."""

    CONFIGURATION = "configuration_error"
    AUTHENTICATION = "authentication_error"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    PROVIDER_CONTRACT = "provider_contract_error"
    VALIDATION = "validation_error"
    STORAGE_STATE = "storage_state_error"
    RATE_LIMIT = "rate_limit"
    TRANSPORT = "transport_error"
    TRANSIENT_HTTP = "transient_http_error"
    HTTP = "http_error"
    UNEXPECTED = "unexpected_error"
    INTERRUPTED = "interrupted_job"
    LEGACY_UNKNOWN = "legacy_unknown"


class ScheduledJobHealth(StrEnum):
    """Deterministic operational health projection for one registered job.

    Precedence is disabled, running, retry_wait, blocked, never_run, incomplete,
    stale, then current. This keeps an active fault visible alongside fresh data.
    """

    DISABLED = "disabled"
    NEVER_RUN = "never_run"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    BLOCKED = "blocked"
    CURRENT = "current"
    STALE = "stale"
    INCOMPLETE = "incomplete"


_FAILURE_RETRY_POLICY: dict[ScheduledJobFailureCategory, bool] = {
    ScheduledJobFailureCategory.CONFIGURATION: False,
    ScheduledJobFailureCategory.AUTHENTICATION: False,
    ScheduledJobFailureCategory.UNSUPPORTED_CAPABILITY: False,
    ScheduledJobFailureCategory.PROVIDER_CONTRACT: False,
    ScheduledJobFailureCategory.VALIDATION: False,
    ScheduledJobFailureCategory.STORAGE_STATE: False,
    ScheduledJobFailureCategory.RATE_LIMIT: True,
    ScheduledJobFailureCategory.TRANSPORT: True,
    ScheduledJobFailureCategory.TRANSIENT_HTTP: True,
    ScheduledJobFailureCategory.HTTP: False,
    ScheduledJobFailureCategory.UNEXPECTED: False,
    ScheduledJobFailureCategory.INTERRUPTED: True,
    ScheduledJobFailureCategory.LEGACY_UNKNOWN: False,
}


class ProviderJobTelemetry(ContractModel):
    """Safe provider/job timing and evidence counters for one completed attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["provider-job-telemetry-v1"] = "provider-job-telemetry-v1"
    job_id: NonEmptyStr
    provider: NonEmptyStr
    domain: ScheduledJobDomain
    started_at: UTCDateTime
    completed_at: UTCDateTime
    duration_ms: int = Field(ge=0)
    provider_call_count: int | None = Field(default=None, ge=0)
    response_bytes: int | None = Field(default=None, ge=0)
    created_count: int = Field(default=0, ge=0)
    reused_count: int = Field(default=0, ge=0)
    coverage_complete: bool | None = None
    failure_category: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "ProviderJobTelemetry":
        if self.completed_at < self.started_at:
            raise ValueError("telemetry completion must not predate start")
        if (self.coverage_complete is None) == (self.failure_category is None):
            raise ValueError("telemetry requires exactly one success or failure outcome")
        return self


class ScheduledJobDefinition(ContractModel):
    """Immutable schedule and scope for one provider-independent job."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["scheduled-job-definition-v1"] = "scheduled-job-definition-v1"
    job_id: NonEmptyStr
    asset_id: NonEmptyStr | None = None
    provider: NonEmptyStr
    domain: ScheduledJobDomain
    data_frequency: NonEmptyStr
    timezone: NonEmptyStr = "America/Lima"
    run_at: time = time(hour=7)
    enabled: bool = True
    max_attempts_per_day: int = Field(default=3, ge=1, le=10)
    retry_backoff_seconds: int = Field(default=900, ge=1, le=86_400)
    freshness_threshold_seconds: int = Field(
        default=172_800,
        ge=60,
        le=31_536_000,
    )

    @field_validator("run_at", mode="before")
    @classmethod
    def require_local_minute(cls, value: object) -> object:
        """Accept HH:MM while rejecting timezone-aware or sub-minute values."""
        if isinstance(value, str):
            try:
                value = time.fromisoformat(value)
            except ValueError as error:
                raise ValueError("run_at must use HH:MM") from error
        if not isinstance(value, time):
            raise ValueError("run_at must be a time")
        if value.tzinfo is not None or value.second != 0 or value.microsecond != 0:
            raise ValueError("run_at must be a timezone-naive whole minute")
        return value

    @field_validator("timezone")
    @classmethod
    def require_known_timezone(cls, value: str) -> str:
        """Require an installed IANA timezone without network access."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("timezone must identify an installed IANA timezone") from error
        return value

    @field_validator(
        "enabled",
        mode="before",
    )
    @classmethod
    def require_boolean(cls, value: object) -> object:
        """Reject truthy strings and integers in operational configuration."""
        if not isinstance(value, bool):
            raise ValueError("enabled must be a bool")
        return value

    @field_validator(
        "max_attempts_per_day",
        "retry_backoff_seconds",
        "freshness_threshold_seconds",
        mode="before",
    )
    @classmethod
    def reject_boolean_integers(cls, value: object, info: ValidationInfo) -> object:
        """Reject booleans accepted by Python's integer hierarchy."""
        if isinstance(value, bool):
            raise ValueError(f"{info.field_name} must be an integer")
        return value

    def scheduled_for(self, local_date: date) -> datetime:
        """Return the local calendar schedule normalized to UTC."""
        local = datetime.combine(local_date, self.run_at, tzinfo=ZoneInfo(self.timezone))
        return local.astimezone(UTC)

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit JSON primitives for persistence and the local API."""
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "asset_id": self.asset_id,
            "provider": self.provider,
            "domain": self.domain.value,
            "data_frequency": self.data_frequency,
            "timezone": self.timezone,
            "run_at": self.run_at.strftime("%H:%M"),
            "enabled": self.enabled,
            "max_attempts_per_day": self.max_attempts_per_day,
            "retry_backoff_seconds": self.retry_backoff_seconds,
            "freshness_threshold_seconds": self.freshness_threshold_seconds,
        }


class ScheduledJobInvocation(ContractModel):
    """Exact bounded context supplied to one registered job callback."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    definition: ScheduledJobDefinition
    local_date: date
    scheduled_for: UTCDateTime
    started_at: UTCDateTime
    attempt_number: int = Field(ge=1, le=10)

    @field_validator("local_date", mode="before")
    @classmethod
    def require_calendar_date(cls, value: object) -> object:
        """Load ISO dates without accepting datetimes."""
        if isinstance(value, datetime):
            raise ValueError("local_date must be a date")
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as error:
                raise ValueError("local_date must use YYYY-MM-DD") from error
        if not isinstance(value, date):
            raise ValueError("local_date must be a date")
        return value

    @model_validator(mode="after")
    def validate_schedule(self) -> "ScheduledJobInvocation":
        """Keep the invocation date and schedule bound to its definition."""
        if self.scheduled_for != self.definition.scheduled_for(self.local_date):
            raise ValueError("scheduled_for must match the job definition and local_date")
        return self


class ScheduledJobExecution(ContractModel):
    """Compact evidence summary returned by a successful job callback."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["scheduled-job-execution-v1"] = "scheduled-job-execution-v1"
    job_id: NonEmptyStr
    effective_known_at: UTCDateTime
    evidence_changed: bool
    source_ids: tuple[NonEmptyStr, ...]
    created_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    coverage_complete: bool = True
    traceability_verified: Literal[True] = True

    @field_validator("evidence_changed", "coverage_complete", mode="before")
    @classmethod
    def require_changed_boolean(cls, value: object) -> object:
        """Reject truthy values in the evidence-change contract."""
        if not isinstance(value, bool):
            raise ValueError("execution boolean fields must be bool")
        return value

    @field_validator("created_count", "reused_count", mode="before")
    @classmethod
    def reject_boolean_counts(cls, value: object) -> object:
        """Reject booleans accepted as integer counters."""
        if isinstance(value, bool):
            raise ValueError("execution counts must be integers")
        return value

    @model_validator(mode="after")
    def validate_evidence(self) -> "ScheduledJobExecution":
        """Keep source identities deterministic and change semantics explicit."""
        if not self.source_ids:
            raise ValueError("a successful job must identify at least one source")
        if self.source_ids != tuple(sorted(set(self.source_ids))):
            raise ValueError("source_ids must be unique and sorted")
        if self.evidence_changed != (self.created_count > 0):
            raise ValueError("evidence_changed must match whether new records were created")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return safe JSON primitives without provider credentials or raw payloads."""
        return self.model_dump(mode="json")


class ScheduledJobFailure(ContractModel):
    """Safe bounded failure persisted by the scheduler."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    category: ScheduledJobFailureCategory | NonEmptyStr = Field(max_length=120)
    message: NonEmptyStr = Field(max_length=500)
    retryable: bool

    @field_validator("retryable", mode="before")
    @classmethod
    def require_retryable_boolean(cls, value: object) -> object:
        """Reject ambiguous retry decisions."""
        if not isinstance(value, bool):
            raise ValueError("retryable must be a bool")
        return value

    @field_validator("category", mode="after")
    @classmethod
    def preserve_canonical_category(
        cls, value: ScheduledJobFailureCategory | str
    ) -> ScheduledJobFailureCategory | str:
        """Keep known values typed despite ``StrEnum`` also being a string."""
        try:
            return ScheduledJobFailureCategory(value)
        except ValueError:
            return value

    @model_validator(mode="after")
    def require_canonical_policy(self, info: ValidationInfo) -> "ScheduledJobFailure":
        """Reject new free-text categories while retaining read-only v1 history."""
        if isinstance(self.category, ScheduledJobFailureCategory):
            if self.retryable is not _FAILURE_RETRY_POLICY[self.category]:
                raise ValueError("retryable must match the canonical failure category")
            return self
        if not (info.context or {}).get("allow_legacy_failure_categories", False):
            raise ValueError("failure category must be canonical")
        return self

    @property
    def safe_category(self) -> ScheduledJobFailureCategory:
        """Project unknown retained v1 values without silently making them transient."""
        return (
            self.category
            if isinstance(self.category, ScheduledJobFailureCategory)
            else ScheduledJobFailureCategory.LEGACY_UNKNOWN
        )


def scheduled_job_failure(
    category: ScheduledJobFailureCategory,
    message: str,
) -> ScheduledJobFailure:
    """Create a new failure only through the canonical retry policy table."""
    return ScheduledJobFailure(
        category=category,
        message=message,
        retryable=_FAILURE_RETRY_POLICY[category],
    )


class ScheduledJobRunError(RuntimeError):
    """Carry one caller-sanitized operational failure into the scheduler."""

    def __init__(self, failure: ScheduledJobFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


class ScheduledJobAttempt(ContractModel):
    """Persisted lifecycle and evidence for one job attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["scheduled-job-attempt-v1"] = "scheduled-job-attempt-v1"
    attempt_id: UUID
    definition: ScheduledJobDefinition
    local_date: date
    scheduled_for: UTCDateTime
    attempt_number: int = Field(ge=1, le=10)
    status: ScheduledJobAttemptStatus
    started_at: UTCDateTime
    completed_at: UTCDateTime | None = None
    execution: ScheduledJobExecution | None = None
    failure: ScheduledJobFailure | None = None
    telemetry: ProviderJobTelemetry | None = None

    @field_validator("local_date", mode="before")
    @classmethod
    def require_calendar_date(cls, value: object) -> object:
        """Load an ISO date while rejecting a datetime."""
        if isinstance(value, datetime):
            raise ValueError("local_date must be a date")
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as error:
                raise ValueError("local_date must use YYYY-MM-DD") from error
        if not isinstance(value, date):
            raise ValueError("local_date must be a date")
        return value

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "ScheduledJobAttempt":
        """Keep schedule, timing, result, and failure semantics coherent."""
        if self.scheduled_for != self.definition.scheduled_for(self.local_date):
            raise ValueError("scheduled_for must match the job definition and local_date")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at must not predate started_at")
        if self.status is ScheduledJobAttemptStatus.RUNNING:
            if (
                self.completed_at is not None
                or self.execution is not None
                or self.failure is not None
                or self.telemetry is not None
            ):
                raise ValueError("running attempts cannot contain an outcome")
        elif self.status is ScheduledJobAttemptStatus.SUCCEEDED:
            if self.completed_at is None or self.execution is None or self.failure is not None:
                raise ValueError("succeeded attempts require only execution evidence")
            if self.execution.job_id != self.definition.job_id:
                raise ValueError("execution job_id must match the definition")
        else:
            if self.completed_at is None or self.execution is not None or self.failure is None:
                raise ValueError("failed or skipped attempts require only a safe failure")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return complete bounded state for persistence and audit."""
        return {
            "schema_version": self.schema_version,
            "attempt_id": str(self.attempt_id),
            "definition": self.definition.to_json_dict(),
            "local_date": self.local_date.isoformat(),
            "scheduled_for": self.scheduled_for.isoformat(),
            "attempt_number": self.attempt_number,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "execution": self.execution.to_json_dict() if self.execution else None,
            "failure": self.failure.model_dump(mode="json") if self.failure else None,
            "telemetry": self.telemetry.model_dump(mode="json") if self.telemetry else None,
        }


class MultiAssetScheduleState(ContractModel):
    """Versioned history of job attempts persisted outside analytical storage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["multi-asset-schedule-state-v1"] = "multi-asset-schedule-state-v1"
    attempts: tuple[ScheduledJobAttempt, ...] = Field(max_length=_MAX_ATTEMPTS_RETAINED)

    @model_validator(mode="after")
    def validate_attempts(self) -> "MultiAssetScheduleState":
        """Reject duplicate identities and non-deterministic history ordering."""
        ids = tuple(item.attempt_id for item in self.attempts)
        if len(ids) != len(set(ids)):
            raise ValueError("schedule state contains duplicate attempt identities")
        ordering = tuple(
            (item.started_at, item.definition.job_id, item.attempt_number, str(item.attempt_id))
            for item in self.attempts
        )
        if ordering != tuple(sorted(ordering)):
            raise ValueError("schedule attempts must use deterministic chronological ordering")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return the full recoverable scheduler history."""
        return {
            "schema_version": self.schema_version,
            "attempts": [item.to_json_dict() for item in self.attempts],
        }


class MultiAssetScheduleStateStore:
    """Read and atomically update the multi-job scheduling history."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve(strict=False)
        self._lock = threading.RLock()

    def load(self) -> MultiAssetScheduleState:
        """Load valid state without creating a missing file."""
        with self._lock:
            if not self._path.exists():
                return MultiAssetScheduleState(attempts=())
            try:
                return MultiAssetScheduleState.model_validate_json(
                    self._path.read_text(encoding="utf-8"),
                    context={"allow_legacy_failure_categories": True},
                )
            except (OSError, UnicodeError, ValueError) as error:
                raise AaplOperationalStateError(
                    "multi-asset schedule state is malformed or unreadable"
                ) from error

    def write_attempt(self, attempt: ScheduledJobAttempt) -> None:
        """Append an attempt or replace its running lifecycle atomically."""
        with self._lock:
            state = self.load()
            attempts = list(state.attempts)
            matching = tuple(
                index
                for index, item in enumerate(attempts)
                if item.attempt_id == attempt.attempt_id
            )
            if matching:
                existing = attempts[matching[0]]
                if (
                    existing.status is not ScheduledJobAttemptStatus.RUNNING
                    or attempt.status is ScheduledJobAttemptStatus.RUNNING
                    or existing.definition != attempt.definition
                    or existing.started_at != attempt.started_at
                ):
                    if existing == attempt:
                        return
                    raise AaplOperationalStateError(
                        "multi-asset schedule attempt lifecycle is inconsistent"
                    )
                attempts[matching[0]] = attempt
            else:
                attempts.append(attempt)
            attempts.sort(
                key=lambda item: (
                    item.started_at,
                    item.definition.job_id,
                    item.attempt_number,
                    str(item.attempt_id),
                )
            )
            self._write(MultiAssetScheduleState(attempts=tuple(attempts)))

    def _write(self, state: MultiAssetScheduleState) -> None:
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
                "multi-asset schedule state could not be written"
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


ScheduledJobRun = Callable[[ScheduledJobInvocation], ScheduledJobExecution]
ScheduledJobObserver = Callable[[ScheduledJobAttempt], None]


class RegisteredScheduledJob:
    """Bind one immutable definition to one in-process callback."""

    def __init__(self, definition: ScheduledJobDefinition, run: ScheduledJobRun) -> None:
        self.definition = definition
        self.run = run


class ScheduledJobStatus(ContractModel):
    """Current due, retry, freshness, and issue state for one job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    definition: ScheduledJobDefinition
    latest_attempt: ScheduledJobAttempt | None = None
    latest_success: ScheduledJobAttempt | None = None
    health: ScheduledJobHealth
    freshness: ScheduledJobFreshness
    due: bool
    next_run_at: UTCDateTime
    next_retry_at: UTCDateTime | None = None
    retry_budget_remaining: int = Field(ge=0, le=10)
    failure_category: ScheduledJobFailureCategory | None = None
    latest_duration_ms: int | None = Field(default=None, ge=0)
    issues: tuple[NonEmptyStr, ...] = ()

    def to_json_dict(self) -> dict[str, object]:
        """Return compact job state for the local operational panel."""
        return {
            "definition": self.definition.to_json_dict(),
            "latest_attempt": self.latest_attempt.to_json_dict() if self.latest_attempt else None,
            "latest_success": self.latest_success.to_json_dict() if self.latest_success else None,
            "health": self.health.value,
            "freshness": self.freshness.value,
            "due": self.due,
            "next_run_at": self.next_run_at.isoformat(),
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "retry_budget_remaining": self.retry_budget_remaining,
            "failure_category": self.failure_category.value if self.failure_category else None,
            "latest_duration_ms": self.latest_duration_ms,
            "issues": list(self.issues),
        }


class MultiAssetSchedulerStatus(ContractModel):
    """Aggregate deterministic state across all registered jobs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["multi-asset-scheduler-status-v1"] = "multi-asset-scheduler-status-v1"
    enabled: Literal[True] = True
    jobs: tuple[ScheduledJobStatus, ...]
    due_count: int = Field(ge=0)
    running_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    retry_wait_count: int = Field(ge=0)
    current_count: int = Field(ge=0)
    stale_count: int = Field(ge=0)
    incomplete_count: int = Field(ge=0)
    next_run_at: UTCDateTime
    issues: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_counts(self) -> "MultiAssetSchedulerStatus":
        """Keep aggregate counters aligned with deterministic per-job state."""
        if not self.jobs:
            raise ValueError("multi-asset scheduler requires at least one registered job")
        if tuple(item.definition.job_id for item in self.jobs) != tuple(
            sorted(item.definition.job_id for item in self.jobs)
        ):
            raise ValueError("scheduler jobs must be sorted by job_id")
        if self.due_count != sum(item.due for item in self.jobs):
            raise ValueError("due_count must match jobs")
        if self.running_count != sum(
            item.latest_attempt is not None
            and item.latest_attempt.status is ScheduledJobAttemptStatus.RUNNING
            for item in self.jobs
        ):
            raise ValueError("running_count must match jobs")
        if self.failed_count != sum(
            item.latest_attempt is not None
            and item.latest_attempt.status
            in {ScheduledJobAttemptStatus.FAILED, ScheduledJobAttemptStatus.SKIPPED}
            for item in self.jobs
        ):
            raise ValueError("failed_count must match jobs")
        if self.blocked_count != sum(
            item.health is ScheduledJobHealth.BLOCKED for item in self.jobs
        ):
            raise ValueError("blocked_count must match jobs")
        if self.retry_wait_count != sum(
            item.health is ScheduledJobHealth.RETRY_WAIT for item in self.jobs
        ):
            raise ValueError("retry_wait_count must match jobs")
        if self.current_count != sum(
            item.health is ScheduledJobHealth.CURRENT for item in self.jobs
        ):
            raise ValueError("current_count must match jobs")
        if self.stale_count != sum(
            item.freshness is ScheduledJobFreshness.STALE for item in self.jobs
        ):
            raise ValueError("stale_count must match jobs")
        if self.incomplete_count != sum(
            item.freshness is ScheduledJobFreshness.INCOMPLETE for item in self.jobs
        ):
            raise ValueError("incomplete_count must match jobs")
        if self.next_run_at != min(item.next_run_at for item in self.jobs):
            raise ValueError("next_run_at must match the earliest job")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return one compact aggregate plus explicit per-job state."""
        return {
            "schema_version": self.schema_version,
            "enabled": self.enabled,
            "jobs": [item.to_json_dict() for item in self.jobs],
            "due_count": self.due_count,
            "running_count": self.running_count,
            "failed_count": self.failed_count,
            "blocked_count": self.blocked_count,
            "retry_wait_count": self.retry_wait_count,
            "current_count": self.current_count,
            "stale_count": self.stale_count,
            "incomplete_count": self.incomplete_count,
            "next_run_at": self.next_run_at.isoformat(),
            "issues": list(self.issues),
        }


class MultiAssetScheduler:
    """Run every due job sequentially while preserving each completed attempt."""

    def __init__(
        self,
        jobs: tuple[RegisteredScheduledJob, ...],
        store: MultiAssetScheduleStateStore,
        *,
        observer: ScheduledJobObserver | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        attempt_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if not jobs:
            raise ValueError("multi-asset scheduler requires at least one job")
        job_ids = tuple(item.definition.job_id for item in jobs)
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("registered scheduled job IDs must be unique")
        self._jobs = tuple(sorted(jobs, key=lambda item: item.definition.job_id))
        self._registry_lock = threading.RLock()
        self._store = store
        self._observer = observer
        self._clock = clock
        self._attempt_id_factory = attempt_id_factory
        self._tick_lock = threading.Lock()
        self._active_job_id: str | None = None
        self._observer_issue: str | None = None
        self._pending_notifications: dict[UUID, ScheduledJobAttempt] = {}

    def status(self) -> MultiAssetSchedulerStatus:
        """Inspect every job without fetching providers or writing state."""
        jobs = self._jobs_snapshot()
        return self._status_at(self._now(), self._store.load(), jobs)

    def registered_job_definitions(self) -> tuple[ScheduledJobDefinition, ...]:
        """Return one immutable registry snapshot without provider or state work."""
        return tuple(item.definition for item in self._jobs_snapshot())

    def reconcile_jobs(self, jobs: tuple[RegisteredScheduledJob, ...]) -> None:
        """Atomically publish a provider-free replacement registry."""
        if not jobs:
            raise ValueError("multi-asset scheduler requires at least one job")
        job_ids = tuple(item.definition.job_id for item in jobs)
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("registered scheduled job IDs must be unique")
        replacement = tuple(sorted(jobs, key=lambda item: item.definition.job_id))
        with self._registry_lock:
            self._jobs = replacement

    def tick(self) -> tuple[ScheduledJobAttempt, ...]:
        """Recover interrupted work and execute all currently due jobs once."""
        if not self._tick_lock.acquire(blocking=False):
            return ()
        completed: list[ScheduledJobAttempt] = []
        try:
            self._retry_notifications()
            now = self._now()
            jobs = self._jobs_snapshot()
            state = self._recover_interrupted(now, self._store.load(), completed)
            status = self._status_at(now, state, jobs)
            due_ids = tuple(item.definition.job_id for item in status.jobs if item.due)
            registry = {item.definition.job_id: item for item in jobs}
            for job_id in due_ids:
                job = registry[job_id]
                current = self._now()
                refreshed = self._job_status(job.definition, current, self._store.load())
                if not refreshed.due:
                    continue
                if not self._claim_registered_job(job_id):
                    continue
                try:
                    attempt = self._run_job(job, current)
                    completed.append(attempt)
                finally:
                    self._release_active_job(job_id)
            return tuple(completed)
        finally:
            self._tick_lock.release()

    def run_forever(
        self,
        stop_event: threading.Event,
        *,
        poll_seconds: float = 30.0,
        error_handler: Callable[[str], None] | None = None,
    ) -> None:
        """Poll until stopped and retain the thread after safe scheduler errors."""
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                if error_handler is not None:
                    error_handler("multi-asset scheduler could not evaluate persisted state")
            stop_event.wait(poll_seconds)

    def _run_job(self, job: RegisteredScheduledJob, now: datetime) -> ScheduledJobAttempt:
        definition = job.definition
        local_date = now.astimezone(ZoneInfo(definition.timezone)).date()
        attempts_today = self._attempts_for(
            self._store.load(),
            definition.job_id,
            local_date,
        )
        attempt_number = len(attempts_today) + 1
        attempt_id = self._attempt_id_factory()
        running = ScheduledJobAttempt(
            attempt_id=attempt_id,
            definition=definition,
            local_date=local_date,
            scheduled_for=definition.scheduled_for(local_date),
            attempt_number=attempt_number,
            status=ScheduledJobAttemptStatus.RUNNING,
            started_at=now,
        )
        self._store.write_attempt(running)
        try:
            invocation = ScheduledJobInvocation(
                definition=definition,
                local_date=local_date,
                scheduled_for=running.scheduled_for,
                started_at=now,
                attempt_number=attempt_number,
            )
            execution = job.run(invocation)
            if execution.job_id != definition.job_id:
                raise ValueError("scheduled execution job_id does not match its definition")
        except ScheduledJobRunError as error:
            completed = ScheduledJobAttempt(
                **running.model_dump(
                    exclude={"status", "completed_at", "execution", "failure", "telemetry"}
                ),
                status=ScheduledJobAttemptStatus.FAILED,
                completed_at=self._now(),
                failure=error.failure,
            )
        except AaplOperationalStateError:
            completed = ScheduledJobAttempt(
                **running.model_dump(
                    exclude={"status", "completed_at", "execution", "failure", "telemetry"}
                ),
                status=ScheduledJobAttemptStatus.FAILED,
                completed_at=self._now(),
                failure=scheduled_job_failure(
                    ScheduledJobFailureCategory.STORAGE_STATE,
                    "scheduled operational state is incompatible or unavailable",
                ),
            )
        except ValueError:
            completed = ScheduledJobAttempt(
                **running.model_dump(
                    exclude={"status", "completed_at", "execution", "failure", "telemetry"}
                ),
                status=ScheduledJobAttemptStatus.FAILED,
                completed_at=self._now(),
                failure=scheduled_job_failure(
                    ScheduledJobFailureCategory.VALIDATION,
                    "scheduled job output failed validation",
                ),
            )
        except Exception:  # noqa: BLE001
            completed = ScheduledJobAttempt(
                **running.model_dump(
                    exclude={"status", "completed_at", "execution", "failure", "telemetry"}
                ),
                status=ScheduledJobAttemptStatus.FAILED,
                completed_at=self._now(),
                failure=scheduled_job_failure(
                    ScheduledJobFailureCategory.UNEXPECTED,
                    "the scheduled job failed unexpectedly",
                ),
            )
        else:
            completed = ScheduledJobAttempt(
                **running.model_dump(
                    exclude={"status", "completed_at", "execution", "failure", "telemetry"}
                ),
                status=ScheduledJobAttemptStatus.SUCCEEDED,
                completed_at=self._now(),
                execution=execution,
            )
        completed = completed.model_copy(update={"telemetry": _attempt_telemetry(completed)})
        self._store.write_attempt(completed)
        self._notify(completed)
        return completed

    def _recover_interrupted(
        self,
        now: datetime,
        state: MultiAssetScheduleState,
        completed: list[ScheduledJobAttempt],
    ) -> MultiAssetScheduleState:
        latest_by_job = self._latest_by_job(state)
        for job_id, attempt in sorted(latest_by_job.items()):
            if (
                attempt.status is not ScheduledJobAttemptStatus.RUNNING
                or job_id == self._active_job_id
            ):
                continue
            recovered = ScheduledJobAttempt(
                **attempt.model_dump(
                    exclude={"status", "completed_at", "execution", "failure", "telemetry"}
                ),
                status=ScheduledJobAttemptStatus.FAILED,
                completed_at=now,
                failure=scheduled_job_failure(
                    ScheduledJobFailureCategory.INTERRUPTED,
                    "the prior scheduled job was interrupted before completion",
                ),
            )
            recovered = recovered.model_copy(update={"telemetry": _attempt_telemetry(recovered)})
            self._store.write_attempt(recovered)
            self._notify(recovered)
            completed.append(recovered)
        return self._store.load()

    def _status_at(
        self,
        now: datetime,
        state: MultiAssetScheduleState,
        jobs: tuple[RegisteredScheduledJob, ...],
    ) -> MultiAssetSchedulerStatus:
        statuses = tuple(self._job_status(item.definition, now, state) for item in jobs)
        issues = tuple(issue for item in statuses for issue in item.issues)
        if self._observer_issue is not None:
            issues = (*issues, self._observer_issue)
        return MultiAssetSchedulerStatus(
            jobs=statuses,
            due_count=sum(item.due for item in statuses),
            running_count=sum(
                item.latest_attempt is not None
                and item.latest_attempt.status is ScheduledJobAttemptStatus.RUNNING
                for item in statuses
            ),
            failed_count=sum(
                item.latest_attempt is not None
                and item.latest_attempt.status
                in {ScheduledJobAttemptStatus.FAILED, ScheduledJobAttemptStatus.SKIPPED}
                for item in statuses
            ),
            blocked_count=sum(item.health is ScheduledJobHealth.BLOCKED for item in statuses),
            retry_wait_count=sum(item.health is ScheduledJobHealth.RETRY_WAIT for item in statuses),
            current_count=sum(item.health is ScheduledJobHealth.CURRENT for item in statuses),
            stale_count=sum(item.freshness is ScheduledJobFreshness.STALE for item in statuses),
            incomplete_count=sum(
                item.freshness is ScheduledJobFreshness.INCOMPLETE for item in statuses
            ),
            next_run_at=min(item.next_run_at for item in statuses),
            issues=issues,
        )

    def _jobs_snapshot(self) -> tuple[RegisteredScheduledJob, ...]:
        with self._registry_lock:
            return self._jobs

    def _claim_registered_job(self, job_id: str) -> bool:
        """Make a still-registered due job active atomically with reconciliation."""
        with self._registry_lock:
            if all(item.definition.job_id != job_id for item in self._jobs):
                return False
            self._active_job_id = job_id
            return True

    def _release_active_job(self, job_id: str) -> None:
        with self._registry_lock:
            if self._active_job_id == job_id:
                self._active_job_id = None

    def _job_status(
        self,
        definition: ScheduledJobDefinition,
        now: datetime,
        state: MultiAssetScheduleState,
    ) -> ScheduledJobStatus:
        local_now = now.astimezone(ZoneInfo(definition.timezone))
        local_date = local_now.date()
        scheduled = definition.scheduled_for(local_date)
        attempts_today = self._attempts_for(state, definition.job_id, local_date)
        latest = self._latest_by_job(state).get(definition.job_id)
        latest_success = self._latest_success_by_job(state).get(definition.job_id)
        due = False
        next_run = scheduled
        next_retry: datetime | None = None
        issues: list[str] = []
        health: ScheduledJobHealth
        if not definition.enabled:
            next_run = definition.scheduled_for(local_date + timedelta(days=1))
            health = ScheduledJobHealth.DISABLED
        elif now < scheduled:
            next_run = scheduled
            health = ScheduledJobHealth.NEVER_RUN
        elif not attempts_today:
            due = True
            next_run = now
            health = ScheduledJobHealth.NEVER_RUN
        else:
            today_latest = attempts_today[-1]
            if today_latest.status is ScheduledJobAttemptStatus.RUNNING:
                next_run = definition.scheduled_for(local_date + timedelta(days=1))
                health = ScheduledJobHealth.RUNNING
                if definition.job_id != self._active_job_id:
                    issues.append(f"{definition.job_id}: interrupted scheduled job")
            elif today_latest.status is ScheduledJobAttemptStatus.SUCCEEDED:
                next_run = definition.scheduled_for(local_date + timedelta(days=1))
                health = ScheduledJobHealth.CURRENT
            elif today_latest.failure is not None and not today_latest.failure.retryable:
                next_run = definition.scheduled_for(local_date + timedelta(days=1))
                health = ScheduledJobHealth.BLOCKED
                issues.append(f"{definition.job_id}: latest scheduled job failed")
            elif (
                today_latest.failure is not None
                and today_latest.failure.retryable
                and len(attempts_today) < definition.max_attempts_per_day
            ):
                retry_at = today_latest.completed_at + timedelta(
                    seconds=self._retry_delay_seconds(definition, today_latest.attempt_number)
                )
                next_retry = retry_at
                due = now >= retry_at
                next_run = now if due else retry_at
                health = ScheduledJobHealth.RETRY_WAIT
                issues.append(f"{definition.job_id}: latest scheduled job failed")
            else:
                next_run = definition.scheduled_for(local_date + timedelta(days=1))
                health = ScheduledJobHealth.BLOCKED
                issues.append(f"{definition.job_id}: daily retry budget exhausted")
        if (
            latest is not None
            and latest.local_date < local_date
            and latest.status
            in {ScheduledJobAttemptStatus.FAILED, ScheduledJobAttemptStatus.SKIPPED}
        ):
            issues.append(f"{definition.job_id}: prior scheduled job failed")
        freshness = self._freshness(definition, latest_success, now)
        if freshness is ScheduledJobFreshness.STALE:
            issues.append(f"{definition.job_id}: provider check is stale")
        elif freshness is ScheduledJobFreshness.INCOMPLETE:
            issues.append(f"{definition.job_id}: latest coverage is incomplete")
        if health in {ScheduledJobHealth.CURRENT, ScheduledJobHealth.NEVER_RUN}:
            if freshness is ScheduledJobFreshness.INCOMPLETE:
                health = ScheduledJobHealth.INCOMPLETE
            elif freshness is ScheduledJobFreshness.STALE:
                health = ScheduledJobHealth.STALE
        return ScheduledJobStatus(
            definition=definition,
            latest_attempt=latest,
            latest_success=latest_success,
            health=health,
            freshness=freshness,
            due=due,
            next_run_at=next_run,
            next_retry_at=next_retry,
            retry_budget_remaining=max(definition.max_attempts_per_day - len(attempts_today), 0),
            failure_category=latest.failure.safe_category if latest and latest.failure else None,
            latest_duration_ms=(
                latest.telemetry.duration_ms if latest and latest.telemetry else None
            ),
            issues=tuple(dict.fromkeys(issues)),
        )

    @staticmethod
    def _retry_delay_seconds(definition: ScheduledJobDefinition, attempt_number: int) -> int:
        """Return the bounded exponential delay after a retryable attempt."""
        return definition.retry_backoff_seconds * (2 ** (attempt_number - 1))

    def _notify(self, attempt: ScheduledJobAttempt) -> None:
        if self._observer is None:
            return
        try:
            self._observer(attempt)
        except Exception:  # noqa: BLE001
            self._pending_notifications[attempt.attempt_id] = attempt
            self._observer_issue = "scheduled job observer could not persist its result"
        else:
            self._pending_notifications.pop(attempt.attempt_id, None)
            if not self._pending_notifications:
                self._observer_issue = None

    def _retry_notifications(self) -> None:
        """Retry transient observer failures without rerunning provider jobs."""
        for attempt in tuple(self._pending_notifications.values()):
            self._notify(attempt)

    @staticmethod
    def _attempts_for(
        state: MultiAssetScheduleState,
        job_id: str,
        local_date: date,
    ) -> tuple[ScheduledJobAttempt, ...]:
        return tuple(
            item
            for item in state.attempts
            if item.definition.job_id == job_id and item.local_date == local_date
        )

    @staticmethod
    def _latest_by_job(
        state: MultiAssetScheduleState,
    ) -> dict[str, ScheduledJobAttempt]:
        latest: dict[str, ScheduledJobAttempt] = {}
        for attempt in state.attempts:
            latest[attempt.definition.job_id] = attempt
        return latest

    @staticmethod
    def _latest_success_by_job(
        state: MultiAssetScheduleState,
    ) -> dict[str, ScheduledJobAttempt]:
        latest: dict[str, ScheduledJobAttempt] = {}
        for attempt in state.attempts:
            if attempt.status is ScheduledJobAttemptStatus.SUCCEEDED:
                latest[attempt.definition.job_id] = attempt
        return latest

    @staticmethod
    def _freshness(
        definition: ScheduledJobDefinition,
        latest_success: ScheduledJobAttempt | None,
        now: datetime,
    ) -> ScheduledJobFreshness:
        if latest_success is None or latest_success.execution is None:
            return ScheduledJobFreshness.NEVER_RUN
        if not latest_success.execution.coverage_complete:
            return ScheduledJobFreshness.INCOMPLETE
        checked_at = latest_success.completed_at
        if checked_at is None:
            return ScheduledJobFreshness.NEVER_RUN
        if now - checked_at > timedelta(seconds=definition.freshness_threshold_seconds):
            return ScheduledJobFreshness.STALE
        return ScheduledJobFreshness.CURRENT

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduler clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _attempt_telemetry(attempt: ScheduledJobAttempt) -> ProviderJobTelemetry:
    """Build telemetry from the already-captured lifecycle without another clock read."""
    if attempt.completed_at is None:
        raise ValueError("completed attempt telemetry requires completed_at")
    execution = attempt.execution
    failure = attempt.failure
    return ProviderJobTelemetry(
        job_id=attempt.definition.job_id,
        provider=attempt.definition.provider,
        domain=attempt.definition.domain,
        started_at=attempt.started_at,
        completed_at=attempt.completed_at,
        duration_ms=int((attempt.completed_at - attempt.started_at).total_seconds() * 1000),
        created_count=execution.created_count if execution else 0,
        reused_count=execution.reused_count if execution else 0,
        coverage_complete=execution.coverage_complete if execution else None,
        failure_category=failure.category if failure else None,
    )


__all__ = [
    "MultiAssetScheduleState",
    "MultiAssetScheduleStateStore",
    "MultiAssetScheduler",
    "MultiAssetSchedulerStatus",
    "ProviderJobTelemetry",
    "RegisteredScheduledJob",
    "ScheduledJobAttempt",
    "ScheduledJobAttemptStatus",
    "ScheduledJobDefinition",
    "ScheduledJobDomain",
    "ScheduledJobExecution",
    "ScheduledJobFailure",
    "ScheduledJobFailureCategory",
    "ScheduledJobFreshness",
    "ScheduledJobHealth",
    "ScheduledJobInvocation",
    "ScheduledJobRunError",
    "ScheduledJobStatus",
    "scheduled_job_failure",
]
