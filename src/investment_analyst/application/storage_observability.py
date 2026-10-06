"""Additive per-attempt observability of local storage and duration.

The records produced here are operational instrumentation only. They are not analytical
evidence: they carry no ``available_at``, they never enter a point-in-time query, and no
metric, diagnostic, candidate or alert may read them. The collector measures what the
filesystem and a read-only engine can observe around one existing scheduled execution,
appends its own bounded artifact under a declared state root, and never opens a second writer.

One observation cycle is bound by :meth:`StorageObservabilityCollector.begin_attempt` and
:meth:`StorageObservabilityCollector.complete_attempt`, and it partitions its measured window
into the five durations of the storage observability contract:

- ``verification``: loading and validating the previously persisted artifact at cycle start
  by the collector.
- ``query``: local measurement reads by the collector, namely the physical database and WAL sizes
  and exact row counts by document table. Logical document bytes are outside this per-attempt phase.
- ``job_execution``: the measured execution window of the job callable, where job execution
  happens. A single opaque callable cannot be sub-attributed from this surface without provider
  instrumentation.
- ``persistence``: bounded artifact persistence performed by the collector inside the cycle,
  that is the once-per-day compaction. The O(1) append of the record itself closes after the window.
- ``collector_unattributed``: the residual window of the collector that closes it, where deltas
  and the created/reused classification are derived; it absorbs whole-millisecond rounding so the
  five stages reconcile exactly.

The collector measures its own window with one clock, and the five stages reconcile exactly with
``total_ms``. Scheduler lifecycle timestamps are used for ``job_execution_ms`` only when they fit
inside that measured window in chronological order. If they do not, the terminal record is still
preserved, the job duration is not inferred (zero in the existing integer contract), the interval
remains unattributed, and ``collector_overhead_ms`` is ``None``. Otherwise, that field records the
share of the same window the instrument itself consumed, separately from the measured job stage.

The same window also classifies the row growth the attempt produced. The collector measures the
exact row count per document table before the execution and again when it closes, and partitions
the created rows the attempt reports into new evidence rows, revisions and derived rows, with the
growth observed outside those two families left explicitly unclassified. Per-attempt records leave
``table_bytes`` empty to mean logical bytes were not measured in this phase. Rewriting one identity
in place adds no row, so the part of the created count that no table gained is reported as a
revision instead of being inferred from the tables afterwards.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

import duckdb
from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_ARTIFACT_FILE_NAME = "storage_observability_v1.jsonl"
_MAX_RETAINED_DAYS = 90
_DOCUMENT_COLUMN = "document_json"
_TABLE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MICROSECONDS_PER_MILLISECOND = 1_000
_EVIDENCE_TABLES = frozenset({"raw_record_index", "normalized_observations"})
_DERIVED_TABLES = frozenset({"metric_results", "diagnostic_results"})
_COLLECTOR_MEMORY_LIMIT = "256MB"
_COLLECTOR_THREADS = 1
StorageObservabilityFailureReason = Literal[
    "measurement_timeout",
    "engine_unavailable",
    "engine_error",
    "artifact_invalid",
    "artifact_unreadable",
    "artifact_write_failed",
    "collector_error",
]
StorageObservabilityMeasurementState = Literal["complete", "partial", "unavailable"]
StorageObservabilityFailurePhase = Literal["begin", "end", "append", "verify"]
_ALLOWED_FAILURE_REASONS = frozenset(
    {
        "measurement_timeout",
        "engine_unavailable",
        "engine_error",
        "artifact_invalid",
        "artifact_unreadable",
        "artifact_write_failed",
        "collector_error",
    }
)


def storage_observability_artifact_path(state_root: Path) -> Path:
    """Return the bounded artifact location under one declared state root."""
    return Path(state_root).expanduser().resolve(strict=False) / _ARTIFACT_FILE_NAME


class StorageObservabilityError(RuntimeError):
    """Carry one sanitized observability failure into the caller boundary."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: StorageObservabilityFailureReason = "collector_error",
        measurement_elapsed_ns: int = 0,
        query_open_ns: int = 0,
        query_select_ns: int = 0,
        connection_opens: int = 0,
        select_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.reason_code = (
            reason_code if reason_code in _ALLOWED_FAILURE_REASONS else "collector_error"
        )
        self.measurement_elapsed_ns = max(measurement_elapsed_ns, 0)
        self.query_open_ns = max(query_open_ns, 0)
        self.query_select_ns = max(query_select_ns, 0)
        self.connection_opens = max(connection_opens, 0)
        self.select_count = max(select_count, 0)


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


class StorageObservabilityTableRows(ContractModel):
    """Exact row count for one validated document table at an attempt boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table_name: NonEmptyStr
    row_count: int = Field(ge=0)

    def to_json_dict(self) -> dict[str, object]:
        return {"table_name": self.table_name, "row_count": self.row_count}


class StorageObservabilityDurations(ContractModel):
    """Per-attempt duration breakdown measured with a single clock."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_ms: int = Field(ge=0)
    job_execution_ms: int = Field(ge=0)
    query_ms: int = Field(ge=0)
    collector_unattributed_ms: int = Field(ge=0)
    persistence_ms: int = Field(ge=0)
    verification_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_reconciliation(self) -> StorageObservabilityDurations:
        """Require the five stages to account for the whole measured window."""
        attributed = (
            self.job_execution_ms
            + self.query_ms
            + self.collector_unattributed_ms
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
            "job_execution_ms": self.job_execution_ms,
            "query_ms": self.query_ms,
            "collector_unattributed_ms": self.collector_unattributed_ms,
            "persistence_ms": self.persistence_ms,
            "verification_ms": self.verification_ms,
        }


StorageObservabilityDurationsV2 = StorageObservabilityDurations


class StorageObservabilityDurationsV3(StorageObservabilityDurations):
    """V3 duration breakdown with engine opens and SQL reads inside query_ms."""

    query_open_ms: int = Field(ge=0)
    query_select_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_query_subattribution(self) -> StorageObservabilityDurationsV3:
        if self.query_open_ms + self.query_select_ms > self.query_ms:
            raise ValueError("query opening and SELECT time must fit within query_ms")
        return self

    def to_json_dict(self) -> dict[str, object]:
        return {
            **super().to_json_dict(),
            "query_open_ms": self.query_open_ms,
            "query_select_ms": self.query_select_ms,
        }


class StorageObservabilityDurationsV1(ContractModel):
    """Per-attempt duration breakdown under the v1 contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_ms: int = Field(ge=0)
    network_ms: int = Field(ge=0)
    query_ms: int = Field(ge=0)
    calculation_ms: int = Field(ge=0)
    persistence_ms: int = Field(ge=0)
    verification_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_reconciliation(self) -> StorageObservabilityDurationsV1:
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


class StorageObservabilityGrowthClassification(ContractModel):
    """How the rows one attempt created split across the observed table roles.

    The four counts partition the created rows the attempt reported. ``new_evidence_rows`` and
    ``derived_rows`` are the rows the evidence and the derived tables actually gained during the
    window; ``revision_rows`` are the created rows that replaced an identity instead of adding
    one, so no table grew for them; ``unclassified_rows`` is the observed growth in any other
    measured table, kept explicit instead of being absorbed into another category.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    new_evidence_rows: int | None = Field(default=None, ge=0)
    revision_rows: int | None = Field(default=None, ge=0)
    derived_rows: int | None = Field(default=None, ge=0)
    unclassified_rows: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_classification(self) -> StorageObservabilityGrowthClassification:
        """Keep the four counts either absent or reported together."""
        reported = (
            self.new_evidence_rows is not None,
            self.revision_rows is not None,
            self.derived_rows is not None,
            self.unclassified_rows is not None,
        )
        if any(reported) and not all(reported):
            raise ValueError("growth classification must be reported as a whole")
        return self

    @property
    def classified_rows(self) -> int | None:
        """Return the created rows accounted for by the classification."""
        if self.new_evidence_rows is None:
            return None
        return (
            self.new_evidence_rows + self.revision_rows + self.derived_rows + self.unclassified_rows
        )

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit JSON primitives for persistence."""
        return {
            "new_evidence_rows": self.new_evidence_rows,
            "revision_rows": self.revision_rows,
            "derived_rows": self.derived_rows,
            "unclassified_rows": self.unclassified_rows,
        }


class StorageObservabilityRecord(ContractModel):
    """Operational storage and duration facts observed for one scheduled attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["storage-observability-v2"] = "storage-observability-v2"
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
    growth: StorageObservabilityGrowthClassification | None = None
    collector_overhead_ms: int | None = Field(default=None, ge=0)
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
        if self.growth is not None:
            if self.rows_created is None:
                raise ValueError("growth classification requires the created rows of the attempt")
            if self.growth.classified_rows != self.rows_created:
                raise ValueError("growth classification must account for every created row")
        if (
            self.collector_overhead_ms is not None
            and self.collector_overhead_ms + self.durations.job_execution_ms
            != self.durations.total_ms
        ):
            raise ValueError("collector overhead must be separate from the measured job stage")
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
            "growth": None if self.growth is None else self.growth.to_json_dict(),
            "collector_overhead_ms": self.collector_overhead_ms,
            "durations": self.durations.to_json_dict(),
        }


StorageObservabilityRecordV2 = StorageObservabilityRecord


class StorageObservabilityRecordV1(ContractModel):
    """Operational storage and duration facts observed under the v1 contract."""

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
    growth: StorageObservabilityGrowthClassification | None = None
    collector_overhead_ms: int | None = Field(default=None, ge=0)
    durations: StorageObservabilityDurationsV1

    @model_validator(mode="after")
    def validate_record(self) -> StorageObservabilityRecordV1:
        """Keep identity, classification, and table accounting deterministic."""
        if isinstance(self.local_date, datetime):
            raise ValueError("local_date must be a date")
        _require_coherent_evidence(self.evidence_changed, self.rows_created, self.rows_reused)
        names = tuple(item.table_name for item in self.table_bytes)
        if names != tuple(sorted(set(names))):
            raise ValueError("table bytes must be unique and sorted by table name")
        if self.growth is not None:
            if self.rows_created is None:
                raise ValueError("growth classification requires the created rows of the attempt")
            if self.growth.classified_rows != self.rows_created:
                raise ValueError("growth classification must account for every created row")
        if (
            self.collector_overhead_ms is not None
            and self.collector_overhead_ms + self.durations.network_ms != self.durations.total_ms
        ):
            raise ValueError("collector overhead must be separate from the measured job stage")
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
            "growth": None if self.growth is None else self.growth.to_json_dict(),
            "collector_overhead_ms": self.collector_overhead_ms,
            "durations": self.durations.to_json_dict(),
        }


class StorageObservabilityRecordV3(ContractModel):
    """Terminal V3 measurement, with partial facts left explicitly unknown."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["storage-observability-v3"] = "storage-observability-v3"
    observed_at: UTCDateTime
    attempt_id: UUID
    job_id: NonEmptyStr
    attempt_number: int = Field(ge=1, le=10)
    local_date: date
    attempt_status: NonEmptyStr
    measurement_state: StorageObservabilityMeasurementState
    failure_phase: StorageObservabilityFailurePhase | None = None
    failure_reason: StorageObservabilityFailureReason | None = None
    evidence_changed: bool | None = None
    rows_created: int | None = Field(default=None, ge=0)
    rows_reused: int | None = Field(default=None, ge=0)
    database_bytes_before: int | None = Field(default=None, ge=0)
    database_bytes_after: int | None = Field(default=None, ge=0)
    wal_bytes_before: int | None = Field(default=None, ge=0)
    wal_bytes_after: int | None = Field(default=None, ge=0)
    table_rows_before: tuple[StorageObservabilityTableRows, ...] | None = None
    table_rows_after: tuple[StorageObservabilityTableRows, ...] | None = None
    table_bytes: tuple[StorageObservabilityTableBytes, ...] = ()
    growth: StorageObservabilityGrowthClassification | None = None
    collector_overhead_ms: int | None = Field(default=None, ge=0)
    durations: StorageObservabilityDurationsV3 | None = None

    @model_validator(mode="after")
    def validate_record(self) -> StorageObservabilityRecordV3:
        if isinstance(self.local_date, datetime):
            raise ValueError("local_date must be a date")
        _require_coherent_evidence(self.evidence_changed, self.rows_created, self.rows_reused)
        if (self.failure_phase is None) is not (self.failure_reason is None):
            raise ValueError("failure phase and reason must be reported together")
        before_known = (
            self.database_bytes_before is not None
            and self.wal_bytes_before is not None
            and self.table_rows_before is not None
        )
        after_known = (
            self.database_bytes_after is not None
            and self.wal_bytes_after is not None
            and self.table_rows_after is not None
        )
        if self.measurement_state == "complete" and not (before_known and after_known):
            raise ValueError("complete measurement requires both attempt boundaries")
        if self.measurement_state == "partial" and before_known and after_known:
            raise ValueError("partial measurement cannot contain both complete boundaries")
        if self.measurement_state == "partial" and not any(
            value is not None
            for value in (
                self.database_bytes_before,
                self.database_bytes_after,
                self.wal_bytes_before,
                self.wal_bytes_after,
                self.table_rows_before,
                self.table_rows_after,
            )
        ):
            raise ValueError("partial measurement requires at least one known value")
        if self.measurement_state == "unavailable" and (
            before_known
            or after_known
            or any(
                value is not None
                for value in (
                    self.database_bytes_before,
                    self.database_bytes_after,
                    self.wal_bytes_before,
                    self.wal_bytes_after,
                    self.table_rows_before,
                    self.table_rows_after,
                )
            )
        ):
            raise ValueError("unavailable measurement cannot claim storage boundaries")
        for rows in (self.table_rows_before, self.table_rows_after):
            if rows is not None:
                names = tuple(item.table_name for item in rows)
                if names != tuple(sorted(set(names))):
                    raise ValueError("table row counts must be unique and sorted by table name")
        names = tuple(item.table_name for item in self.table_bytes)
        if names != tuple(sorted(set(names))):
            raise ValueError("table bytes must be unique and sorted by table name")
        if self.growth is not None:
            if self.rows_created is None:
                raise ValueError("growth classification requires the created rows of the attempt")
            if self.growth.classified_rows != self.rows_created:
                raise ValueError("growth classification must account for every created row")
        if self.durations is None and self.collector_overhead_ms is not None:
            raise ValueError("collector overhead requires measured durations")
        if (
            self.durations is not None
            and self.collector_overhead_ms is not None
            and self.collector_overhead_ms + self.durations.job_execution_ms
            != self.durations.total_ms
        ):
            raise ValueError("collector overhead must be separate from the measured job stage")
        return self

    @property
    def database_delta_bytes(self) -> int | None:
        if self.database_bytes_before is None or self.database_bytes_after is None:
            return None
        return self.database_bytes_after - self.database_bytes_before

    @property
    def wal_delta_bytes(self) -> int | None:
        if self.wal_bytes_before is None or self.wal_bytes_after is None:
            return None
        return self.wal_bytes_after - self.wal_bytes_before

    def to_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "observed_at": self.observed_at.isoformat(),
            "attempt_id": str(self.attempt_id),
            "job_id": self.job_id,
            "attempt_number": self.attempt_number,
            "local_date": self.local_date.isoformat(),
            "attempt_status": self.attempt_status,
            "measurement_state": self.measurement_state,
            "failure_phase": self.failure_phase,
            "failure_reason": self.failure_reason,
            "evidence_changed": self.evidence_changed,
            "rows_created": self.rows_created,
            "rows_reused": self.rows_reused,
            "database_bytes_before": self.database_bytes_before,
            "database_bytes_after": self.database_bytes_after,
            "wal_bytes_before": self.wal_bytes_before,
            "wal_bytes_after": self.wal_bytes_after,
            "table_rows_before": (
                None
                if self.table_rows_before is None
                else [item.to_json_dict() for item in self.table_rows_before]
            ),
            "table_rows_after": (
                None
                if self.table_rows_after is None
                else [item.to_json_dict() for item in self.table_rows_after]
            ),
            "table_bytes": [item.to_json_dict() for item in self.table_bytes],
            "growth": None if self.growth is None else self.growth.to_json_dict(),
            "collector_overhead_ms": self.collector_overhead_ms,
            "durations": None if self.durations is None else self.durations.to_json_dict(),
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


class StorageObservabilityFailureSummary(ContractModel):
    """Count terminal observations by the sanitized collector failure phase and reason."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: StorageObservabilityFailurePhase
    reason: StorageObservabilityFailureReason
    attempt_count: int = Field(ge=1)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "reason": self.reason,
            "attempt_count": self.attempt_count,
        }


class StorageObservabilityDailyJobSummaryV2(ContractModel):
    """Daily aggregation that preserves unknown totals and measurement coverage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: NonEmptyStr
    attempt_count: int = Field(ge=1)
    attempts_with_evidence: int = Field(ge=0)
    measurement_complete_attempts: int = Field(ge=0)
    measurement_partial_attempts: int = Field(ge=0)
    measurement_unavailable_attempts: int = Field(ge=0)
    failure_summaries: tuple[StorageObservabilityFailureSummary, ...] = ()
    database_bytes_delta: int | None = None
    wal_bytes_delta: int | None = None
    rows_created: int | None = Field(default=None, ge=0)
    rows_reused: int | None = Field(default=None, ge=0)
    total_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_summary(self) -> StorageObservabilityDailyJobSummaryV2:
        coverage = (
            self.measurement_complete_attempts
            + self.measurement_partial_attempts
            + self.measurement_unavailable_attempts
        )
        if coverage != self.attempt_count:
            raise ValueError("measurement coverage must reconcile with the attempt count")
        if self.attempts_with_evidence > self.attempt_count:
            raise ValueError("evidence coverage cannot exceed the attempt count")
        keys = tuple((item.phase, item.reason) for item in self.failure_summaries)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("daily failure summaries must be unique and ordered")
        if sum(item.attempt_count for item in self.failure_summaries) > self.attempt_count:
            raise ValueError("failure coverage cannot exceed the attempt count")
        return self

    def to_json_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "attempt_count": self.attempt_count,
            "attempts_with_evidence": self.attempts_with_evidence,
            "measurement_complete_attempts": self.measurement_complete_attempts,
            "measurement_partial_attempts": self.measurement_partial_attempts,
            "measurement_unavailable_attempts": self.measurement_unavailable_attempts,
            "failure_summaries": [item.to_json_dict() for item in self.failure_summaries],
            "database_bytes_delta": self.database_bytes_delta,
            "wal_bytes_delta": self.wal_bytes_delta,
            "rows_created": self.rows_created,
            "rows_reused": self.rows_reused,
            "total_ms": self.total_ms,
        }


class StorageObservabilityDailySnapshotV2(ContractModel):
    """Compact daily snapshot with separate terminal and measurement coverage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["storage-observability-daily-snapshot-v2"] = (
        "storage-observability-daily-snapshot-v2"
    )
    utc_date: date
    record_count: int = Field(ge=1)
    measurement_complete_attempts: int = Field(ge=0)
    measurement_partial_attempts: int = Field(ge=0)
    measurement_unavailable_attempts: int = Field(ge=0)
    failure_summaries: tuple[StorageObservabilityFailureSummary, ...] = ()
    job_summaries: tuple[StorageObservabilityDailyJobSummaryV2, ...]

    @model_validator(mode="after")
    def validate_snapshot(self) -> StorageObservabilityDailySnapshotV2:
        if isinstance(self.utc_date, datetime):
            raise ValueError("utc_date must be a date")
        job_ids = tuple(item.job_id for item in self.job_summaries)
        if job_ids != tuple(sorted(set(job_ids))):
            raise ValueError("daily summaries must be unique and sorted by job id")
        if sum(item.attempt_count for item in self.job_summaries) != self.record_count:
            raise ValueError("daily summary attempts must reconcile with the record count")
        failure_keys = tuple((item.phase, item.reason) for item in self.failure_summaries)
        if failure_keys != tuple(sorted(set(failure_keys))):
            raise ValueError("snapshot failure summaries must be unique and ordered")
        failure_counts: dict[tuple[str, str], int] = {}
        for summary in self.job_summaries:
            for item in summary.failure_summaries:
                key = (item.phase, item.reason)
                failure_counts[key] = failure_counts.get(key, 0) + item.attempt_count
        expected_failures = tuple(
            (phase, reason, count) for (phase, reason), count in sorted(failure_counts.items())
        )
        actual_failures = tuple(
            (item.phase, item.reason, item.attempt_count) for item in self.failure_summaries
        )
        if actual_failures != expected_failures:
            raise ValueError("snapshot failure summaries must reconcile with job summaries")
        if sum(item.attempt_count for item in self.failure_summaries) > self.record_count:
            raise ValueError("snapshot failure coverage cannot exceed terminal records")
        coverage = (
            self.measurement_complete_attempts
            + self.measurement_partial_attempts
            + self.measurement_unavailable_attempts
        )
        if coverage != self.record_count:
            raise ValueError("snapshot measurement coverage must match terminal records")
        if (
            sum(item.measurement_complete_attempts for item in self.job_summaries)
            != self.measurement_complete_attempts
            or sum(item.measurement_partial_attempts for item in self.job_summaries)
            != self.measurement_partial_attempts
            or sum(item.measurement_unavailable_attempts for item in self.job_summaries)
            != self.measurement_unavailable_attempts
        ):
            raise ValueError("snapshot measurement coverage must reconcile with job summaries")
        return self

    def to_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "utc_date": self.utc_date.isoformat(),
            "record_count": self.record_count,
            "measurement_complete_attempts": self.measurement_complete_attempts,
            "measurement_partial_attempts": self.measurement_partial_attempts,
            "measurement_unavailable_attempts": self.measurement_unavailable_attempts,
            "failure_summaries": [item.to_json_dict() for item in self.failure_summaries],
            "job_summaries": [item.to_json_dict() for item in self.job_summaries],
        }


class StorageObservabilityState(ContractModel):
    """Bounded recovery view of the persisted observability artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    daily_snapshots: tuple[
        StorageObservabilityDailySnapshot | StorageObservabilityDailySnapshotV2, ...
    ] = ()
    records: tuple[
        StorageObservabilityRecord | StorageObservabilityRecordV1 | StorageObservabilityRecordV3,
        ...,
    ] = ()

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
    snapshots: list[StorageObservabilityDailySnapshot | StorageObservabilityDailySnapshotV2] = []
    records: list[
        StorageObservabilityRecord | StorageObservabilityRecordV1 | StorageObservabilityRecordV3
    ] = []
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
        if schema_version == "storage-observability-v2":
            records.append(StorageObservabilityRecord.model_validate(payload))
        elif schema_version == "storage-observability-v3":
            records.append(StorageObservabilityRecordV3.model_validate(payload))
        elif schema_version == "storage-observability-v1":
            records.append(StorageObservabilityRecordV1.model_validate(payload))
        elif schema_version == "storage-observability-daily-snapshot-v1":
            snapshots.append(StorageObservabilityDailySnapshot.model_validate(payload))
        elif schema_version == "storage-observability-daily-snapshot-v2":
            snapshots.append(StorageObservabilityDailySnapshotV2.model_validate(payload))
        else:
            raise StorageObservabilityError(
                f"observability artifact line {number} has an unknown schema version"
            )
    return StorageObservabilityState(
        daily_snapshots=tuple(snapshots),
        records=tuple(records),
    )


def _document_table_names(connection: duckdb.DuckDBPyConnection) -> tuple[str, ...]:
    """List the document tables the engine reports, rejecting an unusable identifier."""
    names = connection.execute(
        "SELECT table_name FROM information_schema.columns"
        " WHERE column_name = ? ORDER BY table_name",
        [_DOCUMENT_COLUMN],
    ).fetchall()
    resolved: list[str] = []
    for (name,) in names:
        if not isinstance(name, str) or not _TABLE_IDENTIFIER.fullmatch(name):
            raise StorageObservabilityError("engine reported an unsupported table name")
        resolved.append(name)
    return tuple(resolved)


def _table_row_count(connection: duckdb.DuckDBPyConnection, table_name: str) -> int:
    """Return the exact row count of one already validated document table."""
    row = connection.execute(f'SELECT count(*) FROM "{table_name}"').fetchone()
    if row is None:
        raise StorageObservabilityError("engine did not return a table measurement")
    return int(row[0])


def _growth_classification(
    observation: ScheduledJobObservation,
    *,
    rows_before: tuple[tuple[str, int], ...],
    rows_after: tuple[tuple[str, int], ...] = (),
    table_bytes: tuple[StorageObservabilityTableBytes, ...] = (),
) -> StorageObservabilityGrowthClassification | None:
    """Partition the created rows of one attempt, or decline when it cannot be observed."""
    resolved_after = rows_after or tuple((item.table_name, item.row_count) for item in table_bytes)
    if observation.rows_created is None or not resolved_after:
        return None
    before = dict(rows_before)
    added = tuple((name, count - before.get(name, count)) for name, count in resolved_after)
    observed_added = sum(count for _, count in added if count > 0)
    new_evidence_rows = sum(
        count for name, count in added if count > 0 and name in _EVIDENCE_TABLES
    )
    derived_rows = sum(count for name, count in added if count > 0 and name in _DERIVED_TABLES)
    revision_rows = observation.rows_created - observed_added
    if revision_rows < 0:
        return None
    return StorageObservabilityGrowthClassification(
        new_evidence_rows=new_evidence_rows,
        revision_rows=revision_rows,
        derived_rows=derived_rows,
        unclassified_rows=observed_added - new_evidence_rows - derived_rows,
    )


def _file_bytes(path: Path) -> int:
    """Measure one file exactly, treating an absent file as zero bytes."""
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _file_state(path: Path) -> tuple[int, tuple[int, int] | None]:
    """Return exact bytes and stable filesystem identity, distinguishing absence."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return 0, None
    return stat.st_size, (stat.st_dev, stat.st_ino)


def _milliseconds(delta: timedelta) -> int:
    """Convert one exact timedelta into whole milliseconds."""
    return delta // timedelta(microseconds=_MICROSECONDS_PER_MILLISECOND)


def _nanoseconds_to_milliseconds(value: int) -> int:
    """Convert monotonic nanoseconds to whole milliseconds."""
    return max(value, 0) // 1_000_000


def _measurement_state(
    database_before: int | None,
    wal_before: int | None,
    rows_before: tuple[tuple[str, int], ...] | None,
    database_after: int | None,
    wal_after: int | None,
    rows_after: tuple[tuple[str, int], ...] | None,
) -> StorageObservabilityMeasurementState:
    """Classify the two boundaries without treating missing values as zero."""
    before_known = (
        database_before is not None and wal_before is not None and rows_before is not None
    )
    after_known = database_after is not None and wal_after is not None and rows_after is not None
    if before_known and after_known:
        return "complete"
    if any(
        value is not None
        for value in (
            database_before,
            wal_before,
            rows_before,
            database_after,
            wal_after,
            rows_after,
        )
    ):
        return "partial"
    return "unavailable"


def _aware_utc(value: datetime) -> datetime:
    """Normalize explicit lifecycle timestamps without accepting naive values."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise StorageObservabilityError("observation timestamps must be timezone-aware")
    return value.astimezone(UTC)


@contextmanager
def _measurement_deadline(
    connection: duckdb.DuckDBPyConnection,
    timeout_seconds: float,
) -> Iterator[None]:
    """Interrupt read-only table measurements when their explicit deadline expires."""
    finished = threading.Event()

    def interrupt_if_running() -> None:
        if not finished.is_set():
            connection.interrupt()

    timer = threading.Timer(timeout_seconds, interrupt_if_running)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        finished.set()
        timer.cancel()


def _line(payload: dict[str, object]) -> str:
    """Render one deterministic compact artifact line."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class _ReadOnlyMeasurement:
    table_rows: tuple[tuple[str, int], ...]
    elapsed_ns: int
    query_open_ns: int
    query_select_ns: int
    connection_opens: int
    select_count: int


def _read_only_measurement_worker(database_path: str, channel: Connection) -> None:
    """Serve bounded read requests, opening and closing a fresh read-only connection each time."""
    table_names: tuple[str, ...] | None = None
    count_query: str | None = None
    while True:
        try:
            request = channel.recv()
        except EOFError:
            return
        if request is None:
            return
        opened_at = time.perf_counter_ns()
        query_open_ns = 0
        query_select_ns = 0
        connection_opened = False
        select_count = 0
        connection: duckdb.DuckDBPyConnection | None = None
        try:
            connection = duckdb.connect(database_path, read_only=True)
            connection.execute(f"SET memory_limit = '{_COLLECTOR_MEMORY_LIMIT}'")
            connection.execute(f"SET threads = {_COLLECTOR_THREADS}")
            connection_opened = True
            query_open_ns = time.perf_counter_ns() - opened_at
            select_started = time.perf_counter_ns()
            select_count = 0
            if table_names is None:
                select_count += 1
                table_names = _document_table_names(connection)
                count_query = " UNION ALL ".join(
                    f"SELECT '{name}' AS table_name, count(*) AS row_count FROM \"{name}\""
                    for name in table_names
                )
            if table_names:
                select_count += 1
                if count_query is None:
                    raise StorageObservabilityError("document table inventory is unavailable")
                rows = connection.execute(count_query).fetchall()
                measured = tuple(sorted((str(name), int(count)) for name, count in rows))
                if tuple(name for name, _ in measured) != table_names:
                    raise StorageObservabilityError("engine returned an inconsistent table count")
            else:
                measured = ()
            query_select_ns = time.perf_counter_ns() - select_started
            channel.send(
                (
                    "ok",
                    measured,
                    query_open_ns,
                    query_select_ns,
                    int(connection_opened),
                    select_count,
                )
            )
        except duckdb.Error:
            table_names = None
            count_query = None
            if query_open_ns == 0:
                query_open_ns = time.perf_counter_ns() - opened_at
                reason = "engine_unavailable"
            else:
                query_select_ns = time.perf_counter_ns() - opened_at - query_open_ns
                reason = "engine_error"
            channel.send(
                (
                    "error",
                    reason,
                    query_open_ns,
                    query_select_ns,
                    int(connection_opened),
                    select_count,
                )
            )
        except Exception:  # noqa: BLE001
            table_names = None
            count_query = None
            if query_open_ns == 0:
                query_open_ns = time.perf_counter_ns() - opened_at
                reason = "engine_unavailable"
            else:
                query_select_ns = time.perf_counter_ns() - opened_at - query_open_ns
                reason = "engine_error"
            channel.send(
                (
                    "error",
                    reason,
                    query_open_ns,
                    query_select_ns,
                    int(connection_opened),
                    select_count,
                )
            )
        finally:
            if connection is not None:
                connection.close()


@dataclass
class StorageObservationHandle:
    """One measured attempt window opened before the execution starts."""

    job_id: str
    attempt_id: UUID
    opened_at: datetime
    opened_monotonic_ns: int
    execution_started_at: datetime | None = None
    execution_started_monotonic_ns: int | None = None
    verification_ms: int = 0
    query_ms_before: int = 0
    query_open_ms_before: int = 0
    query_select_ms_before: int = 0
    database_bytes_before: int | None = None
    database_identity_before: tuple[int, int] | None = None
    wal_bytes_before: int | None = None
    table_rows_before: tuple[tuple[str, int], ...] | None = None
    failure_phase: StorageObservabilityFailurePhase | None = None
    failure_reason: StorageObservabilityFailureReason | None = None
    measurement_state: StorageObservabilityMeasurementState = "unavailable"
    terminal_record: StorageObservabilityRecordV3 | None = None
    persistence_state: StorageObservabilityState | None = None
    persistence_fingerprint: tuple[int, int, int, int] | None = None
    auto_close_cycle: bool = False
    measurement_finished: bool = False
    completed: bool = field(default=False)


class StorageObservabilityCollector:
    """Measure one scheduled attempt with a single clock and a read-only engine."""

    def __init__(
        self,
        *,
        state_root: Path,
        database_path: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        measurement_timeout_seconds: float = 5.0,
    ) -> None:
        if (
            isinstance(measurement_timeout_seconds, bool)
            or not isinstance(measurement_timeout_seconds, (int, float))
            or not math.isfinite(measurement_timeout_seconds)
            or measurement_timeout_seconds <= 0
        ):
            raise ValueError("measurement timeout must be finite and greater than zero")
        self._artifact_path = storage_observability_artifact_path(state_root)
        self._database_path = Path(database_path).expanduser().resolve(strict=False)
        self._wal_path = Path(f"{self._database_path}.wal")
        self._clock = clock
        self._measurement_timeout_seconds = measurement_timeout_seconds
        self._lock = threading.RLock()
        self._cycle_active = False
        self._measurement_process: multiprocessing.Process | None = None
        self._measurement_channel: Connection | None = None
        self._measurement_open_count = 0
        self._measurement_select_count = 0
        self._measurement_worker_exit_codes: list[int | None] = []

    @property
    def artifact_path(self) -> Path:
        """Return the bounded artifact location under the declared state root."""
        return self._artifact_path

    def state(self) -> StorageObservabilityState:
        """Load and validate the persisted artifact without creating it."""
        with self._lock:
            return self._load_state()

    def start_cycle(self) -> None:
        """Scope the reusable, connection-free reader worker to one scheduler tick."""
        with self._lock:
            if self._cycle_active:
                raise StorageObservabilityError("measurement cycle is already active")
            self._cycle_active = True
            self._measurement_open_count = 0
            self._measurement_select_count = 0
            self._measurement_worker_exit_codes = []

    def close_cycle(self) -> None:
        """Stop and reap the cycle worker, including after a timed out request."""
        with self._lock:
            self._stop_measurement_worker()
            self._cycle_active = False

    def unavailable_handle(
        self,
        observation: ScheduledJobObservation,
        *,
        observed_at: datetime,
    ) -> StorageObservationHandle:
        """Prepare a restart recovery envelope without inventing a prior measurement."""
        with self._lock:
            observed_at = _aware_utc(observed_at)
            handle = StorageObservationHandle(
                job_id=observation.job_id,
                attempt_id=observation.attempt_id,
                opened_at=observed_at,
                opened_monotonic_ns=time.perf_counter_ns(),
                measurement_state="unavailable",
                measurement_finished=True,
            )
            handle.terminal_record = StorageObservabilityRecordV3(
                observed_at=observed_at,
                attempt_id=observation.attempt_id,
                job_id=observation.job_id,
                attempt_number=observation.attempt_number,
                local_date=observation.local_date,
                attempt_status=observation.attempt_status,
                measurement_state="unavailable",
                evidence_changed=observation.evidence_changed,
                rows_created=observation.rows_created,
                rows_reused=observation.rows_reused,
            )
            return handle

    def failed_begin_handle(
        self,
        job_id: str,
        attempt_id: UUID,
        *,
        observed_at: datetime,
        error: Exception,
    ) -> StorageObservationHandle:
        """Retain a failed begin as a handle so its terminal envelope can still be written."""
        reason = (
            error.reason_code if isinstance(error, StorageObservabilityError) else "collector_error"
        )
        with self._lock:
            observed_at = _aware_utc(observed_at)
            return StorageObservationHandle(
                job_id=job_id,
                attempt_id=attempt_id,
                opened_at=observed_at,
                opened_monotonic_ns=time.perf_counter_ns(),
                execution_started_at=observed_at,
                execution_started_monotonic_ns=time.perf_counter_ns(),
                failure_phase="begin",
                failure_reason=reason,
                measurement_state="unavailable",
                auto_close_cycle=not self._cycle_active,
            )

    def begin_attempt(self, *, job_id: str, attempt_id: UUID) -> StorageObservationHandle:
        """Open one window, retaining a terminal handle even when its first read fails."""
        with self._lock:
            auto_close_cycle = not self._cycle_active
            if auto_close_cycle:
                self._cycle_active = True
                self._measurement_open_count = 0
                self._measurement_select_count = 0
                self._measurement_worker_exit_codes = []
            try:
                opened_at = self._now()
            except Exception:  # noqa: BLE001
                opened_at = datetime.now(UTC)
                begin_failure = StorageObservabilityError("observability clock is invalid")
            else:
                begin_failure = None
            handle = StorageObservationHandle(
                job_id=job_id,
                attempt_id=attempt_id,
                opened_at=opened_at,
                opened_monotonic_ns=time.perf_counter_ns(),
                auto_close_cycle=auto_close_cycle,
            )
            if begin_failure is not None:
                self._record_measurement_failure(handle, "begin", begin_failure)
            if handle.failure_reason is None:
                verification_started = time.perf_counter_ns()
                try:
                    self._load_state()
                except StorageObservabilityError as error:
                    self._record_measurement_failure(handle, "begin", error)
                except Exception:  # noqa: BLE001
                    self._record_measurement_failure(
                        handle,
                        "begin",
                        StorageObservabilityError("observability artifact could not be verified"),
                    )
                finally:
                    handle.verification_ms = _nanoseconds_to_milliseconds(
                        time.perf_counter_ns() - verification_started
                    )
            if handle.failure_reason is None or handle.failure_reason not in {
                "artifact_invalid",
                "artifact_unreadable",
            }:
                query_started = time.perf_counter_ns()
                try:
                    (
                        handle.database_bytes_before,
                        handle.database_identity_before,
                    ) = _file_state(self._database_path)
                    handle.wal_bytes_before = _file_bytes(self._wal_path)
                    measurement = self._request_read_measurement()
                    handle.table_rows_before = measurement.table_rows
                    handle.query_open_ms_before = _nanoseconds_to_milliseconds(
                        measurement.query_open_ns
                    )
                    handle.query_select_ms_before = _nanoseconds_to_milliseconds(
                        measurement.query_select_ns
                    )
                except StorageObservabilityError as error:
                    handle.query_open_ms_before = _nanoseconds_to_milliseconds(error.query_open_ns)
                    handle.query_select_ms_before = _nanoseconds_to_milliseconds(
                        error.query_select_ns
                    )
                    self._record_measurement_failure(handle, "begin", error)
                except OSError:
                    self._record_measurement_failure(
                        handle,
                        "begin",
                        StorageObservabilityError(
                            "physical database measurement failed", reason_code="engine_error"
                        ),
                    )
                finally:
                    elapsed_ns = time.perf_counter_ns() - query_started
                    handle.query_ms_before = _nanoseconds_to_milliseconds(elapsed_ns)
            try:
                handle.execution_started_at = self._now()
            except Exception:  # noqa: BLE001
                handle.execution_started_at = opened_at
                self._record_measurement_failure(
                    handle,
                    "begin",
                    StorageObservabilityError("observability clock is invalid"),
                )
            handle.execution_started_monotonic_ns = time.perf_counter_ns()
            handle.measurement_state = _measurement_state(
                handle.database_bytes_before,
                handle.wal_bytes_before,
                handle.table_rows_before,
                None,
                None,
                None,
            )
            return handle

    def complete_attempt(
        self,
        handle: StorageObservationHandle,
        observation: ScheduledJobObservation,
        *,
        execution_completed_at: datetime | None = None,
        result_persisted_at: datetime | None = None,
        job_execution_ms: int | None = None,
    ) -> StorageObservabilityRecordV3:
        """Freeze the first terminal candidate, then append and verify it idempotently."""
        with self._lock:
            if observation.attempt_id != handle.attempt_id or observation.job_id != handle.job_id:
                raise StorageObservabilityError("observation correlation does not match its window")
            if handle.completed and handle.terminal_record is not None:
                return handle.terminal_record
            execution_completed_at = _aware_utc(
                execution_completed_at if execution_completed_at is not None else self._now()
            )
            result_persisted_at = _aware_utc(
                result_persisted_at if result_persisted_at is not None else execution_completed_at
            )
            if result_persisted_at < execution_completed_at:
                raise StorageObservabilityError("durable result time predates job completion")
            if handle.terminal_record is None:
                try:
                    collector_closed_at = self._now()
                except StorageObservabilityError as error:
                    collector_closed_at = execution_completed_at
                    self._record_measurement_failure(handle, "end", error)
                lifecycle_timing_consistent = (
                    handle.execution_started_at is not None
                    and handle.execution_started_at <= execution_completed_at
                    and execution_completed_at <= result_persisted_at <= collector_closed_at
                )
                self._freeze_terminal_record(
                    handle,
                    observation,
                    execution_completed_at=execution_completed_at,
                    job_execution_ms=job_execution_ms,
                    lifecycle_timing_consistent=lifecycle_timing_consistent,
                )
            record = handle.terminal_record
            if record is None:
                raise StorageObservabilityError("terminal observation candidate is unavailable")
            try:
                self._persist_candidate(
                    record,
                    known_state=handle.persistence_state,
                    known_fingerprint=handle.persistence_fingerprint,
                )
            finally:
                if handle.auto_close_cycle:
                    self._stop_measurement_worker()
                    self._cycle_active = False
            handle.completed = True
            return record

    def _freeze_terminal_record(
        self,
        handle: StorageObservationHandle,
        observation: ScheduledJobObservation,
        *,
        execution_completed_at: datetime,
        job_execution_ms: int | None,
        lifecycle_timing_consistent: bool,
    ) -> None:
        """Measure the end boundary once and retain one immutable terminal candidate."""
        query_started = time.perf_counter_ns()
        database_bytes_after: int | None = None
        wal_bytes_after: int | None = None
        table_rows_after: tuple[tuple[str, int], ...] | None = None
        query_open_ms_after = 0
        query_select_ms_after = 0
        database_identity_after: tuple[int, int] | None = None
        try:
            database_bytes_after, database_identity_after = _file_state(self._database_path)
            wal_bytes_after = _file_bytes(self._wal_path)
            measurement = self._request_read_measurement()
            table_rows_after = measurement.table_rows
            query_open_ms_after = _nanoseconds_to_milliseconds(measurement.query_open_ns)
            query_select_ms_after = _nanoseconds_to_milliseconds(measurement.query_select_ns)
        except StorageObservabilityError as error:
            query_open_ms_after = _nanoseconds_to_milliseconds(error.query_open_ns)
            query_select_ms_after = _nanoseconds_to_milliseconds(error.query_select_ns)
            self._record_measurement_failure(handle, "end", error)
        except OSError:
            self._record_measurement_failure(
                handle,
                "end",
                StorageObservabilityError(
                    "physical database measurement failed", reason_code="engine_error"
                ),
            )
        if database_identity_after != handle.database_identity_before:
            database_bytes_after = None
            wal_bytes_after = None
            table_rows_after = None
            self._record_measurement_failure(
                handle,
                "end",
                StorageObservabilityError(
                    "database identity changed during the observation",
                    reason_code="engine_error",
                ),
            )
        elif (
            handle.table_rows_before is not None
            and table_rows_after is not None
            and tuple(name for name, _ in handle.table_rows_before)
            != tuple(name for name, _ in table_rows_after)
        ):
            table_rows_after = None
            self._record_measurement_failure(
                handle,
                "end",
                StorageObservabilityError(
                    "document table inventory changed during the observation",
                    reason_code="engine_error",
                ),
            )
        query_ms_after = _nanoseconds_to_milliseconds(time.perf_counter_ns() - query_started)
        table_bytes: tuple[StorageObservabilityTableBytes, ...] = ()
        handle.measurement_state = _measurement_state(
            handle.database_bytes_before,
            handle.wal_bytes_before,
            handle.table_rows_before,
            database_bytes_after,
            wal_bytes_after,
            table_rows_after,
        )
        state: StorageObservabilityState | None
        try:
            state = self._load_state()
        except StorageObservabilityError as error:
            self._record_measurement_failure(handle, "end", error)
            state = None
        persisted_started = time.perf_counter_ns()
        if state is not None:
            try:
                state = self._compact(state, execution_completed_at.date())
            except StorageObservabilityError as error:
                self._record_measurement_failure(handle, "end", error)
                state = None
        if state is not None:
            try:
                handle.persistence_fingerprint = self._artifact_fingerprint()
            except StorageObservabilityError as error:
                self._record_measurement_failure(handle, "end", error)
                state = None
        handle.persistence_state = state
        persistence_ms = _nanoseconds_to_milliseconds(time.perf_counter_ns() - persisted_started)
        calculated_ns = time.perf_counter_ns()
        total_ms = _nanoseconds_to_milliseconds(calculated_ns - handle.opened_monotonic_ns)
        verification_ms = handle.verification_ms
        query_ms = handle.query_ms_before + query_ms_after
        job_ms = max(job_execution_ms or 0, 0) if job_execution_ms is not None else 0
        if (
            job_execution_ms is None
            and lifecycle_timing_consistent
            and handle.execution_started_at is not None
        ):
            job_ms = _milliseconds(execution_completed_at - handle.execution_started_at)
        collector_unattributed_ms = total_ms - (
            verification_ms + query_ms + persistence_ms + job_ms
        )
        if collector_unattributed_ms < 0:
            job_ms = 0
            collector_unattributed_ms = max(
                total_ms - (verification_ms + query_ms + persistence_ms), 0
            )
            lifecycle_timing_consistent = False
        query_open_ms = handle.query_open_ms_before + query_open_ms_after
        query_select_ms = handle.query_select_ms_before + query_select_ms_after
        if query_open_ms + query_select_ms > query_ms:
            query_open_ms = min(query_open_ms, query_ms)
            query_select_ms = min(query_select_ms, query_ms - query_open_ms)
        durations = StorageObservabilityDurationsV3(
            total_ms=total_ms,
            job_execution_ms=job_ms,
            query_ms=query_ms,
            collector_unattributed_ms=collector_unattributed_ms,
            persistence_ms=persistence_ms,
            verification_ms=verification_ms,
            query_open_ms=query_open_ms,
            query_select_ms=query_select_ms,
        )
        rows_before = handle.table_rows_before or ()
        growth = (
            _growth_classification(
                observation,
                rows_before=rows_before,
                rows_after=table_rows_after or (),
                table_bytes=table_bytes,
            )
            if handle.table_rows_before is not None and table_rows_after is not None
            else None
        )
        handle.terminal_record = StorageObservabilityRecordV3(
            observed_at=execution_completed_at,
            attempt_id=observation.attempt_id,
            job_id=observation.job_id,
            attempt_number=observation.attempt_number,
            local_date=observation.local_date,
            attempt_status=observation.attempt_status,
            measurement_state=handle.measurement_state,
            failure_phase=handle.failure_phase,
            failure_reason=handle.failure_reason,
            evidence_changed=observation.evidence_changed,
            rows_created=observation.rows_created,
            rows_reused=observation.rows_reused,
            database_bytes_before=handle.database_bytes_before,
            database_bytes_after=database_bytes_after,
            wal_bytes_before=handle.wal_bytes_before,
            wal_bytes_after=wal_bytes_after,
            table_rows_before=(
                None
                if handle.table_rows_before is None
                else tuple(
                    StorageObservabilityTableRows(table_name=name, row_count=count)
                    for name, count in handle.table_rows_before
                )
            ),
            table_rows_after=(
                None
                if table_rows_after is None
                else tuple(
                    StorageObservabilityTableRows(table_name=name, row_count=count)
                    for name, count in table_rows_after
                )
            ),
            table_bytes=table_bytes,
            growth=growth,
            collector_overhead_ms=(
                max(total_ms - job_ms, 0) if lifecycle_timing_consistent else None
            ),
            durations=durations,
        )
        handle.measurement_finished = True

    def _record_measurement_failure(
        self,
        handle: StorageObservationHandle,
        phase: StorageObservabilityFailurePhase,
        error: StorageObservabilityError,
    ) -> None:
        if handle.failure_reason is None:
            handle.failure_phase = phase
            handle.failure_reason = error.reason_code

    def _persist_candidate(
        self,
        record: StorageObservabilityRecordV3,
        *,
        known_state: StorageObservabilityState | None = None,
        known_fingerprint: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Append once or verify an identical prior append without changing its candidate."""
        current_fingerprint = self._artifact_fingerprint()
        state = (
            known_state
            if known_state is not None and known_fingerprint == current_fingerprint
            else self._load_state()
        )
        matches = tuple(item for item in state.records if item.attempt_id == record.attempt_id)
        if matches:
            if len(matches) != 1 or matches[0].to_json_dict() != record.to_json_dict():
                raise StorageObservabilityError(
                    "attempt identity already has a different terminal observation",
                    reason_code="artifact_invalid",
                )
            self._verify_append(record)
            return
        self._compact(state, record.observed_at.date())
        self._append_line(record)
        self._verify_append(record)

    def _ensure_measurement_worker(self) -> tuple[multiprocessing.Process, Connection]:
        process = self._measurement_process
        channel = self._measurement_channel
        if process is not None and channel is not None and process.is_alive():
            return process, channel
        self._stop_measurement_worker()
        context = multiprocessing.get_context("spawn")
        parent_channel, child_channel = context.Pipe(duplex=True)
        process = context.Process(
            target=_read_only_measurement_worker,
            args=(str(self._database_path), child_channel),
            name="investment-analyst-storage-reader",
            daemon=False,
        )
        try:
            process.start()
        except Exception as error:  # noqa: BLE001
            parent_channel.close()
            child_channel.close()
            raise StorageObservabilityError(
                "read-only measurement worker could not start",
                reason_code="engine_unavailable",
            ) from error
        child_channel.close()
        self._measurement_process = process
        self._measurement_channel = parent_channel
        return process, parent_channel

    def _stop_measurement_worker(self) -> None:
        process = self._measurement_process
        channel = self._measurement_channel
        self._measurement_process = None
        self._measurement_channel = None
        if channel is not None:
            with suppress(BrokenPipeError, EOFError, OSError):
                channel.send(None)
            channel.close()
        if process is None:
            return
        process.join(timeout=0.25)
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.25)
        if process.is_alive():
            process.kill()
            process.join(timeout=0.25)
        if process.is_alive():
            raise StorageObservabilityError(
                "read-only measurement worker could not be reaped",
                reason_code="collector_error",
            )
        self._measurement_worker_exit_codes.append(process.exitcode)
        process.close()

    def _request_read_measurement(self) -> _ReadOnlyMeasurement:
        """Supervise one fresh open/query/close request under its own monotonic deadline."""
        request_started = time.perf_counter_ns()
        if not self._database_path.exists():
            return _ReadOnlyMeasurement((), 0, 0, 0, 0, 0)
        deadline_ns = request_started + int(self._measurement_timeout_seconds * 1_000_000_000)
        try:
            process, channel = self._ensure_measurement_worker()
            remaining_ns = deadline_ns - time.perf_counter_ns()
            if remaining_ns <= 0:
                raise TimeoutError
            channel.send("measure")
            if not channel.poll(remaining_ns / 1_000_000_000):
                raise TimeoutError
            response = cast(tuple[object, ...], channel.recv())
            elapsed_ns = time.perf_counter_ns() - request_started
            if not response:
                raise EOFError
            state = response[0]
            if state == "ok" and len(response) == 6:
                table_rows = cast(tuple[tuple[str, int], ...], response[1])
                query_open_ns = cast(int, response[2])
                query_select_ns = cast(int, response[3])
                self._measurement_open_count += cast(int, response[4])
                self._measurement_select_count += cast(int, response[5])
                return _ReadOnlyMeasurement(
                    table_rows=table_rows,
                    elapsed_ns=elapsed_ns,
                    query_open_ns=query_open_ns,
                    query_select_ns=query_select_ns,
                    connection_opens=cast(int, response[4]),
                    select_count=cast(int, response[5]),
                )
            if state == "error" and len(response) == 6:
                raw_reason = response[1]
                reason = (
                    cast(StorageObservabilityFailureReason, raw_reason)
                    if raw_reason in _ALLOWED_FAILURE_REASONS
                    else "engine_error"
                )
                raise StorageObservabilityError(
                    "read-only engine measurement failed",
                    reason_code=reason,
                    measurement_elapsed_ns=elapsed_ns,
                    query_open_ns=cast(int, response[2]),
                    query_select_ns=cast(int, response[3]),
                    connection_opens=cast(int, response[4]),
                    select_count=cast(int, response[5]),
                )
            raise EOFError
        except TimeoutError as error:
            elapsed_ns = time.perf_counter_ns() - request_started
            self._stop_measurement_worker()
            raise StorageObservabilityError(
                "read-only engine measurement timed out",
                reason_code="measurement_timeout",
                measurement_elapsed_ns=elapsed_ns,
            ) from error
        except StorageObservabilityError as error:
            self._measurement_open_count += error.connection_opens
            self._measurement_select_count += error.select_count
            raise
        except (EOFError, BrokenPipeError, OSError) as error:
            elapsed_ns = time.perf_counter_ns() - request_started
            self._stop_measurement_worker()
            raise StorageObservabilityError(
                "read-only measurement worker exited unexpectedly",
                reason_code="engine_unavailable",
                measurement_elapsed_ns=elapsed_ns,
            ) from error

    def _measure_table_bytes(self) -> tuple[StorageObservabilityTableBytes, ...]:
        """Measure exact document bytes and rows for an explicit full measurement."""
        if not self._database_path.exists():
            return ()
        connection = self._open_read_only_engine()
        try:
            with _measurement_deadline(connection, self._measurement_timeout_seconds):
                measured: list[StorageObservabilityTableBytes] = []
                for name in _document_table_names(connection):
                    row = connection.execute(
                        f"SELECT count(*), "
                        f'coalesce(sum(octet_length(encode("{_DOCUMENT_COLUMN}"))), 0)'
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
        except duckdb.InterruptException as error:
            raise StorageObservabilityError(
                "read-only engine measurement timed out", reason_code="measurement_timeout"
            ) from error
        except duckdb.Error as error:
            raise StorageObservabilityError(
                "read-only engine measurement failed", reason_code="engine_error"
            ) from error
        finally:
            connection.close()
        return tuple(measured)

    def _measure_table_row_counts(self) -> tuple[tuple[str, int], ...]:
        """Measure the exact row count per table with a read-only engine."""
        if not self._database_path.exists():
            return ()
        return self._request_read_measurement().table_rows

    def _open_read_only_engine(self) -> duckdb.DuckDBPyConnection:
        """Open the engine read-only so no measurement can ever write."""
        try:
            connection = duckdb.connect(str(self._database_path), read_only=True)
            try:
                connection.execute(f"SET memory_limit = '{_COLLECTOR_MEMORY_LIMIT}'")
                connection.execute(f"SET threads = {_COLLECTOR_THREADS}")
            except Exception:
                connection.close()
                raise
            return connection
        except duckdb.Error as error:
            raise StorageObservabilityError(
                "read-only engine measurement is unavailable",
                reason_code="engine_unavailable",
            ) from error

    def _compact(
        self, state: StorageObservabilityState, record_day: date
    ) -> StorageObservabilityState:
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
            return state
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
        return retained

    def _append_line(
        self,
        record: StorageObservabilityRecord
        | StorageObservabilityRecordV1
        | StorageObservabilityRecordV3,
    ) -> None:
        """Append one compact line without rewriting the retained history."""
        try:
            self._artifact_path.parent.mkdir(parents=True, exist_ok=True)
            with self.artifact_path.open("a", encoding="utf-8") as stream:
                stream.write(f"{_line(record.to_json_dict())}\n")
        except OSError as error:
            raise StorageObservabilityError(
                "observability artifact could not be written",
                reason_code="artifact_write_failed",
            ) from error

    def _rewrite(self, lines: Sequence[str]) -> None:
        """Replace the bounded artifact atomically after a closed day is folded."""
        temporary = self.artifact_path.with_name(f"{self.artifact_path.name}.tmp")
        try:
            self._artifact_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
            os.replace(temporary, self.artifact_path)
        except (OSError, UnicodeError) as error:
            raise StorageObservabilityError(
                "observability artifact could not be compacted",
                reason_code="artifact_write_failed",
            ) from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                raise StorageObservabilityError(
                    "observability artifact could not be compacted",
                    reason_code="artifact_write_failed",
                ) from error

    def _verify_append(
        self,
        record: StorageObservabilityRecord
        | StorageObservabilityRecordV1
        | StorageObservabilityRecordV3,
    ) -> None:
        """Re-read the journal and require one exact matching terminal record."""
        expected = record.to_json_dict()
        matches = 0
        try:
            with self.artifact_path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise StorageObservabilityError(
                            "persisted artifact contains an invalid line",
                            reason_code="artifact_invalid",
                        ) from error
                    if not isinstance(payload, dict):
                        raise StorageObservabilityError(
                            "persisted artifact line is not an object",
                            reason_code="artifact_invalid",
                        )
                    if payload.get("attempt_id") != str(record.attempt_id):
                        continue
                    matches += 1
                    if payload != expected:
                        raise StorageObservabilityError(
                            "persisted attempt differs from its terminal candidate",
                            reason_code="artifact_invalid",
                        )
        except (OSError, UnicodeError) as error:
            raise StorageObservabilityError(
                "observability artifact could not be verified",
                reason_code="artifact_unreadable",
            ) from error
        if matches != 1:
            raise StorageObservabilityError(
                "persisted artifact does not verify the terminal record",
                reason_code="artifact_invalid",
            )

    def _artifact_fingerprint(self) -> tuple[int, int, int, int] | None:
        """Return a cheap identity for reuse of this collector's validated state."""
        try:
            stat = self.artifact_path.stat()
        except FileNotFoundError:
            return None
        except NotADirectoryError:
            return None
        except OSError as error:
            raise StorageObservabilityError(
                "observability artifact is unreadable", reason_code="artifact_unreadable"
            ) from error
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def _load_state(self) -> StorageObservabilityState:
        """Load the bounded artifact without creating a missing file."""
        path = self.artifact_path
        if not path.exists():
            return StorageObservabilityState()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise StorageObservabilityError(
                "observability artifact is unreadable", reason_code="artifact_unreadable"
            ) from error
        try:
            return parse_storage_observability_state(text)
        except (StorageObservabilityError, ValueError) as error:
            raise StorageObservabilityError(
                "observability artifact is invalid", reason_code="artifact_invalid"
            ) from error

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise StorageObservabilityError("observability clock must be timezone-aware")
        return value.astimezone(UTC)


def _daily_snapshot(
    utc_date: date,
    records: tuple[
        StorageObservabilityRecord | StorageObservabilityRecordV1 | StorageObservabilityRecordV3,
        ...,
    ],
) -> StorageObservabilityDailySnapshotV2:
    """Fold one closed day while retaining unknowns and measurement coverage."""
    summaries: list[StorageObservabilityDailyJobSummaryV2] = []
    for job_id in sorted({item.job_id for item in records}):
        daily = tuple(item for item in records if item.job_id == job_id)
        complete = sum(
            not isinstance(item, StorageObservabilityRecordV3)
            or item.measurement_state == "complete"
            for item in daily
        )
        partial = sum(
            isinstance(item, StorageObservabilityRecordV3) and item.measurement_state == "partial"
            for item in daily
        )
        unavailable = sum(
            isinstance(item, StorageObservabilityRecordV3)
            and item.measurement_state == "unavailable"
            for item in daily
        )
        summaries.append(
            StorageObservabilityDailyJobSummaryV2(
                job_id=job_id,
                attempt_count=len(daily),
                attempts_with_evidence=sum(item.rows_created is not None for item in daily),
                measurement_complete_attempts=complete,
                measurement_partial_attempts=partial,
                measurement_unavailable_attempts=unavailable,
                failure_summaries=_failure_summaries(daily),
                database_bytes_delta=_sum_if_known(
                    tuple(item.database_delta_bytes for item in daily)
                ),
                wal_bytes_delta=_sum_if_known(tuple(item.wal_delta_bytes for item in daily)),
                rows_created=_sum_if_known(tuple(item.rows_created for item in daily)),
                rows_reused=_sum_if_known(tuple(item.rows_reused for item in daily)),
                total_ms=_sum_if_known(
                    tuple(
                        item.durations.total_ms
                        if not isinstance(item, StorageObservabilityRecordV3)
                        or item.durations is not None
                        else None
                        for item in daily
                    )
                ),
            )
        )
    return StorageObservabilityDailySnapshotV2(
        utc_date=utc_date,
        record_count=len(records),
        measurement_complete_attempts=sum(item.measurement_complete_attempts for item in summaries),
        measurement_partial_attempts=sum(item.measurement_partial_attempts for item in summaries),
        measurement_unavailable_attempts=sum(
            item.measurement_unavailable_attempts for item in summaries
        ),
        failure_summaries=_failure_summaries(records),
        job_summaries=tuple(summaries),
    )


def _sum_if_known(values: tuple[int | None, ...]) -> int | None:
    """Sum a magnitude only when every contributing attempt measured or reported it."""
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _failure_summaries(
    records: tuple[
        StorageObservabilityRecord | StorageObservabilityRecordV1 | StorageObservabilityRecordV3,
        ...,
    ],
) -> tuple[StorageObservabilityFailureSummary, ...]:
    counts: dict[tuple[str, str], int] = {}
    for item in records:
        if not isinstance(item, StorageObservabilityRecordV3):
            continue
        if item.failure_phase is None or item.failure_reason is None:
            continue
        key = (item.failure_phase, item.failure_reason)
        counts[key] = counts.get(key, 0) + 1
    return tuple(
        StorageObservabilityFailureSummary(phase=phase, reason=reason, attempt_count=count)
        for (phase, reason), count in sorted(counts.items())
    )


__all__ = [
    "ScheduledJobObservation",
    "StorageObservabilityCollector",
    "StorageObservabilityDailyJobSummary",
    "StorageObservabilityDailyJobSummaryV2",
    "StorageObservabilityDailySnapshot",
    "StorageObservabilityDailySnapshotV2",
    "StorageObservabilityDurations",
    "StorageObservabilityDurationsV1",
    "StorageObservabilityDurationsV2",
    "StorageObservabilityDurationsV3",
    "StorageObservabilityError",
    "StorageObservabilityFailurePhase",
    "StorageObservabilityFailureReason",
    "StorageObservabilityFailureSummary",
    "StorageObservabilityGrowthClassification",
    "StorageObservabilityRecord",
    "StorageObservabilityRecordV1",
    "StorageObservabilityRecordV2",
    "StorageObservabilityRecordV3",
    "StorageObservabilityState",
    "StorageObservabilityTableBytes",
    "StorageObservabilityTableRows",
    "StorageObservationHandle",
    "parse_storage_observability_state",
    "storage_observability_artifact_path",
]
