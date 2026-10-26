"""Typed physical admission budget for one selective historical recovery."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

HISTORICAL_RECOVERY_BUDGET_SCHEMA = "historical-recovery-budget-v1"
HISTORICAL_RECOVERY_SEQUENCE_BUDGET_SCHEMA = "historical-recovery-sequence-budget-v1"
DEFAULT_RESERVE_BYTES = 5_000_000_000


class HistoricalRecoveryBudgetError(RuntimeError):
    """Raised when a recovery operation is not physically admitted."""


class HistoricalRecoveryPhase(ContractModel):
    """One serialized phase with the bytes that coexist on each volume."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: NonEmptyStr
    host_resident_bytes: int = Field(ge=0)
    wsl_resident_bytes: int = Field(ge=0)


class HistoricalRecoverySequenceBudget(ContractModel):
    """Fail-closed peak over a serialized phase calendar."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["historical-recovery-sequence-budget-v1"] = (
        HISTORICAL_RECOVERY_SEQUENCE_BUDGET_SCHEMA
    )
    measured_at: UTCDateTime
    phases: tuple[HistoricalRecoveryPhase, ...]
    host_free_bytes: int = Field(ge=0)
    wsl_free_bytes: int = Field(ge=0)
    reserve_bytes: int = Field(ge=0)
    host_peak_bytes: int = Field(ge=0)
    wsl_peak_bytes: int = Field(ge=0)
    host_required_bytes: int = Field(ge=0)
    wsl_required_bytes: int = Field(ge=0)
    admitted: bool
    reason: NonEmptyStr


def measure_historical_recovery_sequence_budget(
    *,
    phases: Iterable[HistoricalRecoveryPhase],
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
    host_free_bytes: Callable[[], int] | None = None,
    wsl_free_bytes: Callable[[], int] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> HistoricalRecoverySequenceBudget:
    typed = tuple(phases)
    if not typed:
        raise HistoricalRecoveryBudgetError("recovery sequence requires at least one phase")
    if isinstance(reserve_bytes, bool) or reserve_bytes < 0:
        raise HistoricalRecoveryBudgetError("recovery reserve must be a non-negative int")
    host_free = (host_free_bytes or (lambda: _free_bytes(Path("/mnt/c"))))()
    wsl_free = (wsl_free_bytes or (lambda: _free_bytes(Path("/"))))()
    host_peak = max(phase.host_resident_bytes for phase in typed)
    wsl_peak = max(phase.wsl_resident_bytes for phase in typed)
    host_required = reserve_bytes + host_peak
    wsl_required = reserve_bytes + wsl_peak
    admitted = host_free >= host_required and wsl_free >= wsl_required
    if admitted:
        reason = "host and WSL free space admit every serialized phase peak"
    elif host_free < host_required:
        reason = "host free space is below reserve plus the highest coexisting phase"
    else:
        reason = "WSL free space is below reserve plus the highest coexisting phase"
    return HistoricalRecoverySequenceBudget(
        measured_at=clock(),
        phases=typed,
        host_free_bytes=host_free,
        wsl_free_bytes=wsl_free,
        reserve_bytes=reserve_bytes,
        host_peak_bytes=host_peak,
        wsl_peak_bytes=wsl_peak,
        host_required_bytes=host_required,
        wsl_required_bytes=wsl_required,
        admitted=admitted,
        reason=reason,
    )


class HistoricalRecoveryBudget(ContractModel):
    """Measured free space and conservative peak demand for one recovery step."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["historical-recovery-budget-v1"] = HISTORICAL_RECOVERY_BUDGET_SCHEMA
    measured_at: UTCDateTime
    host_free_bytes: int = Field(ge=0)
    wsl_free_bytes: int = Field(ge=0)
    selected_payload_bytes: int = Field(ge=0)
    present_payload_bytes: int = Field(ge=0)
    import_new_bytes: int = Field(ge=0)
    import_peak_bytes: int = Field(ge=0)
    backup_new_bytes: int = Field(ge=0)
    backup_reused_bytes: int = Field(ge=0)
    restore_peak_bytes: int = Field(ge=0)
    reserve_bytes: int = Field(ge=0)
    host_required_bytes: int = Field(ge=0)
    wsl_required_bytes: int = Field(ge=0)
    admitted: bool
    reason: NonEmptyStr


def _free_bytes(path: Path) -> int:
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return 0


def measure_historical_recovery_budget(
    *,
    selected_payload_bytes: int,
    present_payload_bytes: int,
    import_peak_bytes: int,
    backup_new_bytes: int,
    backup_reused_bytes: int,
    restore_peak_bytes: int,
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
    host_free_bytes: Callable[[], int] | None = None,
    wsl_free_bytes: Callable[[], int] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> HistoricalRecoveryBudget:
    """Compute a fail-closed admission receipt without writing to either workspace."""
    for value in (
        selected_payload_bytes,
        present_payload_bytes,
        import_peak_bytes,
        backup_new_bytes,
        backup_reused_bytes,
        restore_peak_bytes,
        reserve_bytes,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HistoricalRecoveryBudgetError("recovery budget inputs must be non-negative ints")
    host_free = (host_free_bytes or (lambda: _free_bytes(Path("/mnt/c"))))()
    wsl_free = (wsl_free_bytes or (lambda: _free_bytes(Path("/"))))()
    if host_free < 0 or wsl_free < 0:
        raise HistoricalRecoveryBudgetError("recovery free-space readers are invalid")
    import_new_bytes = max(selected_payload_bytes - present_payload_bytes, 0)
    backup_phase = import_new_bytes + backup_new_bytes
    restore_phase = backup_phase + restore_peak_bytes
    peak = max(import_peak_bytes, backup_phase, restore_phase)
    host_required = reserve_bytes + peak
    wsl_required = reserve_bytes + peak
    admitted = host_free >= host_required and wsl_free >= wsl_required
    if admitted:
        reason = "host and WSL free space admit the reserve plus every coexisting phase"
    elif host_free < host_required:
        reason = "host free space is below reserve plus the highest coexisting phase"
    else:
        reason = "WSL free space is below reserve plus the highest coexisting phase"
    return HistoricalRecoveryBudget(
        measured_at=clock(),
        host_free_bytes=host_free,
        wsl_free_bytes=wsl_free,
        selected_payload_bytes=selected_payload_bytes,
        present_payload_bytes=present_payload_bytes,
        import_new_bytes=import_new_bytes,
        import_peak_bytes=import_peak_bytes,
        backup_new_bytes=backup_new_bytes,
        backup_reused_bytes=backup_reused_bytes,
        restore_peak_bytes=restore_peak_bytes,
        reserve_bytes=reserve_bytes,
        host_required_bytes=host_required,
        wsl_required_bytes=wsl_required,
        admitted=admitted,
        reason=reason,
    )


def require_historical_recovery_admission(budget: HistoricalRecoveryBudget) -> None:
    """Stop before any writer starts when the measured budget is not admitted."""
    if not budget.admitted:
        raise HistoricalRecoveryBudgetError(f"historical recovery is not admitted: {budget.reason}")


__all__ = [
    "DEFAULT_RESERVE_BYTES",
    "HISTORICAL_RECOVERY_BUDGET_SCHEMA",
    "HISTORICAL_RECOVERY_SEQUENCE_BUDGET_SCHEMA",
    "HistoricalRecoveryBudget",
    "HistoricalRecoveryBudgetError",
    "HistoricalRecoveryPhase",
    "HistoricalRecoverySequenceBudget",
    "measure_historical_recovery_budget",
    "measure_historical_recovery_sequence_budget",
    "require_historical_recovery_admission",
]
