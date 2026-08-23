"""Read-only operational readiness over durable local evidence."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from investment_analyst.alerts.analytical_models import AnalyticalScreeningDomain
from investment_analyst.alerts.analytical_state import (
    AnalyticalMonitorReceipt,
    AnalyticalMonitorReceiptStatus,
    AnalyticalScreeningState,
    AnalyticalScreeningStateStore,
)
from investment_analyst.application.manual_operations import (
    ManualOperationStateDocument,
    ManualOperationStateStore,
    ManualOperationStatus,
)
from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduleState,
    MultiAssetScheduleStateStore,
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
    ScheduledJobDomain,
    ScheduledJobFailureCategory,
    scheduled_job_failure,
)
from investment_analyst.application.operational_alerts import (
    OperationalAlertState,
    OperationalAlertStateStore,
    OperationalRuleId,
    OperationalScreeningResult,
)
from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.core.models.base import ContractModel, UTCDateTime
from investment_analyst.workspace.models import WorkspaceInspection, WorkspacePaths
from investment_analyst.workspace.service import WorkspaceError, WorkspaceService

_SCHEDULE_STATE_FILE = "multi_asset_schedule_state_v1.json"
_OPERATIONAL_ALERT_STATE_FILE = "operational_alert_state_v1.json"
_ANALYTICAL_STATE_FILE = "analytical_screening_state_v1.json"
_MANUAL_OPERATION_STATE_FILE = "manual_operation_state_v1.json"
_DATABASE_RELATIVE_PATH = Path("data/processed/investment_analyst.duckdb")
_MAX_MIN_LOCAL_DATES = 366


class OperationalReadinessDecision(StrEnum):
    """Binary decision emitted by the readiness probe."""

    PASS = "PASS"
    NOT_READY = "NOT_READY"


class OperationalReadinessReasonCode(StrEnum):
    """Bounded versioned reasons that prevent operational readiness."""

    ACTIVE_MANUAL_OPERATION = "active_manual_operation"
    ANALYTICAL_RECEIPT_JOIN_INCOMPLETE = "analytical_receipt_join_incomplete"
    ANALYTICAL_RECEIPT_ORPHAN = "analytical_receipt_orphan"
    ANALYTICAL_RESULT_DUPLICATE_REFERENCE = "analytical_result_duplicate_reference"
    ANALYTICAL_RESULT_JOIN_INCOMPLETE = "analytical_result_join_incomplete"
    ATTEMPT_BUDGET_EXCEEDED = "attempt_budget_exceeded"
    ATTEMPT_SEQUENCE_INVALID = "attempt_sequence_invalid"
    INSUFFICIENT_LOCAL_DATES = "insufficient_local_dates"
    OPERATIONAL_SCREENING_JOIN_INCOMPLETE = "operational_screening_join_incomplete"
    OPERATIONAL_SCREENING_ORPHAN = "operational_screening_orphan"
    RETRY_BACKOFF_VIOLATION = "retry_backoff_violation"
    RETRY_NOT_ALLOWED = "retry_not_allowed"
    RUNNING_SCHEDULE_ATTEMPT = "running_schedule_attempt"
    UNKNOWN_FAILURE_CATEGORY = "unknown_failure_category"


class OperationalReadinessError(RuntimeError):
    """Safe error raised when readiness cannot produce a valid report."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OperationalReadinessInputError(OperationalReadinessError):
    """Raised for invalid explicit probe inputs."""


class OperationalReadinessStateError(OperationalReadinessError):
    """Raised when durable state is missing, malformed, or unstable."""


class OperationalReadinessParameters(ContractModel):
    """Effective immutable inputs used by one readiness decision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["operational-readiness-parameters-v1"] = (
        "operational-readiness-parameters-v1"
    )
    since: UTCDateTime
    min_local_dates: int = Field(ge=1, le=_MAX_MIN_LOCAL_DATES)

    @field_validator("since", mode="before")
    @classmethod
    def require_explicit_utc(cls, value: object) -> object:
        if not isinstance(value, datetime):
            raise ValueError("since must be a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("since must be timezone-aware")
        if value.utcoffset() != timedelta(0):
            raise ValueError("since must be explicitly UTC")
        return value.astimezone(UTC)

    @field_validator("min_local_dates", mode="before")
    @classmethod
    def reject_boolean_minimum(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("min_local_dates must be an integer")
        return value


class OperationalFailureCategoryCount(ContractModel):
    """One safe failure-category aggregate without provider messages."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    category: ScheduledJobFailureCategory
    count: int = Field(ge=1)

    @field_validator("count", mode="before")
    @classmethod
    def reject_boolean_count(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("count must be an integer")
        return value


class OperationalReadinessSummary(ContractModel):
    """Bounded aggregate of the exact evidence used by readiness."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: UUID
    workspace_format_version: int = Field(ge=1)
    raw_record_count: int = Field(ge=0)
    observation_count: int = Field(ge=0)
    metric_result_count: int = Field(ge=0)
    diagnostic_result_count: int = Field(ge=0)
    scheduler_state_present: Literal[True] = True
    operational_alert_state_present: Literal[True] = True
    analytical_state_present: Literal[True] = True
    manual_operation_state_present: bool
    terminal_attempt_count: int = Field(ge=0)
    running_attempt_count: int = Field(ge=0)
    observed_job_count: int = Field(ge=0)
    qualifying_daily_job_count: int = Field(ge=0)
    maximum_local_date_count: int = Field(ge=0)
    operational_screening_count: int = Field(ge=0)
    analytical_receipt_count: int = Field(ge=0)
    manual_operation_count: int = Field(ge=0)
    active_manual_operation_count: int = Field(ge=0)
    failure_categories: tuple[OperationalFailureCategoryCount, ...]
    evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator(
        "workspace_format_version",
        "raw_record_count",
        "observation_count",
        "metric_result_count",
        "diagnostic_result_count",
        "terminal_attempt_count",
        "running_attempt_count",
        "observed_job_count",
        "qualifying_daily_job_count",
        "maximum_local_date_count",
        "operational_screening_count",
        "analytical_receipt_count",
        "manual_operation_count",
        "active_manual_operation_count",
        mode="before",
    )
    @classmethod
    def reject_boolean_counts(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("summary counts must be integers")
        return value

    @field_validator("manual_operation_state_present", mode="before")
    @classmethod
    def require_presence_boolean(cls, value: object) -> object:
        if not isinstance(value, bool):
            raise ValueError("manual operation presence must be bool")
        return value

    @model_validator(mode="after")
    def validate_ordering(self) -> OperationalReadinessSummary:
        categories = tuple(item.category.value for item in self.failure_categories)
        if categories != tuple(sorted(set(categories))):
            raise ValueError("failure categories must be unique and sorted")
        if self.active_manual_operation_count > self.manual_operation_count:
            raise ValueError("active manual operations exceed total operations")
        return self


class OperationalReadinessReport(ContractModel):
    """Versioned deterministic operational-readiness report."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["operational-readiness-report-v1"] = "operational-readiness-report-v1"
    decision: OperationalReadinessDecision
    parameters: OperationalReadinessParameters
    summary: OperationalReadinessSummary
    reason_codes: tuple[OperationalReadinessReasonCode, ...]
    semantic_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_decision_and_fingerprint(self) -> OperationalReadinessReport:
        reason_values = tuple(item.value for item in self.reason_codes)
        if reason_values != tuple(sorted(set(reason_values))):
            raise ValueError("reason codes must be unique and sorted")
        expected_decision = (
            OperationalReadinessDecision.PASS
            if not self.reason_codes
            else OperationalReadinessDecision.NOT_READY
        )
        if self.decision is not expected_decision:
            raise ValueError("decision must match reason codes")
        if self.semantic_fingerprint != _report_fingerprint(
            self.decision,
            self.parameters,
            self.summary,
            self.reason_codes,
        ):
            raise ValueError("semantic fingerprint does not match report")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return stable compact JSON without filesystem paths or raw evidence."""
        return self.model_dump(mode="json")


@dataclass(frozen=True)
class _FileSignature:
    exists: bool
    kind: str | None
    device: int | None
    inode: int | None
    size: int | None
    modified_ns: int | None
    sha256: str | None


@dataclass(frozen=True)
class _LoadedEvidence:
    inspection: WorkspaceInspection
    schedule: MultiAssetScheduleState
    operational: OperationalAlertState
    analytical: AnalyticalScreeningState
    manual: ManualOperationStateDocument
    manual_present: bool


class OperationalReadinessService:
    """Build one fail-closed readiness report without mutating the workspace."""

    def __init__(self, workspace_service: WorkspaceService | None = None) -> None:
        self._workspace_service = workspace_service or WorkspaceService()

    def check(
        self,
        *,
        workspace: Path,
        since: datetime,
        min_local_dates: int,
    ) -> OperationalReadinessReport:
        try:
            parameters = OperationalReadinessParameters(
                since=since,
                min_local_dates=min_local_dates,
            )
        except ValidationError as error:
            raise OperationalReadinessInputError("invalid_parameters") from error
        root = self._resolve_existing_workspace(workspace)
        paths = self._workspace_service.resolve(root)
        evidence = self._load_stable_evidence(paths)
        reasons = self._reason_codes(evidence, parameters)
        summary = self._summary(evidence, parameters, reasons)
        decision = (
            OperationalReadinessDecision.PASS
            if not reasons
            else OperationalReadinessDecision.NOT_READY
        )
        fingerprint = _report_fingerprint(decision, parameters, summary, reasons)
        return OperationalReadinessReport(
            decision=decision,
            parameters=parameters,
            summary=summary,
            reason_codes=reasons,
            semantic_fingerprint=fingerprint,
        )

    @staticmethod
    def _resolve_existing_workspace(workspace: Path) -> Path:
        if not isinstance(workspace, Path):
            raise OperationalReadinessInputError("workspace_path_invalid")
        try:
            root = workspace.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise OperationalReadinessInputError("workspace_path_invalid") from error
        if not root.is_dir():
            raise OperationalReadinessInputError("workspace_path_invalid")
        return root

    def _load_stable_evidence(self, paths: WorkspacePaths) -> _LoadedEvidence:
        state_paths = self._state_paths(paths)
        for name in ("scheduler", "operational", "analytical"):
            if not state_paths[name].is_file():
                raise OperationalReadinessStateError("required_state_missing")
        monitored = self._monitored_paths(paths, state_paths)
        before = _capture_signatures(monitored)
        try:
            inspection = self._workspace_service.inspect(paths.root)
            schedule = MultiAssetScheduleStateStore(state_paths["scheduler"]).load()
            operational = OperationalAlertStateStore(state_paths["operational"]).load()
            analytical = AnalyticalScreeningStateStore(state_paths["analytical"]).load()
            manual_present = state_paths["manual"].is_file()
            manual = ManualOperationStateStore(state_paths["manual"]).load()
        except (WorkspaceError, AaplOperationalStateError, OSError, ValueError) as error:
            raise OperationalReadinessStateError("state_invalid") from error
        after = _capture_signatures(monitored)
        if before != after:
            raise OperationalReadinessStateError("snapshot_changed")
        if inspection.status != "ready" or inspection.errors:
            raise OperationalReadinessStateError("workspace_not_ready")
        return _LoadedEvidence(
            inspection=inspection,
            schedule=schedule,
            operational=operational,
            analytical=analytical,
            manual=manual,
            manual_present=manual_present,
        )

    @staticmethod
    def _state_paths(paths: WorkspacePaths) -> dict[str, Path]:
        return {
            "scheduler": paths.state_root / _SCHEDULE_STATE_FILE,
            "operational": paths.state_root / _OPERATIONAL_ALERT_STATE_FILE,
            "analytical": paths.state_root / _ANALYTICAL_STATE_FILE,
            "manual": paths.state_root / _MANUAL_OPERATION_STATE_FILE,
        }

    @staticmethod
    def _monitored_paths(
        paths: WorkspacePaths,
        state_paths: dict[str, Path],
    ) -> tuple[tuple[str, Path, bool], ...]:
        return (
            ("workspace_root", paths.root, False),
            ("manifest", paths.manifest_path, True),
            ("storage_root", paths.storage_root, False),
            ("database", paths.storage_root / _DATABASE_RELATIVE_PATH, False),
            ("raw_storage", paths.storage_root / "data/raw", False),
            ("parquet_storage", paths.storage_root / "data/exports", False),
            ("state_root", paths.state_root, False),
            ("scheduler", state_paths["scheduler"], True),
            ("operational", state_paths["operational"], True),
            ("analytical", state_paths["analytical"], True),
            ("manual", state_paths["manual"], True),
        )

    @staticmethod
    def _reason_codes(
        evidence: _LoadedEvidence,
        parameters: OperationalReadinessParameters,
    ) -> tuple[OperationalReadinessReasonCode, ...]:
        reasons: set[OperationalReadinessReasonCode] = set()
        attempts = evidence.schedule.attempts
        terminal = tuple(
            item
            for item in attempts
            if item.status is not ScheduledJobAttemptStatus.RUNNING
            and item.completed_at is not None
            and item.completed_at >= parameters.since
        )
        running = tuple(
            item
            for item in attempts
            if item.status is ScheduledJobAttemptStatus.RUNNING
            and item.started_at >= parameters.since
        )
        if running:
            reasons.add(OperationalReadinessReasonCode.RUNNING_SCHEDULE_ATTEMPT)
        active_manual = tuple(
            item
            for item in evidence.manual.operations
            if item.status in {ManualOperationStatus.QUEUED, ManualOperationStatus.RUNNING}
        )
        if active_manual:
            reasons.add(OperationalReadinessReasonCode.ACTIVE_MANUAL_OPERATION)

        dates_by_job: dict[str, set[date]] = defaultdict(set)
        for item in terminal:
            dates_by_job[item.definition.job_id].add(item.local_date)
        if not any(
            len(local_dates) >= parameters.min_local_dates for local_dates in dates_by_job.values()
        ):
            reasons.add(OperationalReadinessReasonCode.INSUFFICIENT_LOCAL_DATES)

        OperationalReadinessService._check_retry_policy(attempts, terminal, reasons)
        OperationalReadinessService._check_observer_joins(
            evidence,
            terminal,
            parameters.since,
            reasons,
        )
        return tuple(sorted(reasons, key=lambda item: item.value))

    @staticmethod
    def _check_retry_policy(
        attempts: tuple[ScheduledJobAttempt, ...],
        observed_terminal: tuple[ScheduledJobAttempt, ...],
        reasons: set[OperationalReadinessReasonCode],
    ) -> None:
        observed_groups = {(item.definition.job_id, item.local_date) for item in observed_terminal}
        groups: dict[tuple[str, date], list[ScheduledJobAttempt]] = defaultdict(list)
        for item in attempts:
            key = (item.definition.job_id, item.local_date)
            if key in observed_groups:
                groups[key].append(item)
        for items in groups.values():
            items.sort(
                key=lambda item: (
                    item.attempt_number,
                    item.started_at,
                    str(item.attempt_id),
                )
            )
            definition = items[0].definition
            numbers = tuple(item.attempt_number for item in items)
            if numbers != tuple(range(1, len(items) + 1)) or any(
                item.definition != definition for item in items
            ):
                reasons.add(OperationalReadinessReasonCode.ATTEMPT_SEQUENCE_INVALID)
            if len(items) > definition.max_attempts_per_day or any(
                item.attempt_number > item.definition.max_attempts_per_day for item in items
            ):
                reasons.add(OperationalReadinessReasonCode.ATTEMPT_BUDGET_EXCEEDED)
            for item in items:
                if item.failure is None:
                    continue
                category = item.failure.category
                if (
                    not isinstance(category, ScheduledJobFailureCategory)
                    or category is ScheduledJobFailureCategory.LEGACY_UNKNOWN
                ):
                    reasons.add(OperationalReadinessReasonCode.UNKNOWN_FAILURE_CATEGORY)
                elif (
                    item.failure.retryable
                    is not scheduled_job_failure(
                        category,
                        "readiness-policy-check",
                    ).retryable
                ):
                    reasons.add(OperationalReadinessReasonCode.RETRY_NOT_ALLOWED)
            for previous, current in zip(items, items[1:], strict=False):
                if (
                    previous.status is not ScheduledJobAttemptStatus.FAILED
                    or previous.failure is None
                    or not previous.failure.retryable
                ):
                    reasons.add(OperationalReadinessReasonCode.RETRY_NOT_ALLOWED)
                    continue
                if previous.completed_at is None or current.started_at < (
                    previous.completed_at
                    + timedelta(seconds=previous.definition.retry_backoff_seconds)
                ):
                    reasons.add(OperationalReadinessReasonCode.RETRY_BACKOFF_VIOLATION)

    @staticmethod
    def _check_observer_joins(
        evidence: _LoadedEvidence,
        observed_terminal: tuple[ScheduledJobAttempt, ...],
        since: datetime,
        reasons: set[OperationalReadinessReasonCode],
    ) -> None:
        attempts_by_id = {item.attempt_id: item for item in evidence.schedule.attempts}
        screenings_by_attempt: dict[UUID, list[object]] = defaultdict(list)
        for result in evidence.operational.screenings:
            screenings_by_attempt[result.evidence_attempt_id].append(result)
            attempt = attempts_by_id.get(result.evidence_attempt_id)
            if result.known_at >= since and (
                attempt is None or attempt.status is ScheduledJobAttemptStatus.RUNNING
            ):
                reasons.add(OperationalReadinessReasonCode.OPERATIONAL_SCREENING_ORPHAN)

        receipts_by_attempt = {item.attempt_id: item for item in evidence.analytical.receipts}
        for receipt in evidence.analytical.receipts:
            attempt = attempts_by_id.get(receipt.attempt_id)
            if receipt.processed_at >= since and (
                attempt is None or attempt.status is ScheduledJobAttemptStatus.RUNNING
            ):
                reasons.add(OperationalReadinessReasonCode.ANALYTICAL_RECEIPT_ORPHAN)

        join_ids = {item.attempt_id for item in observed_terminal}
        join_ids.update(
            item.evidence_attempt_id
            for item in evidence.operational.screenings
            if item.known_at >= since
        )
        join_ids.update(
            item.attempt_id for item in evidence.analytical.receipts if item.processed_at >= since
        )
        for attempt_id in sorted(join_ids, key=str):
            attempt = attempts_by_id.get(attempt_id)
            if attempt is None or attempt.status is ScheduledJobAttemptStatus.RUNNING:
                continue
            OperationalReadinessService._check_operational_join(
                attempt,
                tuple(screenings_by_attempt.get(attempt_id, ())),
                reasons,
            )
            receipt = receipts_by_attempt.get(attempt_id)
            if receipt is None:
                reasons.add(OperationalReadinessReasonCode.ANALYTICAL_RECEIPT_JOIN_INCOMPLETE)
            else:
                OperationalReadinessService._check_analytical_join(
                    attempt,
                    receipt,
                    evidence.analytical,
                    reasons,
                )

        references: Counter[UUID] = Counter(
            result_id
            for receipt in evidence.analytical.receipts
            for result_id in receipt.result_ids
        )
        if any(count > 1 for count in references.values()):
            reasons.add(OperationalReadinessReasonCode.ANALYTICAL_RESULT_DUPLICATE_REFERENCE)
        referenced = set(references)
        if any(
            item.computed_at >= since and item.result_id not in referenced
            for item in evidence.analytical.results
        ):
            reasons.add(OperationalReadinessReasonCode.ANALYTICAL_RESULT_JOIN_INCOMPLETE)

    @staticmethod
    def _check_operational_join(
        attempt: ScheduledJobAttempt,
        screenings: tuple[OperationalScreeningResult, ...],
        reasons: set[OperationalReadinessReasonCode],
    ) -> None:
        expected_rules = frozenset(OperationalRuleId)
        if (
            len(screenings) != len(expected_rules)
            or frozenset(getattr(item, "rule_id", None) for item in screenings) != expected_rules
        ):
            reasons.add(OperationalReadinessReasonCode.OPERATIONAL_SCREENING_JOIN_INCOMPLETE)
            return
        expected_category = attempt.failure.category if attempt.failure else None
        if any(
            item.job_id != attempt.definition.job_id
            or item.asset_id != attempt.definition.asset_id
            or item.provider != attempt.definition.provider
            or item.domain != attempt.definition.domain.value
            or item.known_at != attempt.completed_at
            or item.condition.observed_status is not attempt.status
            or item.condition.observed_category != expected_category
            for item in screenings
        ):
            reasons.add(OperationalReadinessReasonCode.OPERATIONAL_SCREENING_JOIN_INCOMPLETE)

    @staticmethod
    def _check_analytical_join(
        attempt: ScheduledJobAttempt,
        receipt: AnalyticalMonitorReceipt,
        state: AnalyticalScreeningState,
        reasons: set[OperationalReadinessReasonCode],
    ) -> None:
        if (
            receipt.job_id != attempt.definition.job_id
            or receipt.asset_id != attempt.definition.asset_id
            or attempt.completed_at is None
            or receipt.processed_at < attempt.completed_at
        ):
            reasons.add(OperationalReadinessReasonCode.ANALYTICAL_RECEIPT_JOIN_INCOMPLETE)
            return
        results_by_id = {item.result_id: item for item in state.results}
        if receipt.status is AnalyticalMonitorReceiptStatus.SKIPPED:
            if receipt.result_ids:
                reasons.add(OperationalReadinessReasonCode.ANALYTICAL_RECEIPT_JOIN_INCOMPLETE)
            if attempt.status is not ScheduledJobAttemptStatus.SUCCEEDED and (
                receipt.reason != f"attempt_{attempt.status.value}"
            ):
                reasons.add(OperationalReadinessReasonCode.ANALYTICAL_RECEIPT_JOIN_INCOMPLETE)
            return
        if (
            attempt.status is not ScheduledJobAttemptStatus.SUCCEEDED
            or attempt.execution is None
            or not attempt.execution.coverage_complete
            or not attempt.execution.evidence_changed
            or not receipt.result_ids
        ):
            reasons.add(OperationalReadinessReasonCode.ANALYTICAL_RECEIPT_JOIN_INCOMPLETE)
            return
        expected_domain = _analytical_domain(attempt.definition.domain)
        joined = tuple(results_by_id.get(result_id) for result_id in receipt.result_ids)
        if expected_domain is None or any(item is None for item in joined):
            reasons.add(OperationalReadinessReasonCode.ANALYTICAL_RESULT_JOIN_INCOMPLETE)
            return
        expected_result_ids = {
            item.result_id
            for item in state.results
            if item.asset_id == attempt.definition.asset_id
            and item.rule.domain is expected_domain
            and item.known_at == attempt.execution.effective_known_at
            and item.source_id in attempt.execution.source_ids
            and item.computed_at == receipt.processed_at
        }
        if set(receipt.result_ids) != expected_result_ids:
            reasons.add(OperationalReadinessReasonCode.ANALYTICAL_RESULT_JOIN_INCOMPLETE)
        if any(
            item.asset_id != attempt.definition.asset_id
            or item.rule.domain is not expected_domain
            or item.known_at != attempt.execution.effective_known_at
            or item.source_id not in attempt.execution.source_ids
            or item.computed_at != receipt.processed_at
            or not item.traceability_verified
            for item in joined
            if item is not None
        ):
            reasons.add(OperationalReadinessReasonCode.ANALYTICAL_RESULT_JOIN_INCOMPLETE)

    @staticmethod
    def _summary(
        evidence: _LoadedEvidence,
        parameters: OperationalReadinessParameters,
        reasons: tuple[OperationalReadinessReasonCode, ...],
    ) -> OperationalReadinessSummary:
        del reasons
        terminal = tuple(
            item
            for item in evidence.schedule.attempts
            if item.status is not ScheduledJobAttemptStatus.RUNNING
            and item.completed_at is not None
            and item.completed_at >= parameters.since
        )
        running = tuple(
            item
            for item in evidence.schedule.attempts
            if item.status is ScheduledJobAttemptStatus.RUNNING
            and item.started_at >= parameters.since
        )
        dates_by_job: dict[str, set[date]] = defaultdict(set)
        for item in terminal:
            dates_by_job[item.definition.job_id].add(item.local_date)
        active_manual = tuple(
            item
            for item in evidence.manual.operations
            if item.status in {ManualOperationStatus.QUEUED, ManualOperationStatus.RUNNING}
        )
        category_counts: Counter[ScheduledJobFailureCategory] = Counter()
        for item in terminal:
            if item.failure is not None:
                category_counts[item.failure.safe_category] += 1
        joined_ids = {item.attempt_id for item in terminal}
        screening_count = sum(
            item.evidence_attempt_id in joined_ids for item in evidence.operational.screenings
        )
        receipt_count = sum(item.attempt_id in joined_ids for item in evidence.analytical.receipts)
        evidence_fingerprint = _evidence_fingerprint(evidence, terminal, parameters)
        inspection = evidence.inspection
        return OperationalReadinessSummary(
            workspace_id=inspection.workspace_id,
            workspace_format_version=inspection.format_version,
            raw_record_count=inspection.raw_record_count,
            observation_count=inspection.observation_count,
            metric_result_count=inspection.metric_result_count,
            diagnostic_result_count=inspection.diagnostic_result_count,
            manual_operation_state_present=evidence.manual_present,
            terminal_attempt_count=len(terminal),
            running_attempt_count=len(running),
            observed_job_count=len(dates_by_job),
            qualifying_daily_job_count=sum(
                len(local_dates) >= parameters.min_local_dates
                for local_dates in dates_by_job.values()
            ),
            maximum_local_date_count=max(
                (len(value) for value in dates_by_job.values()),
                default=0,
            ),
            operational_screening_count=screening_count,
            analytical_receipt_count=receipt_count,
            manual_operation_count=len(evidence.manual.operations),
            active_manual_operation_count=len(active_manual),
            failure_categories=tuple(
                OperationalFailureCategoryCount(category=category, count=count)
                for category, count in sorted(
                    category_counts.items(),
                    key=lambda item: item[0].value,
                )
            ),
            evidence_fingerprint=evidence_fingerprint,
        )


def _capture_signatures(
    monitored: tuple[tuple[str, Path, bool], ...],
) -> tuple[tuple[str, _FileSignature], ...]:
    signatures: list[tuple[str, _FileSignature]] = []
    for name, path, hash_content in monitored:
        try:
            stat = path.stat()
        except FileNotFoundError:
            signatures.append((name, _FileSignature(False, None, None, None, None, None, None)))
            continue
        except OSError as error:
            raise OperationalReadinessStateError("state_unreadable") from error
        kind = "file" if path.is_file() else "directory" if path.is_dir() else "other"
        digest: str | None = None
        if hash_content and kind == "file":
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as error:
                raise OperationalReadinessStateError("state_unreadable") from error
        signatures.append(
            (
                name,
                _FileSignature(
                    True,
                    kind,
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    digest,
                ),
            )
        )
    return tuple(signatures)


def _analytical_domain(domain: ScheduledJobDomain) -> AnalyticalScreeningDomain | None:
    if domain is ScheduledJobDomain.MARKET_DAILY:
        return AnalyticalScreeningDomain.MARKET
    if domain is ScheduledJobDomain.FUNDAMENTALS:
        return AnalyticalScreeningDomain.FUNDAMENTALS
    return None


def _safe_category(attempt: ScheduledJobAttempt) -> str | None:
    if attempt.failure is None:
        return None
    return attempt.failure.safe_category.value


def _evidence_fingerprint(
    evidence: _LoadedEvidence,
    terminal: tuple[ScheduledJobAttempt, ...],
    parameters: OperationalReadinessParameters,
) -> str:
    terminal_ids = {item.attempt_id for item in terminal}
    payload = {
        "schema_version": "operational-readiness-evidence-fingerprint-v1",
        "parameters": parameters.model_dump(mode="json"),
        "workspace": {
            "workspace_id": str(evidence.inspection.workspace_id),
            "format_version": evidence.inspection.format_version,
            "counts": {
                "raw": evidence.inspection.raw_record_count,
                "observations": evidence.inspection.observation_count,
                "metrics": evidence.inspection.metric_result_count,
                "diagnostics": evidence.inspection.diagnostic_result_count,
            },
        },
        "attempts": [
            {
                "attempt_id": str(item.attempt_id),
                "job_id": item.definition.job_id,
                "asset_id": item.definition.asset_id,
                "provider": item.definition.provider,
                "domain": item.definition.domain.value,
                "local_date": item.local_date.isoformat(),
                "scheduled_for": item.scheduled_for.isoformat(),
                "attempt_number": item.attempt_number,
                "status": item.status.value,
                "started_at": item.started_at.isoformat(),
                "completed_at": item.completed_at.isoformat() if item.completed_at else None,
                "failure_category": _safe_category(item),
                "retryable": item.failure.retryable if item.failure else None,
                "execution": (
                    {
                        "effective_known_at": item.execution.effective_known_at.isoformat(),
                        "source_ids": list(item.execution.source_ids),
                        "created_count": item.execution.created_count,
                        "reused_count": item.execution.reused_count,
                        "coverage_complete": item.execution.coverage_complete,
                    }
                    if item.execution
                    else None
                ),
            }
            for item in terminal
        ],
        "operational_screenings": [
            {
                "result_id": str(item.result_id),
                "rule_id": item.rule_id.value,
                "attempt_id": str(item.evidence_attempt_id),
                "known_at": item.known_at.isoformat(),
                "state": item.condition.state.value,
                "activated": item.activated,
            }
            for item in evidence.operational.screenings
            if item.evidence_attempt_id in terminal_ids
        ],
        "analytical_receipts": [
            {
                "attempt_id": str(item.attempt_id),
                "job_id": item.job_id,
                "asset_id": item.asset_id,
                "status": item.status.value,
                "reason": item.reason,
                "processed_at": item.processed_at.isoformat(),
                "result_ids": [str(value) for value in item.result_ids],
            }
            for item in evidence.analytical.receipts
            if item.attempt_id in terminal_ids
        ],
        "analytical_results": [
            {
                "result_id": str(item.result_id),
                "semantic_fingerprint": item.semantic_fingerprint(),
            }
            for item in evidence.analytical.results
            if any(
                item.result_id in receipt.result_ids
                for receipt in evidence.analytical.receipts
                if receipt.attempt_id in terminal_ids
            )
        ],
        "manual_operations": [
            {
                "operation_id": str(item.operation_id),
                "fingerprint": item.fingerprint,
                "status": item.status.value,
                "submitted_at": item.submitted_at.isoformat(),
                "started_at": item.started_at.isoformat() if item.started_at else None,
                "completed_at": item.completed_at.isoformat() if item.completed_at else None,
            }
            for item in evidence.manual.operations
        ],
        "manual_state_present": evidence.manual_present,
    }
    return _sha256_json(payload)


def _report_fingerprint(
    decision: OperationalReadinessDecision,
    parameters: OperationalReadinessParameters,
    summary: OperationalReadinessSummary,
    reason_codes: tuple[OperationalReadinessReasonCode, ...],
) -> str:
    return _sha256_json(
        {
            "schema_version": "operational-readiness-report-fingerprint-v1",
            "decision": decision.value,
            "parameters": parameters.model_dump(mode="json"),
            "summary": summary.model_dump(mode="json"),
            "reason_codes": [item.value for item in reason_codes],
        }
    )


def _sha256_json(payload: dict[str, object]) -> str:
    document = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(document).hexdigest()


__all__ = [
    "OperationalFailureCategoryCount",
    "OperationalReadinessDecision",
    "OperationalReadinessError",
    "OperationalReadinessInputError",
    "OperationalReadinessParameters",
    "OperationalReadinessReasonCode",
    "OperationalReadinessReport",
    "OperationalReadinessService",
    "OperationalReadinessStateError",
    "OperationalReadinessSummary",
]
