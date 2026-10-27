"""Resolve the preserved acquisition of one SEC observation from v2 evidence."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from investment_analyst.core.models import NormalizedObservation, RawRecord
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    SecCompanyFactsNormalizer,
    sec_fact_to_observation,
)
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.sec_provenance_evidence import SecProvenanceEvidenceStore
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex
from investment_analyst.workspace.sec_provenance_models import (
    SecAcquisitionProvenance,
    SecProvenanceObservationBinding,
    SecProvenanceUnavailableError,
    observation_digest,
    payload_digest,
)

_MAX_CACHED_GROUPS = 16


class SecAcquisitionProvenanceResolver:
    """Prove a historical observation against v2 evidence and rebuild its context."""

    def __init__(
        self,
        store: SecProvenanceEvidenceStore,
        *,
        storage: LocalStorage,
        configuration: SecAssetConfiguration,
        normalizer: SecCompanyFactsNormalizer,
    ) -> None:
        self._store = store
        self._storage = storage
        self._configuration = configuration
        self._normalizer = normalizer
        self._cache: OrderedDict[tuple[str, UUID, datetime], dict[UUID, NormalizedObservation]] = (
            OrderedDict()
        )

    def resolve(
        self,
        observation: NormalizedObservation,
        *,
        companyfacts_record: RawRecord,
    ) -> NormalizedObservation:
        """Return the exact reconstruction of one observation or fail closed."""
        artifact = self._store.for_observation(observation.observation_id)
        if artifact is None:
            raise SecProvenanceUnavailableError(
                "no preserved acquisition evidence exists for this observation"
            )
        binding = artifact.binding_for(observation.observation_id)
        if binding is None:
            raise SecProvenanceUnavailableError("evidence does not bind this observation")
        self._validate_binding(artifact, observation, companyfacts_record, binding)
        original = self._original_record(artifact, companyfacts_record)
        submissions = self._submissions(observation, binding.submissions_record_id)
        rebuilt = self._group(artifact, original, submissions, observation.normalized_at)
        expected = rebuilt.get(observation.observation_id)
        if expected != observation:
            raise SecProvenanceUnavailableError(
                "preserved acquisition does not reconstruct the stored observation"
            )
        return expected

    def _validate_binding(
        self,
        artifact: SecAcquisitionProvenance,
        observation: NormalizedObservation,
        companyfacts_record: RawRecord,
        binding: SecProvenanceObservationBinding,
    ) -> None:
        if artifact.destination_workspace_id != self._workspace_id():
            raise SecProvenanceUnavailableError("evidence belongs to another workspace")
        if artifact.raw_record_id != companyfacts_record.record_id:
            raise SecProvenanceUnavailableError("evidence belongs to another raw record")
        if (
            artifact.asset_id != observation.asset_id
            or artifact.asset_id != companyfacts_record.asset_id
            or artifact.source_id != observation.source.source_id
            or artifact.source_id != companyfacts_record.source.source_id
            or artifact.raw_schema_version != companyfacts_record.schema_version
        ):
            raise SecProvenanceUnavailableError("evidence identity does not match the observation")
        checksum = companyfacts_record.source.checksum_sha256
        if artifact.payload_checksum_sha256 != checksum:
            raise SecProvenanceUnavailableError("persisted payload checksum differs from evidence")
        if artifact.payload_digest_sha256 != payload_digest(companyfacts_record.payload):
            raise SecProvenanceUnavailableError("persisted payload differs from evidence")
        if binding.observation_sha256 != observation_digest(observation):
            raise SecProvenanceUnavailableError("evidence does not bind this observation content")
        if binding.transformation_version != observation.transformation_version:
            raise SecProvenanceUnavailableError("evidence binds another transformation version")
        if binding.normalized_at != observation.normalized_at:
            raise SecProvenanceUnavailableError("evidence binds another normalization clock")

    def _original_record(
        self,
        artifact: SecAcquisitionProvenance,
        companyfacts_record: RawRecord,
    ) -> RawRecord:
        payload = companyfacts_record.payload
        if not isinstance(payload, Mapping):
            raise SecProvenanceUnavailableError("persisted raw payload is not an object")
        original = artifact.original_record(payload)
        if sha256_hex(canonical_json_bytes(original)) != artifact.original_raw_sha256:
            raise SecProvenanceUnavailableError("original acquisition cannot be reproduced")
        return original

    def _submissions(self, observation: NormalizedObservation, submissions_id: UUID) -> RawRecord:
        try:
            submissions = self._storage.raw_records.get(submissions_id)
        except Exception as error:
            raise SecProvenanceUnavailableError(
                "referenced submissions snapshot is unavailable"
            ) from error
        checksum = submissions.source.checksum_sha256
        if not isinstance(checksum, str):
            raise SecProvenanceUnavailableError("submissions snapshot has no payload checksum")
        if submissions.asset_id != observation.asset_id:
            raise SecProvenanceUnavailableError("submissions snapshot belongs to another asset")
        return submissions

    def _group(
        self,
        artifact: SecAcquisitionProvenance,
        original: RawRecord,
        submissions: RawRecord,
        normalized_at: datetime,
    ) -> dict[UUID, NormalizedObservation]:
        key = (artifact.identity_digest, submissions.record_id, normalized_at)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        if submissions.source.checksum_sha256 != self._expected_submissions_checksum(
            artifact, submissions.record_id
        ):
            raise SecProvenanceUnavailableError("submissions payload differs from evidence")
        extraction = self._normalizer.extract(original, submissions, normalized_at=normalized_at)
        rebuilt = {
            item.observation_id: item
            for item in (
                sec_fact_to_observation(
                    fact,
                    original,
                    submissions,
                    normalized_at=normalized_at,
                    configuration=self._configuration,
                )
                for fact in extraction.facts
            )
        }
        self._cache[key] = rebuilt
        while len(self._cache) > _MAX_CACHED_GROUPS:
            self._cache.popitem(last=False)
        return rebuilt

    @staticmethod
    def _expected_submissions_checksum(
        artifact: SecAcquisitionProvenance,
        submissions_id: UUID,
    ) -> str:
        for binding in artifact.bindings:
            if binding.submissions_record_id == submissions_id:
                return binding.submissions_payload_checksum_sha256
        raise SecProvenanceUnavailableError("submissions snapshot is not bound by evidence")

    def _workspace_id(self) -> UUID:
        workspace_id = self._storage.paths.workspace_id
        if workspace_id is None:
            raise SecProvenanceUnavailableError("workspace identity is unresolved")
        return workspace_id


def build_evidence_resolver(
    storage: LocalStorage,
    *,
    configuration: SecAssetConfiguration,
    normalizer: SecCompanyFactsNormalizer,
) -> SecAcquisitionProvenanceResolver | None:
    """Compose the resolver for a v2 workspace, or nothing for other formats."""
    if storage.paths.format_version != 2:
        return None
    store = SecProvenanceEvidenceStore(storage.paths.root, read_only=True)
    return SecAcquisitionProvenanceResolver(
        store,
        storage=storage,
        configuration=configuration,
        normalizer=normalizer,
    )


__all__ = [
    "SecAcquisitionProvenanceResolver",
    "build_evidence_resolver",
]
