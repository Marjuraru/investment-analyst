"""Focused phase-budget tests for the R2 recovery sequence."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from investment_analyst.workspace.historical_recovery_budget import (
    HistoricalRecoveryBudgetError,
    HistoricalRecoveryPhase,
    measure_historical_recovery_budget,
    measure_historical_recovery_sequence_budget,
)

_NOW = datetime(2026, 10, 23, tzinfo=UTC)


def _budget(*, host: int = 100, wsl: int = 100, backup: int = 1, restore: int = 4):
    return measure_historical_recovery_budget(
        selected_payload_bytes=10,
        present_payload_bytes=8,
        import_peak_bytes=2,
        backup_new_bytes=backup,
        backup_reused_bytes=10,
        restore_peak_bytes=restore,
        reserve_bytes=5,
        host_free_bytes=lambda: host,
        wsl_free_bytes=lambda: wsl,
        clock=lambda: _NOW,
    )


def test_host_insufficient_while_wsl_abundant() -> None:
    budget = _budget(host=11, wsl=100)
    assert budget.host_required_bytes == 12
    assert budget.wsl_required_bytes == 12
    assert budget.admitted is False
    assert "host" in budget.reason


def test_backup_delta_is_in_the_peak() -> None:
    without = _budget(host=100, wsl=100, backup=0)
    with_delta = _budget(host=100, wsl=100, backup=2)
    assert without.host_required_bytes == 11
    assert with_delta.host_required_bytes == 13
    assert with_delta.admitted is True


def test_full_restore_exceeds_the_import_delta() -> None:
    budget = _budget(host=100, wsl=100, backup=1, restore=4)
    assert budget.import_new_bytes == 2
    assert budget.restore_peak_bytes > budget.import_new_bytes
    assert budget.host_required_bytes == 12
    assert budget.wsl_required_bytes == 12


def test_hardlinks_are_counted_once() -> None:
    first = _budget(host=100, wsl=100)
    second = measure_historical_recovery_budget(
        selected_payload_bytes=10,
        present_payload_bytes=8,
        import_peak_bytes=2,
        backup_new_bytes=1,
        backup_reused_bytes=10_000,
        restore_peak_bytes=4,
        reserve_bytes=5,
        host_free_bytes=lambda: 100,
        wsl_free_bytes=lambda: 100,
        clock=lambda: _NOW,
    )
    assert first.host_required_bytes == second.host_required_bytes


def test_serialized_phases_use_the_peak_not_the_sum() -> None:
    budget = measure_historical_recovery_sequence_budget(
        phases=(
            HistoricalRecoveryPhase(name="import", host_resident_bytes=10, wsl_resident_bytes=10),
            HistoricalRecoveryPhase(name="restore", host_resident_bytes=30, wsl_resident_bytes=20),
        ),
        reserve_bytes=5,
        host_free_bytes=lambda: 100,
        wsl_free_bytes=lambda: 100,
        clock=lambda: _NOW,
    )
    assert budget.host_peak_bytes == 30
    assert budget.wsl_peak_bytes == 20
    assert budget.host_required_bytes == 35
    assert budget.wsl_required_bytes == 25
    assert budget.admitted is True


def test_persistent_growth_is_retained_across_phases() -> None:
    budget = measure_historical_recovery_sequence_budget(
        phases=(
            HistoricalRecoveryPhase(name="import", host_resident_bytes=2, wsl_resident_bytes=2),
            HistoricalRecoveryPhase(name="backup", host_resident_bytes=3, wsl_resident_bytes=3),
            HistoricalRecoveryPhase(name="restore", host_resident_bytes=7, wsl_resident_bytes=7),
        ),
        reserve_bytes=5,
        host_free_bytes=lambda: 100,
        wsl_free_bytes=lambda: 100,
        clock=lambda: _NOW,
    )
    assert budget.host_peak_bytes == 7
    assert budget.host_required_bytes == 12


def test_sequence_rejects_empty_phase_calendar() -> None:
    with pytest.raises(HistoricalRecoveryBudgetError, match="at least one phase"):
        measure_historical_recovery_sequence_budget(
            phases=(),
            host_free_bytes=lambda: 100,
            wsl_free_bytes=lambda: 100,
            clock=lambda: _NOW,
        )
