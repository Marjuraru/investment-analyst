"""Additive per-attempt observability of local storage and duration.

The records produced here are operational instrumentation only. They are not analytical
evidence: they carry no ``available_at``, they never enter a point-in-time query, and no
metric, diagnostic, candidate or alert may read them. The collector measures what the
filesystem and a read-only engine can observe around one existing scheduled execution,
appends its own bounded artifact under a declared state root, and never opens a second writer.

One observation cycle is bound by :meth:`StorageObservabilityCollector.begin_attempt` and
:meth:`StorageObservabilityCollector.complete_attempt`, and it partitions its measured window
into the five durations of the storage observability contract:

- ``verification``: loading and validating the previously persisted artifact at cycle start.
- ``query``: local measurement reads, namely the physical database and WAL sizes and the
  read-only engine query for exact document bytes per table.
- ``network``: the measured execution window of the job callable, where provider transport
  work happens. A single opaque callable cannot be sub-attributed from this surface.
- ``persistence``: bounded artifact persistence performed inside the cycle, that is the
  once-per-day compaction. The O(1) append of the record itself closes after the window.
- ``calculation``: the remainder of the window that closes it, where deltas and the
  created/reused classification are derived; it absorbs whole-millisecond rounding so the
  five stages reconcile exactly.

Every duration comes from one clock and the five stages reconcile exactly with ``total_ms``,
so no second clock can disagree with the recorded breakdown.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

import duckdb
from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_ARTIFACT_FILE_NAME = "storage_observability_v1.jsonl"
_MAX_RETAINED_DAYS = 90
_DOCUMENT_COLUMN = "document_json"
_TABLE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MICROSECONDS_PER_MILLISECOND = 1_000


class StorageObservabilityError(RuntimeError):
    """Carry one sanitized observability failure into the caller boundary."""


class StorageObservabilityTableBytes(ContractModel):
    """Exact logical size of one persisted document table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table_name: NonEmptyStr
    row_count: int = Field(ge=0)
    document_bytes: int = Field(ge=0)

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit JSON primitives for persistence."""
        return {
            "table_name": self.table_name,
            "row_count": self.row_count,
            "document_bytes": self.document_bytes,
        }


class StorageObservabilityDurations(ContractModel):
    """Per-attempt duration breakdown measured with a single clock."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_ms: int = Field(ge=0)
    network_ms: int = Field(ge=0)
    query_ms: int = Field(ge=0)
    calculation_ms: int = Field(ge=0)
    persistence_ms: int = Field(ge=0)
    verification_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_reconciliation(self) -> StorageObservabilityDurations:
        """Require the five stages to account for the whole measured window."""
        attributed = (
            self.network_ms
            + self.query_ms
            + self.calculation_ms
            + self.persistence_ms
            + self.verification_ms
        )
        if attributed != self.total_ms:
            raise ValueError("stage durations must reconcile with total_ms")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit JSON primitives for persistence."""
        return {
            "total_ms": self.total_ms,
            "network_ms": self.network_ms,
            "query_ms": self.query_ms,
            "calculation_ms": self.calculation_ms,
            "persistence_ms": self.persistence_ms,
            "verification_ms": self.verification_ms,
        }


class ScheduledJobObservation(ContractModel):
    """Correlation and outcome facts the scheduler supplies for one completed attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: UUID
    job_id: NonEmptyStr
    attempt_number: int = Field(ge=1, le=10)
    local_date: date
    attempt_status: NonEmptyStr
    evidence_changed: bool | None = None
    rows_created: int | None = Field(default=None, ge=0)
    rows_reused: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_evidence_classification(self) -> ScheduledJobObservation:
        """Keep the created/reused classification coherent with the attempt outcome."""
        _require_coherent_evidence(self.evidence_changed, self.rows_created, self.rows_reused)
        return self


def _require_coherent_evidence(
    evidence_changed: bool | None,
    rows_created: int | None,
    rows_reused: int | None,
) -> None:
    """Reject a partial or contradictory created/reused classification."""
    reported = (evidence_changed is not None, rows_created is not None, rows_reused is not None)
    if any(reported) and not all(reported):
        raise ValueError("created, reused and evidence change must be reported together")
    if (
        evidence_changed is not None
        and rows_created is not None
        and evidence_changed != (rows_created > 0)
    ):
        raise ValueError("evidence_changed must match whether rows were created")


class StorageObservabilityRecord(ContractModel):
    """Operational storage and duration facts observed for one scheduled attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["storage-observability-v1"] = "storage-observability-v1"
    observed_at: UTCDateTime
    attempt_id: UUID
    job_id: NonEmptyStr
    attempt_number: int = Field(ge=1, le=10)
    local_date: date
    attempt_status: NonEmptyStr
    evidence_changed: bool | None = None
    rows_created: int | None = Field(default=None, ge=0)
    rows_reused: int | None = Field(default=None, ge=0)
    database_bytes_before: int = Field(ge=0)
    database_bytes_after: int = Field(ge=0)
    wal_bytes_before: int = Field(ge=0)
    wal_bytes_after: int = Field(ge=0)
    table_bytes: tuple[StorageObservabilityTableBytes, ...] = ()
    durations: StorageObservabilityDurations

    @model_validator(mode="after")
    def validate_record(self) -> StorageObservabilityRecord:
        """Keep identity, classification, and table accounting deterministic."""
        if isinstance(self.local_date, datetime):
            raise ValueError("local_date must be a date")
        _require_coherent_evidence(self.evidence_changed, self.rows_created, self.rows_reused)
        names = tuple(item.table_name for item in self.table_bytes)
        if names != tuple(sorted(set(names))):
            raise ValueError("table bytes must be unique and sorted by table name")
        return self

    @property
    def database_delta_bytes(self) -> int:
        """Return the measured change of the physical database file."""
        return self.database_bytes_after - self.database_bytes_before

    @property
    def wal_delta_bytes(self) -> int:
        """Return the measured change of the write-ahead log."""
        return self.wal_bytes_after - self.wal_bytes_before

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit JSON primitives for persistence."""
        return {
            "schema_version": self.schema_version,
            "observed_at": self.observed_at.isoformat(),
            "attempt_id": str(self.attempt_id),
            "job_id": self.job_id,
            "attempt_number": self.attempt_number,
            "local_date": self.local_date.isoformat(),
            "attempt_status": self.attempt_status,
            "evidence_changed": self.evidence_changed,
            "rows_created": self.rows_created,
            "rows_reused": self.rows_reused,
            "database_bytes_before": self.database_bytes_before,
            "database_bytes_after": self.database_bytes_after,
            "wal_bytes_before": self.wal_bytes_before,
            "wal_bytes_after": self.wal_bytes_after,
            "table_bytes": [item.to_json_dict() for item in self.table_bytes],
            "durations": self.durations.to_json_dict(),
        }


class StorageObservabilityDailyJobSummary(ContractModel):
    """Compact per-job aggregate folded into one daily snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: NonEmptyStr
    attempt_count: int = Field(ge=1)
    attempts_with_evidence: int = Field(ge=0)
    database_bytes_delta: int
    wal_bytes_delta: int
    rows_created: int = Field(ge=0)
    rows_reused: int = Field(ge=0)
    total_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_coverage(self) -> StorageObservabilityDailyJobSummary:
        """Require evidence coverage to stay within the attempt count."""
        if self.attempts_with_evidence > self.attempt_count:
            raise ValueError("evidence coverage cannot exceed the attempt count")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit JSON primitives for persistence."""
        return {
            "job_id": self.job_id,
            "attempt_count": self.attempt_count,
            "attempts_with_evidence": self.attempts_with_evidence,
            "database_bytes_delta": self.database_bytes_delta,
            "wal_bytes_delta": self.wal_bytes_delta,
            "rows_created": self.rows_created,
            "rows_reused": self.rows_reused,
            "total_ms": self.total_ms,
        }


class StorageObservabilityDailySnapshot(ContractModel):
    """Compact bounded aggregate for one closed UTC day."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["storage-observability-daily-snapshot-v1"] = (
        "storage-observability-daily-snapshot-v1"
    )
    utc_date: date
    record_count: int = Field(ge=1)
    job_summaries: tuple[StorageObservabilityDailyJobSummary, ...]

    @model_validator(mode="after")
    def validate_snapshot(self) -> StorageObservabilityDailySnapshot:
        """Keep the daily aggregate deterministic and internally consistent."""
        if isinstance(self.utc_date, datetime):
            raise ValueError("utc_date must be a date")
        job_ids = tuple(item.job_id for item in self.job_summaries)
        if job_ids != tuple(sorted(set(job_ids))):
            raise ValueError("daily summaries must be unique and sorted by job id")
        if sum(item.attempt_count for item in self.job_summaries) != self.record_count:
            raise ValueError("daily summary attempts must reconcile with the record count")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit JSON primitives for persistence."""
        return {
            "schema_version": self.schema_version,
            "utc_date": self.utc_date.isoformat(),
            "record_count": self.record_count,
            "job_summaries": [item.to_json_dict() for item in self.job_summaries],
        }


class StorageObservabilityState(ContractModel):
    """Bounded recovery view of the persisted observability artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    daily_snapshots: tuple[StorageObservabilityDailySnapshot, ...] = ()
    records: tuple[StorageObservabilityRecord, ...] = ()

    @model_validator(mode="after")
    def validate_state(self) -> StorageObservabilityState:
        """Keep the retained history bounded, ordered, and uncorrelated by identity."""
        if len(self.daily_snapshots) > _MAX_RETAINED_DAYS:
            raise ValueError("daily snapshots must stay within the retention bound")
        dates = tuple(item.utc_date for item in self.daily_snapshots)
        if dates != tuple(sorted(set(dates))):
            raise ValueError("daily snapshots must be unique and ordered by date")
        observed = tuple(item.observed_at for item in self.records)
        if observed != tuple(sorted(observed)):
            raise ValueError("records must be ordered by observation time")
        attempt_ids = tuple(item.attempt_id for item in self.records)
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("records must not repeat an attempt identity")
        if self.records and self.daily_snapshots:
            open_day = self.records[0].observed_at.date()
            if any(item.utc_date >= open_day for item in self.daily_snapshots):
                raise ValueError("closed daily snapshots must precede every open record")
        return self


def parse_storage_observability_state(text: str) -> StorageObservabilityState:
    """Parse and validate one persisted artifact without writing anything."""
    snapshots: list[StorageObservabilityDailySnapshot] = []
    records: list[StorageObservabilityRecord] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise StorageObservabilityError(
                f"observability artifact line {number} is not valid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise StorageObservabilityError(
                f"observability artifact line {number} is not a JSON object"
            )
        schema_version = payload.get("schema_version")
        if schema_version == "storage-observability-v1":
            records.append(StorageObservabilityRecord.model_validate(payload))
        elif schema_version == "storage-observability-daily-snapshot-v1":
            snapshots.append(StorageObservabilityDailySnapshot.model_validate(payload))
        else:
            raise StorageObservabilityError(
                f"observability artifact line {number} has an unknown schema version"
            )
    return StorageObservabilityState(
        daily_snapshots=tuple(snapshots),
        records=tuple(records),
    )


def _file_bytes(path: Path) -> int:
    """Measure one file exactly, treating an absent file as zero bytes."""
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _milliseconds(delta: timedelta) -> int:
    """Convert one exact timedelta into whole milliseconds."""
    return delta // timedelta(microseconds=_MICROSECONDS_PER_MILLISECOND)


def _line(payload: dict[str, object]) -> str:
    """Render one deterministic compact artifact line."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class StorageObservationHandle:
    """One measured attempt window opened before the execution starts."""

    job_id: str
    attempt_id: UUID
    opened_at: datetime
    verified_at: datetime
    execution_started_at: datetime
    database_bytes_before: int
    wal_bytes_before: int
    completed: bool = field(default=False)


class StorageObservabilityCollector:
    """Measure one scheduled attempt with a single clock and a read-only engine."""

    def __init__(
        self,
        *,
        state_root: Path,
        database_path: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._state_root = Path(state_root).expanduser().resolve(strict=False)
        self._database_path = Path(database_path).expanduser().resolve(strict=False)
        self._wal_path = Path(f"{self._database_path}.wal")
        self._clock = clock
        self._lock = threading.RLock()

    @property
    def artifact_path(self) -> Path:
        """Return the bounded artifact location under the declared state root."""
        return self._state_root / _ARTIFACT_FILE_NAME

    def state(self) -> StorageObservabilityState:
        """Load and validate the persisted artifact without creating it."""
        with self._lock:
            return self._load_state()

    def begin_attempt(self, *, job_id: str, attempt_id: UUID) -> StorageObservationHandle:
        """Open one observation window and capture the physical bytes before it."""
        with self._lock:
            opened_at = self._now()
            self._load_state()
            verified_at = self._now()
            database_bytes_before = _file_bytes(self._database_path)
            wal_bytes_before = _file_bytes(self._wal_path)
            return StorageObservationHandle(
                job_id=job_id,
                attempt_id=attempt_id,
                opened_at=opened_at,
                verified_at=verified_at,
                execution_started_at=self._now(),
                database_bytes_before=database_bytes_before,
                wal_bytes_before=wal_bytes_before,
            )

    def complete_attempt(
        self,
        handle: StorageObservationHandle,
        observation: ScheduledJobObservation,
    ) -> StorageObservabilityRecord:
        """Close one observation window, persist its record, and verify the append."""
        with self._lock:
            if handle.completed:
                raise StorageObservabilityError("observation window was already completed")
            if observation.attempt_id != handle.attempt_id or observation.job_id != handle.job_id:
                raise StorageObservabilityError("observation correlation does not match its window")
            execution_completed_at = self._now()
            database_bytes_after = _file_bytes(self._database_path)
            wal_bytes_after = _file_bytes(self._wal_path)
            measured_at = self._now()
            table_bytes = self._measure_table_bytes()
            queried_at = self._now()
            state = self._load_state()
            self._compact(state, execution_completed_at.date())
            persisted_at = self._now()
            calculated_at = self._now()
            record = StorageObservabilityRecord(
                observed_at=execution_completed_at,
                attempt_id=observation.attempt_id,
                job_id=observation.job_id,
                attempt_number=observation.attempt_number,
                local_date=observation.local_date,
                attempt_status=observation.attempt_status,
                evidence_changed=observation.evidence_changed,
                rows_created=observation.rows_created,
                rows_reused=observation.rows_reused,
                database_bytes_before=handle.database_bytes_before,
                database_bytes_after=database_bytes_after,
                wal_bytes_before=handle.wal_bytes_before,
                wal_bytes_after=wal_bytes_after,
                table_bytes=table_bytes,
                durations=self._durations(
                    handle,
                    execution_completed_at=execution_completed_at,
                    measured_at=measured_at,
                    queried_at=queried_at,
                    persisted_at=persisted_at,
                    calculated_at=calculated_at,
                ),
            )
            self._append_line(record)
            handle.completed = True
            self._verify_append(record)
            return record

    def _durations(
        self,
        handle: StorageObservationHandle,
        *,
        execution_completed_at: datetime,
        measured_at: datetime,
        queried_at: datetime,
        persisted_at: datetime,
        calculated_at: datetime,
    ) -> StorageObservabilityDurations:
        """Reconcile every measured stage with one clock and whole milliseconds."""
        verification_ms = _milliseconds(handle.verified_at - handle.opened_at)
        query_ms = (
            _milliseconds(handle.execution_started_at - handle.verified_at)
            + _milliseconds(measured_at - execution_completed_at)
            + _milliseconds(queried_at - measured_at)
        )
        network_ms = _milliseconds(execution_completed_at - handle.execution_started_at)
        persistence_ms = _milliseconds(persisted_at - queried_at)
        total_ms = _milliseconds(calculated_at - handle.opened_at)
        calculation_ms = total_ms - (network_ms + query_ms + persistence_ms + verification_ms)
        if calculation_ms < 0:
            raise StorageObservabilityError("measured window does not reconcile with its stages")
        return StorageObservabilityDurations(
            total_ms=total_ms,
            network_ms=network_ms,
            query_ms=query_ms,
            calculation_ms=calculation_ms,
            persistence_ms=persistence_ms,
            verification_ms=verification_ms,
        )

    def _measure_table_bytes(self) -> tuple[StorageObservabilityTableBytes, ...]:
        """Measure exact document bytes per table with the engine opened read-only."""
        if not self._database_path.exists():
            return ()
        try:
            connection = duckdb.connect(str(self._database_path), read_only=True)
        except duckdb.Error as error:
            raise StorageObservabilityError(
                "read-only engine measurement is unavailable"
            ) from error
        try:
            names = connection.execute(
                "SELECT table_name FROM information_schema.columns"
                " WHERE column_name = ? ORDER BY table_name",
                [_DOCUMENT_COLUMN],
            ).fetchall()
            measured: list[StorageObservabilityTableBytes] = []
            for (name,) in names:
                if not isinstance(name, str) or not _TABLE_IDENTIFIER.fullmatch(name):
                    raise StorageObservabilityError("engine reported an unsupported table name")
                row = connection.execute(
                    f'SELECT count(*), coalesce(sum(octet_length(encode("{_DOCUMENT_COLUMN}"))), 0)'
                    f' FROM "{name}"'
                ).fetchone()
                if row is None:
                    raise StorageObservabilityError("engine did not return a table measurement")
                measured.append(
                    StorageObservabilityTableBytes(
                        table_name=name,
                        row_count=int(row[0]),
                        document_bytes=int(row[1]),
                    )
                )
        except duckdb.Error as error:
            raise StorageObservabilityError("read-only engine measurement failed") from error
        finally:
            connection.close()
        return tuple(sorted(measured, key=lambda item: item.table_name))

    def _compact(self, state: StorageObservabilityState, record_day: date) -> None:
        """Fold every closed UTC day once, keeping the retained history bounded."""
        retained_dates = tuple(item.utc_date for item in state.daily_snapshots)
        if retained_dates and record_day <= retained_dates[-1]:
            raise StorageObservabilityError("observation date is behind the retained history")
        closed_days = tuple(
            sorted(
                {
                    item.observed_at.date()
                    for item in state.records
                    if item.observed_at.date() < record_day
                }
            )
        )
        if not closed_days:
            return
        snapshots = list(state.daily_snapshots)
        for closed_day in closed_days:
            if any(item.utc_date == closed_day for item in snapshots):
                raise StorageObservabilityError("closed day already has a daily snapshot")
            resolved = tuple(
                item for item in state.records if item.observed_at.date() == closed_day
            )
            snapshots.append(_daily_snapshot(closed_day, resolved))
        retained = StorageObservabilityState(
            daily_snapshots=tuple(snapshots[-_MAX_RETAINED_DAYS:]),
            records=tuple(item for item in state.records if item.observed_at.date() >= record_day),
        )
        lines = [_line(item.to_json_dict()) for item in retained.daily_snapshots]
        lines.extend(_line(item.to_json_dict()) for item in retained.records)
        self._rewrite(lines)

    def _append_line(self, record: StorageObservabilityRecord) -> None:
        """Append one compact line without rewriting the retained history."""
        self._state_root.mkdir(parents=True, exist_ok=True)
        with self.artifact_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{_line(record.to_json_dict())}\n")

    def _rewrite(self, lines: Sequence[str]) -> None:
        """Replace the bounded artifact atomically after a closed day is folded."""
        self._state_root.mkdir(parents=True, exist_ok=True)
        temporary = self.artifact_path.with_name(f"{self.artifact_path.name}.tmp")
        try:
            temporary.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
            os.replace(temporary, self.artifact_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _verify_append(self, record: StorageObservabilityRecord) -> None:
        """Re-read the artifact and require the appended record to be its open tail."""
        state = self._load_state()
        if not state.records or state.records[-1].attempt_id != record.attempt_id:
            raise StorageObservabilityError(
                "persisted artifact does not expose the appended record"
            )

    def _load_state(self) -> StorageObservabilityState:
        """Load the bounded artifact without creating a missing file."""
        path = self.artifact_path
        if not path.exists():
            return StorageObservabilityState()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise StorageObservabilityError("observability artifact is unreadable") from error
        return parse_storage_observability_state(text)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise StorageObservabilityError("observability clock must be timezone-aware")
        return value.astimezone(UTC)


def _daily_snapshot(
    utc_date: date,
    records: tuple[StorageObservabilityRecord, ...],
) -> StorageObservabilityDailySnapshot:
    """Fold one closed day of records into its compact bounded aggregate."""
    summaries: list[StorageObservabilityDailyJobSummary] = []
    for job_id in sorted({item.job_id for item in records}):
        daily = tuple(item for item in records if item.job_id == job_id)
        summaries.append(
            StorageObservabilityDailyJobSummary(
                job_id=job_id,
                attempt_count=len(daily),
                attempts_with_evidence=sum(item.rows_created is not None for item in daily),
                database_bytes_delta=sum(item.database_delta_bytes for item in daily),
                wal_bytes_delta=sum(item.wal_delta_bytes for item in daily),
                rows_created=sum(item.rows_created or 0 for item in daily),
                rows_reused=sum(item.rows_reused or 0 for item in daily),
                total_ms=sum(item.durations.total_ms for item in daily),
            )
        )
    return StorageObservabilityDailySnapshot(
        utc_date=utc_date,
        record_count=len(records),
        job_summaries=tuple(summaries),
    )


__all__ = [
    "ScheduledJobObservation",
    "StorageObservabilityCollector",
    "StorageObservabilityDailyJobSummary",
    "StorageObservabilityDailySnapshot",
    "StorageObservabilityDurations",
    "StorageObservabilityError",
    "StorageObservabilityRecord",
    "StorageObservabilityState",
    "StorageObservabilityTableBytes",
    "StorageObservationHandle",
    "parse_storage_observability_state",
]
