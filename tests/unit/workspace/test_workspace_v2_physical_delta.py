"""Focused streaming coverage for the packed physical parent index."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.workspace import workspace_v2_physical_delta as delta_module
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_backup import (
    WorkspaceV2BackupService,
)
from investment_analyst.workspace.workspace_v2_physical_delta import (
    WorkspaceV2PhysicalDeltaError,
    WorkspaceV2PhysicalDeltaIndexService,
    WorkspaceV2PhysicalDeltaService,
    _iter_delta_files,
    _iter_entries,
    iter_physical_delta_index,
    verify_physical_delta_index,
)


def _link_journal_fixture(tmp_path: Path, *, mutable: bool = False):
    from investment_analyst.storage.workspace_v2_inventory import reconciliation_file_reference

    retained, slot = tmp_path / "retained", tmp_path / "slot"
    retained.mkdir()
    slot.mkdir()
    content = b"immutable witness fixture"
    digest = hashlib.sha256(content).hexdigest()
    relative = (
        "storage/v2/index.duckdb"
        if mutable
        else f"storage/v2/raw/raw/sha256/{digest[:2]}/{digest[2:4]}/{digest}"
    )
    source = retained / relative
    target = slot / relative
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    item = delta_module.PhysicalDeltaFile(
        path=relative,
        size_bytes=len(content),
        sha256=digest,
        blocks=(
            delta_module.PhysicalDeltaBlock(
                offset_bytes=0, length_bytes=len(content), sha256=digest, origin="parent"
            ),
        ),
    )
    inventory = tmp_path / "inventory.jsonl"
    inventory.write_text(item.model_dump_json() + "\n")
    ref = reconciliation_file_reference(inventory, "inventory")
    header = delta_module.ImmutableLinkHeader(
        operation_id=uuid4(),
        action="LINK",
        retained_root=retained,
        retained_workspace_id=uuid4(),
        slot=slot,
        inventory=ref,
        parent_manifest=ref,
        parent_index=ref,
        parent_proof=ref,
        verifier_sha256=delta_module._immutable_link_verifier_sha256(),
        byte_budget=1_000_000,
        created_at=datetime.now(UTC),
    )
    return header, item, source, target


def test_link_unlink_chain_explains_ctime_without_rehashing_known_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_analyst.storage.workspace_v2_inventory import reconciliation_file_reference

    header, item, source, target = _link_journal_fixture(tmp_path)
    first = delta_module.ImmutableLinkJournal(tmp_path / "link", header).open()
    before = source.stat()
    first.apply_page(((0, item),))
    assert target.stat().st_ino == before.st_ino and source.stat().st_nlink == 2
    reference = reconciliation_file_reference(first.root / "page-00000000.trailer.json", "control")
    unheader = header.model_copy(
        update={"action": "UNLINK", "previous_journal": reference, "operation_id": uuid4()}
    )
    second = delta_module.ImmutableLinkJournal(tmp_path / "unlink", unheader).open()
    with monkeypatch.context() as patch:
        patch.setattr(
            delta_module, "_verify_file", lambda *args: pytest.fail("known payload reread")
        )
        second.apply_page(((0, item),))
    assert not target.exists() and source.stat().st_nlink == 1
    assert source.stat().st_ino == before.st_ino
    assert source.stat().st_ctime_ns != before.st_ctime_ns
    assert (
        delta_module.ImmutableLinkJournal(tmp_path / "unlink", unheader)
        .open()
        .confirmed_inventory_ordinal
        == 0
    )


@pytest.mark.parametrize("crash_at", ["link", "trailer"])
def test_link_journal_recovers_confirmed_tail_without_repeating_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_at: str,
) -> None:
    header, item, source, target = _link_journal_fixture(tmp_path)
    journal = delta_module.ImmutableLinkJournal(tmp_path / "journal", header).open()
    real_link, real_seal = os.link, journal._seal
    with monkeypatch.context() as crash:
        if crash_at == "link":

            def interrupted_link(first, second, **kwargs):
                real_link(first, second, **kwargs)
                if Path(first) == source:
                    raise RuntimeError("fixture interrupted after link")

            crash.setattr(delta_module.os, "link", interrupted_link)
        else:

            def interrupted_trailer(path, document):
                if path.name.endswith(".trailer.json"):
                    raise RuntimeError("fixture interrupted before trailer")
                return real_seal(path, document)

            crash.setattr(journal, "_seal", interrupted_trailer)
        with pytest.raises(RuntimeError, match="fixture interrupted"):
            journal.apply_page(((0, item),))
    assert target.stat().st_ino == source.stat().st_ino and source.stat().st_nlink == 2
    reopened = delta_module.ImmutableLinkJournal(journal.root, header).open()
    reopened.apply_page(((0, item),))
    assert source.stat().st_nlink == 2
    assert reopened.confirmed_inventory_ordinal == 0


def test_adopting_old_link_hashes_present_content_without_inventing_preimage(
    tmp_path: Path,
) -> None:
    header, item, source, target = _link_journal_fixture(tmp_path)
    target.parent.mkdir(parents=True)
    os.link(source, target)
    journal = delta_module.ImmutableLinkJournal(tmp_path / "journal", header).open()
    journal.apply_page(((0, item),), adopt_existing=True)
    _, entries = journal._read_page(journal.root, 0, None)
    assert entries[0].phase == "BASELINE" and entries[0].before is None
    assert entries[0].after == delta_module.ImmutableLinkIdentity.observe(source)


@pytest.mark.parametrize("fault", ["ctime", "mutable", "budget", "symlink"])
def test_link_journal_rejects_unknown_mutation_mutable_paths_and_budget(
    tmp_path: Path,
    fault: str,
) -> None:
    from investment_analyst.storage.workspace_v2_inventory import reconciliation_file_reference

    header, item, source, target = _link_journal_fixture(tmp_path, mutable=fault == "mutable")
    if fault == "budget":
        header = header.model_copy(update={"byte_budget": 1})
    journal = delta_module.ImmutableLinkJournal(tmp_path / "journal", header)
    if fault == "budget":
        with pytest.raises(WorkspaceV2PhysicalDeltaError, match="budget"):
            journal.open()
        assert not target.exists()
        return
    journal.open()
    if fault == "ctime":
        journal.apply_page(((0, item),))
        proof = reconciliation_file_reference(
            journal.root / "page-00000000.trailer.json", "control"
        )
        header = header.model_copy(
            update={"action": "UNLINK", "operation_id": uuid4(), "previous_journal": proof}
        )
        stamp = source.stat()
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        journal = delta_module.ImmutableLinkJournal(tmp_path / "unlink", header).open()
    if fault == "symlink":
        original = source.with_name("original")
        source.rename(original)
        source.symlink_to(original)
    with pytest.raises(WorkspaceV2PhysicalDeltaError):
        journal.apply_page(((0, item),))
    if fault == "ctime":
        assert target.exists()
    else:
        assert not target.exists()


def _backup(tmp_path: Path, *, immutable: bool = False) -> tuple[WorkspaceV2BackupService, Path]:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=2)
    (source / "state" / "empty.bin").touch()
    (source / "state" / "payload.bin").write_bytes(b"physical-delta" * 20_000)
    if immutable:
        for prefix in ("storage/v2/raw/raw/sha256", "data/documents/sha256"):
            content = b'{"fixture":"immutable"}'
            checksum = hashlib.sha256(content).hexdigest()
            path = source / prefix / checksum[:2] / checksum[2:4] / checksum
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    backup = WorkspaceV2BackupService(service)
    root = tmp_path / "backup"
    backup.create_packed(
        source,
        root,
        volume_bytes=32_000,
        fragment_bytes=4_000,
        available_bytes=lambda: 10_000_000_000,
    )
    return backup, root


def _builder(backup: WorkspaceV2BackupService) -> WorkspaceV2PhysicalDeltaIndexService:
    return WorkspaceV2PhysicalDeltaIndexService(
        backup,
        clock=lambda: datetime(2026, 10, 15, tzinfo=UTC),
        checkpoint_interval=1,
    )


@pytest.mark.parametrize("fault", [None, "touch", "content", "code", "root", "parent", "receipt"])
def test_immutable_parent_cache_uses_verified_restore_and_invalidates_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    from investment_analyst.workspace.workspace_v2_remote import RemoteBackupRestoreReceipt

    backup, packed = _backup(tmp_path, immutable=True)
    index_manifest = _builder(backup).build(packed, tmp_path / "index")
    index = tmp_path / "index" / index_manifest.index_path
    workspace = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    receipt_path = tmp_path / "remote-restored.json"
    receipt = RemoteBackupRestoreReceipt(
        backup_id=index_manifest.parent_backup_id,
        remote_manifest_sha256="a" * 64,
        restored_workspace_id=workspace.manifest(source).workspace_id,
        volume_count=1,
        downloaded_volumes=1,
        downloaded_bytes=1,
        completed_at=datetime.now(UTC),
    )
    receipt_path.write_text(receipt.model_dump_json())
    physical = WorkspaceV2PhysicalDeltaService(workspace)
    proof_path = tmp_path / "parent-proof.json"
    proof = physical.prepare_immutable_parent_reuse(
        source,
        index,
        receipt_path,
        proof_path,
        parent_manifest_sha256="a" * 64,
    )
    blob = next((source / "storage/v2/raw/raw/sha256").rglob("?" * 64))
    if fault == "touch":
        blob.touch()
    elif fault == "content":
        blob.write_bytes(b"corrupt")
    elif fault in {"code", "root", "parent"}:
        field, value = {
            "code": ("verifier_sha256", "0" * 64),
            "root": ("source_root", tmp_path / "other"),
            "parent": ("parent_index_sha256", "0" * 64),
        }[fault]
        proof_path.write_text(proof.model_copy(update={field: value}).model_dump_json())
    elif fault == "receipt":
        receipt_path.write_text(receipt.model_copy(update={"backup_id": uuid4()}).model_dump_json())
    hashed = []
    original = delta_module._plan_delta_file

    def count_reads(root, relative, prior, output, block_bytes):
        hashed.append(relative)
        return original(root, relative, prior, output, block_bytes)

    monkeypatch.setattr(delta_module, "_plan_delta_file", count_reads)
    arguments = dict(
        parent_manifest_sha256="a" * 64, epoch=uuid4(), immutable_parent_proof=proof_path
    )
    output = tmp_path / "delta"
    if fault in {"content", "code", "root", "parent", "receipt"}:
        with pytest.raises(WorkspaceV2PhysicalDeltaError):
            physical.plan(source, index, output, **arguments)
        return
    manifest = physical.plan(source, index, output, **arguments)
    assert manifest.new_bytes == 0
    assert "storage/v2/index.duckdb" in hashed
    assert "state/payload.bin" in hashed
    assert (str(blob.relative_to(source)) in hashed) == (fault == "touch")
    assert not any(path.startswith("data/documents/sha256/") for path in hashed)
    assert physical.plan(source, index, output, **arguments) == manifest


def test_physical_delta_index_streams_complete_parent_coverage(tmp_path: Path) -> None:
    backup, root = _backup(tmp_path)

    manifest = _builder(backup).build(root, tmp_path / "index")
    trailer = verify_physical_delta_index(tmp_path / "index" / manifest.index_path)

    assert trailer.parent_backup_id == manifest.parent_backup_id
    assert trailer.file_count > 1
    assert trailer.entry_count >= trailer.file_count
    assert trailer.logical_bytes > 0
    assert sum(1 for _ in iter_physical_delta_index(tmp_path / "index" / manifest.index_path)) == (
        trailer.entry_count
    )
    checkpoint = json.loads(
        (tmp_path / "index" / "physical-delta-checkpoint.json").read_text(encoding="utf-8")
    )
    assert checkpoint["phase"] == "sealed"
    assert checkpoint["entry_count"] == trailer.entry_count
    entries = (tmp_path / "index" / manifest.index_path).read_text(encoding="utf-8").splitlines()
    assert any('"path":"state/empty.bin"' in line for line in entries)
    assert any('"parent_object_key":"volumes/' in line for line in entries)


def test_physical_delta_index_resumes_an_exact_partial_prefix(tmp_path: Path) -> None:
    backup, root = _backup(tmp_path)
    destination = tmp_path / "index"
    destination.mkdir()
    inspected_root, packed = backup.inspect_packed_for_restore(root)
    first = next(_iter_entries(inspected_root, packed))
    partial = destination / ".physical-delta-index.jsonl.fixture.tmp"
    partial.write_text(
        json.dumps(first.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    manifest = _builder(backup).build(root, destination)

    assert verify_physical_delta_index(destination / manifest.index_path).entry_count > 1
    assert not partial.exists()


def test_physical_delta_index_rejects_missing_parent_volume(tmp_path: Path) -> None:
    backup, root = _backup(tmp_path)
    _, packed = backup.inspect_packed_for_restore(root)
    journal = (root / "packed-backup-volumes.jsonl").read_text(encoding="utf-8").splitlines()
    first_volume = json.loads(journal[0])["path"]
    (root / first_volume).unlink()

    with pytest.raises(WorkspaceV2PhysicalDeltaError, match="absent or corrupt"):
        _builder(backup).build(root, tmp_path / "index")


def test_physical_delta_index_rejects_corrupt_sealed_index(tmp_path: Path) -> None:
    backup, root = _backup(tmp_path)
    destination = tmp_path / "index"
    manifest = _builder(backup).build(root, destination)
    index = destination / manifest.index_path
    content = index.read_text(encoding="utf-8")
    index.write_text(content.replace("parent_object_key", "parent_object_kez", 1), encoding="utf-8")

    with pytest.raises(WorkspaceV2PhysicalDeltaError, match="malformed|bind"):
        verify_physical_delta_index(index)


def _physical_fixture(tmp_path: Path) -> tuple[WorkspaceV2PhysicalDeltaService, Path, Path, Path]:
    backup, backup_root = _backup(tmp_path)
    _builder(backup).build(backup_root, tmp_path / "index")
    parent = tmp_path / "parent"
    backup.restore_packed(backup_root, parent, available_bytes=lambda: 10_000_000_000)
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    return (
        WorkspaceV2PhysicalDeltaService(service),
        tmp_path / "source",
        parent,
        tmp_path / "index" / "physical-delta-index.jsonl",
    )


@pytest.mark.parametrize("fault", [None, "missing", "extra", "orphan", "checksum"])
def test_full_raw_physical_coverage_uses_index_membership_without_payload_hydration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    from investment_analyst.storage.resource_policy import (
        DatabaseResourceAssignment,
        DuckDBResourcePolicy,
        MaintenanceExecution,
        MaintenanceResourceBudget,
        maintenance_execution,
        maintenance_resource_budget,
    )

    physical, source, _, index = _physical_fixture(tmp_path)
    workspace = WorkspaceService(environ={}, home=tmp_path / "home")
    moment = datetime(2026, 10, 17, tzinfo=UTC)
    record = RawRecord(
        record_id=uuid4(),
        source=SourceReference(
            source_id="fixture:raw", record_key="membership", retrieved_at=moment
        ),
        available_at=moment,
        received_at=moment,
        payload={"exact": "1.00"},
        schema_version="fixture-v1",
    )
    with workspace.open_storage(
        workspace.resolve(source), WorkspaceAccessMode.READ_WRITE
    ) as storage:
        storage.raw_records.save(record)
        storage.store.connection.execute("CREATE TABLE _physical_raw_coverage(value VARCHAR)")
        storage.store.connection.execute("INSERT INTO _physical_raw_coverage VALUES ('preserve')")
    raw = next((source / "storage/v2/raw/raw/sha256").rglob("?" * 64))
    if fault == "orphan":
        data = b"unindexed preserved bytes"
        checksum = hashlib.sha256(data).hexdigest()
        orphan = raw.parents[2] / checksum[:2] / checksum[2:4] / checksum
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(data)
    output = tmp_path / "delta"
    manifest = physical.plan(source, index, output, parent_manifest_sha256="a" * 64, epoch=uuid4())
    if fault == "missing":
        raw.unlink()
    elif fault == "extra":
        (source / "state/unexpected.txt").write_text("unknown")
    original = Path.open

    def no_payload_open(path, *args, **kwargs):
        assert path != raw, "coverage must not hydrate raw payloads"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_payload_open)
    budget = MaintenanceResourceBudget(
        operation_id=uuid4(),
        profile="verification",
        assignments=(
            DatabaseResourceAssignment(
                database=source / "storage/v2/index.duckdb",
                policy=DuckDBResourcePolicy(
                    memory_limit_bytes=1_000_000_000,
                    threads=1,
                    spill_limit_bytes=1_000_000_000,
                    temp_directory=tmp_path / "coverage-temp",
                ),
            ),
        ),
    )
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(MaintenanceExecution(budget, lambda *_: None)),
        workspace.open_storage(workspace.resolve(source), WorkspaceAccessMode.READ_ONLY) as storage,
    ):
        connection = storage.store.connection
        if fault == "checksum":
            connection.execute("CREATE TEMP TABLE raw_v2_index AS SELECT * FROM main.raw_v2_index")
            connection.execute("UPDATE raw_v2_index SET checksum_sha256=?", ["0" * 64])
        if fault is not None:
            with pytest.raises(WorkspaceV2PhysicalDeltaError, match="coverage|membership"):
                delta_module._verify_full_recovery_coverage(
                    source, output / "files.jsonl", manifest, connection
                )
            assert connection.execute("SELECT * FROM main._physical_raw_coverage").fetchall() == [
                ("preserve",)
            ]
            return
        coverage = delta_module._verify_full_recovery_coverage(
            source, output / "files.jsonl", manifest, connection
        )
        assert coverage["raw_indexed"] == coverage["raw_files"] == 1
        assert coverage["physical_files"] == manifest.file_count
        assert coverage["physical_bytes"] == manifest.logical_bytes
        assert len(coverage["raw_corpus_digest"]) == 64
        assert connection.execute("SELECT * FROM main._physical_raw_coverage").fetchall() == [
            ("preserve",)
        ]


class _PhysicalTransport:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.uploads: list[str] = []
        self.fetches: list[str] = []
        self.fail_at: int | None = None
        self.batches: list[tuple[str, tuple[str, ...]]] = []

    def copy_immutable_object(self, source: Path, key: str) -> tuple[object, ...]:
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != source.read_bytes():
                raise RuntimeError("immutable conflict")
        else:
            shutil.copyfile(source, target)
        self.uploads.append(key)
        return ()

    def fetch_sealed_object(self, key: str, destination: Path) -> tuple[object, ...]:
        if self.fail_at is not None and len(self.fetches) == self.fail_at:
            raise RuntimeError("interrupted remote read")
        shutil.copyfile(self.root / key, destination)
        self.fetches.append(key)
        return ()

    def copy_immutable_batch(self, root: Path, keys: tuple[str, ...]) -> tuple[object, ...]:
        assert 1 <= len(keys) <= 32
        self.batches.append(("upload", keys))
        for key in keys:
            self.copy_immutable_object(root / key, key)
        return ()

    def fetch_sealed_batch(self, keys: tuple[str, ...], root: Path) -> tuple[object, ...]:
        assert not root.exists()
        self.batches.append(("download", keys))
        for key in keys:
            destination = root / key
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.fetch_sealed_object(key, destination)
        return ()


@pytest.mark.parametrize("fault", [None, "missing", "corrupt", "missing_proof", "namespace"])
def test_independent_reads_restore_without_upload_blocks_and_repair_only_from_drive(
    tmp_path: Path,
    fault: str | None,
) -> None:
    service, source, parent, index = _physical_fixture(tmp_path)
    (source / "state/new.bin").write_bytes(b"independent new data")
    output, cache = tmp_path / "delta", tmp_path / "reads"
    service.plan(source, index, output, parent_manifest_sha256="a" * 64, epoch=uuid4())
    transport = _PhysicalTransport(tmp_path / "remote")
    options = {"independent_cache_root": cache, "configuration_sha256": "b" * 64}
    service.upload(
        output,
        transport,
        checkpoint_path=tmp_path / "upload.json",
        receipt_path=tmp_path / "uploaded.json",
        **options,
    )
    payload = next((cache / "physical-delta/objects").glob("*.bin"))
    local = output / payload.relative_to(cache)
    assert payload.stat().st_ino != local.stat().st_ino
    assert payload.stat().st_ino != (transport.root / payload.relative_to(cache)).stat().st_ino
    for path in (output / "physical-delta/objects").glob("*"):
        path.unlink()
    if fault == "missing":
        payload.unlink()
    elif fault == "corrupt":
        payload.write_bytes(b"corrupt retained data")
    elif fault == "missing_proof":
        payload.with_name(payload.name + ".read.json").unlink()
    elif fault == "namespace":
        options["configuration_sha256"] = "c" * 64
    before = len(transport.fetches)
    arguments = dict(
        parent_index=index,
        parent_root=parent,
        parent_manifest_sha256="a" * 64,
        destination=tmp_path / "restored",
        transport=transport,
        checkpoint_path=tmp_path / "restore.json",
        receipt_path=tmp_path / "restored.json",
        **options,
    )
    if fault == "namespace":
        with pytest.raises(WorkspaceV2PhysicalDeltaError, match="namespace"):
            service.restore(output, **arguments)
        assert len(transport.fetches) == before
        assert not (tmp_path / "restored").exists()
        return
    receipt = service.restore(output, **arguments)
    assert receipt.kind == "restore"
    assert len(transport.fetches) - before == (0 if fault is None else 1)
    assert (tmp_path / "restored/state/new.bin").read_bytes() == b"independent new data"
    assert service.restore(output, **arguments) == receipt


@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_invalid_independent_read_never_falls_back_to_upload_staging(
    tmp_path: Path,
    fault: str,
) -> None:
    service, source, parent, index = _physical_fixture(tmp_path)
    (source / "state/new.bin").write_bytes(b"new data")
    output, cache = tmp_path / "delta", tmp_path / "reads"
    service.plan(source, index, output, parent_manifest_sha256="a" * 64, epoch=uuid4())
    transport = _PhysicalTransport(tmp_path / "remote")
    options = {"independent_cache_root": cache, "configuration_sha256": "b" * 64}
    service.upload(
        output,
        transport,
        checkpoint_path=tmp_path / "upload.json",
        receipt_path=tmp_path / "uploaded.json",
        **options,
    )
    payload = next((cache / "physical-delta/objects").glob("*.bin"))
    remote = transport.root / payload.relative_to(cache)
    assert (output / payload.relative_to(cache)).is_file()
    payload.write_bytes(b"corrupt cache")
    if fault == "missing":
        remote.unlink()
    else:
        remote.write_bytes(b"corrupt remote")
    with pytest.raises((FileNotFoundError, WorkspaceV2PhysicalDeltaError)):
        service.restore(
            output,
            parent_index=index,
            parent_root=parent,
            parent_manifest_sha256="a" * 64,
            destination=tmp_path / "restored",
            transport=transport,
            checkpoint_path=tmp_path / "restore.json",
            receipt_path=tmp_path / "restored.json",
            **options,
        )
    assert not (tmp_path / "restored.json").exists()


def test_independent_read_upload_resumes_after_interruption(tmp_path: Path) -> None:
    service, source, parent, index = _physical_fixture(tmp_path)
    (source / "state/one.bin").write_bytes(b"one")
    (source / "state/two.bin").write_bytes(b"two")
    output, cache = tmp_path / "delta", tmp_path / "reads"
    service.plan(source, index, output, parent_manifest_sha256="a" * 64, epoch=uuid4())
    transport = _PhysicalTransport(tmp_path / "remote")
    options = {"independent_cache_root": cache, "configuration_sha256": "b" * 64}
    transport.fail_at = 1
    with pytest.raises(RuntimeError, match="interrupted"):
        service.upload(
            output,
            transport,
            checkpoint_path=tmp_path / "upload.json",
            receipt_path=tmp_path / "uploaded.json",
            **options,
        )
    first = transport.fetches[0]
    assert (cache / first).is_file()
    transport.fail_at = None
    service.upload(
        output,
        transport,
        checkpoint_path=tmp_path / "upload.json",
        receipt_path=tmp_path / "uploaded.json",
        **options,
    )
    assert transport.fetches.count(first) == 1
    before = len(transport.fetches)
    for path in (output / "physical-delta/objects").glob("*"):
        path.unlink()
    service.restore(
        output,
        parent_index=index,
        parent_root=parent,
        parent_manifest_sha256="a" * 64,
        destination=tmp_path / "restored",
        transport=transport,
        checkpoint_path=tmp_path / "restore.json",
        receipt_path=tmp_path / "restored.json",
        **options,
    )
    assert len(transport.fetches) == before


def test_batch_transfer_reuses_verified_single_reads_and_recovers_without_staging(
    tmp_path: Path,
) -> None:
    service, source, parent, index = _physical_fixture(tmp_path)
    for number in range(35):
        (source / f"state/new-{number:03}.bin").write_bytes(str(number).encode())
    output, cache = tmp_path / "delta", tmp_path / "reads"
    service.plan(source, index, output, parent_manifest_sha256="a" * 64, epoch=uuid4())
    transport = _PhysicalTransport(tmp_path / "remote")
    options = {"independent_cache_root": cache, "configuration_sha256": "b" * 64}
    transport.fail_at = 1
    with pytest.raises(RuntimeError, match="interrupted"):
        service.upload(
            output,
            transport,
            checkpoint_path=tmp_path / "upload.json",
            receipt_path=tmp_path / "uploaded.json",
            **options,
        )
    verified = transport.fetches[0]
    transport.fail_at = 2
    with pytest.raises(RuntimeError, match="interrupted"):
        service.upload(
            output,
            transport,
            checkpoint_path=tmp_path / "upload.json",
            receipt_path=tmp_path / "uploaded.json",
            batch_transfer=True,
            **options,
        )
    assert not (tmp_path / "uploaded.json").exists()
    transport.fail_at = None
    receipt = service.upload(
        output,
        transport,
        checkpoint_path=tmp_path / "upload.json",
        receipt_path=tmp_path / "uploaded.json",
        batch_transfer=True,
        **options,
    )
    assert receipt.kind == "upload"
    assert transport.uploads.count(verified) == transport.fetches.count(verified) == 1
    assert max(len(keys) for _, keys in transport.batches) == 32
    assert any(path.is_dir() for path in cache.glob(".drive-batch-*"))
    before = len(transport.fetches)
    for path in (output / "physical-delta/objects").glob("*"):
        path.unlink()
    service.restore(
        output,
        parent_index=index,
        parent_root=parent,
        parent_manifest_sha256="a" * 64,
        destination=tmp_path / "restored",
        transport=transport,
        checkpoint_path=tmp_path / "restore.json",
        receipt_path=tmp_path / "restored.json",
        **options,
    )
    assert len(transport.fetches) == before


@pytest.mark.parametrize("changes", [0, 1, 3])
def test_physical_photo_reuses_parent_and_restores_only_remote_new_bytes(
    tmp_path: Path, changes: int
) -> None:
    service, source, parent, index = _physical_fixture(tmp_path)
    original_parent = (parent / "state/payload.bin").read_bytes()
    if changes >= 1:
        data = bytearray((source / "state/payload.bin").read_bytes())
        data[4_010] ^= 1
        (source / "state/payload.bin").write_bytes(data)
    if changes == 3:
        (source / "state/new.bin").write_bytes(b"new-file")
        (source / "state/payload.bin").write_bytes(
            (source / "state/payload.bin").read_bytes()[:-13]
        )
        (source / "state/empty.bin").unlink()
    output = tmp_path / "delta"
    epoch = uuid4()
    manifest = service.plan(source, index, output, parent_manifest_sha256="a" * 64, epoch=epoch)
    assert (
        service.plan(source, index, output, parent_manifest_sha256="a" * 64, epoch=epoch)
        == manifest
    )
    if changes == 0:
        assert manifest.new_bytes == 0
        assert not (output / "physical-delta/objects").exists()
    elif changes == 1:
        assert manifest.new_bytes == 4_000
    assert (parent / "state/payload.bin").read_bytes() == original_parent
    transport = _PhysicalTransport(tmp_path / "remote")
    transfer = service.upload(
        output,
        transport,
        checkpoint_path=tmp_path / "upload.json",
        receipt_path=tmp_path / "uploaded.json",
    )  # type: ignore[arg-type]
    assert transfer.epoch == epoch
    if changes:
        for path in (output / "physical-delta/objects").glob("*"):
            path.unlink()  # Recovery must never use local upload payload.
    restored = tmp_path / "restored"
    receipt = service.restore(
        output,
        parent_index=index,
        parent_root=parent,
        parent_manifest_sha256="a" * 64,
        destination=restored,
        transport=transport,
        checkpoint_path=tmp_path / "restore.json",
        receipt_path=tmp_path / "restored.json",
    )  # type: ignore[arg-type]
    assert receipt.epoch == epoch
    for item in _iter_delta_files(output / "files.jsonl"):
        if item.deleted:
            assert not (restored / item.path).exists()
        else:
            assert hashlib.sha256((restored / item.path).read_bytes()).hexdigest() == item.sha256
    WorkspaceService(environ={}, home=tmp_path / "home").inspect(restored)


def test_physical_photo_rejects_changed_epoch_source_and_symlink(tmp_path: Path) -> None:
    service, source, _, index = _physical_fixture(tmp_path)
    output = tmp_path / "delta"
    epoch = uuid4()
    service.plan(source, index, output, parent_manifest_sha256="a" * 64, epoch=epoch)
    with pytest.raises(WorkspaceV2PhysicalDeltaError, match="epoch binding"):
        service.plan(source, index, output, parent_manifest_sha256="a" * 64, epoch=uuid4())
    (source / "state/payload.bin").write_bytes(b"changed")
    with pytest.raises(WorkspaceV2PhysicalDeltaError, match="source or cursor"):
        service.plan(source, index, output, parent_manifest_sha256="a" * 64, epoch=epoch)
    (source / "state/link").symlink_to(tmp_path)
    with pytest.raises(WorkspaceV2PhysicalDeltaError, match="unsafe"):
        service.plan(
            source, index, tmp_path / "delta-two", parent_manifest_sha256="a" * 64, epoch=uuid4()
        )


def test_recovery_reuses_only_immutable_parent_inodes_and_retires_safely(tmp_path: Path) -> None:
    from investment_analyst.workspace.workspace_v2_lifecycle import WorkspaceV2LifecycleService

    workspace = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    workspace.initialize(source, format_version=2)
    moment = datetime(2026, 10, 15, tzinfo=UTC)
    raw = RawRecord(
        record_id=uuid4(),
        asset_id="equity:us:aapl",
        source=SourceReference(source_id="fixture:raw", record_key="one", retrieved_at=moment),
        available_at=moment,
        received_at=moment,
        payload={"value": "1.00"},
        schema_version="fixture-v1",
    )
    with workspace.open_storage(
        workspace.resolve(source), WorkspaceAccessMode.READ_WRITE
    ) as storage:
        storage.raw_records.save(raw)
    backup = WorkspaceV2BackupService(workspace)
    packed = tmp_path / "packed"
    backup.create_packed(
        source,
        packed,
        volume_bytes=32_000,
        fragment_bytes=4_000,
        available_bytes=lambda: 10_000_000_000,
    )
    index_manifest = WorkspaceV2PhysicalDeltaIndexService(backup).build(packed, tmp_path / "index")
    index = tmp_path / "index" / index_manifest.index_path
    new_raw = raw.model_copy(
        update={
            "record_id": uuid4(),
            "source": raw.source.model_copy(update={"record_key": "two"}),
            "payload": {"value": "2.00"},
        }
    )
    with workspace.open_storage(
        workspace.resolve(source), WorkspaceAccessMode.READ_WRITE
    ) as storage:
        storage.raw_records.save(new_raw)
    (source / "state/new.json").write_bytes(b'{"new":true}')
    physical = WorkspaceV2PhysicalDeltaService(workspace)
    output = tmp_path / "delta"
    epoch = uuid4()
    physical.plan(source, index, output, parent_manifest_sha256="a" * 64, epoch=epoch)
    transport = _PhysicalTransport(tmp_path / "remote")
    physical.upload(
        output,
        transport,
        checkpoint_path=tmp_path / "upload.json",
        receipt_path=tmp_path / "uploaded.json",
    )  # type: ignore[arg-type]
    for path in (output / "physical-delta/objects").glob("*"):
        path.unlink()
    recovered = tmp_path / "recovered"
    options = {
        "parent_index": index,
        "parent_root": source,
        "parent_manifest_sha256": "a" * 64,
        "destination": recovered,
        "transport": transport,
        "checkpoint_path": tmp_path / "restore.json",
        "receipt_path": tmp_path / "restored.json",
        "reuse_parent_immutables": True,
    }
    receipt = physical.restore(output, **options)
    assert receipt.parent_linked_files == 1
    assert receipt.parent_linked_bytes > 0
    blobs = tuple((source / "storage/v2/raw/raw/sha256").rglob("?" * 64))
    blob = next(
        path
        for path in blobs
        if (recovered / path.relative_to(source)).stat().st_ino == path.stat().st_ino
    )
    new_blob = next(path for path in blobs if path != blob)
    assert (recovered / new_blob.relative_to(source)).stat().st_ino != new_blob.stat().st_ino
    relative = blob.relative_to(source)
    assert (recovered / relative).stat().st_ino == blob.stat().st_ino
    database = Path("storage/v2/index.duckdb")
    assert (recovered / database).stat().st_ino != (source / database).stat().st_ino
    assert (recovered / "state/new.json").stat().st_ino != (source / "state/new.json").stat().st_ino
    assert physical.restore(output, **options) == receipt
    with workspace.open_storage(
        workspace.resolve(recovered), WorkspaceAccessMode.READ_ONLY
    ) as storage:
        assert storage.raw_records.get(raw.record_id) == raw
        assert storage.raw_records.get(new_raw.record_id) == new_raw
    retired = WorkspaceV2LifecycleService(workspace).retire_delta_recovery_slot(
        receipt,
        delta_root=output,
        operation_id=epoch,
        checkpoint_path=tmp_path / "retirement.json",
        proof_path=tmp_path / "proof.json",
    )
    assert retired.phase.value == "retired"
    assert not recovered.exists()
    assert blob.exists() and blob.stat().st_nlink == 1
    with workspace.open_storage(
        workspace.resolve(source), WorkspaceAccessMode.READ_ONLY
    ) as storage:
        assert storage.raw_records.get(raw.record_id) == raw


def test_physical_transfer_resumes_confirmed_files_and_rejects_corrupt_remote(
    tmp_path: Path,
) -> None:
    service, source, parent, index = _physical_fixture(tmp_path)
    (source / "state/new.bin").write_bytes(b"new")
    (source / "state/payload.bin").write_bytes(b"changed")
    output = tmp_path / "delta"
    service.plan(source, index, output, parent_manifest_sha256="a" * 64, epoch=uuid4())
    transport = _PhysicalTransport(tmp_path / "remote")
    transport.fail_at = 1
    with pytest.raises(RuntimeError, match="interrupted"):
        service.upload(
            output,
            transport,
            checkpoint_path=tmp_path / "upload.json",
            receipt_path=tmp_path / "uploaded.json",
        )  # type: ignore[arg-type]
    prior = list(transport.uploads)
    transport.fail_at = None
    service.upload(
        output,
        transport,
        checkpoint_path=tmp_path / "upload.json",
        receipt_path=tmp_path / "uploaded.json",
    )  # type: ignore[arg-type]
    assert transport.uploads.count(prior[0]) == 1
    payload = next((tmp_path / "remote/physical-delta/objects").glob("*"))
    payload.write_bytes(b"bad")
    with pytest.raises(WorkspaceV2PhysicalDeltaError, match="absent or corrupt"):
        service.restore(
            output,
            parent_index=index,
            parent_root=parent,
            parent_manifest_sha256="a" * 64,
            destination=tmp_path / "restored",
            transport=transport,
            checkpoint_path=tmp_path / "restore.json",
            receipt_path=tmp_path / "restored.json",
        )  # type: ignore[arg-type]


@pytest.mark.parametrize("after_commit", [False, True])
def test_physical_plan_recovers_only_unconfirmed_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_commit: bool
) -> None:
    service, source, _, index = _physical_fixture(tmp_path)
    output = tmp_path / "delta"
    epoch = uuid4()
    original = delta_module._commit_plan_cursor

    def interrupted(*args: object) -> None:
        if after_commit:
            original(*args)
        raise RuntimeError("interrupted commit boundary")

    monkeypatch.setattr(delta_module, "_commit_plan_cursor", interrupted)
    with pytest.raises(RuntimeError, match="commit boundary"):
        service.plan(source, index, output, parent_manifest_sha256="a" * 64, epoch=epoch)
    partial = output / "files.partial.jsonl"
    with partial.open("ab") as stream:
        stream.write(b'{"torn":')
    monkeypatch.setattr(delta_module, "_commit_plan_cursor", original)
    manifest = service.plan(source, index, output, parent_manifest_sha256="a" * 64, epoch=epoch)
    assert manifest.new_bytes == 0
    assert not partial.exists()


def test_physical_recovery_reopens_changed_database_with_exact_decimal_and_lineage(
    tmp_path: Path,
) -> None:
    service, source, parent, index = _physical_fixture(tmp_path)
    workspace = WorkspaceService(environ={}, home=tmp_path / "home")
    moment = datetime(2026, 10, 15, tzinfo=UTC)
    reference = SourceReference(
        source_id="fixture:physical-delta", record_key="changed-db", retrieved_at=moment
    )
    raw = RawRecord(
        record_id=uuid4(),
        asset_id="equity:us:aapl",
        source=reference,
        event_time=moment,
        available_at=moment,
        received_at=moment,
        payload={"close": "210.5000000001"},
        schema_version="fixture-v1",
    )
    observation = NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=raw.record_id,
        asset_id=raw.asset_id,
        field_name="close",
        value=Decimal("210.5000000001"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=moment,
        available_at=moment,
        normalized_at=moment,
        source=reference,
        quality=DataQuality.VALID,
        transformation_version="fixture-v1",
    )
    with workspace.open_storage(
        workspace.resolve(source), WorkspaceAccessMode.READ_WRITE
    ) as storage:
        storage.raw_records.save(raw)
        storage.observations.save(observation)
    output = tmp_path / "delta"
    manifest = service.plan(source, index, output, parent_manifest_sha256="a" * 64, epoch=uuid4())
    assert manifest.new_bytes > 0
    transport = _PhysicalTransport(tmp_path / "remote")
    service.upload(
        output,
        transport,
        checkpoint_path=tmp_path / "upload.json",
        receipt_path=tmp_path / "uploaded.json",
    )  # type: ignore[arg-type]
    destination = tmp_path / "restored"
    service.restore(
        output,
        parent_index=index,
        parent_root=parent,
        parent_manifest_sha256="a" * 64,
        destination=destination,
        transport=transport,
        checkpoint_path=tmp_path / "restore.json",
        receipt_path=tmp_path / "restored.json",
    )  # type: ignore[arg-type]
    with workspace.open_storage(
        workspace.resolve(destination), WorkspaceAccessMode.READ_ONLY
    ) as recovered:
        assert recovered.raw_records.get(raw.record_id) == raw
        assert (
            recovered.observations.get_many((observation.observation_id,))[
                observation.observation_id
            ]
            == observation
        )
