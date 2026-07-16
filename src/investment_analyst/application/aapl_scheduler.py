"""Deterministic local scheduling for the one-shot Apple operational runner."""

import fcntl
import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from investment_analyst.application.aapl_bootstrap_models import (
    AaplRefreshMode,
    AaplWorkspaceBootstrapRequest,
)
from investment_analyst.application.aapl_daily_runner import AaplDailyRunExecutionError
from investment_analyst.application.operational_models import (
    AaplDailyRunFailure,
    AaplDailyRunState,
)
from investment_analyst.application.operational_state import (
    AaplDailyRunAlreadyRunningError,
    AaplOperationalStateError,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class AaplLocalServiceAlreadyRunningError(AaplOperationalStateError):
    """Raised when another local UI and scheduler process owns the workspace."""


class AaplLocalServiceLock:
    """Lifetime advisory lock preventing duplicate schedulers for one workspace."""

    def __init__(self, path: Path, *, service_id: UUID, started_at: str) -> None:
        self._path = path
        self._service_id = service_id
        self._started_at = started_at
        self._descriptor: int | None = None

    def __enter__(self) -> "AaplLocalServiceLock":
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as error:
            raise AaplOperationalStateError("local service lock could not be opened") from error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise AaplLocalServiceAlreadyRunningError(
                "another local interface already owns this workspace"
            ) from error
        except OSError as error:
            os.close(descriptor)
            raise AaplOperationalStateError("local service lock could not be acquired") from error
        try:
            metadata = json.dumps(
                {
                    "pid": os.getpid(),
                    "service_id": str(self._service_id),
                    "started_at": self._started_at,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, metadata)
            os.fsync(descriptor)
        except OSError as error:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise AaplOperationalStateError(
                "local service lock metadata could not be written"
            ) from error
        self._descriptor = descriptor
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


class AaplScheduleAttemptStatus(StrEnum):
    """Durable outcome of one local calendar-date scheduling decision."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class AaplDailyScheduleConfig(ContractModel):
    """Immutable schedule supplied explicitly when the local service starts."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["aapl-daily-schedule-config-v1"] = "aapl-daily-schedule-config-v1"
    timezone: NonEmptyStr = "America/Lima"
    run_at: time = time(hour=7)
    market_start: date
    market_end_lag_days: int = Field(default=1, ge=0, le=30)
    fundamental_frequency: DataFrequency = DataFrequency.QUARTERLY
    refresh_mode: AaplRefreshMode = AaplRefreshMode.AUTO
    require_complete: bool = True

    @field_validator("market_start", mode="before")
    @classmethod
    def require_calendar_date(cls, value: object) -> object:
        """Accept an ISO date while rejecting datetimes and ambiguous values."""
        if isinstance(value, datetime):
            raise ValueError("market_start must be a date")
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as error:
                raise ValueError("market_start must use YYYY-MM-DD") from error
        if not isinstance(value, date):
            raise ValueError("market_start must be a date")
        return value

    @field_validator("run_at", mode="before")
    @classmethod
    def require_local_minute(cls, value: object) -> object:
        """Accept HH:MM and reject timezone-aware or sub-minute schedule values."""
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

    @field_validator("market_end_lag_days", mode="before")
    @classmethod
    def reject_boolean_lag(cls, value: object) -> object:
        """Reject booleans accepted by Python's integer hierarchy."""
        if isinstance(value, bool):
            raise ValueError("market_end_lag_days must be an integer")
        return value

    @field_validator("require_complete", mode="before")
    @classmethod
    def require_boolean(cls, value: object) -> object:
        """Reject truthy strings and integers in operational configuration."""
        if not isinstance(value, bool):
            raise ValueError("require_complete must be a bool")
        return value

    @field_validator("timezone")
    @classmethod
    def require_known_timezone(cls, value: str) -> str:
        """Require an installed IANA timezone without consulting the network."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("timezone must identify an installed IANA timezone") from error
        return value

    @model_validator(mode="after")
    def validate_supported_scope(self) -> "AaplDailyScheduleConfig":
        """Keep the automated scope inside the supported Apple MVP contracts."""
        if self.fundamental_frequency not in {
            DataFrequency.ANNUAL,
            DataFrequency.QUARTERLY,
        }:
            raise ValueError("fundamental_frequency must be annual or quarterly")
        return self

    def scheduled_for(self, local_date: date) -> datetime:
        """Return the configured local calendar instant normalized to UTC."""
        local = datetime.combine(local_date, self.run_at, tzinfo=ZoneInfo(self.timezone))
        return local.astimezone(UTC)

    def request_for(self, local_date: date) -> AaplWorkspaceBootstrapRequest:
        """Build one typed inclusive request from an explicit local calendar date."""
        market_end = local_date - timedelta(days=self.market_end_lag_days)
        if market_end < self.market_start:
            raise ValueError("scheduled market end would be earlier than market start")
        return AaplWorkspaceBootstrapRequest(
            market_start=self.market_start,
            market_end=market_end,
            fundamental_frequency=self.fundamental_frequency,
            refresh_mode=self.refresh_mode,
            require_complete=self.require_complete,
        )

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit JSON primitives for the local interface."""
        return {
            "schema_version": self.schema_version,
            "timezone": self.timezone,
            "run_at": self.run_at.strftime("%H:%M"),
            "market_start": self.market_start.isoformat(),
            "market_end_lag_days": self.market_end_lag_days,
            "fundamental_frequency": self.fundamental_frequency.value,
            "refresh_mode": self.refresh_mode.value,
            "require_complete": self.require_complete,
        }


class AaplDailyScheduleAttempt(ContractModel):
    """Atomic latest scheduling attempt, independent from analytical history."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["aapl-daily-schedule-attempt-v1"] = "aapl-daily-schedule-attempt-v1"
    attempt_id: UUID
    local_date: date
    scheduled_for: UTCDateTime
    status: AaplScheduleAttemptStatus
    started_at: UTCDateTime
    completed_at: UTCDateTime | None = None
    run_id: UUID | None = None
    failure: AaplDailyRunFailure | None = None

    @field_validator("local_date", mode="before")
    @classmethod
    def require_calendar_date(cls, value: object, info: ValidationInfo) -> object:
        """Load the persisted ISO date without accepting a datetime."""
        if isinstance(value, datetime):
            raise ValueError(f"{info.field_name} must be a date")
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as error:
                raise ValueError(f"{info.field_name} must use YYYY-MM-DD") from error
        if not isinstance(value, date):
            raise ValueError(f"{info.field_name} must be a date")
        return value

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "AaplDailyScheduleAttempt":
        """Keep schedule status, timing, run identity, and safe failure coherent."""
        if self.completed_at is not None and self.started_at > self.completed_at:
            raise ValueError("started_at must not be later than completed_at")
        if self.status is AaplScheduleAttemptStatus.RUNNING:
            if self.completed_at is not None or self.run_id is not None or self.failure is not None:
                raise ValueError("running schedule attempt cannot contain an outcome")
        elif self.status is AaplScheduleAttemptStatus.SUCCEEDED:
            if self.completed_at is None or self.run_id is None or self.failure is not None:
                raise ValueError("succeeded schedule attempt requires only a run outcome")
        else:
            if self.completed_at is None or self.failure is None or self.run_id is not None:
                raise ValueError("failed or skipped schedule attempt requires a safe failure")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return bounded JSON-safe attempt state."""
        return {
            "schema_version": self.schema_version,
            "attempt_id": str(self.attempt_id),
            "local_date": self.local_date.isoformat(),
            "scheduled_for": self.scheduled_for.isoformat(),
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "run_id": str(self.run_id) if self.run_id else None,
            "failure": self.failure.to_json_dict() if self.failure else None,
        }


class AaplDailySchedulerStatus(ContractModel):
    """Current deterministic schedule status exposed by the local interface."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: Literal[True] = True
    config: AaplDailyScheduleConfig
    latest_attempt: AaplDailyScheduleAttempt | None = None
    due: bool
    next_run_at: UTCDateTime
    issues: tuple[NonEmptyStr, ...] = ()

    def to_json_dict(self) -> dict[str, object]:
        """Return one compact scheduler status."""
        return {
            "enabled": self.enabled,
            "config": self.config.to_json_dict(),
            "latest_attempt": (self.latest_attempt.to_json_dict() if self.latest_attempt else None),
            "due": self.due,
            "next_run_at": self.next_run_at.isoformat(),
            "issues": list(self.issues),
        }


class AaplDailyScheduleStateStore:
    """Read and atomically replace the latest scheduling attempt."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve(strict=False)

    def load(self) -> AaplDailyScheduleAttempt | None:
        """Load valid state without creating a missing state file."""
        if not self._path.exists():
            return None
        try:
            return AaplDailyScheduleAttempt.model_validate_json(
                self._path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise AaplOperationalStateError(
                "daily schedule state is malformed or unreadable"
            ) from error

    def write(self, attempt: AaplDailyScheduleAttempt) -> None:
        """Durably replace state with a private same-directory temporary file."""
        document = (
            json.dumps(
                attempt.to_json_dict(),
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
            raise AaplOperationalStateError("daily schedule state could not be written") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


ScheduledRun = Callable[[AaplWorkspaceBootstrapRequest], AaplDailyRunState]


class AaplDailyScheduler:
    """Evaluate and execute at most one scheduled attempt per local calendar date."""

    def __init__(
        self,
        config: AaplDailyScheduleConfig,
        store: AaplDailyScheduleStateStore,
        run: ScheduledRun,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        attempt_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._config = config
        self._store = store
        self._run = run
        self._clock = clock
        self._attempt_id_factory = attempt_id_factory
        self._tick_lock = threading.Lock()
        self._active = False

    def status(self) -> AaplDailySchedulerStatus:
        """Inspect due state and the next run without writing files."""
        return self._status_at(self._now())

    def tick(self) -> AaplDailyScheduleAttempt | None:
        """Execute one due attempt, returning immediately when not due or already ticking."""
        if not self._tick_lock.acquire(blocking=False):
            return None
        try:
            now = self._now()
            status = self._status_at(now)
            if not status.due:
                return None
            local_date = now.astimezone(ZoneInfo(self._config.timezone)).date()
            attempt_id = self._attempt_id_factory()
            running = AaplDailyScheduleAttempt(
                attempt_id=attempt_id,
                local_date=local_date,
                scheduled_for=self._config.scheduled_for(local_date),
                status=AaplScheduleAttemptStatus.RUNNING,
                started_at=now,
            )
            self._store.write(running)
            self._active = True
            try:
                result = self._run(self._config.request_for(local_date))
            except AaplDailyRunAlreadyRunningError as error:
                completed = AaplDailyScheduleAttempt(
                    attempt_id=attempt_id,
                    local_date=local_date,
                    scheduled_for=running.scheduled_for,
                    status=AaplScheduleAttemptStatus.SKIPPED,
                    started_at=now,
                    completed_at=self._now(),
                    failure=AaplDailyRunFailure(
                        category=type(error).__name__,
                        message="another operational run was already active",
                    ),
                )
            except AaplDailyRunExecutionError as error:
                completed = AaplDailyScheduleAttempt(
                    attempt_id=attempt_id,
                    local_date=local_date,
                    scheduled_for=running.scheduled_for,
                    status=AaplScheduleAttemptStatus.FAILED,
                    started_at=now,
                    completed_at=self._now(),
                    failure=error.failure,
                )
            except (AaplOperationalStateError, ValueError) as error:
                message = str(error).strip()[:500] or "scheduled run failed validation"
                completed = AaplDailyScheduleAttempt(
                    attempt_id=attempt_id,
                    local_date=local_date,
                    scheduled_for=running.scheduled_for,
                    status=AaplScheduleAttemptStatus.FAILED,
                    started_at=now,
                    completed_at=self._now(),
                    failure=AaplDailyRunFailure(
                        category=type(error).__name__,
                        message=message,
                    ),
                )
            except Exception:  # noqa: BLE001
                completed = AaplDailyScheduleAttempt(
                    attempt_id=attempt_id,
                    local_date=local_date,
                    scheduled_for=running.scheduled_for,
                    status=AaplScheduleAttemptStatus.FAILED,
                    started_at=now,
                    completed_at=self._now(),
                    failure=AaplDailyRunFailure(
                        category="unexpected_error",
                        message="the scheduled operational run failed unexpectedly",
                    ),
                )
            else:
                completed = AaplDailyScheduleAttempt(
                    attempt_id=attempt_id,
                    local_date=local_date,
                    scheduled_for=running.scheduled_for,
                    status=AaplScheduleAttemptStatus.SUCCEEDED,
                    started_at=now,
                    completed_at=self._now(),
                    run_id=result.run_id,
                )
            finally:
                self._active = False
            self._store.write(completed)
            return completed
        finally:
            self._tick_lock.release()

    def run_forever(
        self,
        stop_event: threading.Event,
        *,
        poll_seconds: float = 30.0,
        error_handler: Callable[[str], None] | None = None,
    ) -> None:
        """Poll until stopped, retaining the thread after a safe scheduler error."""
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                if error_handler is not None:
                    error_handler("daily scheduler could not evaluate its persisted state")
            stop_event.wait(poll_seconds)

    def _status_at(self, now: datetime) -> AaplDailySchedulerStatus:
        local_now = now.astimezone(ZoneInfo(self._config.timezone))
        local_date = local_now.date()
        scheduled = self._config.scheduled_for(local_date)
        latest = self._store.load()
        attempted_today = latest is not None and latest.local_date == local_date
        due = now >= scheduled and not attempted_today
        next_run = scheduled
        if attempted_today:
            next_run = self._config.scheduled_for(local_date + timedelta(days=1))
        issues: list[str] = []
        if (
            latest is not None
            and latest.status is AaplScheduleAttemptStatus.RUNNING
            and not self._active
        ):
            issues.append("the latest scheduled attempt was interrupted")
        if latest is not None and latest.status is AaplScheduleAttemptStatus.FAILED:
            issues.append("the latest scheduled attempt failed")
        return AaplDailySchedulerStatus(
            config=self._config,
            latest_attempt=latest,
            due=due,
            next_run_at=next_run,
            issues=tuple(issues),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduler clock must return a timezone-aware datetime")
        return value.astimezone(UTC)
