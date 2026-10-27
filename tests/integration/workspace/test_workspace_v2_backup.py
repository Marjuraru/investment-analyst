"""Backup inventory and semantic verification of v2 acquisition evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from investment_analyst.core.models import ContractModel, SourceReference
from investment_analyst.storage.sec_provenance_evidence import SecProvenanceEvidenceStore
from investment_analyst.workspace.sec_provenance_models import (
    SecAcquisitionProvenance,
    SecProvenanceObservationBinding,
    observation_digest,
    payload_digest,
)
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_backup import (
    WorkspaceV2BackupError,
    validate_sec_provenance_evidence,
)
from investment_analyst.workspace.workspace_v2_backup_inventory import write_inventory

BASE = datetime(2026, 8, 1, 4, 6, 18, 471994, tzinfo=UTC)


class _Observation(ContractModel):
    value: str


def _artifact(workspace_id: UUID) -> SecAcquisitionProvenance:
    return SecAcquisitionProvenance(
        destination_workspace_id=workspace_id,
        source_workspace_id=UUID("19cf83b5-8df3-4663-9369-4b61f86eceea"),
        raw_record_id=uuid5(NAMESPACE_URL, "backup-evidence-raw"),
        asset_id="equity:us:aapl",
        source_id="sec-edgar:aapl:companyfacts",
        raw_schema_version="sec-edgar-companyfacts-snapshot-v1",
        payload_checksum_sha256="a" * 64,
        payload_digest_sha256=payload_digest({"document": {"facts": {}}}),
        original_raw_sha256="b" * 64,
        original_source=SourceReference(
            source_id="sec-edgar:aapl:companyfacts",
            record_key="CIK0000320193:company_facts:0123456789abcdef",
            retrieved_at=BASE,
            raw_uri="https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
            checksum_sha256="a" * 64,
        ),
        original_event_time=BASE,
        original_available_at=BASE,
        original_received_at=BASE,
        bindings=(
            SecProvenanceObservationBinding(
                observation_id=uuid5(NAMESPACE_URL, "backup-evidence-observation"),
                observation_sha256=observation_digest(_Observation(value="1")),
                submissions_record_id=uuid5(NAMESPACE_URL, "backup-evidence-submissions"),
                submissions_payload_checksum_sha256="c" * 64,
                normalized_at=BASE,
                transformation_version="sec-aapl-companyfacts-normalizer-v1",
            ),
        ),
    )


def _workspace(tmp_path: Path):
    service = WorkspaceService()
    root = service.initialize(tmp_path / "workspace", format_version=2).paths.root
    store = SecProvenanceEvidenceStore(
        service.resolve(root).root / "storage" / "v2", read_only=False
    )
    artifact = _artifact(service.manifest(root).workspace_id)
    store.publish(artifact)
    return service, root, store, artifact


def test_inventory_includes_evidence_and_validates_it(tmp_path: Path) -> None:
    _, root, store, artifact = _workspace(tmp_path)
    assert validate_sec_provenance_evidence(root) == (1, 1)
    inventory = write_inventory(root, tmp_path / "inventory.jsonl")
    assert inventory.file_count > 0
    entries = [
        json.loads(line)
        for line in (tmp_path / "inventory.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    paths = [entry["path"] for entry in entries if "path" in entry]
    relative = store.artifact_path(artifact.identity_digest).relative_to(root).as_posix()
    assert relative in paths
    assert any(path.startswith("storage/v2/sec-provenance/bindings/") for path in paths)


def test_corrupt_evidence_is_rejected(tmp_path: Path) -> None:
    _, root, store, artifact = _workspace(tmp_path)
    path = store.artifact_path(artifact.identity_digest)
    original = path.read_bytes()
    document = json.loads(original)
    document["artifact"]["original_raw_sha256"] = "d" * 64
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(WorkspaceV2BackupError, match="verification failed"):
        validate_sec_provenance_evidence(root)
    path.write_bytes(original)
    assert validate_sec_provenance_evidence(root) == (1, 1)


def test_orphan_or_missing_binding_is_rejected(tmp_path: Path) -> None:
    _, root, store, artifact = _workspace(tmp_path)
    binding = store.binding_path(artifact.bindings[0].observation_id)
    payload = binding.read_bytes()
    binding.unlink()
    with pytest.raises(WorkspaceV2BackupError, match="incomplete"):
        validate_sec_provenance_evidence(root)
    binding.write_bytes(payload)
    assert validate_sec_provenance_evidence(root) == (1, 1)
    store.artifact_path(artifact.identity_digest).unlink()
    with pytest.raises(WorkspaceV2BackupError):
        validate_sec_provenance_evidence(root)


def test_workspace_without_evidence_is_valid(tmp_path: Path) -> None:
    service = WorkspaceService()
    root = service.initialize(tmp_path / "workspace", format_version=2).paths.root
    assert validate_sec_provenance_evidence(root) == (0, 0)
