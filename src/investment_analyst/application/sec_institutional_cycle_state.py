"""Strict operational state persistence for the scheduled Form 13F cycle."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

SEC_INSTITUTIONAL_CYCLE_STATE_SCHEMA_VERSION = "sec-institutional-cycle-state-v1"
SEC_INSTITUTIONAL_CYCLE_STATE_FILE_NAME = "sec_institutional_cycle_state_v1.json"


class SecInstitutionalCycleStateError(AaplOperationalStateError):
    """Operational cycle state is missing, malformed, or contradictory."""


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


def _canonical_payload(state: SecInstitutionalCycleState) -> bytes:
    data = {
        "schema_version": state.schema_version,
        "updated_at": state.updated_at.isoformat(),
        "dataset_period_start": (
            state.dataset_period_start.isoformat()
            if state.dataset_period_start is not None
            else None
        ),
        "dataset_period_end": (
            state.dataset_period_end.isoformat() if state.dataset_period_end is not None else None
        ),
        "dataset_url": state.dataset_url,
        "dataset_sha256": state.dataset_sha256,
        "dataset_last_validated_at": (
            state.dataset_last_validated_at.isoformat()
            if state.dataset_last_validated_at is not None
            else None
        ),
        "snapshot_id": str(state.snapshot_id) if state.snapshot_id is not None else None,
        "manager_cursor": state.manager_cursor,
        "total_managers": state.total_managers,
        "cycle_count": state.cycle_count,
        "last_processed_cik": state.last_processed_cik,
        "last_status": state.last_status,
        "last_error_code": state.last_error_code,
    }
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_state_checksum(state: SecInstitutionalCycleState) -> str:
    return hashlib.sha256(_canonical_payload(state)).hexdigest()


class SecInstitutionalCycleState(_Strict):
    """Atomic operational state tracking Form 13F dataset checking and cursor progression."""

    schema_version: Literal["sec-institutional-cycle-state-v1"] = (
        SEC_INSTITUTIONAL_CYCLE_STATE_SCHEMA_VERSION
    )
    updated_at: UTCDateTime
    dataset_period_start: date | None = None
    dataset_period_end: date | None = None
    dataset_url: NonEmptyStr | None = None
    dataset_sha256: NonEmptyStr | None = None
    dataset_last_validated_at: UTCDateTime | None = None
    snapshot_id: UUID | None = None
    manager_cursor: int = Field(default=0, ge=0)
    total_managers: int | None = Field(default=None, ge=0)
    cycle_count: int = Field(default=0, ge=0)
    last_processed_cik: NonEmptyStr | None = None
    last_status: Literal["success", "completed", "skipped", "failed"] | None = None
    last_error_code: NonEmptyStr | None = None
    checksum_sha256: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_snapshot_and_cursor(self) -> SecInstitutionalCycleState:
        if self.snapshot_id is None:
            if self.manager_cursor != 0:
                raise ValueError("manager cursor must be zero when snapshot_id is absent")
            if any(
                item is not None
                for item in (
                    self.dataset_period_start,
                    self.dataset_period_end,
                    self.dataset_url,
                    self.dataset_sha256,
                    self.dataset_last_validated_at,
                )
            ):
                raise ValueError("dataset metadata must be absent when snapshot_id is absent")
        else:
            if any(
                item is None
                for item in (
                    self.dataset_period_start,
                    self.dataset_period_end,
                    self.dataset_url,
                    self.dataset_sha256,
                    self.dataset_last_validated_at,
                )
            ):
                raise ValueError("dataset metadata must be complete when snapshot_id is present")
            assert self.dataset_period_start is not None
            assert self.dataset_period_end is not None
            if self.dataset_period_start > self.dataset_period_end:
                raise ValueError("dataset period start must precede period end")

            if self.total_managers is not None and self.manager_cursor > self.total_managers:
                raise ValueError("manager cursor cannot exceed total managers in snapshot")
        if self.checksum_sha256 is not None:
            expected = compute_state_checksum(self)
            if self.checksum_sha256 != expected:
                raise ValueError("state checksum mismatch")
        return self

    def with_checksum(self) -> SecInstitutionalCycleState:
        checksum = compute_state_checksum(self)
        return self.model_copy(update={"checksum_sha256": checksum})


class SecInstitutionalCycleStateStore:
    """Atomic, fail-closed persistence for ``sec_institutional_cycle_state_v1.json``."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve(strict=False)
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> SecInstitutionalCycleState:
        """Load valid state without creating a file when none exists."""
        with self._lock:
            if not self._path.exists():
                return SecInstitutionalCycleState(
                    updated_at=datetime.now(UTC),
                    manager_cursor=0,
                )
            try:
                text = self._path.read_text(encoding="utf-8")
                return SecInstitutionalCycleState.model_validate_json(text)
            except (OSError, UnicodeError, ValueError) as error:
                raise SecInstitutionalCycleStateError(
                    f"institutional cycle state is malformed or unreadable: {error}"
                ) from error

    def write(self, state: SecInstitutionalCycleState) -> SecInstitutionalCycleState:
        """Atomically persist valid state with updated checksum."""
        with self._lock:
            prepared = state.with_checksum()
            document = prepared.model_dump_json(by_alias=True).encode("utf-8") + b"\n"
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
                return prepared
            except OSError as error:
                raise SecInstitutionalCycleStateError(
                    "institutional cycle state could not be written"
                ) from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                temporary.unlink(missing_ok=True)
