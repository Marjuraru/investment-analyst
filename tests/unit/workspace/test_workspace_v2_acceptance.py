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
