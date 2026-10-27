"""Append-only SEC acquisition provenance evidence stored inside a v2 workspace."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from investment_analyst.core.models import ContractModel
from investment_analyst.workspace.sec_provenance_models import (
    SecAcquisitionProvenance,
    SecAcquisitionProvenanceDocument,
    SecProvenanceBindingReference,
    SecProvenanceError,
    SecProvenanceUnavailableError,
)

EVIDENCE_DIRECTORY = "sec-provenance"
_ACQUISITIONS = "acquisitions"
_BINDINGS = "bindings"


def _reject_unsafe(path: Path) -> None:
    if path.is_symlink() or any(item.is_symlink() for item in path.parents):
        raise SecProvenanceUnavailableError("provenance evidence path is unsafe")


def _sharded(root: Path, digest: str, suffix: str) -> Path:
    return root / digest[:2] / digest[2:4] / f"{digest}{suffix}"


@dataclass(frozen=True, slots=True)
class SecProvenancePublication:
    """Exact result of one idempotent evidence publication."""

    artifact_created: bool
    bindings_created: int
    bytes_written: int


class SecProvenanceEvidenceStore:
    """Hash-addressed, atomic and idempotent store of acquisition evidence."""

    def __init__(self, storage_root: Path, *, read_only: bool) -> None:
        if not storage_root.is_absolute():
            raise SecProvenanceError("provenance evidence root must be absolute")
        self._root = storage_root / EVIDENCE_DIRECTORY
        self._read_only = read_only
        self._acquisitions = self._root / _ACQUISITIONS
        self._bindings = self._root / _BINDINGS

    @property
    def root(self) -> Path:
        return self._root

    @property
    def read_only(self) -> bool:
        return self._read_only

    def artifact_path(self, identity_digest: str) -> Path:
        return _sharded(self._acquisitions, identity_digest, ".json")

    def binding_path(self, observation_id: UUID) -> Path:
        return _sharded(self._bindings, observation_id.hex, ".json")

    def publish(self, artifact: SecAcquisitionProvenance) -> SecProvenancePublication:
        """Publish one artifact and its bindings idempotently."""
        if self._read_only:
            raise SecProvenanceError("provenance evidence store is read-only")
        document = SecAcquisitionProvenanceDocument.seal(artifact)
        payload = _encode(document)
        created = _publish_file(self.artifact_path(artifact.identity_digest), payload)
        bindings = 0
        size = len(payload) if created else 0
        for binding in artifact.bindings:
            reference = SecProvenanceBindingReference(
                observation_id=binding.observation_id,
                raw_record_id=artifact.raw_record_id,
                identity_digest=artifact.identity_digest,
            )
            encoded = _encode(reference)
            if _publish_file(self.binding_path(binding.observation_id), encoded):
                bindings += 1
                size += len(encoded)
        return SecProvenancePublication(
            artifact_created=created,
            bindings_created=bindings,
            bytes_written=size,
        )

    @staticmethod
    def measure(artifact: SecAcquisitionProvenance) -> tuple[int, int]:
        """Return the exact file count and bytes this artifact would persist."""
        encoded = len(_encode(SecAcquisitionProvenanceDocument.seal(artifact)))
        return 1 + len(artifact.bindings), encoded + len(artifact.bindings) * _binding_size(
            artifact
        )

    def acquisition(self, identity_digest: str) -> SecAcquisitionProvenance:
        """Load and verify one artifact by its canonical identity."""
        path = self.artifact_path(identity_digest)
        _reject_unsafe(path)
        if not path.is_file():
            raise SecProvenanceUnavailableError("acquisition evidence is absent")
        try:
            document = SecAcquisitionProvenanceDocument.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise SecProvenanceUnavailableError("acquisition evidence is malformed") from error
        artifact = document.verified_artifact()
        if artifact.identity_digest != identity_digest:
            raise SecProvenanceUnavailableError("acquisition evidence is filed under another id")
        return artifact

    def binding(self, observation_id: UUID) -> SecProvenanceBindingReference | None:
        """Return the binding of one observation without scanning other artifacts."""
        path = self.binding_path(observation_id)
        _reject_unsafe(path)
        if not path.is_file():
            return None
        try:
            reference = SecProvenanceBindingReference.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise SecProvenanceUnavailableError("acquisition binding is malformed") from error
        if reference.observation_id != observation_id:
            raise SecProvenanceUnavailableError(
                "acquisition binding identifies another observation"
            )
        return reference

    def for_observation(self, observation_id: UUID) -> SecAcquisitionProvenance | None:
        """Resolve the verified artifact that carries one observation."""
        reference = self.binding(observation_id)
        if reference is None:
            return None
        artifact = self.acquisition(reference.identity_digest)
        if artifact.raw_record_id != reference.raw_record_id:
            raise SecProvenanceUnavailableError("acquisition binding references another raw record")
        if artifact.binding_for(observation_id) is None:
            raise SecProvenanceUnavailableError("acquisition binding is not part of its artifact")
        return artifact

    def artifacts(self) -> Iterator[SecAcquisitionProvenance]:
        """Yield every stored artifact in deterministic order for maintenance checks."""
        if not self._acquisitions.is_dir():
            return
        for path in sorted(self._acquisitions.rglob("*.json")):
            _reject_unsafe(path)
            try:
                document = SecAcquisitionProvenanceDocument.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError) as error:
                raise SecProvenanceUnavailableError("acquisition evidence is malformed") from error
            artifact = document.verified_artifact()
            if path != self.artifact_path(artifact.identity_digest):
                raise SecProvenanceUnavailableError("acquisition evidence path is not canonical")
            yield artifact


def _encode(model: ContractModel) -> bytes:
    document = model.model_dump(mode="json")
    return (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _publish_file(path: Path, payload: bytes) -> bool:
    """Publish one file atomically; an identical prior file is an idempotent no-op."""
    _reject_unsafe(path)
    if path.exists():
        if path.read_bytes() != payload:
            raise SecProvenanceError("provenance evidence already exists with different bytes")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise SecProvenanceError(
                    "provenance evidence appeared concurrently with different bytes"
                ) from None
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _binding_size(artifact: SecAcquisitionProvenance) -> int:
    return len(
        _encode(
            SecProvenanceBindingReference(
                observation_id=artifact.bindings[0].observation_id,
                raw_record_id=artifact.raw_record_id,
                identity_digest=artifact.identity_digest,
            )
        )
    )


__all__ = [
    "EVIDENCE_DIRECTORY",
    "SecProvenanceEvidenceStore",
    "SecProvenancePublication",
]
