"""Sealed planning, protected execution and remote control publication for retention."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from investment_analyst.application.local_release import LocalReleaseService
from investment_analyst.storage.serialization import canonical_json_bytes
from investment_analyst.workspace.drive_transport import DriveTransport
from investment_analyst.workspace.storage_retirement_models import (
    OperationalRetentionPlan,
    RemoteRetentionArtifact,
    RetentionCheckpoint,
    RetentionDecision,
    RetentionEntry,
    RetentionEntryKind,
    RetentionPlanRequest,
    RetentionReceipt,
    RetentionRemoteReceipt,
)
from investment_analyst.workspace.workspace_v2_lifecycle import (
    WorkspaceV2LifecycleError,
    _fsync_directory,
    _replace_json,
    _require_no_directory_users,
    _retiring_root,
    _write_new_json,
)

_DEFAULT_ROOT = Path("/home/marjuraru/.local/share/investment-analyst")
_DEFAULT_RESERVE_BYTES = 5_000_000_000
_SMALL_CONTENT_BYTES = 0
_EXECUTABLE = frozenset({RetentionDecision.REMOTE_ONLY, RetentionDecision.RETIRE_AFTER})
_PROTECTED_RELEASE_SHA = "dbbdfd021b0fee20be31b2d2a845874b0e861067"


class StorageRetirementError(RuntimeError):
    """A retention operation has no verified, safe result."""


class RetentionNotAdmitted(StorageRetirementError):
    """A valid request cannot proceed under the declared physical reserve."""


def _host_free_bytes() -> int:
    path = Path("/mnt/c")
    return int(shutil.disk_usage(path if path.exists() else Path("/")).free)


def _wsl_free_bytes() -> int:
    return int(shutil.disk_usage(Path("/")).free)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"retention manifest cannot serialize {type(value).__name__}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class StorageRetirementService:
    """One writer, exact paths, sealed plans and durable terminal evidence."""

    def __init__(
        self,
        *,
        allowed_root: Path = _DEFAULT_ROOT,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        host_free_bytes: Callable[[], int] = _host_free_bytes,
        wsl_free_bytes: Callable[[], int] = _wsl_free_bytes,
        deployment_state_loader: Callable[[], object] | None = None,
        workspace_bindings_loader: Callable[[], object] | None = None,
        systemctl_properties_loader: Callable[[], dict[str, str]] | None = None,
        systemd_unit_path: Path | None = None,
        runtime_root: Path | None = None,
    ) -> None:
        self._allowed_root = allowed_root.expanduser().resolve(strict=False)
        self._clock = clock
        self._host_free_bytes = host_free_bytes
        self._wsl_free_bytes = wsl_free_bytes
        self._deployment_state_loader = deployment_state_loader
        self._workspace_bindings_loader = workspace_bindings_loader
        self._systemctl_properties_loader = systemctl_properties_loader
        self._systemd_unit_path = (
            systemd_unit_path
            if systemd_unit_path is not None
            else Path("/home/marjuraru/.config/systemd/user/investment-analyst.service")
        )
        self._runtime_root = (
            runtime_root if runtime_root is not None else self._allowed_root / "runtime"
        )
        self._deployment_cache: object | None = None
        self._bindings_cache: object | None = None
        self._systemctl_cache: dict[str, str] | None = None
        self._unit_text_cache: str | None = None

    def plan(self, request: RetentionPlanRequest) -> OperationalRetentionPlan:
        """Measure every exact request path and seal the complete inventory."""
        root = self._require_root(request.root)
        self._require_paths_outside_root(root, tuple(item.path for item in request.entries))
        entries: list[RetentionEntry] = []
        for item in request.entries:
            target = self._safe_target(root, item.path)
            measured = self._measure(target)
            if measured["kind"] is not item.kind:
                raise StorageRetirementError(
                    f"retention request kind differs from the filesystem at {item.path}"
                )
            self._assert_not_protected(target)
            entries.append(
                RetentionEntry(
                    path=item.path,
                    kind=item.kind,
                    retention_class=item.retention_class,
                    decision=item.decision,
                    reason=item.reason,
                    last_consumer=item.last_consumer,
                    dependencies=item.dependencies,
                    remote_evidence=item.remote_evidence,
                    retire_after=item.retire_after,
                    file_count=measured["file_count"],
                    size_bytes=measured["size_bytes"],
                    allocated_bytes=measured["allocated_bytes"],
                    exclusive_bytes=measured["exclusive_bytes"],
                    inventory_sha256=measured["inventory_sha256"],
                )
            )
        measured_entries = tuple(sorted(entries, key=lambda item: item.path))
        document = {
            "schema_version": "operational-retention-plan-v1",
            "operation_id": request.operation_id,
            "root": root,
            "created_at": self._now(),
            "entries": tuple(item.model_dump(mode="python") for item in measured_entries),
            "logical_bytes": sum(item.size_bytes for item in measured_entries),
            "allocated_bytes": sum(item.allocated_bytes for item in measured_entries),
            "exclusive_bytes": sum(item.exclusive_bytes for item in measured_entries),
            "retain_bytes": sum(
                item.size_bytes
                for item in measured_entries
                if item.decision in (RetentionDecision.KEEP_LOCAL, RetentionDecision.UNKNOWN)
            ),
            "retire_bytes": sum(
                item.size_bytes
                for item in measured_entries
                if item.decision
                in (
                    RetentionDecision.REMOTE_ONLY,
                    RetentionDecision.RETIRE_AFTER,
                    RetentionDecision.RETIRED,
                )
            ),
            "host_free_bytes": self._free(self._host_free_bytes, "host"),
            "wsl_free_bytes": self._free(self._wsl_free_bytes, "wsl"),
        }
        digest = _sha256_bytes(
            json.dumps(
                document,
                allow_nan=False,
                default=_json_default,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        document["manifest_sha256"] = digest
        try:
            return OperationalRetentionPlan.model_validate(document)
        except ValueError as error:
            raise StorageRetirementError(f"retention plan is inconsistent: {error}") from error

    def plan_sha256(self, plan: OperationalRetentionPlan) -> str:
        return _sha256_bytes(canonical_json_bytes(plan))

    def verify(self, plan: OperationalRetentionPlan, *, plan_sha256: str) -> None:
        """Re-measure executable entries and refuse any drift from the sealed plan."""
        if self.plan_sha256(plan) != plan_sha256:
            raise StorageRetirementError("retention plan digest does not match its content")
        root = self._require_root(plan.root)
        for entry in plan.entries:
            if entry.decision not in _EXECUTABLE:
                continue
            target = self._safe_target(root, entry.path)
            if not target.exists() and _retiring_root(target, plan.operation_id).exists():
                continue
            measured = self._measure(target)
            if (
                measured["kind"] is not entry.kind
                or measured["file_count"] != entry.file_count
                or measured["size_bytes"] != entry.size_bytes
                or measured["allocated_bytes"] != entry.allocated_bytes
                or measured["exclusive_bytes"] != entry.exclusive_bytes
                or measured["inventory_sha256"] != entry.inventory_sha256
            ):
                raise StorageRetirementError(f"retention plan drifted at {entry.path}")
            self._assert_not_protected(target)

    def retire(
        self,
        plan: OperationalRetentionPlan,
        *,
        plan_sha256: str,
        checkpoint_path: Path,
        receipt_path: Path,
        reserve_bytes: int = _DEFAULT_RESERVE_BYTES,
    ) -> RetentionReceipt:
        """Execute a sealed plan exactly once, resuming its durable cursor."""
        checkpoint = self._safe_control_path(checkpoint_path)
        receipt = self._safe_control_path(receipt_path)
        if checkpoint == receipt:
            raise StorageRetirementError("retention checkpoint and receipt must differ")
        if (
            isinstance(reserve_bytes, bool)
            or not isinstance(reserve_bytes, int)
            or reserve_bytes < 0
        ):
            raise StorageRetirementError("retention reserve must be a non-negative integer")
        if self.plan_sha256(plan) != plan_sha256:
            raise StorageRetirementError("retention plan digest does not match its content")
        host_before = self._free(self._host_free_bytes, "host")
        wsl_before = self._free(self._wsl_free_bytes, "wsl")
        if receipt.exists():
            if not checkpoint.exists():
                raise StorageRetirementError("retention receipt exists without its checkpoint")
            existing = RetentionReceipt.model_validate_json(receipt.read_bytes())
            cursor = RetentionCheckpoint.model_validate_json(checkpoint.read_bytes())
            if (
                existing.plan_sha256 != plan_sha256
                or existing.operation_id != plan.operation_id
                or cursor.plan_sha256 != plan_sha256
                or cursor.state != "retired"
            ):
                raise StorageRetirementError("retention terminal evidence differs")
            return existing.model_copy(update={"idempotent_replay": True})

        if host_before < reserve_bytes or wsl_before < reserve_bytes:
            raise RetentionNotAdmitted("retention reserve is below the declared reserve")

        root = self._require_root(plan.root)
        executable = tuple(entry for entry in plan.entries if entry.decision in _EXECUTABLE)
        if not executable:
            raise StorageRetirementError("retention plan has no executable entries")
        self.verify(plan, plan_sha256=plan_sha256)

        if checkpoint.exists():
            cursor = RetentionCheckpoint.model_validate_json(checkpoint.read_bytes())
            if (
                cursor.operation_id != plan.operation_id
                or cursor.plan_sha256 != plan_sha256
                or cursor.state != "retiring"
            ):
                raise StorageRetirementError("retention checkpoint differs from the plan")
            completed = list(cursor.completed_paths)
            started_at = cursor.started_at
        else:
            completed = []
            started_at = self._now()

        def write_cursor(state: str = "retiring") -> RetentionCheckpoint:
            cursor = RetentionCheckpoint(
                operation_id=plan.operation_id,
                plan_sha256=plan_sha256,
                state=state,
                completed_paths=tuple(sorted(completed)),
                completed_logical_bytes=sum(
                    entry.size_bytes for entry in executable if entry.path in completed
                ),
                completed_allocated_bytes=sum(
                    entry.allocated_bytes for entry in executable if entry.path in completed
                ),
                completed_exclusive_bytes=sum(
                    entry.exclusive_bytes for entry in executable if entry.path in completed
                ),
                started_at=started_at,
                updated_at=self._now(),
            )
            _replace_json(checkpoint, cursor.model_dump(mode="json"))
            return cursor

        write_cursor()
        for entry in executable:
            target = self._safe_target(root, entry.path)
            retiring = _retiring_root(target, plan.operation_id)
            if entry.path in completed:
                if target.exists():
                    raise StorageRetirementError(
                        f"completed retention path reappeared at {entry.path}"
                    )
                continue
            self._assert_not_protected(target)
            self._assert_not_protected(retiring)
            if target.exists():
                self._require_unused(target, retiring)
                os.replace(target, retiring)
                _fsync_directory(target.parent)
            elif not retiring.exists():
                raise StorageRetirementError(f"retention plan path is absent at {entry.path}")
            self._require_unused(retiring)
            self._remove(retiring)
            if target.exists() or retiring.exists():
                raise StorageRetirementError(f"retention removal did not complete at {entry.path}")
            completed.append(entry.path)
            write_cursor()

        terminal = write_cursor("retired")
        host_after = self._free(self._host_free_bytes, "host")
        wsl_after = self._free(self._wsl_free_bytes, "wsl")
        remote_evidence = tuple(
            sorted({item for entry in executable for item in entry.remote_evidence})
        )
        document = RetentionReceipt(
            operation_id=plan.operation_id,
            plan_sha256=plan_sha256,
            root=root,
            retired_paths=terminal.completed_paths,
            retired_logical_bytes=terminal.completed_logical_bytes,
            retired_allocated_bytes=terminal.completed_allocated_bytes,
            exclusive_bytes_freed=terminal.completed_exclusive_bytes,
            remote_evidence=remote_evidence,
            host_free_before=host_before,
            host_free_after=host_after,
            wsl_free_before=wsl_before,
            wsl_free_after=wsl_after,
            idempotent_replay=False,
            completed_at=self._now(),
        )
        receipt.parent.mkdir(parents=True, exist_ok=True)
        _write_new_json(receipt, document.model_dump(mode="json"))
        return document

    def publish(
        self,
        plan: OperationalRetentionPlan,
        *,
        plan_sha256: str,
        transport: DriveTransport,
        remote_prefix: str,
        receipt_path: Path,
    ) -> RetentionRemoteReceipt:
        """Publish the plan and control inventory, then prove a byte-exact readback."""
        if self.plan_sha256(plan) != plan_sha256:
            raise StorageRetirementError("retention plan digest does not match its content")
        prefix = self._require_remote_prefix(remote_prefix)
        target = self._safe_control_path(receipt_path)
        if target.exists():
            existing = RetentionRemoteReceipt.model_validate_json(target.read_bytes())
            if existing.plan_sha256 != plan_sha256 or existing.operation_id != plan.operation_id:
                raise StorageRetirementError("retention remote receipt differs")
            return existing
        plan_object = f"{prefix}/plan-{plan.operation_id}.json"
        control_object = f"{prefix}/control-{plan.operation_id}.json"
        control = {
            "schema_version": "operational-retention-control-v1",
            "operation_id": str(plan.operation_id),
            "plan_sha256": plan_sha256,
            "root": str(plan.root),
            "created_at": plan.created_at.isoformat(),
            "entries": [
                {
                    "path": entry.path,
                    "kind": entry.kind.value,
                    "retention_class": entry.retention_class.value,
                    "decision": entry.decision.value,
                    "reason": entry.reason,
                    "last_consumer": entry.last_consumer,
                    "dependencies": list(entry.dependencies),
                    "remote_evidence": list(entry.remote_evidence),
                    "retire_after": entry.retire_after,
                    "file_count": entry.file_count,
                    "size_bytes": entry.size_bytes,
                    "allocated_bytes": entry.allocated_bytes,
                    "exclusive_bytes": entry.exclusive_bytes,
                    "inventory_sha256": entry.inventory_sha256,
                }
                for entry in plan.entries
            ],
            "logical_bytes": plan.logical_bytes,
            "allocated_bytes": plan.allocated_bytes,
            "exclusive_bytes": plan.exclusive_bytes,
            "retain_bytes": plan.retain_bytes,
            "retire_bytes": plan.retire_bytes,
        }
        with TemporaryDirectory(prefix="ia-retention-control-") as scratch:
            plan_path = Path(scratch) / plan_object.rsplit("/", 1)[-1]
            control_path = Path(scratch) / control_object.rsplit("/", 1)[-1]
            plan_path.write_bytes(canonical_json_bytes(plan))
            control_path.write_text(
                json.dumps(control, allow_nan=False, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            artifacts: list[RemoteRetentionArtifact] = []
            for source, object_name in (
                (plan_path, plan_object),
                (control_path, control_object),
            ):
                expected = _sha256_file(source)
                transport.copy_sealed_object(source, object_name)
                readback = Path(scratch) / f"readback-{source.name}"
                transport.fetch_sealed_object(object_name, readback)
                if readback.is_symlink() or not readback.is_file():
                    raise StorageRetirementError("retention remote readback is unsafe")
                if _sha256_file(readback) != expected:
                    raise StorageRetirementError("retention remote readback differs")
                artifacts.append(
                    RemoteRetentionArtifact(
                        object_name=object_name,
                        size_bytes=readback.stat().st_size,
                        sha256=expected,
                    )
                )
        document = RetentionRemoteReceipt(
            operation_id=plan.operation_id,
            plan_sha256=plan_sha256,
            remote_prefix=prefix,
            artifacts=tuple(sorted(artifacts, key=lambda item: item.object_name)),
            readback_verified=True,
            completed_at=self._now(),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_new_json(target, document.model_dump(mode="json"))
        return document

    def status(self, *, checkpoint_path: Path, receipt_path: Path) -> dict[str, object]:
        checkpoint = self._safe_control_path(checkpoint_path)
        receipt = self._safe_control_path(receipt_path)
        if receipt.exists():
            terminal = RetentionReceipt.model_validate_json(receipt.read_bytes())
            return {
                "schema_version": "operational-retention-status-v1",
                "state": "retired",
                "operation_id": str(terminal.operation_id),
                "plan_sha256": terminal.plan_sha256,
                "completed_paths": list(terminal.retired_paths),
                "completed_logical_bytes": terminal.retired_logical_bytes,
                "completed_allocated_bytes": terminal.retired_allocated_bytes,
                "exclusive_bytes_freed": terminal.exclusive_bytes_freed,
                "receipt": str(receipt),
            }
        if not checkpoint.exists():
            return {
                "schema_version": "operational-retention-status-v1",
                "state": "unstarted",
                "completed_paths": [],
                "receipt": None,
            }
        cursor = RetentionCheckpoint.model_validate_json(checkpoint.read_bytes())
        return {
            "schema_version": "operational-retention-status-v1",
            "state": cursor.state,
            "operation_id": str(cursor.operation_id),
            "plan_sha256": cursor.plan_sha256,
            "completed_paths": list(cursor.completed_paths),
            "completed_logical_bytes": cursor.completed_logical_bytes,
            "completed_allocated_bytes": cursor.completed_allocated_bytes,
            "exclusive_bytes_freed": cursor.completed_exclusive_bytes,
            "receipt": None,
        }

    def _measure(self, target: Path) -> dict[str, object]:
        if target.is_symlink():
            raise StorageRetirementError("retention target is a symbolic link")
        if target.is_file():
            records, file_count = self._file_records(target, target.parent)
            kind = RetentionEntryKind.FILE
        elif target.is_dir():
            records, file_count = self._tree_records(target)
            kind = RetentionEntryKind.DIRECTORY
        else:
            raise StorageRetirementError("retention target is missing or unsupported")
        size = sum(int(item["size_bytes"]) for item in records if item["record"] == "file")
        allocated = sum(
            int(item["allocated_bytes"]) for item in records if item["record"] == "file"
        )
        exclusive = sum(
            int(item["exclusive_bytes"]) for item in records if item["record"] == "file"
        )
        digest = _sha256_bytes(
            json.dumps(
                records,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        return {
            "kind": kind,
            "file_count": file_count,
            "size_bytes": size,
            "allocated_bytes": allocated,
            "exclusive_bytes": exclusive,
            "inventory_sha256": digest,
        }

    def _tree_records(self, root: Path) -> tuple[list[dict[str, object]], int]:
        records: list[dict[str, object]] = []
        file_count = 0
        for current, directories, files in os.walk(root, followlinks=False):
            base = Path(current)
            if base.is_symlink():
                raise StorageRetirementError(
                    f"retention tree contains a symbolic link: {base.relative_to(root).as_posix()}"
                )
            directories.sort()
            for name in directories:
                path = base / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    raise StorageRetirementError(
                        f"retention tree contains a symbolic link: {relative}"
                    )
                records.append(
                    {
                        "record": "directory",
                        "path": relative,
                    }
                )
            for name in sorted(files):
                path = base / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink() or not path.is_file():
                    raise StorageRetirementError(
                        f"retention tree contains an unsafe entry: {relative}"
                    )
                records.append(self._file_record(path, root))
                file_count += 1
        records.sort(key=lambda item: (str(item["record"]), str(item["path"])))
        return records, file_count

    def _file_records(self, path: Path, root: Path) -> tuple[list[dict[str, object]], int]:
        return [self._file_record(path, root)], 1

    def _file_record(self, path: Path, root: Path) -> dict[str, object]:
        try:
            stat = path.lstat()
        except OSError as error:
            raise StorageRetirementError("retention file cannot be inspected") from error
        if not path.is_file() or path.is_symlink():
            raise StorageRetirementError("retention file is not a regular file")
        allocated = int(getattr(stat, "st_blocks", 0)) * 512
        content_sha256 = _sha256_file(path) if stat.st_size <= _SMALL_CONTENT_BYTES else None
        return {
            "record": "file",
            "path": path.relative_to(root).as_posix(),
            "size_bytes": stat.st_size,
            "allocated_bytes": allocated,
            "exclusive_bytes": allocated if stat.st_nlink == 1 else 0,
            "mtime_ns": stat.st_mtime_ns,
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "content_sha256": content_sha256,
        }

    def _require_root(self, root: Path) -> Path:
        candidate = root.expanduser()
        if not candidate.is_absolute() or candidate.is_symlink():
            raise StorageRetirementError("retention root must be an absolute real directory")
        resolved = candidate.resolve(strict=True)
        if resolved != candidate.resolve(strict=False):
            raise StorageRetirementError("retention root is unsafe")
        if not resolved.is_dir():
            raise StorageRetirementError("retention root is not a directory")
        if resolved != self._allowed_root and not resolved.is_relative_to(self._allowed_root):
            raise StorageRetirementError("retention root is outside the authorized root")
        return resolved

    def _safe_target(self, root: Path, relative: str) -> Path:
        candidate = root / relative
        if any(path.is_symlink() for path in (candidate, *candidate.parents)):
            raise StorageRetirementError("retention target or ancestor is a symbolic link")
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise StorageRetirementError("retention target escapes the authorized root")
        return candidate

    def _require_paths_outside_root(self, root: Path, paths: tuple[str, ...]) -> None:
        for relative in paths:
            candidate = self._safe_target(root, relative)
            if candidate == root:
                raise StorageRetirementError("retention cannot remove its own root")

    def _assert_not_protected(self, target: Path) -> None:
        resolved = target.resolve(strict=False)
        protected = self._protected_paths()
        for path in protected:
            if resolved == path or resolved.is_relative_to(path) or path.is_relative_to(resolved):
                raise StorageRetirementError(f"retention target is an active reference: {resolved}")
        releases = self._runtime_root / "releases"
        if resolved.parent == releases or releases.is_relative_to(resolved):
            protected_shas = self._protected_release_shas()
            if resolved.name in protected_shas:
                raise StorageRetirementError("release is still referenced by deployment")
        unit_text = self._unit_text()
        if str(resolved) in unit_text:
            raise StorageRetirementError("retention target is referenced by the service unit")
        for value in self._systemctl_properties().values():
            if str(resolved) in value:
                raise StorageRetirementError("retention target is referenced by systemd")
        if self._runtime_root.is_dir():
            for path in self._runtime_root.iterdir():
                if path.is_symlink():
                    destination = path.resolve(strict=False)
                    if destination == resolved or destination.is_relative_to(resolved):
                        raise StorageRetirementError(
                            "retention target is referenced by a runtime symlink"
                        )
        self._require_unused(resolved)

    def _require_unused(self, target: Path, retiring: Path | None = None) -> None:
        for path in (target, retiring):
            if path is None or not path.exists():
                continue
            try:
                _require_no_directory_users(path)
            except WorkspaceV2LifecycleError as error:
                raise StorageRetirementError("retention target remains in use") from error

    def _protected_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        bindings = self._bindings()
        for binding in (getattr(bindings, "current", None), getattr(bindings, "previous", None)):
            if binding is not None and getattr(binding, "workspace_root", None):
                paths.append(Path(binding.workspace_root).expanduser().resolve(strict=False))
        state = self._deployment_state()
        for attribute in ("current_release_path", "previous_release_path"):
            value = getattr(state, attribute, None)
            if value:
                paths.append(Path(value).expanduser().resolve(strict=False))
        return tuple(sorted(set(paths), key=str))

    def _protected_release_shas(self) -> frozenset[str]:
        shas = {_PROTECTED_RELEASE_SHA}
        bindings = self._bindings()
        for binding in (getattr(bindings, "current", None), getattr(bindings, "previous", None)):
            if binding is not None and getattr(binding, "release_sha", None):
                shas.add(binding.release_sha)
        state = self._deployment_state()
        for attribute in ("current", "previous"):
            value = getattr(state, attribute, None)
            if value:
                shas.add(value)
        return frozenset(shas)

    def _deployment_state(self) -> object:
        if self._deployment_cache is None:
            if self._deployment_state_loader is not None:
                self._deployment_cache = self._deployment_state_loader()
            else:
                self._deployment_cache = LocalReleaseService().load_deployment_state()
        return self._deployment_cache

    def _bindings(self) -> object:
        if self._bindings_cache is None:
            if self._workspace_bindings_loader is not None:
                self._bindings_cache = self._workspace_bindings_loader()
            else:
                self._bindings_cache = LocalReleaseService().load_workspace_bindings()
        return self._bindings_cache

    def _unit_text(self) -> str:
        if self._unit_text_cache is None:
            try:
                self._unit_text_cache = (
                    self._systemd_unit_path.read_text(encoding="utf-8")
                    if self._systemd_unit_path.is_file()
                    else ""
                )
            except OSError as error:
                raise StorageRetirementError("service unit cannot be inspected") from error
        return self._unit_text_cache

    def _systemctl_properties(self) -> dict[str, str]:
        if self._systemctl_cache is None:
            if self._systemctl_properties_loader is not None:
                self._systemctl_cache = self._systemctl_properties_loader()
            else:
                try:
                    properties = LocalReleaseService().systemctl.show_properties(
                        "investment-analyst.service", ("ExecStart", "WorkingDirectory")
                    )
                except Exception as error:
                    raise StorageRetirementError("systemd references cannot be verified") from error
                self._systemctl_cache = {str(key): str(value) for key, value in properties.items()}
        return self._systemctl_cache

    def _remove(self, path: Path) -> None:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
        except OSError as error:
            raise StorageRetirementError("retention removal failed") from error
        _fsync_directory(path.parent)

    def _safe_control_path(self, path: Path) -> Path:
        candidate = path.expanduser()
        if (
            not candidate.is_absolute()
            or ".." in candidate.parts
            or any(item.is_symlink() for item in (candidate, *candidate.parents))
        ):
            raise StorageRetirementError("retention control path is unsafe")
        return candidate

    @staticmethod
    def _require_remote_prefix(value: str) -> str:
        if not value or value.startswith("/") or ".." in Path(value).parts or value.endswith("/"):
            raise StorageRetirementError("retention remote prefix is unsafe")
        return value

    @staticmethod
    def _free(reader: Callable[[], int], label: str) -> int:
        value = reader()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StorageRetirementError(f"{label} free bytes reader is invalid")
        return value

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise StorageRetirementError("retention clock must be timezone-aware")
        return value.astimezone(UTC)


__all__ = [
    "RetentionNotAdmitted",
    "StorageRetirementError",
    "StorageRetirementService",
]
