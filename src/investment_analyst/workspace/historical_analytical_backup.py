"""Checkpoint and typed inventory validation for historical analytical staging backups."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from duckdb import DuckDBPyConnection
from pydantic import ConfigDict, Field

from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.storage.historical_analytical_archive import (
    HistoricalAnalyticalArchive,
    HistoricalAnalyticalArchiveError,
    HistoricalAnalyticalCursor,
    ensure_historical_analytical_archive_tables,
    historical_analytical_archive_exists,
)
from investment_analyst.storage.historical_analytical_import import (
    HISTORICAL_ANALYTICAL_IMPORT_STATE_SCHEMA,
    HistoricalAnalyticalImportCursor,
    HistoricalAnalyticalImportError,
    HistoricalAnalyticalImportState,
    verify_saved_prefix,
)
from investment_analyst.storage.historical_analytical_validation import (
    verify_historical_analytical_archive,
)
from investment_analyst.storage.observation_v2_import import ObservationV2ImportState
from investment_analyst.storage.raw_v2_import import RawV2ImportState

_STATE_FILENAME = "historical-analytical-import-state.json"


def _cursor_matches(
    state_cursor: HistoricalAnalyticalImportCursor | None,
    archive_cursor: HistoricalAnalyticalCursor | None,
) -> bool:
    if state_cursor is None or archive_cursor is None:
        return state_cursor is None and archive_cursor is None
    return (
        state_cursor.available_at == archive_cursor.available_at
        and state_cursor.identifier == archive_cursor.identifier
    )


class HistoricalAnalyticalBackupError(ValueError):
    """Raised when a historical analytical checkpoint cannot be backed up safely."""


class HistoricalAnalyticalBackupCounts(ContractModel):
    """Manifest binding for the archive inventory and portable import checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    state_format: Literal["historical-analytical-import-state-v1"] = (
        HISTORICAL_ANALYTICAL_IMPORT_STATE_SCHEMA
    )
    state_digest: NonEmptyStr
    source_workspace_id: NonEmptyStr
    source_fingerprint: NonEmptyStr
    raw_digest: NonEmptyStr
    observation_digest: NonEmptyStr
    metric_digest: NonEmptyStr
    diagnostic_digest: NonEmptyStr
    phase: Literal["metrics", "diagnostics", "verification", "complete"]
    complete: bool
    page_limit: int = Field(ge=1, le=256)
    confirmed_metrics: int = Field(ge=0)
    confirmed_diagnostics: int = Field(ge=0)
    metric_cursor: HistoricalAnalyticalImportCursor | None = None
    diagnostic_cursor: HistoricalAnalyticalImportCursor | None = None
    metrics: int = Field(ge=0)
    diagnostics: int = Field(ge=0)
    components: int = Field(ge=0)
    evidence: int = Field(ge=0)
    sequences: int = Field(ge=0)
    sequence_segments: int = Field(ge=0)
    segments: int = Field(ge=0)
    segment_members: int = Field(ge=0)


def inspect_historical_analytical_backup(
    connection: DuckDBPyConnection,
    staging_root: Path,
    *,
    staging_id: str,
) -> HistoricalAnalyticalBackupCounts | None:
    """Validate archive state and return counts, or ``None`` if no archive exists."""
    try:
        exists = historical_analytical_archive_exists(connection)
    except HistoricalAnalyticalArchiveError as error:
        raise HistoricalAnalyticalBackupError(str(error)) from error
    state_path = staging_root / _STATE_FILENAME
    if not exists:
        if state_path.exists() or state_path.is_symlink():
            raise HistoricalAnalyticalBackupError(
                "historical analytical checkpoint exists without its archive"
            )
        return None
    if state_path.is_symlink() or not state_path.is_file():
        raise HistoricalAnalyticalBackupError(
            "historical analytical archive has no safe import checkpoint"
        )
    try:
        state_bytes = state_path.read_bytes()
        state = HistoricalAnalyticalImportState.model_validate_json(state_bytes)
        if state.staging_id != staging_id:
            raise HistoricalAnalyticalBackupError(
                "historical analytical checkpoint belongs to another staging"
            )
        if state.complete != (state.phase == "complete"):
            raise HistoricalAnalyticalBackupError(
                "historical analytical checkpoint phase and completion disagree"
            )
        ensure_historical_analytical_archive_tables(connection, create=False)
        raw_state = _read_checkpoint(staging_root / "raw-v2-import-state.json", RawV2ImportState)
        observation_state = _read_checkpoint(
            staging_root / "observation-v2-import-state.json", ObservationV2ImportState
        )
        if (
            raw_state.staging_id != staging_id
            or observation_state.staging_id != staging_id
            or raw_state.source_workspace_id != state.source_workspace_id
            or observation_state.source_workspace_id != state.source_workspace_id
            or raw_state.source_fingerprint != observation_state.source_fingerprint
            or raw_state.accumulated_digest != observation_state.raw_digest
            or state.raw_digest != observation_state.raw_digest
            or state.observation_digest != observation_state.accumulated_digest
        ):
            raise HistoricalAnalyticalBackupError(
                "historical checkpoint does not match raw and observation checkpoints"
            )
        if raw_state.format != "raw-v2-import-state-v2":
            raise HistoricalAnalyticalBackupError(
                "historical backup requires a portable raw import checkpoint"
            )
        row = connection.execute("SELECT count(*) FROM raw_v2_index").fetchone()
        observation_row = connection.execute(
            "SELECT count(*) FROM normalized_observations_v2"
        ).fetchone()
        if (
            row is None
            or observation_row is None
            or int(row[0]) != raw_state.confirmed_count
            or int(observation_row[0]) != observation_state.confirmed_count
        ):
            raise HistoricalAnalyticalBackupError(
                "raw or observation checkpoint does not cover its staged inventory"
            )
        (
            metrics,
            diagnostics,
            components,
            evidence,
            sequences,
            sequence_segments,
            segments,
            members,
        ) = _inventory_counts(connection)
        _validate_phase_counts(state, metrics, diagnostics)
        archive = HistoricalAnalyticalArchive(connection)
        rows_verified = archive.verify_rows()
        if (rows_verified.metric_count, rows_verified.diagnostic_count) != (
            metrics,
            diagnostics,
        ):
            raise HistoricalAnalyticalBackupError(
                "historical analytical row counts changed during backup verification"
            )
        if state.complete:
            summary = verify_historical_analytical_archive(connection)
            if (
                summary.metric_count != metrics
                or summary.diagnostic_count != diagnostics
                or state.metric_count != metrics
                or state.diagnostic_count != diagnostics
                or state.metric_digest != rows_verified.metric_digest
                or state.diagnostic_digest != rows_verified.diagnostic_digest
                or not _cursor_matches(state.metric_cursor, rows_verified.metric_cursor)
                or not _cursor_matches(state.diagnostic_cursor, rows_verified.diagnostic_cursor)
            ):
                raise HistoricalAnalyticalBackupError(
                    "complete historical checkpoint does not cover its archive"
                )
        else:
            verify_saved_prefix(archive, state)
    except (
        OSError,
        ValueError,
        HistoricalAnalyticalArchiveError,
        HistoricalAnalyticalImportError,
    ) as error:
        if isinstance(error, HistoricalAnalyticalBackupError):
            raise
        raise HistoricalAnalyticalBackupError(
            "historical analytical checkpoint or archive is incompatible"
        ) from error
    return HistoricalAnalyticalBackupCounts(
        state_digest=hashlib.sha256(state_bytes).hexdigest(),
        source_workspace_id=state.source_workspace_id,
        source_fingerprint=state.source_fingerprint,
        raw_digest=state.raw_digest,
        observation_digest=state.observation_digest,
        metric_digest=state.metric_digest,
        diagnostic_digest=state.diagnostic_digest,
        phase=state.phase,
        complete=state.complete,
        page_limit=state.page_limit,
        confirmed_metrics=state.metric_count,
        confirmed_diagnostics=state.diagnostic_count,
        metric_cursor=state.metric_cursor,
        diagnostic_cursor=state.diagnostic_cursor,
        metrics=metrics,
        diagnostics=diagnostics,
        components=components,
        evidence=evidence,
        sequences=sequences,
        sequence_segments=sequence_segments,
        segments=segments,
        segment_members=members,
    )


def verify_historical_analytical_backup(
    connection: DuckDBPyConnection,
    staging_root: Path,
    *,
    staging_id: str,
    expected: HistoricalAnalyticalBackupCounts,
) -> None:
    """Revalidate a restored v6 checkpoint and its archive against manifest counts."""
    actual = inspect_historical_analytical_backup(connection, staging_root, staging_id=staging_id)
    if actual != expected:
        raise HistoricalAnalyticalBackupError(
            "restored historical analytical archive differs from backup manifest"
        )


def _inventory_counts(
    connection: DuckDBPyConnection,
) -> tuple[int, int, int, int, int, int, int, int]:
    tables = (
        "historical_metric_results",
        "historical_diagnostic_results",
        "historical_analytical_components",
        "historical_analytical_evidence",
        "historical_analytical_sequences",
        "historical_analytical_sequence_segments",
        "historical_analytical_segments",
        "historical_analytical_segment_members",
    )
    values: list[int] = []
    for table in tables:
        row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
        if row is None:
            raise HistoricalAnalyticalBackupError("historical analytical count is unavailable")
        values.append(int(row[0]))
    return (
        values[0],
        values[1],
        values[2],
        values[3],
        values[4],
        values[5],
        values[6],
        values[7],
    )


def _read_checkpoint[T: ContractModel](path: Path, model_type: type[T]) -> T:
    if path.is_symlink() or not path.is_file():
        raise HistoricalAnalyticalBackupError("required staging checkpoint is missing or unsafe")
    try:
        return model_type.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise HistoricalAnalyticalBackupError(
            "required staging checkpoint is incompatible"
        ) from error


def _validate_phase_counts(
    state: HistoricalAnalyticalImportState, metrics: int, diagnostics: int
) -> None:
    if state.phase == "metrics":
        if state.diagnostic_count != 0 or state.diagnostic_cursor is not None:
            raise HistoricalAnalyticalBackupError("diagnostic progress precedes the metric phase")
    elif state.metric_count != metrics:
        raise HistoricalAnalyticalBackupError(
            "confirmed metric phase does not match archived metrics"
        )
    if state.phase in ("verification", "complete") and state.diagnostic_count != diagnostics:
        raise HistoricalAnalyticalBackupError(
            "confirmed diagnostic phase does not match archived diagnostics"
        )


__all__ = [
    "HistoricalAnalyticalBackupCounts",
    "HistoricalAnalyticalBackupError",
    "inspect_historical_analytical_backup",
    "verify_historical_analytical_backup",
]
