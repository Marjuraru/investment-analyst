from __future__ import annotations

import hashlib
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.workspace.storage_retirement import (
    RetentionNotAdmitted,
    StorageRetirementError,
    StorageRetirementService,
)
from investment_analyst.workspace.storage_retirement_models import (
    OperationalRetentionPlan,
    RetentionCheckpoint,
    RetentionClass,
    RetentionDecision,
    RetentionEntryKind,
    RetentionPlanRequest,
    RetentionRequestEntry,
)

_NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
_EVIDENCE = ("rclone:ia_drive_crypt:v2-readiness-20261026/control-1.json",)


def _service(root: Path, **overrides: object) -> StorageRetirementService:
    values = {
        "allowed_root": root,
        "clock": lambda: _NOW,
        "host_free_bytes": lambda: 20_000_000_000,
        "wsl_free_bytes": lambda: 20_000_000_000,
        "deployment_state_loader": lambda: SimpleNamespace(
            current=None,
            previous=None,
            current_release_path=None,
            previous_release_path=None,
        ),
        "workspace_bindings_loader": lambda: SimpleNamespace(current=None, previous=None),
        "systemctl_properties_loader": lambda: {},
        "systemd_unit_path": root / "unit.service",
        "runtime_root": root / "runtime",
    }
    values.update(overrides)
    return StorageRetirementService(**values)  # type: ignore[arg-type]


def _entry(
    path: str,
    *,
    kind: RetentionEntryKind,
    decision: RetentionDecision,
    evidence: tuple[str, ...] = (),
    dependencies: tuple[str, ...] = (),
    retire_after: str | None = None,
) -> RetentionRequestEntry:
    return RetentionRequestEntry(
        path=path,
        kind=kind,
        retention_class=RetentionClass.RESTORE_STAGING,
        decision=decision,
        reason="test disposition",
        last_consumer="no consumer",
        dependencies=dependencies,
        remote_evidence=evidence,
        retire_after=retire_after,
    )


def _request(root: Path, entries: list[RetentionRequestEntry]) -> RetentionPlanRequest:
    return RetentionPlanRequest(
        operation_id=UUID("11111111-1111-1111-1111-111111111111"),
        root=root,
        entries=tuple(sorted(entries, key=lambda item: item.path)),
    )


def _seed(root: Path) -> None:
    (root / "ops" / "stage").mkdir(parents=True)
    (root / "ops" / "stage" / "payload.bin").write_bytes(b"a" * 2048)
    (root / "ops" / "stage" / "control.json").write_text('{"ok":true}', encoding="utf-8")
    (root / "ops" / "keep").mkdir(parents=True)
    (root / "ops" / "keep" / "state.json").write_text('{"keep":true}', encoding="utf-8")


def test_plan_measures_exact_bytes_and_seals_manifest(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed(root)
    service = _service(root)
    plan = service.plan(
        _request(
            root,
            [
                _entry(
                    "ops/stage",
                    kind=RetentionEntryKind.DIRECTORY,
                    decision=RetentionDecision.REMOTE_ONLY,
                    evidence=_EVIDENCE,
                ),
                _entry(
                    "ops/keep",
                    kind=RetentionEntryKind.DIRECTORY,
                    decision=RetentionDecision.KEEP_LOCAL,
                ),
            ],
        )
    )
    stage = next(item for item in plan.entries if item.path == "ops/stage")
    keep = next(item for item in plan.entries if item.path == "ops/keep")
    assert stage.file_count == 2
    assert stage.size_bytes == 2048 + len('{"ok":true}')
    assert stage.remote_evidence == _EVIDENCE
    assert keep.decision is RetentionDecision.KEEP_LOCAL
    assert plan.retire_bytes == stage.size_bytes
    assert plan.retain_bytes == keep.size_bytes
    assert len(plan.manifest_sha256) == 64
    assert service.plan_sha256(plan) != plan.manifest_sha256


def test_plan_rejects_unsafe_path_and_symlink_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed(root)
    service = _service(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret", encoding="utf-8")
    (root / "ops" / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorageRetirementError, match="symbolic link|escapes|unsafe"):
        service.plan(
            _request(
                root,
                [
                    _entry(
                        "ops/link",
                        kind=RetentionEntryKind.DIRECTORY,
                        decision=RetentionDecision.REMOTE_ONLY,
                        evidence=_EVIDENCE,
                    )
                ],
            )
        )
    with pytest.raises(ValidationError, match="normalized and relative"):
        _entry(
            "../escape",
            kind=RetentionEntryKind.DIRECTORY,
            decision=RetentionDecision.KEEP_LOCAL,
        )


def test_plan_refuses_active_workspace_binding(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed(root)
    active = root / "workspaces" / "active"
    active.mkdir(parents=True)
    service = _service(
        root,
        workspace_bindings_loader=lambda: SimpleNamespace(
            current=SimpleNamespace(
                workspace_root=str(active),
                release_sha="a" * 40,
            ),
            previous=None,
        ),
    )
    with pytest.raises(StorageRetirementError, match="active reference"):
        service.plan(
            _request(
                root,
                [
                    _entry(
                        "workspaces/active",
                        kind=RetentionEntryKind.DIRECTORY,
                        decision=RetentionDecision.REMOTE_ONLY,
                        evidence=_EVIDENCE,
                    )
                ],
            )
        )


def test_verify_detects_content_replacement(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed(root)
    service = _service(root)
    plan = service.plan(
        _request(
            root,
            [
                _entry(
                    "ops/stage",
                    kind=RetentionEntryKind.DIRECTORY,
                    decision=RetentionDecision.REMOTE_ONLY,
                    evidence=_EVIDENCE,
                )
            ],
        )
    )
    plan_sha256 = service.plan_sha256(plan)
    service.verify(plan, plan_sha256=plan_sha256)
    (root / "ops" / "stage" / "control.json").write_text('{"ok":false}', encoding="utf-8")
    with pytest.raises(StorageRetirementError, match="drifted"):
        service.verify(plan, plan_sha256=plan_sha256)


def test_retire_is_idempotent_and_writes_terminal_evidence(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed(root)
    service = _service(root)
    plan = service.plan(
        _request(
            root,
            [
                _entry(
                    "ops/stage",
                    kind=RetentionEntryKind.DIRECTORY,
                    decision=RetentionDecision.REMOTE_ONLY,
                    evidence=_EVIDENCE,
                ),
                _entry(
                    "ops/keep",
                    kind=RetentionEntryKind.DIRECTORY,
                    decision=RetentionDecision.KEEP_LOCAL,
                ),
            ],
        )
    )
    plan_sha256 = service.plan_sha256(plan)
    checkpoint = root / "controls" / "retention.checkpoint.json"
    receipt = root / "controls" / "retention.receipt.json"
    first = service.retire(
        plan,
        plan_sha256=plan_sha256,
        checkpoint_path=checkpoint,
        receipt_path=receipt,
    )
    assert not (root / "ops" / "stage").exists()
    assert (root / "ops" / "keep" / "state.json").is_file()
    assert first.retired_paths == ("ops/stage",)
    assert first.idempotent_replay is False
    assert first.remote_evidence == _EVIDENCE
    assert RetentionCheckpoint.model_validate_json(checkpoint.read_bytes()).state == "retired"
    second = service.retire(
        plan,
        plan_sha256=plan_sha256,
        checkpoint_path=checkpoint,
        receipt_path=receipt,
    )
    assert second.idempotent_replay is True
    assert second.retired_paths == first.retired_paths


def test_retire_resumes_after_detach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    _seed(root)
    service = _service(root)
    plan = service.plan(
        _request(
            root,
            [
                _entry(
                    "ops/stage",
                    kind=RetentionEntryKind.DIRECTORY,
                    decision=RetentionDecision.REMOTE_ONLY,
                    evidence=_EVIDENCE,
                )
            ],
        )
    )
    plan_sha256 = service.plan_sha256(plan)
    checkpoint = root / "controls" / "retention.checkpoint.json"
    receipt = root / "controls" / "retention.receipt.json"
    original_rmtree = shutil.rmtree
    calls = {"count": 0}

    def fail_once(path: str | os.PathLike[str], *args: object, **kwargs: object) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("simulated crash after detach")
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("investment_analyst.workspace.storage_retirement.shutil.rmtree", fail_once)
    with pytest.raises(StorageRetirementError, match="removal failed"):
        service.retire(
            plan,
            plan_sha256=plan_sha256,
            checkpoint_path=checkpoint,
            receipt_path=receipt,
        )
    assert not (root / "ops" / "stage").exists()
    assert not receipt.exists()
    assert RetentionCheckpoint.model_validate_json(checkpoint.read_bytes()).state == "retiring"
    monkeypatch.setattr(
        "investment_analyst.workspace.storage_retirement.shutil.rmtree", original_rmtree
    )
    resumed = service.retire(
        plan,
        plan_sha256=plan_sha256,
        checkpoint_path=checkpoint,
        receipt_path=receipt,
    )
    assert resumed.retired_paths == ("ops/stage",)
    assert not (root / "ops" / "stage").exists()
    assert receipt.is_file()


def test_retire_refuses_below_reserve_without_mutation(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed(root)
    service = _service(root, host_free_bytes=lambda: 4_000_000_000)
    plan = service.plan(
        _request(
            root,
            [
                _entry(
                    "ops/stage",
                    kind=RetentionEntryKind.DIRECTORY,
                    decision=RetentionDecision.REMOTE_ONLY,
                    evidence=_EVIDENCE,
                )
            ],
        )
    )
    with pytest.raises(RetentionNotAdmitted):
        service.retire(
            plan,
            plan_sha256=service.plan_sha256(plan),
            checkpoint_path=root / "controls" / "retention.checkpoint.json",
            receipt_path=root / "controls" / "retention.receipt.json",
        )
    assert (root / "ops" / "stage" / "payload.bin").is_file()


def test_hardlinked_file_is_not_counted_as_exclusive(tmp_path: Path) -> None:
    root = tmp_path / "root"
    links = root / "ops" / "links"
    links.mkdir(parents=True)
    (links / "payload.bin").write_bytes(b"a" * 2048)
    os.link(links / "payload.bin", links / "payload-link.bin")
    service = _service(root)
    plan = service.plan(
        _request(
            root,
            [
                _entry(
                    "ops/links",
                    kind=RetentionEntryKind.DIRECTORY,
                    decision=RetentionDecision.REMOTE_ONLY,
                    evidence=_EVIDENCE,
                )
            ],
        )
    )
    entry = plan.entries[0]
    assert entry.file_count == 2
    assert entry.size_bytes == 4096
    assert entry.allocated_bytes > 0
    assert entry.exclusive_bytes == 0


def test_plan_refuses_current_release_reference(tmp_path: Path) -> None:
    root = tmp_path / "root"
    release = root / "runtime" / "releases" / ("a" * 40)
    release.mkdir(parents=True)
    (release / "manifest.json").write_text('{"sha":"a"}', encoding="utf-8")
    service = _service(
        root,
        deployment_state_loader=lambda: SimpleNamespace(
            current="a" * 40,
            previous=None,
            current_release_path=str(release),
            previous_release_path=None,
        ),
    )
    with pytest.raises(StorageRetirementError, match="active reference|deployment"):
        service.plan(
            _request(
                root,
                [
                    _entry(
                        "runtime/releases/" + "a" * 40,
                        kind=RetentionEntryKind.DIRECTORY,
                        decision=RetentionDecision.REMOTE_ONLY,
                        evidence=_EVIDENCE,
                    )
                ],
            )
        )


class _FakeTransport:
    def __init__(self, root: Path, *, corrupt: bool = False) -> None:
        self.root = root
        self.corrupt = corrupt
        self.objects: dict[str, bytes] = {}

    def copy_sealed_object(self, source: Path, remote_object: str) -> tuple[object, ...]:
        self.objects[remote_object] = source.read_bytes()
        return ()

    def fetch_sealed_object(self, remote_object: str, destination: Path) -> tuple[object, ...]:
        payload = self.objects[remote_object]
        if self.corrupt:
            payload = payload + b"corrupt"
        destination.write_bytes(payload)
        return ()


def test_publish_verifies_readback_and_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed(root)
    service = _service(root)
    plan = service.plan(
        _request(
            root,
            [
                _entry(
                    "ops/stage",
                    kind=RetentionEntryKind.DIRECTORY,
                    decision=RetentionDecision.REMOTE_ONLY,
                    evidence=_EVIDENCE,
                )
            ],
        )
    )
    plan_sha256 = service.plan_sha256(plan)
    transport = _FakeTransport(tmp_path)
    receipt_path = root / "controls" / "remote.receipt.json"
    receipt = service.publish(
        plan,
        plan_sha256=plan_sha256,
        transport=transport,  # type: ignore[arg-type]
        remote_prefix="v2-readiness-20261026",
        receipt_path=receipt_path,
    )
    assert receipt.readback_verified is True
    assert len(receipt.artifacts) == 2
    for artifact in receipt.artifacts:
        payload = transport.objects[artifact.object_name]
        assert hashlib.sha256(payload).hexdigest() == artifact.sha256
    replay = service.publish(
        plan,
        plan_sha256=plan_sha256,
        transport=transport,  # type: ignore[arg-type]
        remote_prefix="v2-readiness-20261026",
        receipt_path=receipt_path,
    )
    assert replay == receipt


def test_publish_rejects_corrupt_readback(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed(root)
    service = _service(root)
    plan = service.plan(
        _request(
            root,
            [
                _entry(
                    "ops/stage",
                    kind=RetentionEntryKind.DIRECTORY,
                    decision=RetentionDecision.REMOTE_ONLY,
                    evidence=_EVIDENCE,
                )
            ],
        )
    )
    transport = _FakeTransport(tmp_path, corrupt=True)
    receipt_path = root / "controls" / "remote.receipt.json"
    with pytest.raises(StorageRetirementError, match="readback differs"):
        service.publish(
            plan,
            plan_sha256=service.plan_sha256(plan),
            transport=transport,  # type: ignore[arg-type]
            remote_prefix="v2-readiness-20261026",
            receipt_path=receipt_path,
        )
    assert not receipt_path.exists()


def test_plan_round_trips_through_json(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed(root)
    service = _service(root)
    plan = service.plan(
        _request(
            root,
            [
                _entry(
                    "ops/stage",
                    kind=RetentionEntryKind.DIRECTORY,
                    decision=RetentionDecision.REMOTE_ONLY,
                    evidence=_EVIDENCE,
                )
            ],
        )
    )
    restored = OperationalRetentionPlan.model_validate_json(plan.model_dump_json())
    assert restored == plan
    assert service.plan_sha256(restored) == service.plan_sha256(plan)
