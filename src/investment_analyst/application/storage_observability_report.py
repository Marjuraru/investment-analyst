"""Deterministic read-only report over the persisted storage observability artifact.

The report turns the per-attempt facts the collector already persisted into a readable answer:
two windows of 7 and 30 days over the closed daily snapshots, the budget comparison between the
measured daily growth and a configurable threshold, and an explicit declaration of every day the
window does not cover.

It is operational, not analytical: it carries no ``available_at``, it never enters a point-in-time
query, and no metric, diagnostic, candidate or alert may read it. It opens no engine at all, write
or read-only; it only reads the bounded JSONL artifact under the declared state root. A day with
no persisted daily snapshot is reported as missing and never interpolated, filled with zero or
averaged over, and the open day is declared as unfolded until it closes and folds.

Two executions over the same artifact produce byte-identical output: the report carries no clock,
no path and no ordering that depends on the current working directory. The window ends at the
latest closed day already persisted, or at the explicitly requested date.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.application.storage_observability import (
    StorageObservabilityDailySnapshot,
    StorageObservabilityError,
    StorageObservabilityState,
    parse_storage_observability_state,
    storage_observability_artifact_path,
)
from investment_analyst.core.models.base import ContractModel

_REPORT_WINDOW_DAYS = (7, 30)
DEFAULT_BUDGET_BYTES_PER_DAY = 30_000_000


class StorageObservabilityReportError(RuntimeError):
    """Carry one sanitized report failure into the caller boundary."""


class StorageObservabilityReportDay(ContractModel):
    """Measured facts of one closed UTC day inside a window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    utc_date: date
    job_count: int = Field(ge=1)
    attempts: int = Field(ge=1)
    database_bytes_delta: int
    wal_bytes_delta: int
    rows_created: int = Field(ge=0)
    rows_reused: int = Field(ge=0)
    total_ms: int = Field(ge=0)

    @property
    def total_bytes_delta(self) -> int:
        """Return the measured physical growth of the day."""
        return self.database_bytes_delta + self.wal_bytes_delta

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit JSON primitives for the report."""
        return {
            "utc_date": self.utc_date.isoformat(),
            "job_count": self.job_count,
            "attempts": self.attempts,
            "database_bytes_delta": self.database_bytes_delta,
            "wal_bytes_delta": self.wal_bytes_delta,
            "rows_created": self.rows_created,
            "rows_reused": self.rows_reused,
            "total_ms": self.total_ms,
        }


class StorageObservabilityReportWindow(ContractModel):
    """One inclusive window of closed days, declaring every day it does not cover."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    window_days: int = Field(ge=1)
    start_date: date
    end_date: date
    declared_days: tuple[date, ...]
    missing_days: tuple[date, ...]
    days: tuple[StorageObservabilityReportDay, ...]
    attempts: int = Field(ge=0)
    database_bytes_delta: int
    wal_bytes_delta: int
    rows_created: int = Field(ge=0)
    rows_reused: int = Field(ge=0)
    total_ms: int = Field(ge=0)
    mean_daily_bytes_delta: int | None = None

    @model_validator(mode="after")
    def validate_window(self) -> StorageObservabilityReportWindow:
        """Require the window to partition its days and to reconcile with them."""
        if self.end_date - self.start_date != timedelta(days=self.window_days - 1):
            raise ValueError("window span must match the declared start and end")
        covered = tuple(
            self.start_date + timedelta(days=offset) for offset in range(self.window_days)
        )
        declared = self.declared_days + self.missing_days
        if len(declared) != self.window_days or sorted(declared) != list(covered):
            raise ValueError("declared and missing days must partition the window")
        if len(set(declared)) != len(declared):
            raise ValueError("window days must not repeat")
        if tuple(item.utc_date for item in self.days) != self.declared_days:
            raise ValueError("day measurements must match the declared days in order")
        if (self.mean_daily_bytes_delta is None) is not (not self.days):
            raise ValueError("a mean is only presented when the window has declared days")
        if self.attempts != sum(item.attempts for item in self.days):
            raise ValueError("window attempts must reconcile with its days")
        if self.rows_created != sum(item.rows_created for item in self.days):
            raise ValueError("window created rows must reconcile with its days")
        if self.rows_reused != sum(item.rows_reused for item in self.days):
            raise ValueError("window reused rows must reconcile with its days")
        if self.total_ms != sum(item.total_ms for item in self.days):
            raise ValueError("window duration must reconcile with its days")
        if self.total_bytes_delta != sum(item.total_bytes_delta for item in self.days):
            raise ValueError("window growth must reconcile with its days")
        return self

    @property
    def total_bytes_delta(self) -> int:
        """Return the measured physical growth of every declared day."""
        return self.database_bytes_delta + self.wal_bytes_delta

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit JSON primitives for the report."""
        return {
            "window_days": self.window_days,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "declared_days": [item.isoformat() for item in self.declared_days],
            "missing_days": [item.isoformat() for item in self.missing_days],
            "days": [item.to_json_dict() for item in self.days],
            "attempts": self.attempts,
            "database_bytes_delta": self.database_bytes_delta,
            "wal_bytes_delta": self.wal_bytes_delta,
            "rows_created": self.rows_created,
            "rows_reused": self.rows_reused,
            "total_ms": self.total_ms,
            "mean_daily_bytes_delta": self.mean_daily_bytes_delta,
        }


class StorageObservabilityBudgetAlert(ContractModel):
    """Operational comparison between the measured daily growth and one threshold."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    threshold_bytes_per_day: int = Field(ge=0)
    evaluated_days: int = Field(ge=0)
    exceeded_days: tuple[date, ...]
    exceeded: bool
    peak_day: date | None = None
    peak_bytes_per_day: int | None = None

    @model_validator(mode="after")
    def validate_alert(self) -> StorageObservabilityBudgetAlert:
        """Keep the alert coherent with the days that were actually measured."""
        if self.exceeded is not bool(self.exceeded_days):
            raise ValueError("budget alert must agree with its exceeded days")
        if len(self.exceeded_days) > self.evaluated_days:
            raise ValueError("exceeded days cannot outnumber the evaluated days")
        if tuple(sorted(set(self.exceeded_days))) != self.exceeded_days:
            raise ValueError("exceeded days must be unique and ordered")
        if (self.peak_day is None) is not (self.peak_bytes_per_day is None):
            raise ValueError("peak day and peak growth must be reported together")
        if (self.peak_day is None) is not (self.evaluated_days == 0):
            raise ValueError("a peak exists exactly when a day was evaluated")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit JSON primitives for the report."""
        return {
            "threshold_bytes_per_day": self.threshold_bytes_per_day,
            "evaluated_days": self.evaluated_days,
            "exceeded_days": [item.isoformat() for item in self.exceeded_days],
            "exceeded": self.exceeded,
            "peak_day": None if self.peak_day is None else self.peak_day.isoformat(),
            "peak_bytes_per_day": self.peak_bytes_per_day,
        }


class StorageObservabilityReport(ContractModel):
    """Deterministic read-only answer over one persisted observability artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["storage-observability-report-v1"] = "storage-observability-report-v1"
    artifact_present: bool
    retained_days: int = Field(ge=0)
    unfolded_record_count: int = Field(ge=0)
    anchor_date: date | None
    windows: tuple[StorageObservabilityReportWindow, ...]
    budget: StorageObservabilityBudgetAlert | None

    @model_validator(mode="after")
    def validate_report(self) -> StorageObservabilityReport:
        """Keep the windows anchored, ordered and non-overlapping in their spans."""
        spans = tuple(item.window_days for item in self.windows)
        if spans != tuple(sorted(set(spans))):
            raise ValueError("report windows must be unique and ordered by span")
        if (self.anchor_date is None) is not (not self.windows):
            raise ValueError("an anchor date exists exactly when windows can be computed")
        if any(item.end_date != self.anchor_date for item in self.windows):
            raise ValueError("every window must end at the anchor date")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit JSON primitives for the report."""
        return {
            "schema_version": self.schema_version,
            "artifact_present": self.artifact_present,
            "retained_days": self.retained_days,
            "unfolded_record_count": self.unfolded_record_count,
            "anchor_date": None if self.anchor_date is None else self.anchor_date.isoformat(),
            "windows": [item.to_json_dict() for item in self.windows],
            "budget": None if self.budget is None else self.budget.to_json_dict(),
        }


class StorageObservabilityReportService:
    """Read the persisted artifact and answer with a reproducible report."""

    def __init__(
        self,
        *,
        state_root: Path,
        budget_bytes_per_day: int = DEFAULT_BUDGET_BYTES_PER_DAY,
    ) -> None:
        if budget_bytes_per_day < 0:
            raise StorageObservabilityReportError("budget threshold must not be negative")
        self._artifact_path = storage_observability_artifact_path(state_root)
        self._budget_bytes_per_day = budget_bytes_per_day

    @property
    def artifact_path(self) -> Path:
        """Return the read-only artifact location this report reads."""
        return self._artifact_path

    def report(self, *, as_of: date | None = None) -> StorageObservabilityReport:
        """Build the deterministic report without writing anything."""
        artifact_present = self._artifact_path.is_file()
        state = self._load_state() if artifact_present else StorageObservabilityState()
        snapshots = {item.utc_date: item for item in state.daily_snapshots}
        anchor = as_of if as_of is not None else (max(snapshots) if snapshots else None)
        if anchor is None:
            return StorageObservabilityReport(
                artifact_present=artifact_present,
                retained_days=len(state.daily_snapshots),
                unfolded_record_count=len(state.records),
                anchor_date=None,
                windows=(),
                budget=None,
            )
        windows = tuple(
            _window(span, anchor=anchor, snapshots=snapshots) for span in _REPORT_WINDOW_DAYS
        )
        return StorageObservabilityReport(
            artifact_present=artifact_present,
            retained_days=len(state.daily_snapshots),
            unfolded_record_count=len(state.records),
            anchor_date=anchor,
            windows=windows,
            budget=_budget_alert(
                min(windows, key=lambda item: item.window_days),
                threshold_bytes_per_day=self._budget_bytes_per_day,
            ),
        )

    def _load_state(self) -> StorageObservabilityState:
        """Read and validate the artifact, or fail with one sanitized error."""
        try:
            text = self._artifact_path.read_text(encoding="utf-8")
            return parse_storage_observability_state(text)
        except (OSError, StorageObservabilityError, ValueError) as error:
            raise StorageObservabilityReportError(
                f"storage observability artifact is unusable: {error}"
            ) from error


def _window(
    window_days: int,
    *,
    anchor: date,
    snapshots: dict[date, StorageObservabilityDailySnapshot],
) -> StorageObservabilityReportWindow:
    """Aggregate one inclusive window, declaring every day without a closed snapshot."""
    start = anchor - timedelta(days=window_days - 1)
    covered = tuple(start + timedelta(days=offset) for offset in range(window_days))
    days = tuple(_day(day, snapshots[day]) for day in covered if day in snapshots)
    declared = tuple(item.utc_date for item in days)
    total_bytes = sum(item.total_bytes_delta for item in days)
    return StorageObservabilityReportWindow(
        window_days=window_days,
        start_date=start,
        end_date=anchor,
        declared_days=declared,
        missing_days=tuple(day for day in covered if day not in snapshots),
        days=days,
        attempts=sum(item.attempts for item in days),
        database_bytes_delta=sum(item.database_bytes_delta for item in days),
        wal_bytes_delta=sum(item.wal_bytes_delta for item in days),
        rows_created=sum(item.rows_created for item in days),
        rows_reused=sum(item.rows_reused for item in days),
        total_ms=sum(item.total_ms for item in days),
        mean_daily_bytes_delta=None if not days else total_bytes // len(days),
    )


def _day(day: date, snapshot: StorageObservabilityDailySnapshot) -> StorageObservabilityReportDay:
    """Fold one closed daily snapshot into its measured day."""
    return StorageObservabilityReportDay(
        utc_date=day,
        job_count=len(snapshot.job_summaries),
        attempts=sum(item.attempt_count for item in snapshot.job_summaries),
        database_bytes_delta=sum(item.database_bytes_delta for item in snapshot.job_summaries),
        wal_bytes_delta=sum(item.wal_bytes_delta for item in snapshot.job_summaries),
        rows_created=sum(item.rows_created for item in snapshot.job_summaries),
        rows_reused=sum(item.rows_reused for item in snapshot.job_summaries),
        total_ms=sum(item.total_ms for item in snapshot.job_summaries),
    )


def _budget_alert(
    window: StorageObservabilityReportWindow,
    *,
    threshold_bytes_per_day: int,
) -> StorageObservabilityBudgetAlert:
    """Compare the measured growth of the shortest window against one threshold."""
    exceeded = tuple(
        item.utc_date for item in window.days if item.total_bytes_delta > threshold_bytes_per_day
    )
    peak = max(window.days, key=lambda item: (item.total_bytes_delta, item.utc_date), default=None)
    return StorageObservabilityBudgetAlert(
        threshold_bytes_per_day=threshold_bytes_per_day,
        evaluated_days=len(window.days),
        exceeded_days=exceeded,
        exceeded=bool(exceeded),
        peak_day=None if peak is None else peak.utc_date,
        peak_bytes_per_day=None if peak is None else peak.total_bytes_delta,
    )


__all__ = [
    "DEFAULT_BUDGET_BYTES_PER_DAY",
    "StorageObservabilityBudgetAlert",
    "StorageObservabilityReport",
    "StorageObservabilityReportDay",
    "StorageObservabilityReportError",
    "StorageObservabilityReportService",
    "StorageObservabilityReportWindow",
]
