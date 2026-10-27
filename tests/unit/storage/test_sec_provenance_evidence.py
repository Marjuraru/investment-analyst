"""Unit coverage for append-only acquisition provenance evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from investment_analyst.core.models import ContractModel, SourceReference
from investment_analyst.storage.sec_provenance_evidence import SecProvenanceEvidenceStore
from investment_analyst.workspace.sec_provenance_models import (
    SecAcquisitionProvenance,
    SecProvenanceError,
    SecProvenanceObservationBinding,
    SecProvenanceUnavailableError,
    observation_digest,
    payload_digest,
)

BASE = datetime(2026, 8, 1, 4, 6, 18, 471994, tzinfo=UTC)
LATER = datetime(2026, 10, 21, 20, 49, 52, 509067, tzinfo=UTC)
WORKSPACE = UUID("70c64668-1738-4dfe-ba1f-7e72026bb7fe")


class _Observation(ContractModel):
    value: str


SOURCE_WORKSPACE = UUID("19cf83b5-8df3-4663-9369-4b61f86eceea")


def _artifact() -> SecAcquisitionProvenance:
    raw_id = uuid5(NAMESPACE_URL, "sec-provenance-evidence-raw")
    return SecAcquisitionProvenance(
        destination_workspace_id=WORKSPACE,
        source_workspace_id=SOURCE_WORKSPACE,
        raw_record_id=raw_id,
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
                observation_id=uuid5(NAMESPACE_URL, "sec-provenance-evidence-observation"),
                observation_sha256=observation_digest(_Observation(value="1")),
                submissions_record_id=uuid5(NAMESPACE_URL, "sec-provenance-evidence-submissions"),
                submissions_payload_checksum_sha256="c" * 64,
                normalized_at=BASE,
                transformation_version="sec-aapl-companyfacts-normalizer-v1",
            ),
        ),
    )


def _store(tmp_path: Path, *, read_only: bool = False) -> SecProvenanceEvidenceStore:
    return SecProvenanceEvidenceStore(tmp_path / "v2", read_only=read_only)


def test_publish_is_atomic_hash_addressed_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = _artifact()
    publication = store.publish(artifact)
    assert publication.artifact_created is True
    assert publication.bindings_created == 1
    assert publication.bytes_written > 0
    path = store.artifact_path(artifact.identity_digest)
    assert path.is_file()
    assert path.parent.parent.name == artifact.identity_digest[:2]
    assert path.parent.name == artifact.identity_digest[2:4]
    loaded = store.acquisition(artifact.identity_digest)
    assert loaded == artifact
    assert loaded.identity_digest == artifact.identity_digest
    binding = store.binding(artifact.bindings[0].observation_id)
    assert binding is not None
    assert binding.identity_digest == artifact.identity_digest
    assert store.for_observation(artifact.bindings[0].observation_id) == artifact

    replay = store.publish(artifact)
    assert replay.artifact_created is False
    assert replay.bindings_created == 0
    assert replay.bytes_written == 0
    assert [item for item in store.artifacts()] == [artifact]


def test_read_only_never_creates_files_and_rejects_publication(tmp_path: Path) -> None:
    store = _store(tmp_path, read_only=True)
    with pytest.raises(SecProvenanceError, match="read-only"):
        store.publish(_artifact())
    assert not (tmp_path / "v2" / "sec-provenance").exists()
    assert list(store.artifacts()) == []
    assert store.binding(uuid4()) is None


def test_corrupt_or_unbound_evidence_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = _artifact()
    store.publish(artifact)
    path = store.artifact_path(artifact.identity_digest)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["artifact"]["original_raw_sha256"] = "d" * 64
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SecProvenanceUnavailableError, match="digest does not match"):
        store.acquisition(artifact.identity_digest)
    with pytest.raises(SecProvenanceUnavailableError):
        store.for_observation(artifact.bindings[0].observation_id)

    path.unlink()
    with pytest.raises(SecProvenanceUnavailableError, match="absent"):
        store.acquisition(artifact.identity_digest)


def test_conflicting_bytes_and_foreign_files_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = _artifact()
    store.publish(artifact)
    path = store.artifact_path(artifact.identity_digest)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SecProvenanceError, match="different bytes"):
        store.publish(artifact)
    other = store.binding_path(uuid4())
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text(
        json.dumps(
            {
                "schema_version": "sec-acquisition-provenance-binding-v1",
                "observation_id": str(other.stem),
                "raw_record_id": str(artifact.raw_record_id),
                "identity_digest": "e" * 64,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SecProvenanceUnavailableError, match="absent"):
        store.for_observation(UUID(other.stem))


def test_artifact_identity_binds_ordered_bindings() -> None:
    artifact = _artifact()
    assert artifact.identity_digest == artifact.identity_digest
    extra = SecProvenanceObservationBinding(
        observation_id=uuid5(NAMESPACE_URL, "sec-provenance-evidence-observation-2"),
        observation_sha256=observation_digest(_Observation(value="2")),
        submissions_record_id=uuid5(NAMESPACE_URL, "sec-provenance-evidence-submissions"),
        submissions_payload_checksum_sha256="c" * 64,
        normalized_at=LATER,
        transformation_version="sec-aapl-companyfacts-normalizer-v1",
    )
    extended = artifact.model_copy(update={"bindings": (*artifact.bindings, extra)})
    assert extended.identity_digest != artifact.identity_digest
    assert extended.content_digest != artifact.content_digest
