"""Streaming, byte-exact transfer of a frozen workspace operational cut."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from uuid import UUID, uuid4

from investment_analyst.core.models.base import ContractModel
from investment_analyst.storage.serialization import canonical_json_bytes
from investment_analyst.workspace.workspace_v2_lifecycle_models import (
    FrozenSourceCut,
    OperationalStateArtifact,
    OperationalStateArtifactRole,
    OperationalStateFamily,
    OperationalStateSeal,
    OperationalStateTransferReceipt,
)

_CHUNK_BYTES = 1024 * 1024
_DOCUMENTS: tuple[tuple[OperationalStateFamily, str, OperationalStateArtifactRole], ...] = (
    (
        OperationalStateFamily.DAILY_RUN,
        "state/aapl_daily_run_state.json",
        OperationalStateArtifactRole.DOCUMENT,
    ),
    (
        OperationalStateFamily.DAILY_SCHEDULE,
        "state/aapl_daily_schedule_state.json",
        OperationalStateArtifactRole.DOCUMENT,
    ),
    (
        OperationalStateFamily.MULTI_ASSET_SCHEDULE,
        "state/multi_asset_schedule_state_v1.json",
        OperationalStateArtifactRole.DOCUMENT,
    ),
    (
        OperationalStateFamily.ANALYTICAL_SCREENING,
        "state/analytical_screening_state_v1.json",
        OperationalStateArtifactRole.DOCUMENT,
    ),
    (
        OperationalStateFamily.OPERATIONAL_ALERTS,
        "state/operational_alert_state_v1.json",
        OperationalStateArtifactRole.DOCUMENT,
    ),
    (
        OperationalStateFamily.CANDIDATE_OUTBOX,
        "state/candidate_notification_outbox_state_v1.json",
        OperationalStateArtifactRole.DOCUMENT,
    ),
    (
        OperationalStateFamily.CAZATIBURONES_OUTBOX,
        "state/cazatiburones_notification_outbox_state_v1.json",
        OperationalStateArtifactRole.DOCUMENT,
    ),
    (
        OperationalStateFamily.INSTITUTIONAL_CYCLE,
        "state/sec_institutional_cycle_state_v1.json",
        OperationalStateArtifactRole.DOCUMENT,
    ),
    (
        OperationalStateFamily.INSTITUTIONAL_HISTORY,
        "state/sec_institutional_history_state_v1.json",
        OperationalStateArtifactRole.DOCUMENT,
    ),
    (
        OperationalStateFamily.MANUAL_OPERATIONS,
        "state/manual_operation_state_v1.json",
        OperationalStateArtifactRole.DOCUMENT,
    ),
    (
        OperationalStateFamily.STORAGE_TELEMETRY,
        "state/storage_observability_v1.jsonl",
        OperationalStateArtifactRole.TELEMETRY,
    ),
    (
        OperationalStateFamily.ASSET_PREFERENCES,
        "state/asset_preferences.json",
        OperationalStateArtifactRole.DOCUMENT,
    ),
)
_JOURNALS: tuple[tuple[OperationalStateFamily, str], ...] = (
    (OperationalStateFamily.MULTI_ASSET_SCHEDULE, "state/multi_asset_schedule_state_v1_journal"),
    (OperationalStateFamily.ANALYTICAL_SCREENING, "state/analytical_screening_state_v1_journal"),
    (OperationalStateFamily.OPERATIONAL_ALERTS, "state/operational_alert_state_v1_journal"),
)
_EVENT_ROOTS: tuple[tuple[OperationalStateFamily, str], ...] = (
    (
        OperationalStateFamily.ACTIVITY_EVENTS,
        "storage/data/processed/cazatiburones_activity_events_v1",
    ),
    (
        OperationalStateFamily.INSTITUTIONAL_EVENTS,
        "storage/data/processed/cazatiburones_institutional_events_v1",
    ),
)


class OperationalStateTransferError(RuntimeError):
    """A frozen operational-state cut cannot safely advance."""


def operational_model_projection(root: Path) -> dict[str, str]:
    """Read complete existing operational models without replaying writes or actions.

    The byte inventory remains the authority for physical membership. This
    independent projection validates schemas, journal reconstruction, checksums,
    references and all persisted fields through the existing reader contracts.
    Only one family's models are retained at a time; no financial corpus is read.
    """
    from investment_analyst.alerts.analytical_state import AnalyticalScreeningStateStore
    from investment_analyst.alerts.candidate_notifications import CandidateNotificationStore
    from investment_analyst.alerts.cazatiburones_notifications import CazatiburonesNotificationStore
    from investment_analyst.analytics.cazatiburones.activity_event_repository import (
        ActivityEventRepository,
    )
    from investment_analyst.analytics.cazatiburones.institutional_event_repository import (
        InstitutionalEventRepository,
    )
    from investment_analyst.application.aapl_scheduler import AaplDailyScheduleStateStore
    from investment_analyst.application.asset_preferences import AssetPreferencesStore
    from investment_analyst.application.manual_operations import ManualOperationStateStore
    from investment_analyst.application.multi_asset_scheduler import MultiAssetScheduleStateStore
    from investment_analyst.application.operational_alerts import OperationalAlertStateStore
    from investment_analyst.application.operational_state import AaplDailyRunStateStore
    from investment_analyst.application.sec_institutional_cycle_state import (
        SecInstitutionalCycleStateStore,
    )
    from investment_analyst.application.sec_institutional_history_state import (
        SecInstitutionalHistoryStateStore,
    )
    from investment_analyst.application.storage_observability import (
        parse_storage_observability_state,
    )

    source = _safe_root(root)
    _require_operational_layout(source)
    readers: dict[OperationalStateFamily, Callable[[Path], ContractModel | None]] = {
        OperationalStateFamily.DAILY_RUN: lambda path: AaplDailyRunStateStore(path).load(),
        OperationalStateFamily.DAILY_SCHEDULE: lambda path: AaplDailyScheduleStateStore(
            path
        ).load(),
        OperationalStateFamily.MULTI_ASSET_SCHEDULE: lambda path: MultiAssetScheduleStateStore(
            path
        ).load(),
        OperationalStateFamily.ANALYTICAL_SCREENING: lambda path: AnalyticalScreeningStateStore(
            path
        ).load(),
        OperationalStateFamily.OPERATIONAL_ALERTS: lambda path: OperationalAlertStateStore(
            path
        ).load(),
        OperationalStateFamily.CANDIDATE_OUTBOX: lambda path: CandidateNotificationStore(
            path
        ).load(),
        OperationalStateFamily.CAZATIBURONES_OUTBOX: lambda path: CazatiburonesNotificationStore(
            path
        ).load(),
        OperationalStateFamily.INSTITUTIONAL_CYCLE: lambda path: SecInstitutionalCycleStateStore(
            path
        ).load(),
        OperationalStateFamily.INSTITUTIONAL_HISTORY: lambda path: (
            SecInstitutionalHistoryStateStore(path).load()
        ),
        OperationalStateFamily.MANUAL_OPERATIONS: lambda path: ManualOperationStateStore(
            path
        ).load(),
        OperationalStateFamily.ASSET_PREFERENCES: lambda path: AssetPreferencesStore(path).load(),
        OperationalStateFamily.STORAGE_TELEMETRY: lambda path: parse_storage_observability_state(
            path.read_text(encoding="utf-8")
        ),
    }
    journals = dict(_JOURNALS)
    result: dict[str, str] = {}
    for family, relative, _ in _DOCUMENTS:
        path = source / relative
        journal = journals.get(family)
        present = path.is_file() or (
            journal is not None and any(item.is_file() for item in (source / journal).rglob("*"))
        )
        try:
            value = readers[family](path) if present else None
            result[family.value] = _model_projection_digest(value)
            if family is OperationalStateFamily.ASSET_PREFERENCES:
                history = AssetPreferencesStore(path).load_history() if present else ()
                result[family.value + ":history"] = _model_projection_digest(history)
        except Exception as error:
            raise OperationalStateTransferError(
                f"operational model projection is invalid: {family.value}"
            ) from error
    processed = source / "storage/data/processed"
    for family, repository in (
        (OperationalStateFamily.ACTIVITY_EVENTS, ActivityEventRepository),
        (OperationalStateFamily.INSTITUTIONAL_EVENTS, InstitutionalEventRepository),
    ):
        try:
            result[family.value] = _model_projection_digest(
                repository(processed, read_only=True).list_snapshots()
            )
        except Exception as error:
            raise OperationalStateTransferError(
                f"operational model projection is invalid: {family.value}"
            ) from error
    return result


def _model_projection_digest(
    value: ContractModel | tuple[ContractModel, ...] | None,
) -> str:
    digest = hashlib.sha256()
    if value is None:
        digest.update(b"absent")
    elif isinstance(value, ContractModel):
        digest.update(canonical_json_bytes(value))
    else:
        for item in value:
            digest.update(canonical_json_bytes(item))
            digest.update(b"\n")
    return digest.hexdigest()


def compare_operational_models(source: Path, destination: Path) -> dict[str, str]:
    """Compare every field after parsing; a byte-copy receipt alone is insufficient."""
    expected = operational_model_projection(source)
    actual = operational_model_projection(destination)
    if expected != actual:
        raise OperationalStateTransferError("source/destination operational models differ")
    return expected


def _control_name(name: str, prefixes: tuple[str, ...]) -> bool:
    for prefix in prefixes:
        if not name.startswith(prefix + "."):
            continue
        suffix = name[len(prefix) + 1 :]
        if suffix == "json":
            return True
        pieces = suffix.split(".")
        if pieces[-1] != "json":
            continue
        if len(pieces) == 2:
            try:
                return str(UUID(pieces[0])) == pieces[0]
            except ValueError:
                continue
        if len(pieces) == 3 and pieces[0] in {"F", "B"}:
            try:
                return str(UUID(pieces[1])) == pieces[1]
            except ValueError:
                continue
        if len(pieces) == 3 and pieces[0] in {"A", "source"}:
            return re.fullmatch(r"[0-9a-f]{64}", pieces[1]) is not None
    return False


def _require_operational_layout(root: Path) -> None:
    """Keep typed migration provenance; reject all unclassified destination state."""
    from investment_analyst.storage.workspace_v2_inventory import WorkspaceV2InventoryState
    from investment_analyst.workspace.models import WorkspaceMigrationIdentity
    from investment_analyst.workspace.service import WorkspaceError, WorkspaceService
    from investment_analyst.workspace.workspace_v2_cutover import WorkspaceV2CutoverCheckpoint
    from investment_analyst.workspace.workspace_v2_migration_models import (
        HistoricalTransition,
        HistoricalTransitionReceipt,
        WorkspaceV2MigrationWorkCheckpoint,
        WorkspaceV2SourceTransitionState,
    )

    if not (root / "manifest.json").is_file():
        _reject_unclassified_state(root)
        return
    service = WorkspaceService()
    identity = service.manifest(root)
    if identity.format_version != 2:
        _reject_unclassified_state(root)
        return
    state = root / "state"
    if state.is_symlink() or not state.is_dir():
        raise OperationalStateTransferError("operational state root is unsafe")
    known = {Path(relative).name for _, relative, _ in _DOCUMENTS}
    journals = {Path(relative).name for _, relative in _JOURNALS}
    known.update({"aapl_local_service.lock", "aapl_daily_run.lock"})
    controls: tuple[tuple[type[ContractModel], tuple[str, ...]], ...] = (
        (WorkspaceMigrationIdentity, ("workspace-v2-migration-identity",)),
        (WorkspaceV2SourceTransitionState, ("workspace-v2-source-transition",)),
        (
            WorkspaceV2InventoryState,
            ("direct-historical-import-state", "workspace-v2-analytical-import-state"),
        ),
        (
            WorkspaceV2MigrationWorkCheckpoint,
            tuple(
                f"{policy}-{family}-verification-work"
                for policy in ("direct-historical-import-v1", "workspace-v2-analytical-import-v1")
                for family in ("metrics", "diagnostics")
            ),
        ),
        (HistoricalTransition, ("historical-transition",)),
        (HistoricalTransitionReceipt, ("historical-transition-receipt",)),
        (WorkspaceV2CutoverCheckpoint, ("workspace-v2-cutover",)),
        (OperationalStateTransferReceipt, ("workspace-v2-operational-transfer",)),
    )
    try:
        origin = (
            service.v1_migration_identity(service.resolve(root))
            if (state / "workspace-v2-migration-identity.json").is_file()
            else None
        )
    except WorkspaceError as error:
        raise OperationalStateTransferError("migration control identity is invalid") from error

    def parse(path: Path, model: type[ContractModel]) -> ContractModel:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _CHUNK_BYTES:
            raise OperationalStateTransferError("migration control is unsafe")
        try:
            value = model.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as error:
            raise OperationalStateTransferError("migration control is invalid") from error
        owner = getattr(value, "destination_workspace_id", None)
        if isinstance(value, HistoricalTransitionReceipt):
            owner = value.transition.destination_workspace_id
        if isinstance(value, OperationalStateTransferReceipt):
            owner = value.seal.destination_workspace_id
        operation_id = getattr(value, "operation_id", None)
        if isinstance(value, HistoricalTransitionReceipt):
            operation_id = value.transition.operation_id
        if isinstance(value, OperationalStateTransferReceipt):
            operation_id = value.seal.operation_id
        if isinstance(
            value,
            (
                HistoricalTransition,
                HistoricalTransitionReceipt,
                WorkspaceV2CutoverCheckpoint,
                OperationalStateTransferReceipt,
            ),
        ) or (isinstance(value, WorkspaceV2MigrationWorkCheckpoint) and ".F." in path.name):
            named_operation = _control_operation(path.stem.rsplit(".", 1)[-1])
            if named_operation != operation_id:
                raise OperationalStateTransferError("migration control epoch differs")
        legacy_work = (
            isinstance(value, WorkspaceV2MigrationWorkCheckpoint) and ".F." not in path.name
        )
        if owner is not None and owner != identity.workspace_id and not legacy_work:
            raise OperationalStateTransferError("migration control belongs to another workspace")
        source_id = getattr(value, "source_workspace_id", None)
        if (
            source_id is not None
            and origin is not None
            and str(source_id) != str(origin.source_workspace_id)
        ):
            raise OperationalStateTransferError("migration control source identity differs")
        return value

    for entry in state.iterdir():
        if entry.is_symlink():
            raise OperationalStateTransferError("operational state contains a symlink")
        if entry.name in known and entry.is_file():
            continue
        if entry.name in journals and entry.is_dir():
            continue
        if entry.name == "workspace-v2-source-transition-history" and entry.is_dir():
            for archived in entry.iterdir():
                if re.fullmatch(r"[0-9a-f]{64}\.json", archived.name) is None:
                    raise OperationalStateTransferError(
                        "migration archive contains an unknown entry"
                    )
                parse(archived, WorkspaceV2SourceTransitionState)
            continue
        if entry.name.startswith("historical-parent.") and entry.suffix == ".jsonl":
            operation = entry.name.removeprefix("historical-parent.").removesuffix(".jsonl")
            parsed_operation = _control_operation(operation)
            transition = parse(
                state / f"historical-transition.{operation}.json", HistoricalTransition
            )
            if (
                not isinstance(transition, HistoricalTransition)
                or transition.operation_id != parsed_operation
                or transition.parent_membership_path.name != entry.name
            ):
                raise OperationalStateTransferError("historical parent control binding differs")
            expected = transition.parent_membership_sha256
        elif (
            entry.name.startswith("workspace-v2-operational-inventory.F.")
            and entry.suffix == ".jsonl"
        ):
            operation = entry.name.removeprefix(
                "workspace-v2-operational-inventory.F."
            ).removesuffix(".jsonl")
            parsed_operation = _control_operation(operation)
            receipt = parse(
                state / f"workspace-v2-operational-transfer.F.{operation}.json",
                OperationalStateTransferReceipt,
            )
            if (
                not isinstance(receipt, OperationalStateTransferReceipt)
                or receipt.seal.operation_id != parsed_operation
            ):
                raise OperationalStateTransferError("operational inventory control binding differs")
            expected = receipt.seal.inventory_sha256
        else:
            for model, prefixes in controls:
                if _control_name(entry.name, prefixes):
                    parse(entry, model)
                    break
            else:
                raise OperationalStateTransferError(
                    "operational state has an unclassified durable entry"
                )
            continue
        if not entry.is_file():
            raise OperationalStateTransferError("migration inventory is unsafe")
        digest = hashlib.sha256()
        with entry.open("rb") as stream:
            while chunk := stream.read(_CHUNK_BYTES):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise OperationalStateTransferError("migration inventory control digest differs")


def _control_operation(text: str) -> UUID:
    try:
        value = UUID(text)
    except ValueError as error:
        raise OperationalStateTransferError("migration control epoch name is invalid") from error
    if str(value) != text:
        raise OperationalStateTransferError("migration control epoch name is invalid")
    return value


def inventory_operational_state(root: Path) -> Iterator[OperationalStateArtifact]:
    """Yield the closed family inventory in deterministic order and bounded memory."""
    source = _safe_root(root)
    _reject_unclassified_state(source)
    yield from _inventory_known_operational_state(source)


def _inventory_known_operational_state(source: Path) -> Iterator[OperationalStateArtifact]:
    """Known families only; a v2 destination also owns migration control files."""
    for family, relative, role in _DOCUMENTS:
        path = source / relative
        if path.exists():
            yield _artifact(family, role, source, path)
    for family, relative in _JOURNALS:
        yield from _iter_tree(source, family, OperationalStateArtifactRole.JOURNAL, relative)
    for family, relative in _EVENT_ROOTS:
        yield from _iter_tree(source, family, OperationalStateArtifactRole.EVENT, relative)


def _reject_unclassified_state(root: Path) -> None:
    """Fail closed when ``state`` contains a durable file outside the closed families."""
    state_root = root / "state"
    if not state_root.exists():
        return
    if state_root.is_symlink() or not state_root.is_dir():
        raise OperationalStateTransferError("operational state root is unsafe")
    known_documents = {relative.removeprefix("state/") for _, relative, _ in _DOCUMENTS}
    known_journals = {relative.removeprefix("state/") for _, relative in _JOURNALS}
    excluded_locks = {"aapl_local_service.lock", "aapl_daily_run.lock"}
    for entry in state_root.iterdir():
        if entry.is_symlink():
            raise OperationalStateTransferError("operational state contains a symlink")
        if entry.name in excluded_locks:
            continue
        if entry.name in known_documents:
            continue
        if entry.name in known_journals and entry.is_dir():
            continue
        raise OperationalStateTransferError("operational state has an unclassified durable entry")


def seal_operational_state(
    source_root: Path,
    destination_root: Path,
    *,
    source: FrozenSourceCut,
    destination_workspace_id: UUID,
    inventory_path: Path,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> OperationalStateSeal:
    """Seal an inventory after the caller has physically closed the source writer."""
    root = _safe_root(source_root)
    destination = _safe_root(destination_root)
    if source.source_root != root or not source.writer_closed:
        raise OperationalStateTransferError("operational source is not the declared frozen cut")
    if root == destination or root in destination.parents:
        raise OperationalStateTransferError("operational destination must be disjoint from source")
    inventory = inventory_path.expanduser().resolve(strict=False)
    _ensure_safe_output(inventory)
    if inventory.exists():
        digest, present = _verify_source_inventory(root, inventory)
        return _seal(
            source,
            destination,
            destination_workspace_id,
            digest,
            present,
            clock,
        )
    digest = hashlib.sha256()
    present: set[OperationalStateFamily] = set()
    temporary = inventory.with_name(f".{inventory.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            for artifact in inventory_operational_state(root):
                encoded = _inventory_line(artifact)
                digest.update(encoded)
                stream.buffer.write(encoded)
                present.add(artifact.family)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, inventory)
        _fsync_directory(inventory.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return _seal(
        source,
        destination,
        destination_workspace_id,
        digest.hexdigest(),
        present,
        clock,
    )


def _seal(
    source: FrozenSourceCut,
    destination: Path,
    destination_workspace_id: UUID,
    inventory_sha256: str,
    present: set[OperationalStateFamily],
    clock: Callable[[], datetime],
) -> OperationalStateSeal:
    return OperationalStateSeal(
        operation_id=source.operation_id,
        source=source,
        destination_root=destination,
        destination_workspace_id=destination_workspace_id,
        inventory_sha256=inventory_sha256,
        sealed_at=_utc_now(clock),
        present_families=tuple(sorted(present)),
        absent_families=tuple(sorted(set(OperationalStateFamily) - present)),
    )


def transfer_operational_state(
    source_root: Path,
    destination_root: Path,
    *,
    seal: OperationalStateSeal,
    inventory_path: Path,
    checkpoint_path: Path,
    receipt_path: Path,
    available_bytes: Callable[[], int],
    reserve_bytes: int,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> OperationalStateTransferReceipt:
    """Copy or verify one inventory line at a time, retaining only a cursor checkpoint."""
    source = _safe_root(source_root)
    destination = _safe_root(destination_root)
    if reserve_bytes < 0:
        raise OperationalStateTransferError("operational capacity reserve is invalid")
    if seal.source.source_root != source or seal.destination_root != destination:
        raise OperationalStateTransferError("operational seal binding differs")
    existing = _read_receipt(receipt_path)
    current_digest, _ = _verify_source_inventory(source, inventory_path)
    if current_digest != seal.inventory_sha256:
        raise OperationalStateTransferError("sealed inventory digest differs")
    _verify_destination_inventory(destination, inventory_path, allow_missing=existing is None)
    if existing is not None:
        if existing.seal == seal:
            return existing
        raise OperationalStateTransferError("operational receipt binding differs")
    cursor, confirmed_bytes = _load_checkpoint(checkpoint_path, seal)
    inventory_digest = hashlib.sha256()
    destination_digest = hashlib.sha256()
    completed: set[OperationalStateFamily] = set()
    count = 0
    for count, artifact in enumerate(_read_inventory(inventory_path, inventory_digest), start=1):
        _verify_source_artifact(source, artifact)
        if count > cursor:
            _require_capacity(available_bytes, reserve_bytes, artifact.size_bytes)
            copy_artifact(source, destination, artifact)
            target = _artifact(
                artifact.family, artifact.role, destination, destination / artifact.path
            )
            if target != artifact:
                raise OperationalStateTransferError(
                    "destination artifact differs from sealed source"
                )
            confirmed_bytes += artifact.size_bytes
            _save_checkpoint(checkpoint_path, seal, count, confirmed_bytes)
        else:
            target = _artifact(
                artifact.family, artifact.role, destination, destination / artifact.path
            )
            if target != artifact:
                raise OperationalStateTransferError(
                    "checkpointed destination artifact differs from seal"
                )
        destination_digest.update(_inventory_line(target))
        completed.add(artifact.family)
    if inventory_digest.hexdigest() != seal.inventory_sha256:
        raise OperationalStateTransferError("sealed inventory digest differs")
    if destination_digest.hexdigest() != seal.inventory_sha256:
        raise OperationalStateTransferError("destination inventory digest differs")
    receipt = OperationalStateTransferReceipt(
        seal=seal,
        source_inventory_sha256=inventory_digest.hexdigest(),
        destination_inventory_sha256=destination_digest.hexdigest(),
        artifact_count=count,
        transferred_bytes=confirmed_bytes,
        completed_families=tuple(sorted(completed)),
        completed_at=_utc_now(clock),
    )
    _write_json(receipt_path, receipt.model_dump(mode="json"), replace=False)
    checkpoint_path.unlink(missing_ok=True)
    return receipt


def _verify_source_inventory(
    root: Path, inventory: Path
) -> tuple[str, set[OperationalStateFamily]]:
    digest = hashlib.sha256()
    present: set[OperationalStateFamily] = set()
    for expected, actual in zip_longest(
        _read_inventory(inventory, digest), inventory_operational_state(root)
    ):
        if expected is None or actual is None or expected != actual:
            raise OperationalStateTransferError(
                "sealed source artifact changed or inventory membership differs"
            )
        present.add(expected.family)
    return digest.hexdigest(), present


def _verify_destination_inventory(root: Path, inventory: Path, *, allow_missing: bool) -> None:
    actual_entries = _inventory_known_operational_state(root)
    actual = next(actual_entries, None)
    for expected in _read_inventory(inventory, hashlib.sha256()):
        if actual is not None and actual.path == expected.path:
            if actual != expected:
                raise OperationalStateTransferError(
                    "destination operational preimage has no matching ownership proof"
                )
            actual = next(actual_entries, None)
        elif not allow_missing:
            raise OperationalStateTransferError(
                "destination operational inventory differs from seal"
            )
    if actual is not None:
        raise OperationalStateTransferError("destination contains unique operational content")


def copy_artifact(
    source_root: Path, destination_root: Path, artifact: OperationalStateArtifact
) -> None:
    """Copy one sealed artifact atomically, skipping an already-identical destination."""
    source = source_root / artifact.path
    destination = destination_root / artifact.path
    _verify_source_artifact(source_root, artifact)
    if (
        destination.exists()
        and _artifact(artifact.family, artifact.role, destination_root, destination) == artifact
    ):
        return
    if destination.exists():
        raise OperationalStateTransferError(
            "destination operational preimage has no matching ownership proof"
        )
    _ensure_parent_chain(destination_root, destination.parent)
    if destination.is_symlink():
        raise OperationalStateTransferError("destination artifact is unsafe")
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            while chunk := reader.read(_CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
            raise OperationalStateTransferError("source artifact changed during transfer")
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _iter_tree(
    root: Path, family: OperationalStateFamily, role: OperationalStateArtifactRole, relative: str
) -> Iterator[OperationalStateArtifact]:
    directory = root / relative
    if not directory.exists():
        return
    if directory.is_symlink() or not directory.is_dir():
        raise OperationalStateTransferError("operational state directory is unsafe")
    yield from _iter_directory(root, directory, family, role)


def _iter_directory(
    root: Path,
    directory: Path,
    family: OperationalStateFamily,
    role: OperationalStateArtifactRole,
) -> Iterator[OperationalStateArtifact]:
    """Walk one directory at a time; never materialize a whole journal or event tree."""
    with os.scandir(directory) as entries:
        names = sorted(entries, key=lambda entry: entry.name)
    for entry in names:
        path = Path(entry.path)
        if entry.is_symlink():
            raise OperationalStateTransferError("operational state contains a symlink")
        if entry.is_dir(follow_symlinks=False):
            yield from _iter_directory(root, path, family, role)
        elif entry.is_file(follow_symlinks=False):
            yield _artifact(family, role, root, path)
        else:
            raise OperationalStateTransferError("operational state entry is unsafe")


def _read_inventory(path: Path, digest: hashlib._Hash) -> Iterator[OperationalStateArtifact]:
    if path.is_symlink() or not path.is_file():
        raise OperationalStateTransferError("sealed inventory is unavailable")
    with path.open("rb") as stream:
        for raw in stream:
            digest.update(raw)
            try:
                yield OperationalStateArtifact.model_validate_json(raw)
            except ValueError as error:
                raise OperationalStateTransferError("sealed inventory line is malformed") from error


def _load_checkpoint(path: Path, seal: OperationalStateSeal) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    if path.is_symlink() or not path.is_file():
        raise OperationalStateTransferError("operational checkpoint is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            value["operation_id"] != str(seal.operation_id)
            or value["inventory_sha256"] != seal.inventory_sha256
        ):
            raise ValueError
        cursor = value["cursor"]
        total = value["confirmed_bytes"]
        if not isinstance(cursor, int) or cursor < 0 or not isinstance(total, int) or total < 0:
            raise ValueError
        return cursor, total
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise OperationalStateTransferError("operational checkpoint binding differs") from error


def _save_checkpoint(
    path: Path, seal: OperationalStateSeal, cursor: int, confirmed_bytes: int
) -> None:
    _write_json(
        path,
        {
            "operation_id": str(seal.operation_id),
            "inventory_sha256": seal.inventory_sha256,
            "cursor": cursor,
            "confirmed_bytes": confirmed_bytes,
        },
        replace=True,
    )


def _read_receipt(path: Path) -> OperationalStateTransferReceipt | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise OperationalStateTransferError("operational receipt is unsafe")
    try:
        return OperationalStateTransferReceipt.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise OperationalStateTransferError("operational receipt is malformed") from error


def _write_json(path: Path, value: object, *, replace: bool) -> None:
    _ensure_safe_output(path)
    if path.exists() and not replace:
        raise OperationalStateTransferError("operational output already exists")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _artifact(
    family: OperationalStateFamily, role: OperationalStateArtifactRole, root: Path, path: Path
) -> OperationalStateArtifact:
    if path.is_symlink() or not path.is_file() or not path.is_relative_to(root):
        raise OperationalStateTransferError("operational artifact is unsafe")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return OperationalStateArtifact(
        family=family,
        role=role,
        path=path.relative_to(root).as_posix(),
        size_bytes=size,
        sha256=digest.hexdigest(),
    )


def _verify_source_artifact(root: Path, artifact: OperationalStateArtifact) -> None:
    if _artifact(artifact.family, artifact.role, root, root / artifact.path) != artifact:
        raise OperationalStateTransferError("sealed source artifact changed")


def _inventory_line(artifact: OperationalStateArtifact) -> bytes:
    return (artifact.model_dump_json() + "\n").encode()


def _safe_root(path: Path) -> Path:
    if path.expanduser().is_symlink():
        raise OperationalStateTransferError("operational root is unsafe")
    root = path.expanduser().resolve(strict=False)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise OperationalStateTransferError("operational root is unsafe")
    return root


def _ensure_safe_output(path: Path) -> None:
    if path.is_symlink():
        raise OperationalStateTransferError("operational output path is unsafe")
    _ensure_parent_chain(path.parent, path.parent)


def _ensure_parent_chain(root: Path, parent: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    parent.mkdir(parents=True, exist_ok=True)
    current = parent
    while True:
        if current.is_symlink():
            raise OperationalStateTransferError("operational output parent is unsafe")
        if current == root:
            return
        if current.parent == current:
            raise OperationalStateTransferError("operational output escapes root")
        current = current.parent


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise OperationalStateTransferError("operational clock must be timezone-aware")
    return value.astimezone(UTC)


def _require_capacity(
    available_bytes: Callable[[], int], reserve_bytes: int, artifact_bytes: int
) -> None:
    try:
        observed = available_bytes()
    except OSError as error:
        raise OperationalStateTransferError(
            "operational capacity measurement is unavailable"
        ) from error
    if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
        raise OperationalStateTransferError("operational capacity measurement is invalid")
    if observed - reserve_bytes < artifact_bytes:
        raise OperationalStateTransferError("operational capacity is insufficient")


__all__ = [
    "OperationalStateTransferError",
    "copy_artifact",
    "inventory_operational_state",
    "seal_operational_state",
    "transfer_operational_state",
]
