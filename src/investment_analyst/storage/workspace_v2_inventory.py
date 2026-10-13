"""Resumable transfer of a verified historical archive into compact workspace v2."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import ConfigDict, Field, field_validator

from investment_analyst.core.models import DiagnosticResult, MetricResult
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.storage.compact_analytical_v2 import CompactAnalyticalStore
from investment_analyst.storage.errors import StorageError
from investment_analyst.storage.historical_analytical_archive import (
    HistoricalAnalyticalArchive,
    HistoricalAnalyticalCursor,
)
from investment_analyst.storage.historical_analytical_import import (
    DirectHistoricalAnalyticalSource,
)
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex
from investment_analyst.workspace.workspace_v2_migration_models import (
    WorkspaceV2MigrationVerificationProof,
    WorkspaceV2MigrationWorkCheckpoint,
    WorkspaceV2MigrationWorkPhase,
)

_PAGE = 256
_STATE_FILENAME = "workspace-v2-analytical-import-state.json"
_DIRECT_STATE_FILENAME = "direct-historical-import-state.json"
_ARCHIVE_POLICY = "workspace-v2-analytical-import-v1"
_DIRECT_POLICY = "direct-historical-import-v1"
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
    policy_version: Literal["workspace-v2-analytical-import-v1", "direct-historical-import-v1"] = (
        _ARCHIVE_POLICY
    )
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


def _inventory_digest(inventory: tuple[int, str, int, str]) -> str:
    return hashlib.sha256(
        json.dumps(inventory, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()


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
        source: HistoricalAnalyticalArchive | DirectHistoricalAnalyticalSource,
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
        allow_source_transition: bool = False,
        direct_transport: bool = False,
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
        self._allow_source_transition = allow_source_transition
        self._policy_version = _DIRECT_POLICY if direct_transport else _ARCHIVE_POLICY
        self._max_page_hydrated = 0
        self._source_root = source_path
        self._destination_root = destination_path
        self._state_root = state_path
        filename = _DIRECT_STATE_FILENAME if direct_transport else _STATE_FILENAME
        self._state_path = state_path / filename

    def _verification_checkpoint_path(self, family: Literal["metrics", "diagnostics"]) -> Path:
        return self._state_root / f"{self._policy_version}-{family}-verification-work.json"

    def _verification_checkpoint(
        self,
        *,
        family: Literal["metrics", "diagnostics"],
        cursor: HistoricalAnalyticalCursor,
        confirmed_count: int,
        digest: str,
    ) -> WorkspaceV2MigrationWorkCheckpoint:
        return WorkspaceV2MigrationWorkCheckpoint(
            operation_id=uuid5(
                NAMESPACE_URL,
                f"{self._policy_version}:{self._source_fingerprint}:{self._destination_root}",
            ),
            phase=WorkspaceV2MigrationWorkPhase.VERIFY,
            snapshot_id=uuid5(NAMESPACE_URL, self._source_fingerprint),
            source_fingerprint=self._source_fingerprint,
            destination_workspace_id=uuid5(NAMESPACE_URL, str(self._destination_root)),
            source_root=self._source_root,
            destination_root=self._destination_root,
            policy_version=self._policy_version,
            reader_policy="direct-historical-readonly-v1",
            verifier_policy=f"paired-{family}-inventory-v1",
            tree_fingerprint=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            environment_fingerprint=hashlib.sha256(
                f"{self._page_limit}:{self._policy_version}".encode("ascii")
            ).hexdigest(),
            cursor=f"{cursor.available_at.isoformat()}|{cursor.identifier}",
            ordinal=(confirmed_count + self._page_limit - 1) // self._page_limit,
            offset_bytes=0,
            page_limit=self._page_limit,
            confirmed_count=confirmed_count,
            confirmed_bytes=0,
            accumulated_digest=digest,
            input_references=(family,),
            updated_at=self._clock(),
        )

    def _write_verification_checkpoint(
        self,
        *,
        family: Literal["metrics", "diagnostics"],
        cursor: HistoricalAnalyticalCursor,
        confirmed_count: int,
        digest: str,
    ) -> None:
        path = self._verification_checkpoint_path(family)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    self._verification_checkpoint(
                        family=family,
                        cursor=cursor,
                        confirmed_count=confirmed_count,
                        digest=digest,
                    ).model_dump_json()
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_verification_checkpoint(
        self, family: Literal["metrics", "diagnostics"]
    ) -> tuple[HistoricalAnalyticalCursor | None, int, str]:
        path = self._verification_checkpoint_path(family)
        if not path.exists():
            return None, 0, _EMPTY_DIGEST
        if path.is_symlink() or not path.is_file():
            raise WorkspaceV2InventoryError("verification checkpoint is unsafe")
        try:
            checkpoint = WorkspaceV2MigrationWorkCheckpoint.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            text_cursor = checkpoint.cursor
            timestamp, identifier = text_cursor.split("|", 1) if text_cursor else ("", "")
            cursor = HistoricalAnalyticalCursor(
                available_at=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
                identifier=UUID(identifier),
            )
        except (OSError, ValueError, TypeError) as error:
            raise WorkspaceV2InventoryError("verification checkpoint is malformed") from error
        if (
            checkpoint.phase is not WorkspaceV2MigrationWorkPhase.VERIFY
            or checkpoint.source_fingerprint != self._source_fingerprint
            or checkpoint.destination_root != self._destination_root
            or checkpoint.policy_version != self._policy_version
            or checkpoint.page_limit != self._page_limit
            or checkpoint.input_references != (family,)
        ):
            raise WorkspaceV2InventoryError("verification checkpoint binding changed")
        return cursor, checkpoint.confirmed_count, checkpoint.accumulated_digest

    def run(self) -> WorkspaceV2InventorySummary:
        """Resume only from an exact source/destination-bound checkpoint."""
        state = self._load_or_create_state()
        try:
            self._validate_binding(state)
        except WorkspaceV2InventoryError:
            if not self._allow_source_transition:
                raise
            self._archive_historic_state()
            state = WorkspaceV2InventoryState(
                source_workspace_id=self._source_workspace_id,
                source_fingerprint=self._source_fingerprint,
                policy_version=self._policy_version,
                metric_count=0,
                metric_digest=_EMPTY_DIGEST,
                diagnostic_count=0,
                diagnostic_digest=_EMPTY_DIGEST,
                phase="metrics",
                page_limit=self._page_limit,
                updated_at=self._clock(),
            )
            self._write_state(state)
        if state.phase not in ("verification", "complete"):
            self._validate_confirmed_prefix(state)

        metric_created = metric_reused = 0
        diagnostic_created = diagnostic_reused = 0
        if state.phase == "metrics":
            state, metric_created, metric_reused = self._import_metrics(state)
        if state.phase == "diagnostics":
            state, diagnostic_created, diagnostic_reused = self._import_diagnostics(state)
        if state.phase in ("verification", "complete"):
            actual = self._validate_confirmed_prefix(state)
            expected = (
                state.metric_count,
                state.metric_digest,
                state.diagnostic_count,
                state.diagnostic_digest,
            )
            if actual != expected:
                raise WorkspaceV2InventoryError(
                    "workspace v2 historical digest differs from source"
                )
            if (
                self._target.count_metrics(origin="HISTORICAL"),
                self._target.count_diagnostics(origin="HISTORICAL"),
            ) != (state.metric_count, state.diagnostic_count):
                raise WorkspaceV2InventoryError("workspace v2 historical count differs from source")
            metric_count, metric_digest, diagnostic_count, diagnostic_digest = actual
            self._target.seal_historical_inventory(
                source_fingerprint=self._source_fingerprint,
                metric_count=metric_count,
                metric_digest=metric_digest,
                diagnostic_count=diagnostic_count,
                diagnostic_digest=diagnostic_digest,
                sealed_at=self._clock(),
                allow_source_transition=self._allow_source_transition,
                verification_proof=(
                    self._verification_proof(actual)
                    if isinstance(self._source, DirectHistoricalAnalyticalSource)
                    else None
                ),
            )
            self._target.verify_historical_relationships()
            if state.phase != "complete":
                state = self._updated_state(state, phase="complete")
                self._write_state(state)
        if state.phase != "complete":
            raise WorkspaceV2InventoryError("workspace v2 historical import is incomplete")
        return WorkspaceV2InventorySummary(
            source_workspace_id=self._source_workspace_id,
            source_fingerprint=self._source_fingerprint,
            metric_count=state.metric_count,
            metric_digest=state.metric_digest,
            diagnostic_count=state.diagnostic_count,
            diagnostic_digest=state.diagnostic_digest,
            metric_created_count=metric_created,
            metric_reused_count=metric_reused,
            diagnostic_created_count=diagnostic_created,
            diagnostic_reused_count=diagnostic_reused,
            max_page_requested=self._page_limit,
            max_page_hydrated=self._max_page_hydrated,
        )

    def _verification_proof(
        self, inventory: tuple[int, str, int, str]
    ) -> WorkspaceV2MigrationVerificationProof:
        inventory_digest = _inventory_digest(inventory)
        operation_id = uuid5(
            NAMESPACE_URL,
            f"{self._policy_version}:{self._source_fingerprint}:{self._destination_root}",
        )
        destination_id = uuid5(NAMESPACE_URL, str(self._destination_root))
        source_id = UUID(self._source_workspace_id)
        tree_fingerprint = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        environment_fingerprint = hashlib.sha256(
            f"{self._page_limit}:{self._policy_version}".encode("ascii")
        ).hexdigest()
        physical_binding = hashlib.sha256(
            f"{self._source_fingerprint}:{self._destination_root}:{inventory_digest}".encode()
        ).hexdigest()
        return WorkspaceV2MigrationVerificationProof(
            operation_id=operation_id,
            snapshot_id=uuid5(NAMESPACE_URL, self._source_fingerprint),
            source_workspace_id=source_id,
            destination_workspace_id=destination_id,
            source_root=self._source_root,
            destination_root=self._destination_root,
            source_fingerprint=self._source_fingerprint,
            inventory_digest=inventory_digest,
            family="analytical",
            subphase="paired-models",
            reader_policy="direct-historical-readonly-v1",
            verifier_policy="paired-model-inventory-v1",
            tree_fingerprint=tree_fingerprint,
            environment_fingerprint=environment_fingerprint,
            coverage_count=inventory[0] + inventory[2],
            source_digest=inventory_digest,
            destination_digest=inventory_digest,
            ordered_unit_proof_root=inventory_digest,
            physical_binding=physical_binding,
            verified_at=self._clock(),
        )

    def _load_or_create_state(self) -> WorkspaceV2InventoryState:
        self._state_root.mkdir(parents=True, exist_ok=True)
        if self._state_path.exists() or self._state_path.is_symlink():
            return _read_state(self._state_path)
        state = WorkspaceV2InventoryState(
            source_workspace_id=self._source_workspace_id,
            source_fingerprint=self._source_fingerprint,
            policy_version=self._policy_version,
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
            or state.policy_version != self._policy_version
            or state.page_limit != self._page_limit
        ):
            raise WorkspaceV2InventoryError(
                "workspace v2 import checkpoint belongs to another source"
            )

    def _archive_historic_state(self) -> None:
        original = self._state_path.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        archive = self._state_path.with_name(
            f"workspace-v2-analytical-import-state.A.{digest}.json"
        )
        if archive.exists():
            if archive.is_symlink() or archive.read_bytes() != original:
                raise WorkspaceV2InventoryError(
                    "historic analytical checkpoint cannot be preserved"
                )
            return
        temporary = archive.with_name(f".{archive.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)

    def _validate_confirmed_prefix(
        self,
        state: WorkspaceV2InventoryState,
    ) -> tuple[int, str, int, str]:
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
        return metric_count, metric_digest, diagnostic_count, diagnostic_digest

    def _verify_metric_prefix(self, cursor: HistoricalAnalyticalCursor | None) -> tuple[int, str]:
        if isinstance(self._source, DirectHistoricalAnalyticalSource):
            return self._verify_direct_metric_prefix(cursor)
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

    def _verify_direct_metric_prefix(
        self, cursor: HistoricalAnalyticalCursor | None
    ) -> tuple[int, str]:
        after, count, digest = self._load_verification_checkpoint("metrics")
        if cursor is None:
            return count, digest
        if after == cursor:
            return count, digest
        for page in self._source.iter_metric_pages(limit=self._page_limit, after=after):
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
            self._write_verification_checkpoint(
                family="metrics",
                cursor=after,
                confirmed_count=count,
                digest=digest,
            )
            if after == cursor:
                return count, digest
            if after.available_at > cursor.available_at or (
                after.available_at == cursor.available_at and after.identifier > cursor.identifier
            ):
                break
        raise WorkspaceV2InventoryError("workspace v2 metric checkpoint cursor is absent")

    def _verify_diagnostic_prefix(
        self, cursor: HistoricalAnalyticalCursor | None
    ) -> tuple[int, str]:
        if isinstance(self._source, DirectHistoricalAnalyticalSource):
            return self._verify_direct_diagnostic_prefix(cursor)
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

    def _verify_direct_diagnostic_prefix(
        self, cursor: HistoricalAnalyticalCursor | None
    ) -> tuple[int, str]:
        after, count, digest = self._load_verification_checkpoint("diagnostics")
        if cursor is None:
            return count, digest
        if after == cursor:
            return count, digest
        for page in self._source.iter_diagnostic_pages(limit=self._page_limit, after=after):
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
            self._write_verification_checkpoint(
                family="diagnostics",
                cursor=after,
                confirmed_count=count,
                digest=digest,
            )
            if after == cursor:
                return count, digest
            if after.available_at > cursor.available_at or (
                after.available_at == cursor.available_at and after.identifier > cursor.identifier
            ):
                break
        raise WorkspaceV2InventoryError("workspace v2 diagnostic checkpoint cursor is absent")

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
