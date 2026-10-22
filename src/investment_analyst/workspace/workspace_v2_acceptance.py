"""Read-only, evidence-bound DATA-CHASSIS acceptance; never workflow authority."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CodeSHA = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
ObjectiveID = Literal["DC-Q1", "DC-Q2", "DC-Q3", "DC-Q4", "DC-Q5", "DC-Q6", "DC-Q7"]
Status = Literal["MET", "NOT_MET", "UNKNOWN"]
OBJECTIVES: tuple[ObjectiveID, ...] = (
    "DC-Q1",
    "DC-Q2",
    "DC-Q3",
    "DC-Q4",
    "DC-Q5",
    "DC-Q6",
    "DC-Q7",
)
REQUIRED_GATES = (
    "full_incremental_decimal",
    "revision_invalidation",
    "pit_isolation",
    "bounded_pages",
    "features_explanation",
    "f_core_operational_equivalence",
    "f_recovery",
    "compatible_rollback",
    "immutable_drive",
    "runtime_identity",
    "operational_continuity",
    "retirement_coverage",
)


class FinalAcceptanceError(RuntimeError):
    """Sanitized evidence error, with no provider values or credentials."""


class _Model(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CompatibleRecoveryReadback(_Model):
    """Bounded reader smoke over a byte-verified complete recovered F photo."""

    operation_id: UUID
    workspace_id: UUID
    restored_root: Path
    manifest_sha256: Digest
    recovery_receipt_sha256: Digest
    reader_sha: CodeSHA
    reader_module_path: Path
    raw_count: int = Field(ge=0)
    observation_count: int = Field(ge=0)
    metric_count: int = Field(ge=0)
    diagnostic_count: int = Field(ge=0)
    raw_sample_sha256: Digest
    observation_sample_sha256: Digest
    metric_sample_sha256: Digest
    diagnostic_sample_sha256: Digest
    sample_limit: Literal[8] = 8
    checked_at: UTCDateTime

    @model_validator(mode="after")
    def absolute_reader_paths(self) -> CompatibleRecoveryReadback:
        if not self.restored_root.is_absolute() or not self.reader_module_path.is_absolute():
            raise ValueError("recovery readback paths must be absolute")
        return self


class AcceptanceReference(_Model):
    """Exact immutable local artifact; historical SHA and environment remain explicit."""

    path: Path
    sha256: Digest
    code_sha: CodeSHA
    environment: NonEmptyStr

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("acceptance evidence path must be absolute")
        return value


class AcceptanceScope(_Model):
    """Comparable inventory, workload and provider checks, independent of timing."""

    inventory_sha256: Digest
    inputs_sha256: Digest
    results_sha256: Digest
    algorithm_versions_sha256: Digest
    provider_checks_sha256: Digest
    universe: tuple[NonEmptyStr, ...] = Field(min_length=1)
    workload: NonEmptyStr


class ByteComparison(_Model):
    family: NonEmptyStr
    baseline_bytes: int = Field(gt=0)
    current_bytes: int = Field(ge=0)
    baseline_rows: int = Field(ge=0)
    current_rows: int = Field(ge=0)
    baseline_scope: AcceptanceScope
    current_scope: AcceptanceScope
    representation: Literal["active_workspace", "payload_and_references", "funding_with_lineage"]
    shared_lineage_counted_once: Literal[True]
    evidence: tuple[AcceptanceReference, ...] = Field(min_length=1)

    @property
    def comparable(self) -> bool:
        return self.baseline_scope == self.current_scope and self.baseline_rows == self.current_rows


class DurationComparison(_Model):
    family: NonEmptyStr
    asset_id: NonEmptyStr
    baseline_scope: AcceptanceScope
    current_scope: AcceptanceScope
    baseline_execution_ms: tuple[int, ...] = Field(min_length=1)
    current_execution_ms: tuple[int, ...] = Field(min_length=1)
    collector_excluded: Literal[True]
    evidence: tuple[AcceptanceReference, ...] = Field(min_length=1)

    @field_validator("baseline_execution_ms", "current_execution_ms")
    @classmethod
    def positive_durations(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(value <= 0 for value in values):
            raise ValueError("execution duration must be positive")
        return values


class ClosedOrdinaryDay(_Model):
    utc_date: date
    database_bytes_delta: int
    wal_bytes_delta: int
    scope: AcceptanceScope
    ordinary: Literal[True]
    evidence: AcceptanceReference


class OrdinaryBatchBoundary(_Model):
    """Observed DB/WAL sizes at each real attempt boundary, including gaps."""

    attempt_id: UUID
    job_pair: tuple[NonEmptyStr, NonEmptyStr]
    edge: Literal["before", "after"]
    observed_at: UTCDateTime
    database_bytes: int = Field(ge=0)
    wal_bytes: int = Field(ge=0)


class OrdinaryBatchGrowth(_Model):
    scenario: Literal["ordinary", "same_inputs"]
    started_at: UTCDateTime
    completed_at: UTCDateTime
    runtime_sha: CodeSHA
    workspace_id: UUID
    scope: AcceptanceScope
    job_pairs: tuple[tuple[NonEmptyStr, NonEmptyStr], ...]
    attempt_ids: tuple[UUID, ...]
    boundaries: tuple[OrdinaryBatchBoundary, ...]
    complete: bool
    exclusive_writer: bool
    evidence: tuple[AcceptanceReference, ...] = Field(min_length=1)

    @property
    def positive_growth_bytes(self) -> int:
        # No decrease in either file cancels a positive allocation elsewhere,
        # including checkpoint/compaction between two successive attempts.
        return sum(
            max(0, after.database_bytes - before.database_bytes)
            + max(0, after.wal_bytes - before.wal_bytes)
            for before, after in zip(self.boundaries, self.boundaries[1:], strict=False)
        )

    def covers(self, pairs: tuple[tuple[str, str], ...]) -> bool:
        if (
            not self.complete
            or not self.exclusive_writer
            or self.completed_at <= self.started_at
            or len(self.job_pairs) != len(set(self.job_pairs))
            or set(self.job_pairs) != set(pairs)
            or not self.attempt_ids
            or len(self.attempt_ids) != len(set(self.attempt_ids))
            or len(self.boundaries) != 2 * len(self.attempt_ids)
            or self.boundaries[0].observed_at != self.started_at
            or self.boundaries[-1].observed_at != self.completed_at
            or any(
                after.observed_at <= before.observed_at
                for before, after in zip(self.boundaries, self.boundaries[1:], strict=False)
            )
        ):
            return False
        covered = set()
        for index, attempt in enumerate(self.attempt_ids):
            before, after = self.boundaries[2 * index : 2 * index + 2]
            if (
                before.attempt_id != attempt
                or after.attempt_id != attempt
                or before.edge != "before"
                or after.edge != "after"
                or before.job_pair != after.job_pair
                or before.job_pair not in self.job_pairs
            ):
                return False
            covered.add(before.job_pair)
        return covered == set(pairs)


class IdempotenceComparison(_Model):
    family: NonEmptyStr
    asset_id: NonEmptyStr
    baseline_scope: AcceptanceScope
    current_scope: AcceptanceScope
    before_count: int = Field(ge=0)
    after_count: int = Field(ge=0)
    distinct_execution_clocks: Literal[True]
    evidence: tuple[AcceptanceReference, ...] = Field(min_length=1)


class MemoryScenario(_Model):
    scenario: NonEmptyStr
    runtime_sha: CodeSHA
    pid: int = Field(gt=0)
    process_start_ticks: int = Field(gt=0)
    boot_id: UUID
    cgroup: NonEmptyStr
    started_at: UTCDateTime
    completed_at: UTCDateTime
    memory_high_bytes: int = Field(gt=0)
    scenario_peak_bytes: int = Field(ge=0)
    oom_delta: int = Field(ge=0)
    oom_kill_delta: int = Field(ge=0)
    max_delta: int = Field(ge=0)
    complete_scenario_capture: Literal[True]
    evidence: tuple[AcceptanceReference, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_interval(self) -> MemoryScenario:
        if self.completed_at <= self.started_at:
            raise ValueError("memory scenario interval is empty")
        return self


class AcceptanceGate(_Model):
    gate: NonEmptyStr
    status: Status
    reason: NonEmptyStr
    evidence: tuple[AcceptanceReference, ...] = ()

    @model_validator(mode="after")
    def require_evidence(self) -> AcceptanceGate:
        if self.status != "UNKNOWN" and not self.evidence:
            raise ValueError("measured gate requires bound evidence")
        return self


class PhysicalByteCategories(_Model):
    """Distinct quantities; Linux allocation never implies C: reclamation."""

    active_logical_bytes: int = Field(ge=0)
    database_bytes: int = Field(ge=0)
    wal_bytes: int = Field(ge=0)
    raw_bytes: int = Field(ge=0)
    parquet_bytes: int = Field(ge=0)
    linux_allocated_bytes: int = Field(ge=0)
    legacy_bytes: int = Field(ge=0)
    local_backup_bytes: int = Field(ge=0)
    controls_bytes: int = Field(ge=0)
    spill_bytes: int = Field(ge=0)
    host_c_free_bytes: int = Field(ge=0)
    measured_at: UTCDateTime


class FinalAcceptanceInput(_Model):
    base_sha: CodeSHA
    code_sha: CodeSHA
    runtime_sha: CodeSHA
    environment: NonEmptyStr
    data_scope: Literal["permanent", "fixture", "ci"]
    workspace_root: Path
    workspace_id: UUID
    operation_id: UUID
    reported_at: UTCDateTime
    activated_at: UTCDateTime | None = None
    scope: AcceptanceScope
    required_families: tuple[NonEmptyStr, ...] = Field(min_length=1)
    required_job_pairs: tuple[tuple[NonEmptyStr, NonEmptyStr], ...] = Field(min_length=1)
    required_memory_scenarios: tuple[NonEmptyStr, ...] = Field(min_length=1)
    bytes: PhysicalByteCategories
    workspace_comparison: ByteComparison | None = None
    payload_comparisons: tuple[ByteComparison, ...] = ()
    funding_comparison: ByteComparison | None = None
    q4_policy: Literal["seven-closed-utc-days-v1", "ordinary-batch-growth-v1"] = (
        "seven-closed-utc-days-v1"
    )
    ordinary_batches: tuple[OrdinaryBatchGrowth, ...] = ()
    ordinary_days: tuple[ClosedOrdinaryDay, ...] = ()
    durations: tuple[DurationComparison, ...] = ()
    idempotence: tuple[IdempotenceComparison, ...] = ()
    memory: tuple[MemoryScenario, ...] = ()
    gates: tuple[AcceptanceGate, ...]
    required_gates: tuple[NonEmptyStr, ...] = Field(min_length=1)
    receipts: tuple[AcceptanceReference, ...]

    @model_validator(mode="after")
    def unique_bindings(self) -> FinalAcceptanceInput:
        if not self.workspace_root.is_absolute():
            raise ValueError("acceptance workspace root must be absolute")
        if self.activated_at is not None and self.activated_at > self.reported_at:
            raise ValueError("activation cannot be in the future")
        groups = (
            self.required_families,
            self.required_job_pairs,
            self.required_memory_scenarios,
            self.required_gates,
            tuple(item.family for item in self.payload_comparisons),
            tuple((item.family, item.asset_id) for item in self.durations),
            tuple((item.family, item.asset_id) for item in self.idempotence),
            tuple(item.utc_date for item in self.ordinary_days),
            tuple(item.scenario for item in self.memory),
            tuple(item.gate for item in self.gates),
        )
        if any(len(set(group)) != len(group) for group in groups):
            raise ValueError("acceptance coverage contains duplicate members")
        if set(self.required_gates) != set(REQUIRED_GATES):
            raise ValueError("mandatory non-numeric gates cannot be reduced")
        return self


class ObjectiveResult(_Model):
    objective_id: ObjectiveID
    status: Status
    reason: NonEmptyStr
    comparisons: tuple[NonEmptyStr, ...] = ()


class FinalAcceptanceReport(_Model):
    schema_version: Literal[
        "workspace-v2-final-acceptance-v1", "workspace-v2-final-acceptance-v2"
    ] = "workspace-v2-final-acceptance-v1"
    q4_policy: Literal["seven-closed-utc-days-v1", "ordinary-batch-growth-v1"] = (
        "seven-closed-utc-days-v1"
    )
    q4_unit: Literal["B/day", "B/batch"] = "B/day"
    evaluated_at: UTCDateTime
    measurements: FinalAcceptanceInput
    objectives: tuple[ObjectiveResult, ...]
    met: int = Field(ge=0, le=7)
    not_met: int = Field(ge=0, le=7)
    unknown: int = Field(ge=0, le=7)
    accepted: bool


def _median(values: tuple[int, ...]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return Decimal(ordered[middle])
    return Decimal(ordered[middle - 1] + ordered[middle]) / 2


def _reference_tree(value: ContractModel) -> tuple[AcceptanceReference, ...]:
    """Walk only bounded report models, never a financial inventory."""
    found: dict[tuple[Path, str], AcceptanceReference] = {}

    def visit(item: object) -> None:
        if isinstance(item, AcceptanceReference):
            found[(item.path, item.sha256)] = item
        elif isinstance(item, ContractModel):
            for name in type(item).model_fields:
                visit(getattr(item, name))
        elif isinstance(item, tuple):
            for child in item:
                visit(child)

    visit(value)
    return tuple(found.values())


def build_final_acceptance(
    measurements: FinalAcceptanceInput, *, clock: Callable[[], datetime] | None = None
) -> FinalAcceptanceReport:
    """Verify referenced bytes, then evaluate fixed Q1–Q7 without workspace writes."""
    measurements = FinalAcceptanceInput.model_validate(measurements.model_dump())
    evaluated_at = clock() if clock is not None else datetime.now(UTC)
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise FinalAcceptanceError("acceptance_clock_requires_timezone")
    evaluated_at = evaluated_at.astimezone(UTC)
    for reference in _reference_tree(measurements):
        if reference.path.is_symlink() or not reference.path.is_file():
            raise FinalAcceptanceError("evidence_path_invalid")
        digest = hashlib.sha256()
        with reference.path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        if digest.hexdigest() != reference.sha256:
            raise FinalAcceptanceError("evidence_digest_mismatch")

    results: list[ObjectiveResult] = []

    def record(
        identifier: ObjectiveID, passed: bool | None, reason: str, comparisons: tuple[str, ...] = ()
    ) -> None:
        results.append(
            ObjectiveResult(
                objective_id=identifier,
                status="UNKNOWN" if passed is None else "MET" if passed else "NOT_MET",
                reason=reason,
                comparisons=comparisons,
            )
        )

    comparison = measurements.workspace_comparison
    valid = (
        comparison is not None
        and comparison.comparable
        and comparison.representation == "active_workspace"
        and comparison.current_scope == measurements.scope
        and comparison.current_bytes == measurements.bytes.active_logical_bytes
        and measurements.activated_at is not None
    )
    record(
        "DC-Q1",
        None
        if not valid or comparison is None
        else (
            comparison.current_bytes <= 3_000_000_000
            or comparison.current_bytes * 100 <= comparison.baseline_bytes * 45
        ),
        "Active logical workspace: <=3000000000 B or >=55% paired reduction",
    )

    payloads = measurements.payload_comparisons
    valid = set(item.family for item in payloads) == set(measurements.required_families) and all(
        item.comparable
        and item.representation == "payload_and_references"
        and item.current_scope == measurements.scope
        for item in payloads
    )
    record(
        "DC-Q2",
        None
        if not valid
        else all(item.current_bytes * 10 <= item.baseline_bytes for item in payloads),
        "UTF-8 payload and persisted references: >=90% per family; shared lineage counted once",
    )

    comparison = measurements.funding_comparison
    valid = (
        comparison is not None
        and comparison.comparable
        and comparison.representation == "funding_with_lineage"
        and comparison.current_scope == measurements.scope
    )
    record(
        "DC-Q3",
        None
        if not valid or comparison is None
        else comparison.current_bytes * 10 <= comparison.baseline_bytes,
        "Persisted funding and recoverable lineage: >=90% paired reduction",
    )

    if measurements.q4_policy == "ordinary-batch-growth-v1":
        batches = measurements.ordinary_batches
        valid = (
            measurements.activated_at is not None
            and measurements.reported_at <= evaluated_at
            and len(batches) == 2
            and tuple(batch.scenario for batch in batches) == ("ordinary", "same_inputs")
            and all(
                batch.covers(measurements.required_job_pairs)
                and batch.runtime_sha == measurements.runtime_sha
                and batch.workspace_id == measurements.workspace_id
                and batch.scope == measurements.scope
                and measurements.activated_at < batch.started_at
                and batch.completed_at <= measurements.reported_at
                for batch in batches
            )
            and batches[0].completed_at < batches[1].started_at
            and not set(batches[0].attempt_ids).intersection(batches[1].attempt_ids)
            and set((item.family, item.asset_id) for item in measurements.idempotence)
            == set(measurements.required_job_pairs)
            and all(
                item.baseline_scope == item.current_scope == measurements.scope
                for item in measurements.idempotence
            )
        )
        record(
            "DC-Q4",
            None
            if not valid
            else all(batch.positive_growth_bytes <= 30_000_000 for batch in batches)
            and all(item.before_count == item.after_count for item in measurements.idempotence),
            "ordinary-batch-growth-v1: positive DB+WAL growth <=30000000 B/batch; "
            "ordinary and same_inputs; does not establish B/day",
            tuple(f"{batch.scenario}: {batch.positive_growth_bytes} B/batch" for batch in batches),
        )
    else:
        first = (
            None
            if measurements.activated_at is None
            else measurements.activated_at.date() + timedelta(days=1)
        )
        expected = () if first is None else tuple(first + timedelta(days=i) for i in range(7))
        days = {item.utc_date: item for item in measurements.ordinary_days}
        valid = (
            bool(expected)
            and expected[-1] < measurements.reported_at.date()
            and expected[-1] < evaluated_at.date()
            and all(day in days for day in expected)
            and all(
                days[day].scope.universe == measurements.scope.universe
                and days[day].scope.workload == measurements.scope.workload
                for day in expected
            )
        )
        record(
            "DC-Q4",
            None
            if not valid
            else all(
                days[day].database_bytes_delta + days[day].wal_bytes_delta <= 30_000_000
                for day in expected
            ),
            "Seven complete ordinary UTC days after activation; <=30000000 B/day",
            tuple(day.isoformat() for day in expected if day not in days),
        )

    durations = measurements.durations
    valid = (
        set((item.family, item.asset_id) for item in durations)
        == set(measurements.required_job_pairs)
        and all(item.baseline_scope == item.current_scope for item in durations)
        and measurements.activated_at is not None
    )
    summaries = tuple(
        f"{item.family}/{item.asset_id}: baseline median={_median(item.baseline_execution_ms)} ms "
        f"range={max(item.baseline_execution_ms) - min(item.baseline_execution_ms)} ms; "
        f"current median={_median(item.current_execution_ms)} ms "
        f"range={max(item.current_execution_ms) - min(item.current_execution_ms)} ms"
        for item in durations
    )
    record(
        "DC-Q5",
        None
        if not valid
        else all(
            _median(item.current_execution_ms) * 5 <= _median(item.baseline_execution_ms)
            for item in durations
        ),
        "Comparable execution median reduction >=80%; collector excluded",
        summaries,
    )

    pairs = measurements.idempotence
    valid = set((item.family, item.asset_id) for item in pairs) == set(
        measurements.required_job_pairs
    ) and all(item.baseline_scope == item.current_scope for item in pairs)
    record(
        "DC-Q6",
        None if not valid else all(item.before_count == item.after_count for item in pairs),
        "Zero additional equivalent results at distinct clocks; same inputs, versions and IDs",
    )

    memory = measurements.memory
    valid = (
        set(item.scenario for item in memory) == set(measurements.required_memory_scenarios)
        and all(item.runtime_sha == measurements.runtime_sha for item in memory)
        and measurements.activated_at is not None
    )
    record(
        "DC-Q7",
        None
        if not valid
        else all(
            item.scenario_peak_bytes <= 9 * item.memory_high_bytes // 10
            and item.oom_delta == item.oom_kill_delta == item.max_delta == 0
            for item in memory
        ),
        "Complete cgroup scenario peak <=floor(9*MemoryHigh/10); oom/oom_kill/max deltas zero",
    )

    if measurements.data_scope != "permanent":
        results = [
            item.model_copy(
                update={
                    "status": "UNKNOWN",
                    "reason": "Fixture/CI evidence does not establish operational acceptance",
                }
            )
            for item in results
        ]
    met = sum(item.status == "MET" for item in results)
    not_met = sum(item.status == "NOT_MET" for item in results)
    gates = {item.gate: item for item in measurements.gates}
    accepted = (
        met == 7
        and set(gates) == set(measurements.required_gates)
        and all(item.status == "MET" for item in gates.values())
    )
    finite = measurements.q4_policy == "ordinary-batch-growth-v1"
    return FinalAcceptanceReport(
        schema_version=(
            "workspace-v2-final-acceptance-v2" if finite else "workspace-v2-final-acceptance-v1"
        ),
        q4_policy=measurements.q4_policy,
        q4_unit="B/batch" if finite else "B/day",
        evaluated_at=evaluated_at,
        measurements=measurements,
        objectives=tuple(results),
        met=met,
        not_met=not_met,
        unknown=7 - met - not_met,
        accepted=accepted,
    )
