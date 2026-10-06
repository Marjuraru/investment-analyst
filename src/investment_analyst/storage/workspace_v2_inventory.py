"""Resumable transfer of a verified historical archive into compact workspace v2."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import ConfigDict, Field, field_validator

from investment_analyst.core.models import DiagnosticResult, MetricResult
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.storage.compact_analytical_v2 import CompactAnalyticalStore
from investment_analyst.storage.errors import StorageError
from investment_analyst.storage.historical_analytical_archive import (
    HistoricalAnalyticalArchive,
    HistoricalAnalyticalCursor,
)
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex

_PAGE = 256
_STATE_FILENAME = "workspace-v2-analytical-import-state.json"
_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


class WorkspaceV2InventoryError(StorageError):
    """Historical import state, source identity or confirmed prefix is invalid."""


class WorkspaceV2InventoryState(ContractModel):
    """Atomic checkpoint binding one source archive to one scratch destination."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-v2-analytical-import-state-v1"] = (
        "workspace-v2-analytical-import-state-v1"
    )
    source_workspace_id: NonEmptyStr
    source_fingerprint: NonEmptyStr
    metric_count: int = Field(ge=0)
    metric_digest: NonEmptyStr
    diagnostic_count: int = Field(ge=0)
    diagnostic_digest: NonEmptyStr
    phase: Literal["metrics", "diagnostics", "verification", "complete"]
    metric_cursor: HistoricalAnalyticalCursor | None = None
    diagnostic_cursor: HistoricalAnalyticalCursor | None = None
    page_limit: int = Field(ge=1, le=_PAGE)
    updated_at: UTCDateTime

    @field_validator("page_limit", mode="before")
    @classmethod
    def _integer_page_limit(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("workspace v2 import page limit must be an integer")
        return value


class WorkspaceV2InventorySummary(ContractModel):
    """Result and bounded page evidence for one complete archive transfer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    complete: Literal[True] = True
    source_workspace_id: NonEmptyStr
    source_fingerprint: NonEmptyStr
    metric_count: int = Field(ge=0)
    metric_digest: NonEmptyStr
    diagnostic_count: int = Field(ge=0)
    diagnostic_digest: NonEmptyStr
    metric_created_count: int = Field(ge=0)
    metric_reused_count: int = Field(ge=0)
    diagnostic_created_count: int = Field(ge=0)
    diagnostic_reused_count: int = Field(ge=0)
    max_page_requested: int = Field(ge=1, le=_PAGE)
    max_page_hydrated: int = Field(ge=0, le=_PAGE)


def _fold_digest(previous: str, model: MetricResult | DiagnosticResult) -> str:
    checksum = sha256_hex(canonical_json_bytes(model))
    return hashlib.sha256(f"{previous}:{checksum}".encode()).hexdigest()


def _read_state(path: Path) -> WorkspaceV2InventoryState:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceV2InventoryError("workspace v2 import checkpoint is unsafe")
    try:
        return WorkspaceV2InventoryState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceV2InventoryError("workspace v2 import checkpoint is malformed") from error


class WorkspaceV2AnalyticalImporter:
    """Copy an exact verified archive in committed pages into a v2 scratch store."""

    def __init__(
        self,
        source: HistoricalAnalyticalArchive,
        target: CompactAnalyticalStore,
        *,
        source_root: Path,
        destination_root: Path,
        state_root: Path,
        source_workspace_id: str,
        source_fingerprint: str,
        page_limit: int = _PAGE,
        clock: Callable[[], datetime] | None = None,
        failure_injector: Callable[[str], None] | None = None,
    ) -> None:
        if (
            isinstance(page_limit, bool)
            or not isinstance(page_limit, int)
            or not 1 <= page_limit <= _PAGE
        ):
            raise WorkspaceV2InventoryError(
                "workspace v2 import page limit must be between 1 and 256"
            )
        if len(source_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in source_fingerprint
        ):
            raise WorkspaceV2InventoryError("workspace v2 source fingerprint must be SHA-256")
        if destination_root.is_symlink() or state_root.is_symlink():
            raise WorkspaceV2InventoryError("workspace v2 import path cannot be a symbolic link")
        source_path = source_root.resolve(strict=False)
        destination_path = destination_root.resolve(strict=False)
        state_path = state_root.resolve(strict=False)
        if (
            source_path == destination_path
            or source_path in destination_path.parents
            or destination_path in source_path.parents
            or destination_path.is_symlink()
            or state_path != destination_path
            and destination_path not in state_path.parents
        ):
            raise WorkspaceV2InventoryError("workspace v2 import roots must be disjoint and safe")
        if any(parent.is_symlink() for parent in (destination_path, state_path)):
            raise WorkspaceV2InventoryError("workspace v2 import path cannot be a symbolic link")
        self._source = source
        self._target = target
        self._source_workspace_id = source_workspace_id
        self._source_fingerprint = source_fingerprint
        self._page_limit = page_limit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._failure_injector = failure_injector
        self._max_page_hydrated = 0
        self._destination_root = destination_path
        self._state_root = state_path
        self._state_path = state_path / _STATE_FILENAME

    def run(self) -> WorkspaceV2InventorySummary:
        """Resume only from an exact source/destination-bound checkpoint."""
        source_rows = self._source.verify_rows()
        self._source.verify_complete()
        expected = (
            source_rows.metric_count,
            source_rows.metric_digest,
            source_rows.diagnostic_count,
            source_rows.diagnostic_digest,
        )
        state = self._load_or_create_state()
        self._validate_binding(state)
        self._validate_confirmed_prefix(state, expected)

        metric_created = metric_reused = 0
        diagnostic_created = diagnostic_reused = 0
        if state.phase == "metrics":
            state, metric_created, metric_reused = self._import_metrics(state)
        if state.phase == "diagnostics":
            state, diagnostic_created, diagnostic_reused = self._import_diagnostics(state)
        if state.phase in ("verification", "complete"):
            actual = self._target.historical_inventory_digests()
            if actual[0] != source_rows.metric_count or actual[2] != source_rows.diagnostic_count:
                raise WorkspaceV2InventoryError("workspace v2 historical count differs from source")
            metric_count, metric_digest, diagnostic_count, diagnostic_digest = actual
            if actual != expected:
                raise WorkspaceV2InventoryError(
                    "workspace v2 historical digest differs from source"
                )
            self._target.seal_historical_inventory(
                source_fingerprint=self._source_fingerprint,
                metric_count=metric_count,
                metric_digest=metric_digest,
                diagnostic_count=diagnostic_count,
                diagnostic_digest=diagnostic_digest,
                sealed_at=self._clock(),
            )
            self._target.verify_complete()
            if state.phase != "complete":
                state = self._updated_state(state, phase="complete")
                self._write_state(state)
        if state.phase != "complete":
            raise WorkspaceV2InventoryError("workspace v2 historical import is incomplete")
        return WorkspaceV2InventorySummary(
            source_workspace_id=self._source_workspace_id,
            source_fingerprint=self._source_fingerprint,
            metric_count=source_rows.metric_count,
            metric_digest=source_rows.metric_digest,
            diagnostic_count=source_rows.diagnostic_count,
            diagnostic_digest=source_rows.diagnostic_digest,
            metric_created_count=metric_created,
            metric_reused_count=metric_reused,
            diagnostic_created_count=diagnostic_created,
            diagnostic_reused_count=diagnostic_reused,
            max_page_requested=self._page_limit,
            max_page_hydrated=self._max_page_hydrated,
        )

    def _load_or_create_state(self) -> WorkspaceV2InventoryState:
        self._state_root.mkdir(parents=True, exist_ok=True)
        if self._state_path.exists() or self._state_path.is_symlink():
            return _read_state(self._state_path)
        state = WorkspaceV2InventoryState(
            source_workspace_id=self._source_workspace_id,
            source_fingerprint=self._source_fingerprint,
            metric_count=0,
            metric_digest=_EMPTY_DIGEST,
            diagnostic_count=0,
            diagnostic_digest=_EMPTY_DIGEST,
            phase="metrics",
            page_limit=self._page_limit,
            updated_at=self._clock(),
        )
        self._write_state(state)
        return state

    def _validate_binding(self, state: WorkspaceV2InventoryState) -> None:
        if (
            state.source_workspace_id != self._source_workspace_id
            or state.source_fingerprint != self._source_fingerprint
            or state.page_limit != self._page_limit
        ):
            raise WorkspaceV2InventoryError(
                "workspace v2 import checkpoint belongs to another source"
            )

    def _validate_confirmed_prefix(
        self,
        state: WorkspaceV2InventoryState,
        expected: tuple[int, str, int, str],
    ) -> None:
        metric_count, metric_digest = self._verify_metric_prefix(state.metric_cursor)
        diagnostic_count, diagnostic_digest = self._verify_diagnostic_prefix(
            state.diagnostic_cursor
        )
        if (metric_count, metric_digest) != (state.metric_count, state.metric_digest):
            raise WorkspaceV2InventoryError("workspace v2 metric checkpoint prefix changed")
        if (diagnostic_count, diagnostic_digest) != (
            state.diagnostic_count,
            state.diagnostic_digest,
        ):
            raise WorkspaceV2InventoryError("workspace v2 diagnostic checkpoint prefix changed")
        if (
            state.phase in ("diagnostics", "verification", "complete")
            and (
                metric_count,
                metric_digest,
            )
            != expected[:2]
        ):
            raise WorkspaceV2InventoryError("workspace v2 metric source prefix is incomplete")
        if (
            state.phase in ("verification", "complete")
            and (
                diagnostic_count,
                diagnostic_digest,
            )
            != expected[2:]
        ):
            raise WorkspaceV2InventoryError("workspace v2 diagnostic source prefix is incomplete")

    def _verify_metric_prefix(self, cursor: HistoricalAnalyticalCursor | None) -> tuple[int, str]:
        count = 0
        digest = _EMPTY_DIGEST
        after: HistoricalAnalyticalCursor | None = None
        if cursor is None:
            return count, digest
        while cursor is None or after != cursor:
            page = self._source.list_metrics_page(limit=self._page_limit, after=after)
            if not page:
                break
            self._max_page_hydrated = max(self._max_page_hydrated, len(page))
            expected = {item.result_id: item for item in page}
            stored = self._target.get_existing_metrics(expected)
            if stored != expected:
                raise WorkspaceV2InventoryError("workspace v2 confirmed metric prefix differs")
            for item in page:
                digest = _fold_digest(digest, item)
                count += 1
            last = page[-1]
            after = HistoricalAnalyticalCursor(
                available_at=last.available_at, identifier=last.result_id
            )
        if cursor is not None and after != cursor:
            raise WorkspaceV2InventoryError("workspace v2 metric checkpoint cursor is absent")
        return count, digest

    def _verify_diagnostic_prefix(
        self, cursor: HistoricalAnalyticalCursor | None
    ) -> tuple[int, str]:
        count = 0
        digest = _EMPTY_DIGEST
        after: HistoricalAnalyticalCursor | None = None
        if cursor is None:
            return count, digest
        while cursor is None or after != cursor:
            page = self._source.list_diagnostics_page(limit=self._page_limit, after=after)
            if not page:
                break
            self._max_page_hydrated = max(self._max_page_hydrated, len(page))
            expected = {item.diagnostic_id: item for item in page}
            stored = self._target.get_existing_diagnostics(expected)
            if stored != expected:
                raise WorkspaceV2InventoryError("workspace v2 confirmed diagnostic prefix differs")
            for item in page:
                digest = _fold_digest(digest, item)
                count += 1
            last = page[-1]
            after = HistoricalAnalyticalCursor(
                available_at=last.available_at, identifier=last.diagnostic_id
            )
        if cursor is not None and after != cursor:
            raise WorkspaceV2InventoryError("workspace v2 diagnostic checkpoint cursor is absent")
        return count, digest

    def _import_metrics(
        self, state: WorkspaceV2InventoryState
    ) -> tuple[WorkspaceV2InventoryState, int, int]:
        created = reused = 0
        after = state.metric_cursor
        digest = state.metric_digest
        count = state.metric_count
        while True:
            page = self._source.list_metrics_page(limit=self._page_limit, after=after)
            if not page:
                state = self._updated_state(state, phase="diagnostics")
                self._write_state(state)
                return state, created, reused
            self._max_page_hydrated = max(self._max_page_hydrated, len(page))
            receipt = self._target.save_metrics(page, origin="HISTORICAL")
            if self._failure_injector is not None:
                self._failure_injector("after_metric_page_write")
            for item in page:
                digest = _fold_digest(digest, item)
            count += len(page)
            last = page[-1]
            after = HistoricalAnalyticalCursor(
                available_at=last.available_at, identifier=last.result_id
            )
            state = self._updated_state(
                state,
                metric_cursor=after,
                metric_count=count,
                metric_digest=digest,
            )
            self._write_state(state)
            created += len(receipt.created_ids)
            reused += len(receipt.reused_ids)

    def _import_diagnostics(
        self, state: WorkspaceV2InventoryState
    ) -> tuple[WorkspaceV2InventoryState, int, int]:
        created = reused = 0
        after = state.diagnostic_cursor
        digest = state.diagnostic_digest
        count = state.diagnostic_count
        while True:
            page = self._source.list_diagnostics_page(limit=self._page_limit, after=after)
            if not page:
                state = self._updated_state(state, phase="verification")
                self._write_state(state)
                return state, created, reused
            self._max_page_hydrated = max(self._max_page_hydrated, len(page))
            receipt = self._target.save_diagnostics(page, origin="HISTORICAL")
            if self._failure_injector is not None:
                self._failure_injector("after_diagnostic_page_write")
            for item in page:
                digest = _fold_digest(digest, item)
            count += len(page)
            last = page[-1]
            after = HistoricalAnalyticalCursor(
                available_at=last.available_at, identifier=last.diagnostic_id
            )
            state = self._updated_state(
                state,
                diagnostic_cursor=after,
                diagnostic_count=count,
                diagnostic_digest=digest,
            )
            self._write_state(state)
            created += len(receipt.created_ids)
            reused += len(receipt.reused_ids)

    def _updated_state(
        self,
        state: WorkspaceV2InventoryState,
        *,
        phase: Literal["metrics", "diagnostics", "verification", "complete"] | None = None,
        metric_cursor: HistoricalAnalyticalCursor | None = None,
        metric_count: int | None = None,
        metric_digest: str | None = None,
        diagnostic_cursor: HistoricalAnalyticalCursor | None = None,
        diagnostic_count: int | None = None,
        diagnostic_digest: str | None = None,
    ) -> WorkspaceV2InventoryState:
        return state.model_copy(
            update={
                "phase": state.phase if phase is None else phase,
                "metric_cursor": metric_cursor if metric_count is not None else state.metric_cursor,
                "metric_count": state.metric_count if metric_count is None else metric_count,
                "metric_digest": state.metric_digest if metric_digest is None else metric_digest,
                "diagnostic_cursor": (
                    diagnostic_cursor if diagnostic_count is not None else state.diagnostic_cursor
                ),
                "diagnostic_count": (
                    state.diagnostic_count if diagnostic_count is None else diagnostic_count
                ),
                "diagnostic_digest": (
                    state.diagnostic_digest if diagnostic_digest is None else diagnostic_digest
                ),
                "updated_at": self._clock(),
            }
        )

    def _write_state(self, state: WorkspaceV2InventoryState) -> None:
        if self._state_path.is_symlink():
            raise WorkspaceV2InventoryError("workspace v2 import checkpoint is a symbolic link")
        self._state_root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            state.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        temporary = self._state_path.with_name(f".{self._state_path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(f"{payload}\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self._state_path)
        finally:
            temporary.unlink(missing_ok=True)


__all__ = [
    "WorkspaceV2AnalyticalImporter",
    "WorkspaceV2InventoryError",
    "WorkspaceV2InventoryState",
    "WorkspaceV2InventorySummary",
]
