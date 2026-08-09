"""Durable, deduplicated manual operations over the existing single writer."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from investment_analyst.application.btc_intraday_models import BtcIntradayRefreshRequest
from investment_analyst.application.btc_refresh_models import BtcMarketRefreshRequest
from investment_analyst.application.crypto_spot_daily_models import CryptoSpotDailyRefreshRequest
from investment_analyst.application.listed_market_refresh_models import ListedMarketRefreshRequest
from investment_analyst.application.operational_models import AaplDailyRunRequestSnapshot
from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.application.sec_fundamental_refresh_models import (
    SecIssuerFundamentalRefreshRequest,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_MAX_OPERATIONS_RETAINED = 10_000
_MAX_REQUEST_BYTES = 16_384
_SECRET_KEY_MARKERS = ("api_key", "authorization", "credential", "password", "secret", "token")


class ManualOperationKind(StrEnum):
    """Existing synchronous operations available through the durable queue."""

    COMPLETE_REFRESH = "complete_refresh"
    MARKET_DAILY = "market_daily"
    MARKET_INTRADAY = "market_intraday"
    FUNDAMENTALS = "fundamentals"


class ManualOperationStatus(StrEnum):
    """Persisted lifecycle of one requested operation."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ManualOperationRequest(ContractModel):
    """Versioned request containing one already-public operation payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["manual-operation-request-v1"] = "manual-operation-request-v1"
    operation_kind: ManualOperationKind
    payload: dict[NonEmptyStr, JsonValue]

    @field_validator("payload")
    @classmethod
    def require_safe_bounded_payload(
        cls,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """Reject credentials and unbounded state before it reaches disk."""
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > _MAX_REQUEST_BYTES:
            raise ValueError("manual operation payload is too large")
        for key in _iter_keys(value):
            folded = key.casefold()
            if any(marker in folded for marker in _SECRET_KEY_MARKERS):
                raise ValueError("manual operation payload must not contain credentials")
        return value

    @model_validator(mode="after")
    def validate_payload_for_operation(self) -> ManualOperationRequest:
        """Reject payloads that do not match the selected public operation contract."""
        if self.operation_kind is ManualOperationKind.COMPLETE_REFRESH:
            AaplDailyRunRequestSnapshot.model_validate(self.payload)
        elif self.operation_kind is ManualOperationKind.MARKET_DAILY:
            if self.payload.get("asset_id") == "crypto:btc-usd":
                BtcMarketRefreshRequest.model_validate(self.payload)
            elif self.payload.get("asset_id") == "crypto:eth-usd":
                CryptoSpotDailyRefreshRequest.model_validate(self.payload)
            else:
                ListedMarketRefreshRequest.model_validate(self.payload)
        elif self.operation_kind is ManualOperationKind.MARKET_INTRADAY:
            BtcIntradayRefreshRequest.model_validate(self.payload)
        else:
            SecIssuerFundamentalRefreshRequest.model_validate(self.payload)
        return self

    @property
    def fingerprint(self) -> str:
        """Return a deterministic identity for active-operation deduplication."""
        document = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(document).hexdigest()


class ManualOperationResult(ContractModel):
    """Compact evidence returned by a completed manual operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["manual-operation-result-v1"] = "manual-operation-result-v1"
    result_schema_version: NonEmptyStr | None = None
    effective_known_at: UTCDateTime | None = None
    created_count: int | None = Field(default=None, ge=0)
    reused_count: int | None = Field(default=None, ge=0)
    coverage_complete: bool | None = None
    traceability_verified: bool | None = None

    @field_validator("created_count", "reused_count", mode="before")
    @classmethod
    def reject_boolean_counts(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool):
            raise ValueError("manual operation counts must be integers")
        return value

    @field_validator("coverage_complete", "traceability_verified", mode="before")
    @classmethod
    def require_boolean_evidence(cls, value: object) -> object:
        if value is not None and not isinstance(value, bool):
            raise ValueError("manual operation evidence flags must be bool or null")
        return value


class ManualOperationFailure(ContractModel):
    """Bounded failure state that never includes provider exception text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: NonEmptyStr = Field(max_length=120)
    message: NonEmptyStr = Field(max_length=300)
    retryable: bool = False

    @field_validator("retryable", mode="before")
    @classmethod
    def require_retryable_boolean(cls, value: object) -> object:
        if not isinstance(value, bool):
            raise ValueError("retryable must be bool")
        return value


class ManualOperationState(ContractModel):
    """Recoverable state for one queued, running, or completed operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["manual-operation-state-v1"] = "manual-operation-state-v1"
    operation_id: UUID
    fingerprint: NonEmptyStr
    request: ManualOperationRequest
    status: ManualOperationStatus
    submitted_at: UTCDateTime
    started_at: UTCDateTime | None = None
    completed_at: UTCDateTime | None = None
    recovery_count: int = Field(default=0, ge=0)
    result: ManualOperationResult | None = None
    failure: ManualOperationFailure | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> ManualOperationState:
        """Keep timing, request fingerprint, and outcome state coherent."""
        if self.fingerprint != self.request.fingerprint:
            raise ValueError("manual operation fingerprint does not match request")
        if self.started_at is not None and self.started_at < self.submitted_at:
            raise ValueError("manual operation started_at predates submission")
        if self.completed_at is not None and (
            self.started_at is None or self.completed_at < self.started_at
        ):
            raise ValueError("manual operation completion timing is invalid")
        if self.status is ManualOperationStatus.QUEUED:
            if any((self.started_at, self.completed_at, self.result, self.failure)):
                raise ValueError("queued operation cannot contain execution state")
        elif self.status is ManualOperationStatus.RUNNING:
            if self.started_at is None or any((self.completed_at, self.result, self.failure)):
                raise ValueError("running operation requires only started_at")
        elif self.status is ManualOperationStatus.SUCCEEDED:
            if self.completed_at is None or self.result is None or self.failure is not None:
                raise ValueError("succeeded operation requires only result evidence")
        elif self.completed_at is None or self.failure is None or self.result is not None:
            raise ValueError("failed operation requires only bounded failure evidence")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return safe versioned state for persistence and HTTP status."""
        return self.model_dump(mode="json")


class ManualOperationQueueSnapshot(ContractModel):
    """Compact immutable queue snapshot suitable for the overview endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["manual-operation-queue-snapshot-v1"] = (
        "manual-operation-queue-snapshot-v1"
    )
    queued_count: int = Field(ge=0)
    running_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    latest_operation_id: UUID | None = None
    latest_status: ManualOperationStatus | None = None


class ManualOperationStateDocument(ContractModel):
    """Bounded append-only operation history stored beside scheduler state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["manual-operation-state-document-v1"] = (
        "manual-operation-state-document-v1"
    )
    operations: tuple[ManualOperationState, ...] = Field(max_length=_MAX_OPERATIONS_RETAINED)

    @model_validator(mode="after")
    def validate_history(self) -> ManualOperationStateDocument:
        ids = tuple(item.operation_id for item in self.operations)
        if len(ids) != len(set(ids)):
            raise ValueError("manual operation state contains duplicate identities")
        ordering = tuple((item.submitted_at, str(item.operation_id)) for item in self.operations)
        if ordering != tuple(sorted(ordering)):
            raise ValueError("manual operations must use deterministic chronological ordering")
        return self


class ManualOperationStateStore:
    """Atomically persist operation lifecycle without touching analytical storage."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve(strict=False)
        self._lock = threading.RLock()

    def load(self) -> ManualOperationStateDocument:
        with self._lock:
            if not self._path.exists():
                return ManualOperationStateDocument(operations=())
            try:
                return ManualOperationStateDocument.model_validate_json(
                    self._path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError) as error:
                raise AaplOperationalStateError(
                    "manual operation state is malformed or unreadable"
                ) from error

    def write(self, operation: ManualOperationState) -> None:
        with self._lock:
            operations = list(self.load().operations)
            matches = [
                index
                for index, existing in enumerate(operations)
                if existing.operation_id == operation.operation_id
            ]
            if matches:
                existing = operations[matches[0]]
                if existing == operation:
                    return
                _validate_transition(existing, operation)
                operations[matches[0]] = operation
            else:
                operations.append(operation)
            operations.sort(key=lambda item: (item.submitted_at, str(item.operation_id)))
            self._write_document(ManualOperationStateDocument(operations=tuple(operations)))

    def _write_document(self, state: ManualOperationStateDocument) -> None:
        document = (
            json.dumps(
                state.model_dump(mode="json"),
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
        except OSError as error:
            raise AaplOperationalStateError(
                "manual operation state could not be written"
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


ManualOperationDispatch = Callable[[ManualOperationRequest], ManualOperationResult]


class ManualOperationQueue:
    """Run persisted requests asynchronously through one shared writer lock."""

    def __init__(
        self,
        store: ManualOperationStateStore,
        dispatch: ManualOperationDispatch,
        *,
        writer_lock: threading.RLock | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        operation_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._store = store
        self._dispatch = dispatch
        self._writer_lock = writer_lock or threading.RLock()
        self._clock = clock
        self._operation_id_factory = operation_id_factory
        self._run_lock = threading.Lock()
        self._time_lock = threading.Lock()
        self._last_timestamp = _latest_timestamp(self._store.load())
        self._condition = threading.Condition()
        self._stop = False
        self._worker: threading.Thread | None = None
        self.recover_interrupted()

    def start(self) -> None:
        """Start the single daemon worker once."""
        with self._condition:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop = False
            self._worker = threading.Thread(
                target=self._run_forever,
                name="manual-operation-queue",
                daemon=True,
            )
            self._worker.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop the worker without altering queued or running durable state."""
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=timeout)

    def enqueue(self, request: ManualOperationRequest) -> ManualOperationState:
        """Deduplicate equivalent active work and persist a new queued request."""
        with self._condition:
            active = next(
                (
                    item
                    for item in reversed(self._store.load().operations)
                    if item.fingerprint == request.fingerprint
                    and item.status in {ManualOperationStatus.QUEUED, ManualOperationStatus.RUNNING}
                ),
                None,
            )
            if active is not None:
                return active
            queued = ManualOperationState(
                operation_id=self._operation_id_factory(),
                fingerprint=request.fingerprint,
                request=request,
                status=ManualOperationStatus.QUEUED,
                submitted_at=self._now(),
            )
            self._store.write(queued)
            self._condition.notify_all()
            return queued

    def get(self, operation_id: UUID) -> ManualOperationState | None:
        """Return one immutable persisted status without waiting for the writer."""
        return next(
            (item for item in self._store.load().operations if item.operation_id == operation_id),
            None,
        )

    def snapshot(self) -> ManualOperationQueueSnapshot:
        """Return counts only, bounded independently from retained history."""
        operations = self._store.load().operations
        latest = operations[-1] if operations else None
        return ManualOperationQueueSnapshot(
            queued_count=sum(item.status is ManualOperationStatus.QUEUED for item in operations),
            running_count=sum(item.status is ManualOperationStatus.RUNNING for item in operations),
            failed_count=sum(item.status is ManualOperationStatus.FAILED for item in operations),
            latest_operation_id=latest.operation_id if latest else None,
            latest_status=latest.status if latest else None,
        )

    def recover_interrupted(self) -> tuple[ManualOperationState, ...]:
        """Requeue prior running work, preserving its identity and audited recovery count."""
        recovered: list[ManualOperationState] = []
        for operation in self._store.load().operations:
            if operation.status is not ManualOperationStatus.RUNNING:
                continue
            queued = operation.model_copy(
                update={
                    "status": ManualOperationStatus.QUEUED,
                    "started_at": None,
                    "completed_at": None,
                    "recovery_count": operation.recovery_count + 1,
                    "result": None,
                    "failure": None,
                }
            )
            self._store.write(queued)
            recovered.append(queued)
        return tuple(recovered)

    def run_next(self) -> ManualOperationState | None:
        """Execute the oldest queued request once; useful for the worker and tests."""
        if not self._run_lock.acquire(blocking=False):
            return None
        try:
            queued = next(
                (
                    item
                    for item in self._store.load().operations
                    if item.status is ManualOperationStatus.QUEUED
                ),
                None,
            )
            if queued is None:
                return None
            running = queued.model_copy(
                update={"status": ManualOperationStatus.RUNNING, "started_at": self._now()}
            )
            self._store.write(running)
            try:
                with self._writer_lock:
                    result = self._dispatch(running.request)
            except Exception:  # noqa: BLE001
                completed = running.model_copy(
                    update={
                        "status": ManualOperationStatus.FAILED,
                        "completed_at": self._now(),
                        "failure": ManualOperationFailure(
                            category="operation_failed",
                            message=(
                                "manual operation failed; inspect provider-safe operational logs"
                            ),
                            retryable=False,
                        ),
                    }
                )
            else:
                completed = running.model_copy(
                    update={
                        "status": ManualOperationStatus.SUCCEEDED,
                        "completed_at": self._now(),
                        "result": result,
                    }
                )
            self._store.write(completed)
            return completed
        finally:
            self._run_lock.release()

    def _run_forever(self) -> None:
        while True:
            with self._condition:
                if self._stop:
                    return
            if self.run_next() is None:
                with self._condition:
                    if self._stop:
                        return
                    self._condition.wait(timeout=1.0)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("manual operation clock must return a timezone-aware datetime")
        normalized = value.astimezone(UTC)
        with self._time_lock:
            if self._last_timestamp is not None and normalized < self._last_timestamp:
                normalized = self._last_timestamp
            self._last_timestamp = normalized
        return normalized


def _latest_timestamp(state: ManualOperationStateDocument) -> datetime | None:
    timestamps = tuple(
        timestamp
        for operation in state.operations
        for timestamp in (
            operation.submitted_at,
            operation.started_at,
            operation.completed_at,
        )
        if timestamp is not None
    )
    return max(timestamps, default=None)


def _iter_keys(value: JsonValue) -> tuple[str, ...]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            keys.append(key)
            keys.extend(_iter_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.extend(_iter_keys(item))
    return tuple(keys)


def _validate_transition(
    existing: ManualOperationState,
    replacement: ManualOperationState,
) -> None:
    if (
        existing.request != replacement.request
        or existing.fingerprint != replacement.fingerprint
        or existing.submitted_at != replacement.submitted_at
    ):
        raise AaplOperationalStateError("manual operation identity is inconsistent")
    allowed = {
        (ManualOperationStatus.QUEUED, ManualOperationStatus.RUNNING),
        (ManualOperationStatus.RUNNING, ManualOperationStatus.QUEUED),
        (ManualOperationStatus.RUNNING, ManualOperationStatus.SUCCEEDED),
        (ManualOperationStatus.RUNNING, ManualOperationStatus.FAILED),
    }
    if (existing.status, replacement.status) not in allowed:
        raise AaplOperationalStateError("manual operation lifecycle is inconsistent")


__all__ = [
    "ManualOperationFailure",
    "ManualOperationKind",
    "ManualOperationQueue",
    "ManualOperationQueueSnapshot",
    "ManualOperationRequest",
    "ManualOperationResult",
    "ManualOperationState",
    "ManualOperationStateStore",
    "ManualOperationStatus",
]
