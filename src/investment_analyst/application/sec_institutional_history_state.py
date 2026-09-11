"""Strict operational state persistence for the two-close institutional 13F history window."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

SEC_INSTITUTIONAL_HISTORY_STATE_SCHEMA_VERSION = "sec-institutional-history-state-v1"
SEC_INSTITUTIONAL_HISTORY_STATE_FILE_NAME = "sec_institutional_history_state_v1.json"


class SecInstitutionalHistoryStateError(AaplOperationalStateError):
    """Operational history state is missing, malformed, or contradictory."""


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class SecInstitutionalHistoryDatasetState(_Strict):
    """One persisted dataset of the window, exactly as the catalog resolved it."""

    period_start: date
    period_end: date
    dataset_url: NonEmptyStr
    dataset_sha256: NonEmptyStr
    snapshot_id: UUID

    @model_validator(mode="after")
    def validate_period(self) -> SecInstitutionalHistoryDatasetState:
        if self.period_start > self.period_end:
            raise ValueError("dataset period start must precede period end")
        return self


def _canonical_payload(state: SecInstitutionalHistoryState) -> bytes:
    data = state.model_dump(mode="json", exclude={"checksum_sha256"})
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_state_checksum(state: SecInstitutionalHistoryState) -> str:
    return hashlib.sha256(_canonical_payload(state)).hexdigest()


class SecInstitutionalHistoryState(_Strict):
    """Atomic operational state for the ordered pair of adjacent Form 13F datasets."""

    schema_version: Literal["sec-institutional-history-state-v1"] = (
        SEC_INSTITUTIONAL_HISTORY_STATE_SCHEMA_VERSION
    )
    updated_at: UTCDateTime
    phase: Literal["preparing", "ready", "completed"] = "preparing"
    older: SecInstitutionalHistoryDatasetState | None = None
    newer: SecInstitutionalHistoryDatasetState | None = None
    target_cursor: int = Field(default=0, ge=0)
    total_targets: int | None = Field(default=None, ge=0)
    cycle_count: int = Field(default=0, ge=0)
    last_processed_cik: NonEmptyStr | None = None
    last_status: Literal["success", "completed", "preparing", "skipped", "failed"] | None = None
    last_error_code: NonEmptyStr | None = None
    checksum_sha256: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_window_and_cursor(self) -> SecInstitutionalHistoryState:
        if self.older is not None and self.newer is not None:
            if self.older.period_start > self.newer.period_start:
                raise ValueError("window datasets must be ordered older then newer")
            if self.older.period_end >= self.newer.period_start:
                raise ValueError("window datasets must not overlap")
            if self.older.period_end + timedelta(days=1) != self.newer.period_start:
                raise ValueError("window datasets must be adjacent official periods")
            if self.older.snapshot_id == self.newer.snapshot_id:
                raise ValueError("window datasets must carry distinct snapshots")
        if self.phase == "preparing":
            if self.total_targets is not None or self.target_cursor != 0:
                raise ValueError("a preparing window cannot declare targets or a cursor")
        else:
            if self.older is None or self.newer is None or self.total_targets is None:
                raise ValueError("a resolved window requires both datasets and its target total")
            if self.target_cursor > self.total_targets:
                raise ValueError("target cursor cannot exceed the declared target total")
            if self.phase == "completed" and self.target_cursor < self.total_targets:
                raise ValueError("a completed window must have processed every target")
        if self.checksum_sha256 is not None:
            expected = compute_state_checksum(self)
            if self.checksum_sha256 != expected:
                raise ValueError("state checksum mismatch")
        return self

    def with_checksum(self) -> SecInstitutionalHistoryState:
        checksum = compute_state_checksum(self)
        return self.model_copy(update={"checksum_sha256": checksum})


class SecInstitutionalHistoryStateStore:
    """Atomic, fail-closed persistence for ``sec_institutional_history_state_v1.json``."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve(strict=False)
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> SecInstitutionalHistoryState:
        """Load valid state without creating a file when none exists."""
        with self._lock:
            if not self._path.exists():
                return SecInstitutionalHistoryState(updated_at=datetime.now(UTC))
            try:
                text = self._path.read_text(encoding="utf-8")
                return SecInstitutionalHistoryState.model_validate_json(text)
            except (OSError, UnicodeError, ValueError) as error:
                raise SecInstitutionalHistoryStateError(
                    f"institutional history state is malformed or unreadable: {error}"
                ) from error

    def write(self, state: SecInstitutionalHistoryState) -> SecInstitutionalHistoryState:
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
                raise SecInstitutionalHistoryStateError(
                    "institutional history state could not be written"
                ) from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                temporary.unlink(missing_ok=True)


__all__ = [
    "SEC_INSTITUTIONAL_HISTORY_STATE_FILE_NAME",
    "SEC_INSTITUTIONAL_HISTORY_STATE_SCHEMA_VERSION",
    "SecInstitutionalHistoryDatasetState",
    "SecInstitutionalHistoryState",
    "SecInstitutionalHistoryStateError",
    "SecInstitutionalHistoryStateStore",
    "compute_state_checksum",
]
