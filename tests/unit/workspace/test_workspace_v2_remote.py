"""Remote manifest planning tests using small local packed fixtures."""

import hashlib
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.workspace.drive_transport import DriveTransportError
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_backup import WorkspaceV2BackupService
from investment_analyst.workspace.workspace_v2_lifecycle_models import (
    FrozenSourceCut,
    LifecycleContentRole,
    LifecycleFile,
    LocalComplementManifest,
)
from investment_analyst.workspace.workspace_v2_remote import (
    RemoteBackupObject,
    RemoteBackupRestoreReceipt,
    RemoteBackupTransferReceipt,
    WorkspaceV2RemoteError,
    WorkspaceV2RemoteService,
)


def _remote(tmp_path: Path) -> tuple[WorkspaceV2RemoteService, Path]:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=2)
    (source / "state" / "payload.bin").write_bytes(b"packed-data" * 20_000)
    backup_root = tmp_path / "backup"
    backup = WorkspaceV2BackupService(service)
    backup.create_packed(
        source,
        backup_root,
        volume_bytes=64_000,
        fragment_bytes=16_000,
        available_bytes=lambda: 10_000_000_000,
    )
    return (
        WorkspaceV2RemoteService(
            backup,
            clock=lambda: datetime(2026, 10, 13, tzinfo=UTC),
        ),
        backup_root,
    )


def test_base_plan_and_identical_delta_reuse_sealed_objects(tmp_path: Path) -> None:
    remote, backup_root = _remote(tmp_path)
    base = remote.plan_base(backup_root, configuration_fingerprint="a" * 64)
    repeated = remote.plan_base(
        backup_root,
        configuration_fingerprint="a" * 64,
        parent=base.manifest,
    )
    assert base.new_objects
    assert not repeated.new_objects
    assert repeated.reused_objects == repeated.manifest.objects
    published = remote.publish_manifest(repeated, tmp_path / "remote-manifest.json")
    assert published.backup_id == base.manifest.backup_id


def test_changed_packed_backup_reuses_parent_objects_and_uploads_only_new_objects(
    tmp_path: Path,
) -> None:
    """A physical delta uses a parent manifest rather than one remote object per source file."""
    remote, backup_root = _remote(tmp_path)
    base = remote.plan_base(backup_root, configuration_fingerprint="a" * 64)
    source = tmp_path / "source"
    (source / "state" / "payload.bin").write_bytes(b"changed" * 20_000)
    changed_root = tmp_path / "changed-backup"
    remote._backup_service.create_packed(
        source,
        changed_root,
        volume_bytes=64_000,
        fragment_bytes=16_000,
        available_bytes=lambda: 10_000_000_000,
    )

    delta = remote.plan_base(changed_root, configuration_fingerprint="a" * 64, parent=base.manifest)

    assert delta.manifest.parent_backup_id == base.manifest.backup_id
    assert delta.new_objects
    assert delta.reused_objects
    assert len(delta.manifest.objects) < 100


def test_retention_preview_keeps_parent_closure_and_only_prunes_known_objects(
    tmp_path: Path,
) -> None:
    """Retention has logical versions, preserves shared parents and never sees foreign keys."""
    remote, backup_root = _remote(tmp_path)
    base = remote.plan_base(backup_root, configuration_fingerprint="a" * 64).manifest
    manifests = [base]
    for offset in range(1, 25):
        object_item = RemoteBackupObject(
            object_key=f"volumes/{offset:064x}.gz",
            sha256=f"{offset:064x}",
            size_bytes=offset,
        )
        manifests.append(
            base.model_copy(
                update={
                    "backup_id": uuid4(),
                    "created_at": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=offset),
                    "parent_backup_id": base.backup_id,
                    "objects": tuple(
                        sorted((*base.objects, object_item), key=lambda item: item.object_key)
                    ),
                }
            )
        )
    latest = max(manifests, key=lambda item: item.created_at)
    receipt = RemoteBackupRestoreReceipt(
        backup_id=latest.backup_id,
        remote_manifest_sha256="a" * 64,
        restored_workspace_id=uuid4(),
        volume_count=1,
        downloaded_volumes=1,
        downloaded_bytes=1,
        completed_at=datetime(2026, 2, 1, tzinfo=UTC),
    )

    preview = remote.plan_retention_preview(manifests, recovery_receipt=receipt)

    assert base.backup_id in preview.retained_backup_ids
    assert "manifests/foreign-user.json" not in preview.prune_object_keys
    assert preview.prune_object_keys
    assert not set(preview.prune_object_keys) & set(preview.retained_object_keys)


def test_retention_preview_requires_latest_recovery_and_complete_parent_chain(
    tmp_path: Path,
) -> None:
    remote, backup_root = _remote(tmp_path)
    base = remote.plan_base(backup_root, configuration_fingerprint="a" * 64).manifest
    changed = base.model_copy(
        update={"backup_id": uuid4(), "created_at": datetime(2026, 11, 1, tzinfo=UTC)}
    )
    receipt = RemoteBackupRestoreReceipt(
        backup_id=base.backup_id,
        remote_manifest_sha256="a" * 64,
        restored_workspace_id=uuid4(),
        volume_count=1,
        downloaded_volumes=1,
        downloaded_bytes=1,
        completed_at=datetime(2026, 2, 2, tzinfo=UTC),
    )
    with pytest.raises(WorkspaceV2RemoteError, match="latest backup"):
        remote.plan_retention_preview((base, changed), recovery_receipt=receipt)

    missing_parent = changed.model_copy(update={"parent_backup_id": uuid4()})
    with pytest.raises(WorkspaceV2RemoteError, match="parent"):
        remote.plan_retention_preview(
            (missing_parent,),
            recovery_receipt=receipt.model_copy(update={"backup_id": missing_parent.backup_id}),
        )


def test_base_plan_reuses_restore_controls_without_prehashing_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, backup_root = _remote(tmp_path)

    def fail_if_called(path: Path) -> object:
        raise AssertionError("plan must not run the full prehash inspection")

    monkeypatch.setattr(remote._backup_service, "inspect_packed", fail_if_called)
    plan = remote.plan_base(backup_root, configuration_fingerprint="a" * 64)
    assert plan.manifest.objects


def test_remote_plan_rejects_unsealed_backup_and_invalid_configuration(tmp_path: Path) -> None:
    remote, backup_root = _remote(tmp_path)
    with pytest.raises(WorkspaceV2RemoteError, match="fingerprint"):
        remote.plan_base(backup_root, configuration_fingerprint="invalid")
    (backup_root / "packed-backup-volumes.jsonl").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(WorkspaceV2RemoteError, match="sealed volume journal"):
        remote.plan_base(backup_root, configuration_fingerprint="a" * 64)


class _RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []
        self.objects: dict[str, bytes] = {}

    def copy_sealed_object(self, source: Path, remote_object: str) -> tuple[object, ...]:
        self.calls.append((source, remote_object))
        return ()

    def copy_immutable_object(self, source: Path, remote_object: str) -> tuple[object, ...]:
        data = source.read_bytes()
        if remote_object in self.objects and self.objects[remote_object] != data:
            raise DriveTransportError("immutable conflict")
        self.objects[remote_object] = data
        return self.copy_sealed_object(source, remote_object)

    def fetch_sealed_object(self, remote_object: str, destination: Path) -> tuple[object, ...]:
        destination.write_bytes(self.objects[remote_object])
        return ()


class _CopyingTransport:
    def __init__(self, root: Path) -> None:
        self._root = root

    def copy_sealed_object(self, source: Path, remote_object: str) -> tuple[object, ...]:
        target = self._root / remote_object
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return ()

    def fetch_sealed_object(self, remote_object: str, destination: Path) -> tuple[object, ...]:
        source = self._root / remote_object
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return ()


class _InterruptingCopyingTransport(_CopyingTransport):
    def __init__(self, root: Path, *, successful_fetches: int) -> None:
        super().__init__(root)
        self._successful_fetches = successful_fetches
        self._fetches = 0

    def fetch_sealed_object(self, remote_object: str, destination: Path) -> tuple[object, ...]:
        if self._fetches >= self._successful_fetches:
            raise DriveTransportError("interrupted test download")
        self._fetches += 1
        return super().fetch_sealed_object(remote_object, destination)


def _complement(tmp_path: Path) -> Path:
    root = tmp_path / "complement"
    (root / "state").mkdir(parents=True)
    payload = root / "state" / "preserved.json"
    payload.write_text('{"preserved":true}\n', encoding="utf-8")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    source = FrozenSourceCut(
        operation_id=uuid4(),
        source_root=tmp_path / "retired-source",
        workspace_id=uuid4(),
        workspace_format_version=1,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        source_fingerprint="a" * 64,
        writer_closed=True,
        frozen_at=datetime(2026, 10, 13, tzinfo=UTC),
    )
    manifest = LocalComplementManifest(
        source=source,
        created_at=datetime(2026, 10, 13, tzinfo=UTC),
        files=(
            LifecycleFile(
                path="state/preserved.json",
                role=LifecycleContentRole.OPERATIONAL_HISTORIC,
                size_bytes=payload.stat().st_size,
                sha256=digest,
            ),
        ),
        total_bytes=payload.stat().st_size,
    )
    (root / "local-complement-manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    return root


def _complement_with_two_files(tmp_path: Path) -> Path:
    root = _complement(tmp_path)
    extra = root / "state" / "second.json"
    extra.write_text('{"preserved":"second"}\n', encoding="utf-8")
    original = LocalComplementManifest.model_validate_json(
        (root / "local-complement-manifest.json").read_text(encoding="utf-8")
    )
    second = LifecycleFile(
        path="state/second.json",
        role=LifecycleContentRole.OPERATIONAL_HISTORIC,
        size_bytes=extra.stat().st_size,
        sha256=hashlib.sha256(extra.read_bytes()).hexdigest(),
    )
    updated = original.model_copy(
        update={
            "files": tuple(sorted((*original.files, second), key=lambda item: item.path)),
            "total_bytes": original.total_bytes + second.size_bytes,
        }
    )
    (root / "local-complement-manifest.json").write_text(
        updated.model_dump_json(), encoding="utf-8"
    )
    return root


def test_complement_upload_seals_files_then_publishes_its_manifest(tmp_path: Path) -> None:
    remote, _ = _remote(tmp_path)
    transport = _RecordingTransport()
    root = _complement(tmp_path)
    receipt = remote.upload_complement(
        root,
        transport=transport,  # type: ignore[arg-type]
        checkpoint_path=tmp_path / "complement-checkpoint.json",
        receipt_path=tmp_path / "complement-receipt.json",
    )

    assert receipt.objects_uploaded == 1
    assert receipt.objects_reused == 0
    assert (tmp_path / "complement-receipt.json").is_file()
    assert not (tmp_path / "complement-checkpoint.json").exists()
    assert transport.calls[-1][1].startswith("complement-manifests/")


def test_complement_verification_reads_back_each_sealed_object(tmp_path: Path) -> None:
    remote, _ = _remote(tmp_path)
    transport = _CopyingTransport(tmp_path / "remote")
    transfer = remote.upload_complement(
        _complement(tmp_path),
        transport=transport,  # type: ignore[arg-type]
        checkpoint_path=tmp_path / "checkpoint.json",
        receipt_path=tmp_path / "transfer.json",
    )

    verified = remote.verify_complement_from_remote(
        transfer,
        transport=transport,  # type: ignore[arg-type]
        staging_root=tmp_path / "staging",
        checkpoint_path=tmp_path / "verification-checkpoint.json",
        receipt_path=tmp_path / "verification.json",
    )

    assert verified.files_verified == 1
    assert verified.bytes_verified > 0
    assert (tmp_path / "verification.json").is_file()


def test_complement_verification_resumes_after_an_interrupted_object(tmp_path: Path) -> None:
    remote, _ = _remote(tmp_path)
    remote_root = tmp_path / "remote"
    transfer = remote.upload_complement(
        _complement_with_two_files(tmp_path),
        transport=_CopyingTransport(remote_root),  # type: ignore[arg-type]
        checkpoint_path=tmp_path / "upload-checkpoint.json",
        receipt_path=tmp_path / "transfer.json",
    )
    checkpoint = tmp_path / "verification-checkpoint.json"
    with pytest.raises(WorkspaceV2RemoteError, match="download was not confirmed"):
        remote.verify_complement_from_remote(
            transfer,
            transport=_InterruptingCopyingTransport(remote_root, successful_fetches=2),  # type: ignore[arg-type]
            staging_root=tmp_path / "staging",
            checkpoint_path=checkpoint,
            receipt_path=tmp_path / "verification.json",
        )
    assert checkpoint.is_file()

    result = remote.verify_complement_from_remote(
        transfer,
        transport=_CopyingTransport(remote_root),  # type: ignore[arg-type]
        staging_root=tmp_path / "staging",
        checkpoint_path=checkpoint,
        receipt_path=tmp_path / "verification.json",
    )
    assert result.files_verified == 2
    assert not checkpoint.exists()


def test_uploads_each_new_object_once_then_publishes_remote_manifest(tmp_path: Path) -> None:
    remote, backup_root = _remote(tmp_path)
    plan = remote.plan_base(backup_root, configuration_fingerprint="a" * 64)
    transport = _RecordingTransport()
    receipt_path = tmp_path / "receipt.json"

    receipt = remote.upload_base(plan, backup_root, transport, receipt_path)  # type: ignore[arg-type]

    assert receipt.objects_uploaded == len(plan.new_objects)
    assert receipt.objects_reused == 0
    assert receipt_path.is_file()
    assert len(transport.calls) == len(plan.new_objects) + 1
    assert transport.calls[-1][1] == f"manifests/{plan.manifest.backup_id}.json"
    assert {name for _, name in transport.calls[:-1]} == {
        item.object_key for item in plan.new_objects
    }


def test_upload_refuses_existing_receipt_before_remote_write(tmp_path: Path) -> None:
    remote, backup_root = _remote(tmp_path)
    plan = remote.plan_base(backup_root, configuration_fingerprint="a" * 64)
    transport = _RecordingTransport()
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text("already present", encoding="utf-8")

    with pytest.raises(WorkspaceV2RemoteError, match="receipt destination"):
        remote.upload_base(plan, backup_root, transport, receipt_path)  # type: ignore[arg-type]
    assert not transport.calls


class _RemoteObjectTransport:
    def __init__(self, root: Path, *, fail_after_fetches: int | None = None) -> None:
        self._root = root
        self._fail_after_fetches = fail_after_fetches
        self.fetches = 0

    def fetch_sealed_object(self, remote_object: str, destination: Path) -> tuple[object, ...]:
        if self._fail_after_fetches is not None and self.fetches >= self._fail_after_fetches:
            raise DriveTransportError("interrupted test download")
        source = self._root / remote_object
        if not source.is_file():
            raise DriveTransportError("missing test remote object")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        self.fetches += 1
        return ()


def _remote_restore_fixture(
    tmp_path: Path,
    *,
    legacy_controls: bool = False,
) -> tuple[WorkspaceV2RemoteService, RemoteBackupTransferReceipt, Path, Path]:
    remote, backup_root = _remote(tmp_path)
    plan = remote.plan_base(backup_root, configuration_fingerprint="a" * 64)
    if legacy_controls:
        sources = tuple(
            source.model_copy(
                update={
                    "object": source.object.model_copy(
                        update={"object_key": f"controls/{source.relative_path}"}
                    )
                }
            )
            if source.object.object_key.startswith("controls-v2/")
            else source
            for source in plan.sources
        )
        objects = tuple(
            sorted((source.object for source in sources), key=lambda item: item.object_key)
        )
        plan = plan.model_copy(
            update={
                "sources": sources,
                "manifest": plan.manifest.model_copy(update={"objects": objects}),
            }
        )
    remote_root = tmp_path / "remote"
    for source in plan.sources:
        target = remote_root / source.object.object_key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(backup_root / source.relative_path, target)
    manifest_path = tmp_path / "published-remote-manifest.json"
    remote.publish_manifest(plan, manifest_path)
    remote_manifest = remote_root / f"manifests/{plan.manifest.backup_id}.json"
    remote_manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest_path, remote_manifest)
    receipt = RemoteBackupTransferReceipt(
        backup_id=plan.manifest.backup_id,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        objects_uploaded=len(plan.new_objects),
        bytes_uploaded=sum(item.size_bytes for item in plan.new_objects),
        objects_reused=0,
        bytes_reused=0,
        attempts=(),
        completed_at=datetime(2026, 10, 13, tzinfo=UTC),
    )
    shutil.rmtree(backup_root)
    return remote, receipt, remote_root, tmp_path / "restore"


def test_restore_retains_read_compatibility_with_legacy_controls(tmp_path: Path) -> None:
    remote, receipt, remote_root, destination = _remote_restore_fixture(
        tmp_path, legacy_controls=True
    )
    result = remote.restore_from_remote(
        receipt,
        configuration_fingerprint="a" * 64,
        transport=_RemoteObjectTransport(remote_root),  # type: ignore[arg-type]
        staging_root=tmp_path / "staging",
        destination=destination,
        available_bytes=lambda: 10_000_000_000,
        reserve_bytes=1,
        receipt_path=tmp_path / "restored.json",
    )
    assert result.restored_workspace_id


def test_three_control_generations_preserve_parent_bytes(tmp_path: Path) -> None:
    remote, backup_root = _remote(tmp_path)
    transport = _RecordingTransport()
    prior = None
    original: dict[str, bytes] = {}
    for generation in range(3):
        if generation:
            (tmp_path / "source/state/payload.bin").write_bytes(f"generation-{generation}".encode())
            backup_root = tmp_path / f"backup-{generation}"
            remote._backup_service.create_packed(
                tmp_path / "source",
                backup_root,
                volume_bytes=64_000,
                fragment_bytes=16_000,
                available_bytes=lambda: 10_000_000_000,
            )
        plan = remote.plan_base(backup_root, configuration_fingerprint="a" * 64, parent=prior)
        remote.upload_base(plan, backup_root, transport, tmp_path / f"uploaded-{generation}.json")  # type: ignore[arg-type]
        assert all(not item.object_key.startswith("controls/") for item in plan.manifest.objects)
        assert all(transport.objects[key] == value for key, value in original.items())
        original.update(transport.objects)
        prior = plan.manifest


def test_size_only_success_cannot_publish_conflicting_remote_control(tmp_path: Path) -> None:
    remote, backup_root = _remote(tmp_path)
    plan = remote.plan_base(backup_root, configuration_fingerprint="a" * 64)

    class CorruptingTransport(_RecordingTransport):
        def fetch_sealed_object(self, remote_object: str, destination: Path) -> tuple[object, ...]:
            data = self.objects[remote_object]
            destination.write_bytes(b"x" * len(data))
            return ()

    with pytest.raises(WorkspaceV2RemoteError, match="conflicts with its seal"):
        remote.upload_base(plan, backup_root, CorruptingTransport(), tmp_path / "uploaded.json")  # type: ignore[arg-type]
    assert not (tmp_path / "uploaded.json").exists()


def test_upload_rejects_same_size_local_volume_change_before_remote_write(tmp_path: Path) -> None:
    remote, backup_root = _remote(tmp_path)
    plan = remote.plan_base(backup_root, configuration_fingerprint="a" * 64)
    volume_source = next(
        item for item in plan.sources if item.object.object_key.startswith("volumes/")
    )
    payload = backup_root / volume_source.relative_path
    payload.write_bytes(b"x" * payload.stat().st_size)
    transport = _RecordingTransport()
    with pytest.raises(WorkspaceV2RemoteError, match="binding is unavailable or unsafe"):
        remote.upload_base(plan, backup_root, transport, tmp_path / "uploaded.json")  # type: ignore[arg-type]
    assert not any(key.startswith("volumes/") for _, key in transport.calls)


def test_remote_restore_streams_from_remote_without_local_volume_fallback(tmp_path: Path) -> None:
    remote, transfer, remote_root, restore_root = _remote_restore_fixture(tmp_path)
    transport = _RemoteObjectTransport(remote_root)
    result = remote.restore_from_remote(
        transfer,
        configuration_fingerprint="a" * 64,
        transport=transport,  # type: ignore[arg-type]
        staging_root=tmp_path / "staging",
        destination=restore_root,
        available_bytes=lambda: 10_000_000_000,
        reserve_bytes=1,
        receipt_path=tmp_path / "remote-restore-receipt.json",
    )

    assert result.volume_count > 1
    assert result.downloaded_volumes == result.volume_count
    assert result.downloaded_bytes > 0
    assert not tuple((tmp_path / "staging" / "volumes").iterdir())
    assert (restore_root / "state" / "payload.bin").read_bytes() == b"packed-data" * 20_000


def test_remote_restore_resumes_after_interruption_without_reusing_local_volumes(
    tmp_path: Path,
) -> None:
    remote, transfer, remote_root, restore_root = _remote_restore_fixture(tmp_path)
    with pytest.raises(WorkspaceV2RemoteError, match="download was not confirmed"):
        remote.restore_from_remote(
            transfer,
            configuration_fingerprint="a" * 64,
            transport=_RemoteObjectTransport(remote_root, fail_after_fetches=5),  # type: ignore[arg-type]
            staging_root=tmp_path / "staging",
            destination=restore_root,
            available_bytes=lambda: 10_000_000_000,
            reserve_bytes=1,
            receipt_path=tmp_path / "remote-restore-receipt.json",
        )
    checkpoint = tmp_path / ".restore.packed-restore-checkpoint.json"
    assert checkpoint.is_file()

    result = remote.restore_from_remote(
        transfer,
        configuration_fingerprint="a" * 64,
        transport=_RemoteObjectTransport(remote_root),  # type: ignore[arg-type]
        staging_root=tmp_path / "staging",
        destination=restore_root,
        available_bytes=lambda: 10_000_000_000,
        reserve_bytes=1,
        receipt_path=tmp_path / "remote-restore-receipt.json",
    )

    assert result.downloaded_volumes < result.volume_count
    assert restore_root.is_dir()


@pytest.mark.parametrize("failure", ("missing", "corrupt"))
def test_remote_restore_rejects_missing_or_corrupt_volume(tmp_path: Path, failure: str) -> None:
    remote, transfer, remote_root, restore_root = _remote_restore_fixture(tmp_path)
    volume = next((remote_root / "volumes").iterdir())
    if failure == "missing":
        volume.unlink()
    else:
        volume.write_bytes(b"x" * volume.stat().st_size)

    with pytest.raises(WorkspaceV2RemoteError):
        remote.restore_from_remote(
            transfer,
            configuration_fingerprint="a" * 64,
            transport=_RemoteObjectTransport(remote_root),  # type: ignore[arg-type]
            staging_root=tmp_path / "staging",
            destination=restore_root,
            available_bytes=lambda: 10_000_000_000,
            reserve_bytes=1,
            receipt_path=tmp_path / "remote-restore-receipt.json",
        )
    assert not (tmp_path / "remote-restore-receipt.json").exists()
    assert not tuple((tmp_path / "staging" / "volumes").iterdir())


def test_remote_restore_refuses_a_volume_that_exceeds_fresh_capacity(tmp_path: Path) -> None:
    remote, transfer, remote_root, restore_root = _remote_restore_fixture(tmp_path)

    with pytest.raises(WorkspaceV2RemoteError, match="capacity reserve"):
        remote.restore_from_remote(
            transfer,
            configuration_fingerprint="a" * 64,
            transport=_RemoteObjectTransport(remote_root),  # type: ignore[arg-type]
            staging_root=tmp_path / "staging",
            destination=restore_root,
            available_bytes=lambda: 0,
            reserve_bytes=1,
            receipt_path=tmp_path / "remote-restore-receipt.json",
        )


def test_remote_manifest_reconciliation_preserves_a_mismatched_transfer_claim(
    tmp_path: Path,
) -> None:
    remote, transfer, remote_root, _ = _remote_restore_fixture(tmp_path)
    legacy = transfer.model_copy(update={"manifest_sha256": "f" * 64})
    transfer_path = tmp_path / "legacy-transfer.json"
    transfer_path.write_text(legacy.model_dump_json(), encoding="utf-8")
    receipt_path = tmp_path / "reconciliation.json"

    result = remote.reconcile_remote_manifest(
        legacy,
        transfer_receipt_path=transfer_path,
        configuration_fingerprint="a" * 64,
        transport=_RemoteObjectTransport(remote_root),  # type: ignore[arg-type]
        staging_root=tmp_path / "staging",
        receipt_path=receipt_path,
    )

    assert result.transfer_manifest_sha256 == "f" * 64
    assert result.observed_manifest_sha256 != result.transfer_manifest_sha256
    assert receipt_path.is_file()
