"""Bounded, resumable migration from a read-only v1 workspace to workspace v2.

The module only coordinates the existing verified importers.  It deliberately
does not read provider data or derive financial values: every model is copied
through its existing v1/v2 persistence contracts and is re-read by those
contracts before a page checkpoint is advanced.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.storage.compact_analytical_v2 import CompactAnalyticalStore
from investment_analyst.storage.historical_analytical_import import (
    DirectHistoricalAnalyticalSource,
)
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.observation_v2_import import (
    ObservationV2Importer,
    ObservationV2ImportProgress,
    ObservationV2ImportSummary,
)
from investment_analyst.storage.raw_v2_import import (
    RawV2Importer,
    RawV2ImportProgress,
    RawV2ImportState,
    RawV2ImportSummary,
)
from investment_analyst.storage.workspace_v2 import WorkspaceV2Store
from investment_analyst.storage.workspace_v2_inventory import (
    WorkspaceV2AnalyticalImporter,
    WorkspaceV2InventorySummary,
)
from investment_analyst.workspace.backup import WorkspaceBackupService
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import (
    WorkspaceAccessError,
    WorkspaceService,
)
from investment_analyst.workspace.workspace_v2_migration_models import (
    WorkspaceV1MigrationSourceSnapshot,
    WorkspaceV2MigrationPhase,
    WorkspaceV2SourceTransitionState,
)

_MAX_PAGE_LIMIT = 256
_DEFAULT_RESERVE_BYTES = 5_000_000_000
_SOURCE_SNAPSHOT_FILENAME = "workspace-v1-migration-source-snapshot.json"


class WorkspaceV2MigrationError(RuntimeError):
    """Raised when a migration cannot prove its source or destination boundary."""


class WorkspaceV2MigrationProgress(ContractModel):
    """Safe event emitted by a resumable workspace migration phase."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    phase: Literal[
        "inspection_started",
        "inspection_completed",
        "source_hash_started",
        "source_hash_completed",
        "identity_bound",
        "documents_started",
        "documents_completed",
        "raw_prefix_validation_started",
        "raw_prefix_validation_page_verified",
        "raw_prefix_validation_completed",
        "raw_import_page_started",
        "raw_import_page_checkpointed",
        "raw_completion_validation_started",
        "raw_completion_validation_page_verified",
        "raw_completion_validation_completed",
        "observations_started",
        "observations_prefix_validation_started",
        "observations_prefix_validation_page_verified",
        "observations_prefix_validation_completed",
        "observations_import_page_started",
        "observations_import_page_checkpointed",
        "observations_completion_validation_started",
        "observations_completion_validation_page_verified",
        "observations_completion_validation_completed",
        "observations_completed",
        "historical_started",
        "historical_completed",
        "analytical_started",
        "analytical_completed",
        "closure_completed",
    ]
    confirmed_count: int = Field(ge=0)
    page_count: int = Field(ge=0)
    page_size: int = Field(ge=0, le=_MAX_PAGE_LIMIT)

    def to_json_dict(self) -> dict[str, object]:
        """Return only counters and a phase name for operational evidence."""
        return self.model_dump(mode="json")


class WorkspaceV2MigrationSummary(ContractModel):
    """Exact, serializable receipt for one complete migration run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-migration-summary-v1"
    source_workspace_id: NonEmptyStr
    source_fingerprint: NonEmptyStr
    source_root: Path
    destination_root: Path
    destination_workspace_id: NonEmptyStr
    source_snapshot_id: NonEmptyStr | None = None
    page_limit: int = Field(ge=1, le=_MAX_PAGE_LIMIT)
    raw: RawV2ImportSummary
    observations: ObservationV2ImportSummary
    historical: WorkspaceV2InventorySummary
    analytical: WorkspaceV2InventorySummary
    document_count: int = Field(ge=0)
    completed_at: UTCDateTime

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(service: WorkspaceService, source_root: Path) -> str:
    """Return a stable identity binding manifest and v1 database bytes."""
    paths = service.resolve(source_root)
    if paths.root.is_symlink() or paths.manifest_path.is_symlink():
        raise WorkspaceV2MigrationError("migration source must not contain symbolic links")
    inspection = service.inspect(paths.root)
    if inspection.format_version != 1 or inspection.errors:
        raise WorkspaceV2MigrationError("migration source must be a ready workspace v1")
    database = paths.storage_root / "data" / "processed" / "investment_analyst.duckdb"
    if not database.is_file() or database.is_symlink():
        raise WorkspaceV2MigrationError("migration source database is unavailable or unsafe")
    digest = hashlib.sha256()
    for label, path in (("manifest", paths.manifest_path), ("database", database)):
        digest.update(label.encode("ascii"))
        digest.update(b"\0")
        digest.update(_hash_file(path).encode("ascii"))
        digest.update(b"\0")
    documents = paths.storage_root / "data" / "documents" / "sha256"
    if documents.exists():
        if documents.is_symlink() or not documents.is_dir():
            raise WorkspaceV2MigrationError("migration source document store is unsafe")
        for document in sorted(documents.rglob("*")):
            if document.is_symlink():
                raise WorkspaceV2MigrationError("migration source document store is unsafe")
            if not document.is_file():
                continue
            digest.update(document.relative_to(documents).as_posix().encode("ascii"))
            digest.update(b"\0")
            digest.update(_hash_file(document).encode("ascii"))
            digest.update(b"\0")
    return digest.hexdigest()


def _snapshot_inventory_digest(files: object) -> str:
    """Hash the backup service's already verified physical inventory."""
    encoded = json.dumps(
        [item.model_dump(mode="json") for item in files],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_v1_migration_source_snapshot(
    *,
    service: WorkspaceService,
    source_root: Path,
    snapshot_root: Path,
    host_free_bytes: Callable[[], int],
    reserve_bytes: int = _DEFAULT_RESERVE_BYTES,
) -> WorkspaceV1MigrationSourceSnapshot:
    """Create a quiesced, verified source copy and publish its descriptor.

    ``WorkspaceBackupService`` owns the process guard, immutable file inventory,
    copy and verification.  The descriptor is written only after the backup has
    atomically appeared, so an interrupted snapshot is never a migration source.
    """
    source_paths = service.resolve(source_root)
    snapshot_paths = service.resolve(snapshot_root)
    _validate_destination(source_paths.root, snapshot_paths.root)
    inspection = service.inspect(source_paths.root)
    if inspection.format_version != 1 or inspection.status != "ready":
        raise WorkspaceV2MigrationError("migration snapshot source must be a ready workspace v1")
    fingerprint = source_fingerprint(service, source_paths.root)
    estimated_copy_bytes = sum(
        path.stat().st_size for path in source_paths.root.rglob("*") if path.is_file()
    )
    _require_host_capacity(
        host_free_bytes,
        reserve_bytes=reserve_bytes,
        expected_write_bytes=estimated_copy_bytes,
    )
    manifest = WorkspaceBackupService(service).create(source_paths.root, snapshot_paths.root)
    descriptor = WorkspaceV1MigrationSourceSnapshot(
        snapshot_id=manifest.backup_id,
        source_workspace_id=inspection.workspace_id,
        source_created_at=service.manifest(source_paths.root).created_at,
        source_fingerprint=fingerprint,
        snapshot_root=snapshot_paths.root,
        created_at=manifest.created_at,
        physical_file_count=len(manifest.files),
        physical_total_bytes=sum(item.size_bytes for item in manifest.files),
        physical_inventory_sha256=_snapshot_inventory_digest(manifest.files),
    )
    try:
        service.publish_v1_migration_source_snapshot(
            snapshot_paths=snapshot_paths,
            descriptor=descriptor,
        )
    except Exception as error:
        raise WorkspaceV2MigrationError(
            "migration source snapshot descriptor could not be published"
        ) from error
    return descriptor


def load_v1_migration_source_snapshot(
    *, service: WorkspaceService, snapshot_root: Path
) -> WorkspaceV1MigrationSourceSnapshot:
    """Open a published source snapshot and prove its logical binding again."""
    paths = service.resolve(snapshot_root)
    descriptor_path = paths.state_root / _SOURCE_SNAPSHOT_FILENAME
    if descriptor_path.is_symlink() or not descriptor_path.is_file():
        raise WorkspaceV2MigrationError("migration source snapshot descriptor is missing or unsafe")
    try:
        descriptor = WorkspaceV1MigrationSourceSnapshot.model_validate_json(
            descriptor_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise WorkspaceV2MigrationError(
            "migration source snapshot descriptor is incompatible"
        ) from error
    if descriptor.snapshot_root != paths.root:
        raise WorkspaceV2MigrationError("migration source snapshot root does not match descriptor")
    inspection = service.inspect(paths.root)
    if (
        inspection.format_version != 1
        or inspection.errors
        or inspection.workspace_id != descriptor.source_workspace_id
        or source_fingerprint(service, paths.root) != descriptor.source_fingerprint
    ):
        raise WorkspaceV2MigrationError(
            "migration source snapshot no longer matches its descriptor"
        )
    return descriptor


def _validate_destination(source: Path, destination: Path) -> None:
    if not destination.is_absolute():
        raise WorkspaceV2MigrationError("migration destination must be absolute")
    if destination.is_symlink():
        raise WorkspaceV2MigrationError("migration destination must not be a symbolic link")
    source = source.resolve()
    destination = destination.resolve(strict=False)
    if destination == source or source in destination.parents or destination in source.parents:
        raise WorkspaceV2MigrationError("migration destination must be disjoint from source")


def _require_host_capacity(
    host_free_bytes: Callable[[], int],
    *,
    reserve_bytes: int,
    expected_write_bytes: int = 0,
) -> int:
    """Read host capacity through the caller-provided host API, never WSL statvfs."""
    available = host_free_bytes()
    if isinstance(available, bool) or not isinstance(available, int) or available < 0:
        raise WorkspaceV2MigrationError("host capacity reader returned an invalid value")
    if (
        isinstance(expected_write_bytes, bool)
        or not isinstance(expected_write_bytes, int)
        or expected_write_bytes < 0
    ):
        raise WorkspaceV2MigrationError("migration expected write bytes must be non-negative")
    required = reserve_bytes + expected_write_bytes
    if available < required:
        raise WorkspaceV2MigrationError("migration host disk reserve is insufficient")
    return available


def _emit(
    progress: Callable[[WorkspaceV2MigrationProgress], None] | None,
    phase: str,
    *,
    confirmed_count: int = 0,
    page_count: int = 0,
    page_size: int = 0,
) -> None:
    if progress is None:
        return
    try:
        progress(
            WorkspaceV2MigrationProgress(
                phase=phase,
                confirmed_count=confirmed_count,
                page_count=page_count,
                page_size=page_size,
            )
        )
    except Exception as error:
        raise WorkspaceV2MigrationError("migration progress observer failed") from error


def _raw_phase(progress: RawV2ImportProgress) -> WorkspaceV2MigrationProgress:
    mapping = {
        "prefix_validation_started": "raw_prefix_validation_started",
        "prefix_validation_page_verified": "raw_prefix_validation_page_verified",
        "prefix_validation_completed": "raw_prefix_validation_completed",
        "import_page_started": "raw_import_page_started",
        "import_page_checkpointed": "raw_import_page_checkpointed",
        "completion_validation_started": "raw_completion_validation_started",
        "completion_validation_page_verified": "raw_completion_validation_page_verified",
        "completion_validation_completed": "raw_completion_validation_completed",
    }
    return WorkspaceV2MigrationProgress(
        phase=mapping[progress.phase],
        confirmed_count=progress.confirmed_count,
        page_count=progress.page_count,
        page_size=progress.page_size,
    )


def _observation_phase(progress: ObservationV2ImportProgress) -> WorkspaceV2MigrationProgress:
    mapping = {
        "prefix_validation_started": "observations_prefix_validation_started",
        "prefix_validation_page_verified": "observations_prefix_validation_page_verified",
        "prefix_validation_completed": "observations_prefix_validation_completed",
        "import_page_started": "observations_import_page_started",
        "import_page_checkpointed": "observations_import_page_checkpointed",
        "completion_validation_started": "observations_completion_validation_started",
        "completion_validation_page_verified": "observations_completion_validation_page_verified",
        "completion_validation_completed": "observations_completion_validation_completed",
    }
    return WorkspaceV2MigrationProgress(
        phase=mapping[progress.phase],
        confirmed_count=progress.confirmed_count,
        page_count=progress.page_count,
        page_size=progress.page_size,
    )


def _copy_documents(source_documents: Path, destination: LocalStorage) -> int:
    if not source_documents.exists():
        return 0
    if source_documents.is_symlink() or not source_documents.is_dir():
        raise WorkspaceV2MigrationError("migration source document store is unsafe")
    copied = 0
    for document in sorted(source_documents.rglob("*")):
        if document.is_symlink():
            raise WorkspaceV2MigrationError("migration source document store is unsafe")
        if not document.is_file():
            continue
        checksum = document.name
        if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
            raise WorkspaceV2MigrationError("migration source document name is not a SHA-256")
        receipt = destination.documents.copy_verified_file(document, checksum)
        if receipt.sha256 != checksum:
            raise WorkspaceV2MigrationError("migration document identity changed during copy")
        copied += 1
    return copied


def migrate_workspace_v1_to_v2(
    *,
    service: WorkspaceService,
    source_root: Path,
    destination_root: Path,
    page_limit: int = _MAX_PAGE_LIMIT,
    reserve_bytes: int = _DEFAULT_RESERVE_BYTES,
    host_free_bytes: Callable[[], int] | None = None,
    progress: Callable[[WorkspaceV2MigrationProgress], None] | None = None,
    clock: Callable[[], datetime] | None = None,
    reconcile_source: bool = False,
    analytical_failure_injector: Callable[[str], None] | None = None,
) -> WorkspaceV2MigrationSummary:
    """Migrate an unchanged v1 source into a disjoint, resumable v2 workspace."""
    if (
        isinstance(page_limit, bool)
        or not isinstance(page_limit, int)
        or not 1 <= page_limit <= 256
    ):
        raise WorkspaceV2MigrationError("migration page limit must be an integer between 1 and 256")
    if isinstance(reserve_bytes, bool) or not isinstance(reserve_bytes, int) or reserve_bytes < 0:
        raise WorkspaceV2MigrationError("migration reserve bytes must be a non-negative integer")
    if host_free_bytes is None:
        raise WorkspaceV2MigrationError("migration requires an explicit host capacity reader")
    _require_host_capacity(host_free_bytes, reserve_bytes=reserve_bytes)
    _emit(progress, "inspection_started")
    source_paths = service.resolve(source_root)
    destination_paths = service.resolve(destination_root)
    _validate_destination(source_paths.root, destination_paths.root)
    snapshot = (
        load_v1_migration_source_snapshot(service=service, snapshot_root=source_paths.root)
        if reconcile_source
        else None
    )
    source_inspection = service.inspect(source_paths.root)
    if (
        source_inspection.format_version != 1
        or source_inspection.errors
        or (snapshot is None and source_inspection.status != "ready")
    ):
        raise WorkspaceV2MigrationError("migration source must be a ready workspace v1")
    _emit(progress, "inspection_completed")
    _require_host_capacity(host_free_bytes, reserve_bytes=reserve_bytes)
    _emit(progress, "source_hash_started")
    fingerprint = source_fingerprint(service, source_paths.root)
    if snapshot is not None and snapshot.source_fingerprint != fingerprint:
        raise WorkspaceV2MigrationError("migration source snapshot fingerprint changed")
    _emit(progress, "source_hash_completed")
    if destination_paths.root.exists() and not destination_paths.manifest_path.exists():
        raise WorkspaceV2MigrationError(
            "migration destination must be new or an initialized workspace v2"
        )
    initialized = service.initialize(destination_paths.root, format_version=2)
    historic_fingerprint: str | None = None
    historic_raw_digest: str | None = None
    transition: WorkspaceV2SourceTransitionState | None = None
    try:
        historic_identity = service.v1_migration_identity(initialized.paths)
    except WorkspaceAccessError:
        historic_identity = None
    if historic_identity is None:
        service.bind_v1_migration_identity(
            source_paths=source_paths,
            destination_paths=initialized.paths,
            source_fingerprint=fingerprint,
        )
    elif historic_identity.source_fingerprint == fingerprint:
        pass
    elif snapshot is None:
        raise WorkspaceV2MigrationError("migration source differs from the historic identity")
    else:
        source_manifest = service.manifest(source_paths.root)
        if (
            historic_identity.source_workspace_id != source_manifest.workspace_id
            or historic_identity.source_created_at != source_manifest.created_at
        ):
            raise WorkspaceV2MigrationError(
                "migration snapshot has another logical source identity"
            )
        historic_fingerprint = historic_identity.source_fingerprint
        with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_ONLY) as target:
            if not isinstance(target.store, WorkspaceV2Store):
                raise WorkspaceV2MigrationError("migration destination did not open workspace v2")
            checkpoint = target.store.raw_staging.destination / "raw-v2-import-state.json"
            if checkpoint.is_symlink() or not checkpoint.is_file():
                raise WorkspaceV2MigrationError("historic raw checkpoint is missing or unsafe")
            checkpoint_hash = _hash_file(checkpoint)
            try:
                historic_raw_digest = RawV2ImportState.model_validate_json(
                    checkpoint.read_text(encoding="utf-8")
                ).accumulated_digest
            except (OSError, ValueError) as error:
                raise WorkspaceV2MigrationError(
                    "historic raw checkpoint is incompatible"
                ) from error
        transition = service.v2_source_transition(initialized.paths)
        if transition is None:
            transition = WorkspaceV2SourceTransitionState(
                phase=WorkspaceV2MigrationPhase.PREPARED,
                source_workspace_id=historic_identity.source_workspace_id,
                source_created_at=historic_identity.source_created_at,
                historic_source_fingerprint=historic_fingerprint,
                historic_checkpoint_sha256=checkpoint_hash,
                snapshot_id=snapshot.snapshot_id,
                snapshot_source_fingerprint=snapshot.source_fingerprint,
                destination_workspace_id=initialized.manifest.workspace_id,
                confirmed_equivalent_count=0,
                unconfirmed_equivalent_count=0,
                missing_count=0,
                conflicting_count=0,
                updated_at=(clock or (lambda: datetime.now(UTC)))(),
            )
            service.publish_v2_source_transition(
                destination_paths=initialized.paths,
                state=transition,
            )
        else:
            historic_checkpoint = checkpoint.with_name(
                f"raw-v2-import-state.A.{transition.historic_checkpoint_sha256}.json"
            )
            if (
                historic_checkpoint.is_symlink()
                or not historic_checkpoint.is_file()
                or _hash_file(historic_checkpoint) != transition.historic_checkpoint_sha256
                or transition.snapshot_id != snapshot.snapshot_id
                or transition.snapshot_source_fingerprint != snapshot.source_fingerprint
            ):
                raise WorkspaceV2MigrationError(
                    "migration source transition does not match the checkpoint"
                )
            try:
                historic_raw_digest = RawV2ImportState.model_validate_json(
                    historic_checkpoint.read_text(encoding="utf-8")
                ).accumulated_digest
            except (OSError, ValueError) as error:
                raise WorkspaceV2MigrationError(
                    "historic raw checkpoint is incompatible"
                ) from error
    _emit(progress, "identity_bound")
    now = clock or (lambda: datetime.now(UTC))
    with (
        service.open_storage(source_paths, WorkspaceAccessMode.READ_ONLY) as source,
        service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as destination,
    ):
        if not isinstance(destination.store, WorkspaceV2Store):
            raise WorkspaceV2MigrationError("migration destination did not open workspace v2")
        for asset in source.assets.list_all():
            destination.assets.upsert(asset)
        for definition in source.sources.list_all():
            destination.sources.upsert(definition)
        for definition in source.metric_definitions.list_all():
            destination.metric_definitions.upsert(definition)
        _require_host_capacity(host_free_bytes, reserve_bytes=reserve_bytes)
        _emit(progress, "documents_started")
        document_count = _copy_documents(source.paths.documents_dir / "sha256", destination)
        _emit(progress, "documents_completed")
        _require_host_capacity(host_free_bytes, reserve_bytes=reserve_bytes)
        staging = destination.store.raw_staging
        raw = RawV2Importer(
            source,
            staging,
            source_workspace_id=str(source_inspection.workspace_id),
            source_fingerprint=fingerprint,
            historic_source_fingerprint=historic_fingerprint,
            progress=(
                lambda event: _emit(
                    progress,
                    _raw_phase(event).phase,
                    confirmed_count=event.confirmed_count,
                    page_count=event.page_count,
                    page_size=event.page_size,
                )
            ),
        ).run(page_limit=page_limit)
        if transition is not None and transition.phase is not WorkspaceV2MigrationPhase.COMPLETE:
            transition = service.advance_v2_source_transition(
                destination_paths=initialized.paths,
                state=transition.model_copy(
                    update={
                        "phase": WorkspaceV2MigrationPhase.VERIFYING,
                        "confirmed_equivalent_count": raw.imported_count,
                        "updated_at": now(),
                    }
                ),
            )
        _require_host_capacity(host_free_bytes, reserve_bytes=reserve_bytes)
        _emit(progress, "observations_started")
        observations = ObservationV2Importer(
            source,
            staging,
            source_workspace_id=str(source_inspection.workspace_id),
            source_fingerprint=fingerprint,
            raw_digest=raw.corpus_digest,
            historic_source_fingerprint=historic_fingerprint,
            historic_raw_digest=historic_raw_digest,
            progress=(
                lambda event: _emit(
                    progress,
                    _observation_phase(event).phase,
                    confirmed_count=event.confirmed_count,
                    page_count=event.page_count,
                    page_size=event.page_size,
                )
            ),
        ).run(page_limit=page_limit)
        _emit(progress, "observations_completed")
        _require_host_capacity(host_free_bytes, reserve_bytes=reserve_bytes)
        _emit(progress, "analytical_started")
        analytical = WorkspaceV2AnalyticalImporter(
            DirectHistoricalAnalyticalSource(source),
            CompactAnalyticalStore(destination.store.connection),
            source_root=source_paths.root,
            destination_root=initialized.paths.root,
            state_root=initialized.paths.state_root,
            source_workspace_id=str(source_inspection.workspace_id),
            source_fingerprint=fingerprint,
            page_limit=page_limit,
            allow_source_transition=transition is not None,
            direct_transport=True,
            failure_injector=analytical_failure_injector,
        ).run()
        historical = analytical
        _emit(progress, "historical_completed")
        _emit(progress, "analytical_completed")
    _emit(progress, "closure_completed")
    if transition is not None:
        service.advance_v2_source_transition(
            destination_paths=initialized.paths,
            state=transition.model_copy(
                update={"phase": WorkspaceV2MigrationPhase.COMPLETE, "updated_at": now()}
            ),
        )
    return WorkspaceV2MigrationSummary(
        source_workspace_id=str(source_inspection.workspace_id),
        source_fingerprint=fingerprint,
        source_root=source_paths.root,
        destination_root=initialized.paths.root,
        destination_workspace_id=str(initialized.manifest.workspace_id),
        source_snapshot_id=str(snapshot.snapshot_id) if snapshot is not None else None,
        page_limit=page_limit,
        raw=raw,
        observations=observations,
        historical=historical,
        analytical=analytical,
        document_count=document_count,
        completed_at=now(),
    )


__all__ = [
    "create_v1_migration_source_snapshot",
    "load_v1_migration_source_snapshot",
    "WorkspaceV2MigrationError",
    "WorkspaceV2MigrationProgress",
    "WorkspaceV2MigrationSummary",
    "migrate_workspace_v1_to_v2",
    "source_fingerprint",
]
