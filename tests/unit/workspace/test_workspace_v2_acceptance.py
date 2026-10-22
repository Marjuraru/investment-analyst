"""Acceptance rejects incomparable, incomplete and unbound operational evidence."""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.workspace.workspace_v2_acceptance import (
    REQUIRED_GATES,
    AcceptanceGate,
    AcceptanceReference,
    AcceptanceScope,
    ByteComparison,
    ClosedOrdinaryDay,
    DurationComparison,
    FinalAcceptanceError,
    FinalAcceptanceInput,
    IdempotenceComparison,
    MemoryScenario,
    OrdinaryBatchBoundary,
    OrdinaryBatchGrowth,
    PhysicalByteCategories,
    build_final_acceptance,
)


def _input(tmp_path: Path) -> FinalAcceptanceInput:
    artifact = tmp_path / "evidence.json"
    artifact.write_bytes(b'{"fixture":"measurement-contract"}\n')
    reference = AcceptanceReference(
        path=artifact,
        sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        code_sha="a" * 40,
        environment="unit fixture",
    )
    scope = AcceptanceScope(
        inventory_sha256="b" * 64,
        inputs_sha256="c" * 64,
        results_sha256="d" * 64,
        algorithm_versions_sha256="e" * 64,
        provider_checks_sha256="f" * 64,
        universe=("equity:us:aapl",),
        workload="ordinary daily",
    )
    activated = datetime(2026, 10, 15, 12, tzinfo=UTC)

    def comparison(representation: str) -> ByteComparison:
        return ByteComparison.model_validate(
            {
                "family": "market",
                "baseline_bytes": 1000,
                "current_bytes": 100,
                "baseline_rows": 3,
                "current_rows": 3,
                "baseline_scope": scope,
                "current_scope": scope,
                "representation": representation,
                "shared_lineage_counted_once": True,
                "evidence": (reference,),
            }
        )

    return FinalAcceptanceInput(
        base_sha="a" * 40,
        code_sha="b" * 40,
        runtime_sha="a" * 40,
        environment="unit fixture",
        data_scope="fixture",
        workspace_root=tmp_path,
        workspace_id=uuid4(),
        operation_id=uuid4(),
        reported_at=activated + timedelta(days=8),
        activated_at=activated,
        scope=scope,
        required_families=("market",),
        required_job_pairs=(("market", "equity:us:aapl"),),
        required_memory_scenarios=("ordinary_cycle",),
        required_gates=REQUIRED_GATES,
        bytes=PhysicalByteCategories(
            active_logical_bytes=100,
            database_bytes=80,
            wal_bytes=0,
            raw_bytes=20,
            parquet_bytes=0,
            linux_allocated_bytes=4096,
            legacy_bytes=1000,
            local_backup_bytes=0,
            controls_bytes=50,
            spill_bytes=0,
            host_c_free_bytes=50_000_000_000,
            measured_at=activated + timedelta(days=8),
        ),
        workspace_comparison=comparison("active_workspace"),
        payload_comparisons=(comparison("payload_and_references"),),
        funding_comparison=comparison("funding_with_lineage"),
        ordinary_days=tuple(
            ClosedOrdinaryDay(
                utc_date=activated.date() + timedelta(days=i + 1),
                database_bytes_delta=29_999_999,
                wal_bytes_delta=1,
                scope=scope,
                ordinary=True,
                evidence=reference,
            )
            for i in range(7)
        ),
        durations=(
            DurationComparison(
                family="market",
                asset_id="equity:us:aapl",
                baseline_scope=scope,
                current_scope=scope,
                baseline_execution_ms=(100, 120, 110),
                current_execution_ms=(20, 22, 21),
                collector_excluded=True,
                evidence=(reference,),
            ),
        ),
        idempotence=(
            IdempotenceComparison(
                family="market",
                asset_id="equity:us:aapl",
                baseline_scope=scope,
                current_scope=scope,
                before_count=3,
                after_count=3,
                distinct_execution_clocks=True,
                evidence=(reference,),
            ),
        ),
        memory=(
            MemoryScenario(
                scenario="ordinary_cycle",
                runtime_sha="a" * 40,
                pid=1,
                process_start_ticks=1,
                boot_id=uuid4(),
                cgroup="/fixture",
                started_at=activated,
                completed_at=activated + timedelta(minutes=1),
                memory_high_bytes=1000,
                scenario_peak_bytes=900,
                oom_delta=0,
                oom_kill_delta=0,
                max_delta=0,
                complete_scenario_capture=True,
                evidence=(reference,),
            ),
        ),
        gates=tuple(
            AcceptanceGate(
                gate=name, status="MET", reason="fixture assertion", evidence=(reference,)
            )
            for name in REQUIRED_GATES
        ),
        receipts=(reference,),
    )


def test_fixture_never_establishes_operational_acceptance(tmp_path: Path) -> None:
    result = build_final_acceptance(_input(tmp_path))
    assert (result.met, result.not_met, result.unknown, result.accepted) == (0, 0, 7, False)


def test_complete_measurement_contract_has_fixed_denominator(tmp_path: Path) -> None:
    # Exercise the evaluator contract, not a claim of actual operational measurements.
    value = _input(tmp_path).model_copy(update={"data_scope": "permanent"})
    before = value.receipts[0].path.read_bytes()
    result = build_final_acceptance(value, clock=lambda: value.reported_at)
    assert (result.met, result.not_met, result.unknown, result.accepted) == (7, 0, 0, True)
    assert value.receipts[0].path.read_bytes() == before
    assert len(result.objectives) == 7
    assert "range=" in result.objectives[4].comparisons[0]


@pytest.mark.parametrize(
    "change", ["missing_day", "open_day", "partial_cutover_day", "no_activation"]
)
def test_q4_requires_seven_closed_days_after_activation(tmp_path: Path, change: str) -> None:
    value = _input(tmp_path).model_copy(update={"data_scope": "permanent"})
    assert value.activated_at is not None
    if change == "missing_day":
        value = value.model_copy(update={"ordinary_days": value.ordinary_days[1:]})
    elif change == "open_day":
        value = value.model_copy(update={"reported_at": value.activated_at + timedelta(days=7)})
    elif change == "partial_cutover_day":
        first = value.ordinary_days[0].model_copy(update={"utc_date": value.activated_at.date()})
        value = value.model_copy(update={"ordinary_days": (first, *value.ordinary_days[1:])})
    else:
        value = value.model_copy(update={"activated_at": None})
    result = build_final_acceptance(value, clock=lambda: value.reported_at)
    assert result.objectives[3].status == "UNKNOWN"
    assert not result.accepted


def test_future_report_date_cannot_establish_elapsed_q4_days(tmp_path: Path) -> None:
    value = _input(tmp_path).model_copy(update={"data_scope": "permanent"})
    assert value.activated_at is not None
    actual_now = value.activated_at + timedelta(hours=1)
    result = build_final_acceptance(value, clock=lambda: actual_now)
    assert result.evaluated_at == actual_now
    assert result.objectives[3].status == "UNKNOWN"
    assert not result.accepted


def test_acceptance_rejects_a_naive_evaluation_clock(tmp_path: Path) -> None:
    with pytest.raises(FinalAcceptanceError, match="acceptance_clock_requires_timezone"):
        build_final_acceptance(_input(tmp_path), clock=lambda: datetime(2026, 10, 24))


def test_incomparable_scope_and_incomplete_family_stay_unknown(tmp_path: Path) -> None:
    value = _input(tmp_path).model_copy(update={"data_scope": "permanent"})
    assert value.workspace_comparison is not None
    comparison = value.workspace_comparison.model_copy(
        update={"current_scope": value.scope.model_copy(update={"inputs_sha256": "0" * 64})}
    )
    value = value.model_copy(update={"workspace_comparison": comparison, "payload_comparisons": ()})
    result = build_final_acceptance(value, clock=lambda: value.reported_at)
    assert result.objectives[0].status == result.objectives[1].status == "UNKNOWN"
    assert not result.accepted


def test_thresholds_and_memory_event_failure_are_not_met(tmp_path: Path) -> None:
    value = _input(tmp_path).model_copy(update={"data_scope": "permanent"})
    day = value.ordinary_days[0].model_copy(update={"wal_bytes_delta": 2})
    memory = value.memory[0].model_copy(update={"oom_delta": 1})
    value = value.model_copy(
        update={"ordinary_days": (day, *value.ordinary_days[1:]), "memory": (memory,)}
    )
    result = build_final_acceptance(value, clock=lambda: value.reported_at)
    assert result.objectives[3].status == result.objectives[6].status == "NOT_MET"
    assert result.not_met == 2
    assert not result.accepted


def test_changed_artifact_fails_closed_and_gates_cannot_be_reduced(tmp_path: Path) -> None:
    value = _input(tmp_path)
    data = value.model_dump()
    data["required_gates"] = ("f_recovery",)
    with pytest.raises(ValidationError, match="cannot be reduced"):
        FinalAcceptanceInput.model_validate(data)
    value.receipts[0].path.write_bytes(b"changed")
    with pytest.raises(FinalAcceptanceError, match="evidence_digest_mismatch"):
        build_final_acceptance(value)


def _finite_input(tmp_path: Path) -> FinalAcceptanceInput:
    value = _input(tmp_path).model_copy(update={"data_scope": "permanent"})
    assert value.activated_at is not None
    batches = []
    for index, scenario in enumerate(("ordinary", "same_inputs")):
        started = value.activated_at + timedelta(minutes=1 + 2 * index)
        attempts = (uuid4(), uuid4())  # A retry also contributes its positive growth.
        boundaries = tuple(
            OrdinaryBatchBoundary(
                attempt_id=attempts[step // 2],
                job_pair=value.required_job_pairs[0],
                edge="before" if step % 2 == 0 else "after",
                observed_at=started + timedelta(seconds=step),
                database_bytes=1000 + step * 100,
                wal_bytes=step * 10,
            )
            for step in range(4)
        )
        batches.append(
            OrdinaryBatchGrowth(
                scenario=scenario,
                started_at=started,
                completed_at=boundaries[-1].observed_at,
                runtime_sha=value.runtime_sha,
                workspace_id=value.workspace_id,
                scope=value.scope,
                job_pairs=value.required_job_pairs,
                attempt_ids=attempts,
                boundaries=boundaries,
                complete=True,
                exclusive_writer=True,
                evidence=value.receipts,
            )
        )
    return value.model_copy(
        update={"q4_policy": "ordinary-batch-growth-v1", "ordinary_batches": tuple(batches)}
    )


def test_finite_q4_uses_both_shared_batches_without_waiting_for_days(tmp_path: Path) -> None:
    value = _finite_input(tmp_path)
    reported = value.ordinary_batches[-1].completed_at + timedelta(seconds=1)
    value = value.model_copy(update={"reported_at": reported, "ordinary_days": ()})
    report = build_final_acceptance(value, clock=lambda: reported)
    assert report.schema_version == "workspace-v2-final-acceptance-v2"
    assert report.q4_policy == "ordinary-batch-growth-v1"
    assert report.q4_unit == "B/batch"
    assert report.accepted and report.met == 7
    assert [batch.positive_growth_bytes for batch in value.ordinary_batches] == [330, 330]
    assert "does not establish B/day" in report.objectives[3].reason
    assert len(report.objectives) == 7


@pytest.mark.parametrize(
    "fault",
    [
        "absent",
        "partial",
        "concurrent",
        "future_report",
        "future_batch",
        "fixture",
        "no_activation",
        "before_activation",
        "runtime",
        "workspace",
        "scope",
        "inputs",
        "versions",
        "matrix",
        "attempt",
        "overlap",
        "duplicate_attempt",
        "missing_boundary",
        "wrong_edge",
        "wrong_boundary_pair",
        "gap_order",
        "new_equivalents",
    ],
)
def test_finite_q4_rejects_incomplete_or_incompatible_batches(tmp_path: Path, fault: str) -> None:
    value = _finite_input(tmp_path)
    first, second = value.ordinary_batches
    changed = {}
    batch_changed = {}
    clock = value.reported_at
    if fault == "absent":
        changed["ordinary_batches"] = ()
    elif fault in {"partial", "concurrent"}:
        batch_changed["complete" if fault == "partial" else "exclusive_writer"] = False
    elif fault == "future_report":
        clock = first.started_at
    elif fault == "future_batch":
        batch_changed["completed_at"] = clock + timedelta(seconds=1)
    elif fault == "fixture":
        changed["data_scope"] = "fixture"
    elif fault == "no_activation":
        changed["activated_at"] = None
    elif fault == "before_activation":
        changed["activated_at"] = first.completed_at
    elif fault == "runtime":
        batch_changed["runtime_sha"] = "0" * 40
    elif fault == "workspace":
        batch_changed["workspace_id"] = uuid4()
    elif fault in {"scope", "inputs", "versions"}:
        field = {
            "scope": "inventory_sha256",
            "inputs": "inputs_sha256",
            "versions": "algorithm_versions_sha256",
        }[fault]
        batch_changed["scope"] = first.scope.model_copy(update={field: "0" * 64})
    elif fault == "matrix":
        batch_changed["job_pairs"] = ()
    elif fault == "attempt":
        batch_changed["attempt_ids"] = (uuid4(), *first.attempt_ids[1:])
    elif fault == "overlap":
        batch_changed["completed_at"] = second.started_at + timedelta(seconds=1)
    elif fault == "duplicate_attempt":
        batch_changed["attempt_ids"] = (first.attempt_ids[0], first.attempt_ids[0])
    elif fault == "missing_boundary":
        batch_changed["boundaries"] = first.boundaries[:-1]
    elif fault in {"wrong_edge", "wrong_boundary_pair", "gap_order"}:
        field, replacement = {
            "wrong_edge": ("edge", "after"),
            "wrong_boundary_pair": ("job_pair", ("fundamentals", "equity:us:other")),
            "gap_order": ("observed_at", first.completed_at),
        }[fault]
        boundary = first.boundaries[0].model_copy(update={field: replacement})
        batch_changed["boundaries"] = (boundary, *first.boundaries[1:])
    elif fault == "new_equivalents":
        changed["idempotence"] = (value.idempotence[0].model_copy(update={"after_count": 4}),)
    if batch_changed:
        changed["ordinary_batches"] = (first.model_copy(update=batch_changed), second)
    value = value.model_copy(update=changed)
    result = build_final_acceptance(value, clock=lambda: clock)
    assert result.objectives[3].status == ("NOT_MET" if fault == "new_equivalents" else "UNKNOWN")
    assert not result.accepted


@pytest.mark.parametrize("location", ["attempt", "gap", "second_scenario"])
def test_finite_q4_never_offsets_positive_growth_with_decreases(
    tmp_path: Path, location: str
) -> None:
    value = _finite_input(tmp_path)
    index = 1 if location == "second_scenario" else 0
    batch = value.ordinary_batches[index]
    sizes = [0, 30_000_001, 0, 0] if location != "gap" else [0, 0, 30_000_001, 0]
    boundaries = tuple(
        boundary.model_copy(update={"database_bytes": size, "wal_bytes": 0})
        for boundary, size in zip(batch.boundaries, sizes, strict=True)
    )
    modified = batch.model_copy(update={"boundaries": boundaries})
    batches = list(value.ordinary_batches)
    batches[index] = modified
    value = value.model_copy(update={"ordinary_batches": tuple(batches)})
    assert modified.positive_growth_bytes == 30_000_001
    assert (
        build_final_acceptance(value, clock=lambda: value.reported_at).objectives[3].status
        == "NOT_MET"
    )


def test_finite_q4_does_not_net_wal_decrease_against_database_growth(tmp_path: Path) -> None:
    value = _finite_input(tmp_path)
    batch = value.ordinary_batches[0]
    boundaries = tuple(
        boundary.model_copy(update={"database_bytes": db, "wal_bytes": wal})
        for boundary, db, wal in zip(
            batch.boundaries, (0, 30_000_001, 0, 0), (30_000_001, 0, 0, 0), strict=True
        )
    )
    modified = batch.model_copy(update={"boundaries": boundaries})
    assert modified.positive_growth_bytes == 30_000_001
    value = value.model_copy(update={"ordinary_batches": (modified, value.ordinary_batches[1])})
    assert (
        build_final_acceptance(value, clock=lambda: value.reported_at).objectives[3].status
        == "NOT_MET"
    )


def test_legacy_acceptance_report_still_reads_and_evaluates_v1(tmp_path: Path) -> None:
    from investment_analyst.workspace.workspace_v2_acceptance import FinalAcceptanceReport

    value = _input(tmp_path).model_copy(update={"data_scope": "permanent"})
    report = build_final_acceptance(value, clock=lambda: value.reported_at)
    document = report.model_dump(mode="json")
    document.pop("q4_policy")
    document.pop("q4_unit")
    document["measurements"].pop("q4_policy")
    document["measurements"].pop("ordinary_batches")
    legacy = FinalAcceptanceReport.model_validate_json(__import__("json").dumps(document))
    assert legacy.schema_version == "workspace-v2-final-acceptance-v1"
    assert legacy.q4_policy == "seven-closed-utc-days-v1"
    assert legacy.q4_unit == "B/day" and legacy.accepted
