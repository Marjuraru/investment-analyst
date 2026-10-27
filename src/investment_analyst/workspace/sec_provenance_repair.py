"""Narrow, auditable provenance repair for existing SEC raw snapshots.

The module only corrects ``source.retrieved_at`` of an already persisted SEC
snapshot when the retained v1 workspace proves the original retrieval instant
and every dependent observation reconstructs exactly from the corrected record.
Anything else is reported as a conflict and left untouched.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field

from investment_analyst.core.models import ContractModel, RawRecord, UTCDateTime
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    SecCompanyFactsNormalizer,
    sec_fact_to_observation,
)
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.raw_v2 import (
    RawV2TemporalRepair,
    RawV2TemporalRepairReceipt,
    RawV2TemporalStamp,
)
from investment_analyst.storage.sec_provenance_evidence import SecProvenanceEvidenceStore
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex
from investment_analyst.workspace.sec_provenance_models import (
    SecAcquisitionProvenance,
    SecProvenanceClassification,
    SecProvenanceEvidenceBudget,
    SecProvenanceEvidenceConflict,
    SecProvenanceEvidenceEntry,
    SecProvenanceEvidenceGroup,
    SecProvenanceEvidencePlan,
    SecProvenanceEvidenceReceipt,
    SecProvenanceEvidenceReceiptEntry,
    SecProvenanceEvidenceVerification,
    SecProvenanceObservationBinding,
    payload_digest,
)

PLAN_SCHEMA = "sec-provenance-repair-plan-v1"
RECEIPT_SCHEMA = "sec-provenance-repair-receipt-v1"
_ISSUER_SCHEMA_VERSIONS = (
    "sec-edgar-companyfacts-snapshot-v1",
    "sec-edgar-submissions-snapshot-v1",
)


class SecProvenanceRepairError(RuntimeError):
    """Base error for the SEC provenance repair maintenance path."""


class SecProvenanceRepairConflictError(SecProvenanceRepairError):
    """Raised when live state is neither the authorized before nor after state."""


class SecProvenanceRepairScopeError(SecProvenanceRepairError):
    """Raised when a plan or configuration falls outside the admitted scope."""


class SecProvenanceConflictReason(StrEnum):
    """Closed classification of a raw that cannot be repaired by this module."""

    EVIDENCE_MISSING = "evidence-missing"
    PAYLOAD_MISMATCH = "payload-mismatch"
    NO_DEPENDENTS = "no-dependents"
    DEPENDENT_CLOCKS_INCOMPATIBLE = "dependent-clocks-incompatible"
    RECONSTRUCTION_DIVERGES = "reconstruction-diverges"


class SecProvenanceReference(ContractModel):
    """Read-only inventory of one SEC raw shared by both workspaces."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    raw_record_id: UUID
    asset_id: str
    source_id: str
    schema_version: str
    payload_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_retrieved_at: UTCDateTime
    original_retrieved_at: UTCDateTime | None = None
    current_available_at: UTCDateTime
    current_received_at: UTCDateTime
    current_event_time: UTCDateTime
    dependent_observations: int = Field(ge=0)


class SecProvenanceRepairEntry(ContractModel):
    """One sealed, fully proven correction of a single raw record."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    raw_record_id: UUID
    asset_id: str
    source_id: str
    schema_version: str
    payload_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_time: UTCDateTime
    before_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    before: RawV2TemporalStamp
    after: RawV2TemporalStamp
    dependent_observations: int = Field(ge=1)
    dependent_clocks: tuple[UTCDateTime, ...] = Field(min_length=1)
    dependent_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    def as_repair(self) -> RawV2TemporalRepair:
        """Return the exact temporal repair authorization bound by this entry."""
        return RawV2TemporalRepair(
            record_id=self.raw_record_id,
            before_checksum_sha256=self.before_checksum_sha256,
            after_checksum_sha256=self.after_checksum_sha256,
            before=self.before,
            after=self.after,
        )


class SecProvenanceRepairConflict(ContractModel):
    """One raw deliberately excluded from the repair with its closed reason."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    raw_record_id: UUID
    asset_id: str
    source_id: str
    schema_version: str
    reason: SecProvenanceConflictReason
    detail: str
    dependent_observations: int = Field(ge=0)
    current_retrieved_at: UTCDateTime
    original_retrieved_at: UTCDateTime | None = None


class SecProvenanceRepairPlan(ContractModel):
    """Sealed plan identifying one exact provenance repair operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["sec-provenance-repair-plan-v1"] = PLAN_SCHEMA
    generated_at: UTCDateTime
    executor_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    destination_workspace_id: UUID
    source_workspace_id: UUID
    catalog_assets: tuple[str, ...] = Field(min_length=1)
    references: tuple[SecProvenanceReference, ...] = ()
    entries: tuple[SecProvenanceRepairEntry, ...] = ()
    conflicts: tuple[SecProvenanceRepairConflict, ...] = ()

    def digest(self) -> str:
        """Return the canonical digest that identifies this operation."""
        return sha256_hex(canonical_json_bytes(self))

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation with its digest."""
        payload = self.model_dump(mode="json")
        payload["plan_digest"] = self.digest()
        return payload


class SecProvenanceRepairReceiptEntry(ContractModel):
    """Per-raw result so an interrupted application can resume without repeating."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    raw_record_id: UUID
    before_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_relative_path: str
    changed: bool


class SecProvenanceRepairReceipt(ContractModel):
    """Append-once receipt of one applied plan."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["sec-provenance-repair-receipt-v1"] = RECEIPT_SCHEMA
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_workspace_id: UUID
    executor_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    completed_at: UTCDateTime
    entries: tuple[SecProvenanceRepairReceiptEntry, ...] = Field(min_length=1)

    def changed(self) -> int:
        """Return how many records this application actually rewrote."""
        return sum(1 for entry in self.entries if entry.changed)


class SecProvenanceRepairVerification(ContractModel):
    """Post-repair structural proof for one workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified_at: UTCDateTime
    entries: int = Field(ge=0)
    settled: int = Field(ge=0)
    reconstructed: int = Field(ge=0)
    divergent: int = Field(ge=0)
    verified: bool


class _RawRepairRepository(Protocol):
    def get(self, record_id: UUID) -> RawRecord:
        """Hydrate one verified raw record."""

    def list_record_ids(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
    ) -> list[UUID]:
        """List record identifiers matching one bounded filter."""

    def plan_temporal_repair(
        self,
        record_id: UUID,
        *,
        before: RawV2TemporalStamp,
        after: RawV2TemporalStamp,
    ) -> RawV2TemporalRepair:
        """Derive one narrow temporal repair without writing."""

    def repair_temporal_metadata(self, repair: RawV2TemporalRepair) -> RawV2TemporalRepairReceipt:
        """Apply one narrow temporal repair recoverably."""


class _DependentRepository(Protocol):
    def list_ids_for_raw_record(self, raw_record_id: UUID) -> list[UUID]:
        """List the observations that reference one raw record."""

    def get(self, observation_id: UUID) -> object:
        """Hydrate one observation."""


def stamp_of(record: RawRecord) -> RawV2TemporalStamp:
    """Return the three mutable temporal fields of one raw record."""
    return RawV2TemporalStamp(
        available_at=record.available_at,
        received_at=record.received_at,
        source_retrieved_at=record.source.retrieved_at,
    )


def corrected_record(record: RawRecord, after: RawV2TemporalStamp) -> RawRecord:
    """Return the record produced by replacing only the authorized timestamps."""
    return record.model_copy(
        update={
            "available_at": after.available_at,
            "received_at": after.received_at,
            "source": record.source.model_copy(update={"retrieved_at": after.source_retrieved_at}),
        }
    )


class SecProvenanceRepairService:
    """Inventory, seal, apply and verify the narrow SEC provenance repair."""

    def __init__(
        self,
        destination: LocalStorage,
        *,
        source: LocalStorage,
        configurations: Sequence[SecAssetConfiguration],
        destination_workspace_id: UUID,
        source_workspace_id: UUID,
        executor_sha: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not configurations:
            raise SecProvenanceRepairScopeError("at least one SEC issuer must be declared")
        ordered = tuple(sorted(configurations, key=lambda item: item.asset_id))
        if len({item.asset_id for item in ordered}) != len(ordered):
            raise SecProvenanceRepairScopeError("SEC issuer configurations must be unique")
        if len(executor_sha) != 40 or any(
            character not in "0123456789abcdef" for character in executor_sha
        ):
            raise SecProvenanceRepairScopeError("executor SHA must be a complete lower-case SHA-1")
        self._destination = destination
        self._source = source
        self._destination_workspace_id = destination_workspace_id
        self._source_workspace_id = source_workspace_id
        self._configurations = ordered
        self._executor_sha = executor_sha
        self._clock = clock

    @property
    def destination_workspace_id(self) -> UUID:
        return self._destination_workspace_id

    @property
    def source_workspace_id(self) -> UUID:
        return self._source_workspace_id

    def build_plan(self) -> SecProvenanceRepairPlan:
        """Inventory both workspaces and seal every admissible correction."""
        self._require_v2_destination()
        references: list[SecProvenanceReference] = []
        entries: list[SecProvenanceRepairEntry] = []
        conflicts: list[SecProvenanceRepairConflict] = []
        for configuration in self._configurations:
            for schema_version, source_id in self._issuer_sources(configuration):
                for record_id in self._record_ids(source_id, schema_version):
                    recorded = self._destination.raw_records.get(record_id)
                    if recorded.asset_id != configuration.asset_id:
                        continue
                    original = self._source_record(record_id)
                    dependents = self._dependents(record_id)
                    reference = _reference(recorded, original, len(dependents))
                    references.append(reference)
                    if original is None:
                        conflicts.append(
                            _conflict(
                                recorded,
                                None,
                                len(dependents),
                                SecProvenanceConflictReason.EVIDENCE_MISSING,
                                "the retained v1 workspace has no record with this identity",
                            )
                        )
                        continue
                    if not _payloads_match(recorded, original):
                        conflicts.append(
                            _conflict(
                                recorded,
                                original,
                                len(dependents),
                                SecProvenanceConflictReason.PAYLOAD_MISMATCH,
                                "the retained record payload is not identical",
                            )
                        )
                        continue
                    if stamp_of(recorded) == stamp_of(original):
                        continue
                    if not dependents:
                        conflicts.append(
                            _conflict(
                                recorded,
                                original,
                                0,
                                SecProvenanceConflictReason.NO_DEPENDENTS,
                                "no dependent observation constrains the retrieval instant",
                            )
                        )
                        continue
                    entry, reason, detail = self._prove(
                        recorded, original, dependents, schema_version
                    )
                    if entry is not None:
                        entries.append(entry)
                    else:
                        conflicts.append(
                            _conflict(recorded, original, len(dependents), reason, detail)
                        )
        return SecProvenanceRepairPlan(
            generated_at=self._now(),
            executor_sha=self._executor_sha,
            destination_workspace_id=self.destination_workspace_id,
            source_workspace_id=self.source_workspace_id,
            catalog_assets=tuple(item.asset_id for item in self._configurations),
            references=tuple(sorted(references, key=lambda item: str(item.raw_record_id))),
            entries=tuple(sorted(entries, key=lambda item: str(item.raw_record_id))),
            conflicts=tuple(sorted(conflicts, key=lambda item: str(item.raw_record_id))),
        )

    def apply(self, plan: SecProvenanceRepairPlan) -> SecProvenanceRepairReceipt:
        """Apply one sealed plan, resuming completed records without rewriting them."""
        self._require_v2_destination()
        self._verify_plan_identity(plan)
        if not plan.entries:
            raise SecProvenanceRepairScopeError("the sealed plan contains no admissible record")
        for entry in plan.entries:
            recorded = self._destination.raw_records.get(entry.raw_record_id)
            current = stamp_of(recorded)
            if current not in (entry.before, entry.after):
                raise SecProvenanceRepairConflictError(
                    f"raw record {entry.raw_record_id} is in an unexpected temporal state"
                )
            if recorded.event_time != entry.event_time:
                raise SecProvenanceRepairConflictError(
                    f"raw record {entry.raw_record_id} event time is not the sealed value"
                )
        results: list[SecProvenanceRepairReceiptEntry] = []
        for entry in plan.entries:
            recorded = self._destination.raw_records.get(entry.raw_record_id)
            if stamp_of(recorded) == entry.after:
                applied = self._destination.raw_records.repair_temporal_metadata(entry.as_repair())
            else:
                repair = self._destination.raw_records.plan_temporal_repair(
                    entry.raw_record_id,
                    before=entry.before,
                    after=entry.after,
                )
                if (
                    repair.before_checksum_sha256 != entry.before_checksum_sha256
                    or repair.after_checksum_sha256 != entry.after_checksum_sha256
                ):
                    raise SecProvenanceRepairConflictError(
                        f"raw record {entry.raw_record_id} does not reproduce the sealed checksums"
                    )
                applied = self._destination.raw_records.repair_temporal_metadata(repair)
            results.append(
                SecProvenanceRepairReceiptEntry(
                    raw_record_id=entry.raw_record_id,
                    before_checksum_sha256=applied.before_checksum_sha256,
                    after_checksum_sha256=applied.after_checksum_sha256,
                    after_relative_path=applied.after_relative_path,
                    changed=applied.changed,
                )
            )
        return SecProvenanceRepairReceipt(
            plan_digest=plan.digest(),
            destination_workspace_id=self.destination_workspace_id,
            executor_sha=self._executor_sha,
            completed_at=self._now(),
            entries=tuple(results),
        )

    def verify(self, plan: SecProvenanceRepairPlan) -> SecProvenanceRepairVerification:
        """Prove the settled state and the exact reconstruction of every dependent."""
        self._require_v2_destination()
        self._verify_plan_identity(plan)
        settled = 0
        reconstructed = 0
        divergent = 0
        for entry in plan.entries:
            recorded = self._destination.raw_records.get(entry.raw_record_id)
            if stamp_of(recorded) != entry.after:
                divergent += 1
                continue
            settled += 1
            exact, failed = self._reconstruction(recorded, entry)
            reconstructed += exact
            divergent += failed
        return SecProvenanceRepairVerification(
            plan_digest=plan.digest(),
            verified_at=self._now(),
            entries=len(plan.entries),
            settled=settled,
            reconstructed=reconstructed,
            divergent=divergent,
            verified=bool(plan.entries) and settled == len(plan.entries) and divergent == 0,
        )

    def _prove(
        self,
        recorded: RawRecord,
        original: RawRecord,
        dependents: Mapping[UUID, object],
        schema_version: str,
    ) -> tuple[SecProvenanceRepairEntry | None, SecProvenanceConflictReason, str]:
        if schema_version not in _ISSUER_SCHEMA_VERSIONS:
            return None, SecProvenanceConflictReason.EVIDENCE_MISSING, "unsupported schema version"
        after = RawV2TemporalStamp(
            available_at=recorded.available_at,
            received_at=recorded.received_at,
            source_retrieved_at=original.source.retrieved_at,
        )
        if after == stamp_of(recorded):
            return None, SecProvenanceConflictReason.EVIDENCE_MISSING, "no admissible correction"
        corrected = corrected_record(recorded, after)
        clocks = tuple(sorted({item.normalized_at for item in dependents.values()}))
        if schema_version != "sec-edgar-companyfacts-snapshot-v1":
            return (
                None,
                SecProvenanceConflictReason.NO_DEPENDENTS,
                "only company facts snapshots produce normalized observations",
            )
        exact, divergent = _reconstruct(
            corrected, self._destination, self._configurations_of(recorded), dependents
        )
        if divergent:
            return (
                None,
                SecProvenanceConflictReason.DEPENDENT_CLOCKS_INCOMPATIBLE
                if exact
                else SecProvenanceConflictReason.RECONSTRUCTION_DIVERGES,
                f"{divergent} of {len(dependents)} dependent observations do not reconstruct",
            )
        return (
            SecProvenanceRepairEntry(
                raw_record_id=recorded.record_id,
                asset_id=recorded.asset_id,
                source_id=recorded.source.source_id,
                schema_version=recorded.schema_version,
                payload_checksum_sha256=recorded.source.checksum_sha256,
                event_time=recorded.event_time,
                before_checksum_sha256=_record_checksum(recorded),
                after_checksum_sha256=_record_checksum(corrected),
                before=stamp_of(recorded),
                after=after,
                dependent_observations=len(dependents),
                dependent_clocks=clocks,
                dependent_digest=_dependent_digest(dependents),
            ),
            SecProvenanceConflictReason.EVIDENCE_MISSING,
            "",
        )

    def _reconstruction(
        self, recorded: RawRecord, entry: SecProvenanceRepairEntry
    ) -> tuple[int, int]:
        dependents = self._dependents(recorded.record_id)
        if len(dependents) != entry.dependent_observations:
            return 0, entry.dependent_observations
        if _dependent_digest(dependents) != entry.dependent_digest:
            return 0, entry.dependent_observations
        return _reconstruct(
            recorded, self._destination, self._configurations_of(recorded), dependents
        )

    def _configurations_of(self, record: RawRecord) -> SecAssetConfiguration:
        for configuration in self._configurations:
            if configuration.asset_id == record.asset_id:
                return configuration
        raise SecProvenanceRepairScopeError(
            f"raw record {record.record_id} belongs to an undeclared issuer"
        )

    def _issuer_sources(
        self,
        configuration: SecAssetConfiguration,
    ) -> tuple[tuple[str, str], ...]:
        return (
            ("sec-edgar-companyfacts-snapshot-v1", configuration.companyfacts_source_id),
            ("sec-edgar-submissions-snapshot-v1", configuration.submissions_source_id),
        )

    def _record_ids(self, source_id: str, schema_version: str) -> list[UUID]:
        return self._destination.raw_records.list_record_ids(
            source_id=source_id,
            schema_version=schema_version,
        )

    def _source_record(self, record_id: UUID) -> RawRecord | None:
        try:
            return self._source.raw_records.get(record_id)
        except Exception:
            return None

    def _dependents(self, record_id: UUID) -> dict[UUID, object]:
        identifiers = self._destination.observations.list_ids_for_raw_record(record_id)
        if not identifiers:
            return {}
        return self._destination.observations.get_many(identifiers)

    def _verify_plan_identity(self, plan: SecProvenanceRepairPlan) -> None:
        if plan.executor_sha != self._executor_sha:
            raise SecProvenanceRepairScopeError("sealed plan executor does not match this executor")
        if plan.destination_workspace_id != self.destination_workspace_id:
            raise SecProvenanceRepairScopeError("sealed plan destination workspace does not match")
        if plan.source_workspace_id != self.source_workspace_id:
            raise SecProvenanceRepairScopeError("sealed plan source workspace does not match")
        if plan.catalog_assets != tuple(item.asset_id for item in self._configurations):
            raise SecProvenanceRepairScopeError("sealed plan issuers do not match this scope")

    def _require_v2_destination(self) -> None:
        if self._destination.paths.format_version != 2:
            raise SecProvenanceRepairScopeError("destination workspace is not a v2 workspace")
        if self._source.paths.format_version != 1:
            raise SecProvenanceRepairScopeError("retained source workspace is not a v1 workspace")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise SecProvenanceRepairError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _reconstruct(
    record: RawRecord,
    destination: LocalStorage,
    configuration: SecAssetConfiguration,
    dependents: Mapping[UUID, object],
) -> tuple[int, int]:
    results = _reconstruct_results(record, destination, configuration, dependents)
    exact = sum(1 for matched in results.values() if matched)
    return exact, len(results) - exact


def _reconstruct_results(
    record: RawRecord,
    destination: LocalStorage,
    configuration: SecAssetConfiguration,
    dependents: Mapping[UUID, object],
) -> dict[UUID, bool]:
    normalizer = SecCompanyFactsNormalizer(configuration)
    cache: dict[tuple[UUID, datetime], dict[UUID, object]] = {}
    results: dict[UUID, bool] = {}
    for stored in dependents.values():
        key = json.loads(stored.source.record_key)
        submissions_id = UUID(key["submissions_record_id"])
        submissions = destination.raw_records.get(submissions_id)
        cache_key = (submissions_id, stored.normalized_at)
        rebuilt = cache.get(cache_key)
        if rebuilt is None:
            extraction = normalizer.extract(
                record,
                submissions,
                normalized_at=stored.normalized_at,
            )
            rebuilt = {
                item.observation_id: item
                for item in (
                    sec_fact_to_observation(
                        fact,
                        record,
                        submissions,
                        normalized_at=stored.normalized_at,
                        configuration=configuration,
                    )
                    for fact in extraction.facts
                )
            }
            cache[cache_key] = rebuilt
        results[stored.observation_id] = rebuilt.get(stored.observation_id) == stored
    return results


def _reference(
    recorded: RawRecord,
    original: RawRecord | None,
    dependent_count: int,
) -> SecProvenanceReference:
    return SecProvenanceReference(
        raw_record_id=recorded.record_id,
        asset_id=recorded.asset_id,
        source_id=recorded.source.source_id,
        schema_version=recorded.schema_version,
        payload_checksum_sha256=recorded.source.checksum_sha256,
        current_retrieved_at=recorded.source.retrieved_at,
        original_retrieved_at=None if original is None else original.source.retrieved_at,
        current_available_at=recorded.available_at,
        current_received_at=recorded.received_at,
        current_event_time=recorded.event_time,
        dependent_observations=dependent_count,
    )


def _conflict(
    recorded: RawRecord,
    original: RawRecord | None,
    dependent_count: int,
    reason: SecProvenanceConflictReason,
    detail: str,
) -> SecProvenanceRepairConflict:
    return SecProvenanceRepairConflict(
        raw_record_id=recorded.record_id,
        asset_id=recorded.asset_id,
        source_id=recorded.source.source_id,
        schema_version=recorded.schema_version,
        reason=reason,
        detail=detail,
        dependent_observations=dependent_count,
        current_retrieved_at=recorded.source.retrieved_at,
        original_retrieved_at=None if original is None else original.source.retrieved_at,
    )


def _payloads_match(recorded: RawRecord, original: RawRecord) -> bool:
    return (
        recorded.asset_id == original.asset_id
        and recorded.source.source_id == original.source.source_id
        and recorded.schema_version == original.schema_version
        and recorded.source.checksum_sha256 == original.source.checksum_sha256
        and recorded.source.record_key == original.source.record_key
        and recorded.payload == original.payload
    )


def _record_checksum(record: RawRecord) -> str:
    return sha256_hex(canonical_json_bytes(record))


def _dependent_digest(dependents: Mapping[UUID, object]) -> str:
    encoded = json.dumps(
        sorted(str(identifier) for identifier in dependents),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256_hex(encoded.encode("utf-8"))


def load_plan(path: Path) -> SecProvenanceRepairPlan:
    """Load a sealed plan and refuse a document whose digest does not verify."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SecProvenanceRepairScopeError("sealed plan must be a JSON object")
    declared = document.pop("plan_digest", None)
    plan = SecProvenanceRepairPlan.model_validate_json(json.dumps(document))
    if declared is not None and declared != plan.digest():
        raise SecProvenanceRepairScopeError("sealed plan digest does not match its bytes")
    return plan


def write_plan(path: Path, plan: SecProvenanceRepairPlan) -> Path:
    """Write one sealed plan to a new path without overwriting prior evidence."""
    destination = path.expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise SecProvenanceRepairScopeError("sealed plan output path must be new")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(plan.to_json_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return destination


def load_evidence_plan(path: Path) -> SecProvenanceEvidencePlan:
    """Load a sealed evidence plan and refuse a document whose digest does not verify."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SecProvenanceEvidenceError("sealed evidence plan must be a JSON object")
    declared = document.pop("plan_digest", None)
    plan = SecProvenanceEvidencePlan.model_validate_json(json.dumps(document))
    if declared is not None and declared != plan.digest():
        raise SecProvenanceEvidenceError("sealed evidence plan digest does not match its bytes")
    return plan


def write_evidence_plan(path: Path, plan: SecProvenanceEvidencePlan) -> Path:
    """Write one sealed evidence plan to a new path without overwriting evidence."""
    destination = path.expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise SecProvenanceEvidenceError("sealed evidence plan output path must be new")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(plan.to_json_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return destination


def iter_conflict_ids(plan: SecProvenanceRepairPlan) -> Iterable[str]:
    """Return the deterministic identifiers of every excluded raw."""
    return (str(item.raw_record_id) for item in plan.conflicts)


class SecProvenanceEvidenceError(SecProvenanceRepairError):
    """Raised when an evidence plan cannot be built, applied or verified."""


class SecProvenanceEvidenceService:
    """Plan, publish and verify preserved acquisition evidence for SEC raws."""

    def __init__(
        self,
        destination: LocalStorage,
        *,
        source: LocalStorage | None,
        configurations: Sequence[SecAssetConfiguration],
        destination_workspace_id: UUID,
        source_workspace_id: UUID,
        executor_sha: str,
        base_sha: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        ordered = tuple(sorted(configurations, key=lambda item: item.asset_id))
        if not ordered:
            raise SecProvenanceRepairScopeError("at least one SEC issuer must be declared")
        if len({item.asset_id for item in ordered}) != len(ordered):
            raise SecProvenanceRepairScopeError("SEC issuer configurations must be unique")
        if len(executor_sha) != 40 or any(char not in "0123456789abcdef" for char in executor_sha):
            raise SecProvenanceRepairScopeError("executor SHA must be a complete lower-case SHA-1")
        if len(base_sha) != 40 or any(char not in "0123456789abcdef" for char in base_sha):
            raise SecProvenanceRepairScopeError("base SHA must be a complete lower-case SHA-1")
        if destination.paths.format_version != 2:
            raise SecProvenanceRepairScopeError("destination workspace is not a v2 workspace")
        if source is not None and source.paths.format_version != 1:
            raise SecProvenanceRepairScopeError("retained source workspace is not a v1 workspace")
        self._destination = destination
        self._source = source
        self._configurations = ordered
        self._destination_workspace_id = destination_workspace_id
        self._source_workspace_id = source_workspace_id
        self._executor_sha = executor_sha
        self._base_sha = base_sha
        self._clock = clock

    def build_plan(self) -> SecProvenanceEvidencePlan:
        """Classify the declared universe and seal the admissible evidence."""
        if self._source is None:
            raise SecProvenanceEvidenceError("planning requires the retained v1 workspace")
        groups: list[SecProvenanceEvidenceGroup] = []
        entries: list[SecProvenanceEvidenceEntry] = []
        conflicts: list[SecProvenanceEvidenceConflict] = []
        store = SecProvenanceEvidenceStore(self._destination.paths.root, read_only=True)
        for configuration in self._configurations:
            normalizer = SecCompanyFactsNormalizer(configuration)
            for schema_version, source_id in self._issuer_sources(configuration):
                for record_id in self._destination.raw_records.list_record_ids(
                    source_id=source_id,
                    schema_version=schema_version,
                ):
                    recorded = self._destination.raw_records.get(record_id)
                    if recorded.asset_id != configuration.asset_id:
                        continue
                    dependents = self._dependents(record_id)
                    if not dependents:
                        groups.append(
                            _evidence_group(
                                recorded,
                                SecProvenanceClassification.UNREFERENCED_OR_INDIRECT,
                                dependent_observations=0,
                                indirect_references=self._indirect_references(record_id),
                                detail="no direct dependent observation",
                            )
                        )
                        continue
                    results = _reconstruct_results(
                        recorded, self._destination, configuration, dependents
                    )
                    failing = {
                        identifier: observation
                        for identifier, observation in dependents.items()
                        if not results[identifier]
                    }
                    if not failing:
                        groups.append(
                            _evidence_group(
                                recorded,
                                SecProvenanceClassification.CURRENT_VALID,
                                dependent_observations=len(dependents),
                                current_reconstructing=len(dependents),
                            )
                        )
                        continue
                    entry, conflict = self._prove_evidence(
                        recorded,
                        failing,
                        configuration,
                        normalizer,
                        store,
                        dependent_observations=len(dependents),
                        current_reconstructing=len(dependents) - len(failing),
                    )
                    if entry is not None:
                        entries.append(entry)
                        groups.append(
                            _evidence_group(
                                recorded,
                                SecProvenanceClassification.HISTORICAL_PROVENANCE_REQUIRED,
                                dependent_observations=len(dependents),
                                current_reconstructing=len(dependents) - len(failing),
                                historical_required=len(failing),
                                detail="preserved acquisition proven for every dependent",
                            )
                        )
                    else:
                        assert conflict is not None
                        conflicts.append(conflict)
                        groups.append(
                            _evidence_group(
                                recorded,
                                SecProvenanceClassification.UNRESOLVED,
                                dependent_observations=len(dependents),
                                current_reconstructing=len(dependents) - len(failing),
                                historical_required=len(failing),
                                detail=conflict.detail,
                            )
                        )
        budget = _evidence_budget(entries)
        return SecProvenanceEvidencePlan(
            generated_at=self._now(),
            executor_sha=self._executor_sha,
            base_sha=self._base_sha,
            destination_workspace_id=self._destination_workspace_id,
            source_workspace_id=self._source_workspace_id,
            catalog_assets=tuple(item.asset_id for item in self._configurations),
            groups=tuple(sorted(groups, key=lambda item: str(item.raw_record_id))),
            entries=tuple(sorted(entries, key=lambda item: str(item.artifact.raw_record_id))),
            conflicts=tuple(sorted(conflicts, key=lambda item: str(item.raw_record_id))),
            budget=budget,
        )

    def apply(self, plan: SecProvenanceEvidencePlan) -> SecProvenanceEvidenceReceipt:
        """Publish the sealed evidence and its bindings without touching financial data."""
        self._verify_plan_identity(plan)
        if not plan.entries:
            raise SecProvenanceEvidenceError("the sealed plan contains no admissible evidence")
        store = SecProvenanceEvidenceStore(self._destination.paths.root, read_only=False)
        results: list[SecProvenanceEvidenceReceiptEntry] = []
        files = 0
        written = 0
        for entry in plan.entries:
            publication = store.publish(entry.artifact)
            files += (1 if publication.artifact_created else 0) + publication.bindings_created
            written += publication.bytes_written
            results.append(
                SecProvenanceEvidenceReceiptEntry(
                    identity_digest=entry.artifact.identity_digest,
                    raw_record_id=entry.artifact.raw_record_id,
                    artifact_created=publication.artifact_created,
                    bindings_created=publication.bindings_created,
                )
            )
        return SecProvenanceEvidenceReceipt(
            plan_digest=plan.digest(),
            destination_workspace_id=self._destination_workspace_id,
            executor_sha=self._executor_sha,
            completed_at=self._now(),
            entries=tuple(results),
            files_created=files,
            bytes_written=written,
        )

    def verify(self, plan: SecProvenanceEvidencePlan) -> SecProvenanceEvidenceVerification:
        """Prove every bound observation from the destination workspace alone."""
        self._verify_plan_identity(plan)
        store = SecProvenanceEvidenceStore(self._destination.paths.root, read_only=True)
        artifacts = 0
        bindings = 0
        reconstructed = 0
        divergent = 0
        for entry in plan.entries:
            artifact = store.acquisition(entry.artifact.identity_digest)
            if artifact.content_digest != entry.artifact.content_digest:
                raise SecProvenanceEvidenceError("published evidence differs from the sealed plan")
            artifacts += 1
            configuration = self._configurations_of(artifact.asset_id)
            normalizer = SecCompanyFactsNormalizer(configuration)
            current = self._destination.raw_records.get(artifact.raw_record_id)
            if payload_digest(current.payload) != artifact.payload_digest_sha256:
                divergent += len(artifact.bindings)
                bindings += len(artifact.bindings)
                continue
            original = artifact.original_record(current.payload)
            if sha256_hex(canonical_json_bytes(original)) != artifact.original_raw_sha256:
                divergent += len(artifact.bindings)
                bindings += len(artifact.bindings)
                continue
            for binding in artifact.bindings:
                bindings += 1
                reference = store.binding(binding.observation_id)
                if reference is None or reference.identity_digest != artifact.identity_digest:
                    divergent += 1
                    continue
                stored = self._destination.observations.get(binding.observation_id)
                if _observation_digest(stored) != binding.observation_sha256:
                    divergent += 1
                    continue
                submissions = self._destination.raw_records.get(binding.submissions_record_id)
                if (
                    submissions.source.checksum_sha256
                    != binding.submissions_payload_checksum_sha256
                ):
                    divergent += 1
                    continue
                rebuilt = _reconstruct_observation(
                    original,
                    submissions,
                    stored,
                    configuration,
                    normalizer,
                )
                if rebuilt == stored:
                    reconstructed += 1
                else:
                    divergent += 1
        return SecProvenanceEvidenceVerification(
            plan_digest=plan.digest(),
            verified_at=self._now(),
            artifacts=artifacts,
            bindings=bindings,
            reconstructed=reconstructed,
            divergent=divergent,
            verified=bool(plan.entries) and divergent == 0 and reconstructed == bindings,
        )

    def _prove_evidence(
        self,
        recorded: RawRecord,
        failing: Mapping[UUID, object],
        configuration: SecAssetConfiguration,
        normalizer: SecCompanyFactsNormalizer,
        store: SecProvenanceEvidenceStore,
        *,
        dependent_observations: int,
        current_reconstructing: int,
    ) -> tuple[SecProvenanceEvidenceEntry | None, SecProvenanceEvidenceConflict | None]:
        original = self._source_record(recorded.record_id)
        if original is None:
            return None, _evidence_conflict(
                recorded,
                "no retained v1 raw with this identity",
                dependent_observations,
                current_reconstructing,
                ("raw:absent-in-v1",),
            )
        differences = _payload_differences(recorded, original)
        if differences:
            return None, _evidence_conflict(
                recorded,
                "persisted payload differs from the retained acquisition",
                dependent_observations,
                current_reconstructing,
                tuple(differences),
            )
        bindings: list[SecProvenanceObservationBinding] = []
        problems: list[str] = []
        cache: dict[tuple[UUID, datetime], dict[UUID, object]] = {}
        for identifier in sorted(failing, key=str):
            stored = failing[identifier]
            source_observation = self._source_observation(identifier)
            if source_observation is None:
                problems.append(f"observation:{identifier}:absent-in-v1")
                continue
            if _observation_digest(source_observation) != _observation_digest(stored):
                problems.append(f"observation:{identifier}:content-differs-from-v1")
                continue
            record_key = json.loads(stored.source.record_key)
            submissions_id = UUID(record_key["submissions_record_id"])
            try:
                submissions = self._destination.raw_records.get(submissions_id)
            except Exception:
                problems.append(f"submissions:{submissions_id}:absent")
                continue
            cache_key = (submissions_id, stored.normalized_at)
            rebuilt = cache.get(cache_key)
            if rebuilt is None:
                extraction = normalizer.extract(
                    original,
                    submissions,
                    normalized_at=stored.normalized_at,
                )
                rebuilt = {
                    item.observation_id: item
                    for item in (
                        sec_fact_to_observation(
                            fact,
                            original,
                            submissions,
                            normalized_at=stored.normalized_at,
                            configuration=configuration,
                        )
                        for fact in extraction.facts
                    )
                }
                cache[cache_key] = rebuilt
            if rebuilt.get(identifier) != stored:
                problems.append(f"observation:{identifier}:not-reconstructible")
                continue
            bindings.append(
                SecProvenanceObservationBinding(
                    observation_id=identifier,
                    observation_sha256=_observation_digest(stored),
                    submissions_record_id=submissions_id,
                    submissions_payload_checksum_sha256=str(submissions.source.checksum_sha256),
                    normalized_at=stored.normalized_at,
                    transformation_version=stored.transformation_version,
                )
            )
        if problems:
            return None, _evidence_conflict(
                recorded,
                "historical provenance cannot be proven for every dependent",
                dependent_observations,
                current_reconstructing,
                tuple(problems),
            )
        artifact = SecAcquisitionProvenance(
            destination_workspace_id=self._destination_workspace_id,
            source_workspace_id=self._source_workspace_id,
            raw_record_id=recorded.record_id,
            asset_id=recorded.asset_id,
            source_id=recorded.source.source_id,
            raw_schema_version=recorded.schema_version,
            payload_checksum_sha256=str(recorded.source.checksum_sha256),
            payload_digest_sha256=payload_digest(recorded.payload),
            original_raw_sha256=sha256_hex(canonical_json_bytes(original)),
            original_source=original.source,
            original_event_time=original.event_time,
            original_available_at=original.available_at,
            original_received_at=original.received_at,
            bindings=tuple(sorted(bindings, key=lambda item: str(item.observation_id))),
        )
        return (
            SecProvenanceEvidenceEntry(
                artifact=artifact,
                evidence_present_before=store.artifact_path(artifact.identity_digest).is_file(),
                dependent_observations=len(failing),
                reconstructed=len(bindings),
            ),
            None,
        )

    def _issuer_sources(
        self,
        configuration: SecAssetConfiguration,
    ) -> tuple[tuple[str, str], ...]:
        return (
            ("sec-edgar-companyfacts-snapshot-v1", configuration.companyfacts_source_id),
            ("sec-edgar-submissions-snapshot-v1", configuration.submissions_source_id),
        )

    def _dependents(self, record_id: UUID) -> dict[UUID, object]:
        identifiers = self._destination.observations.list_ids_for_raw_record(record_id)
        if not identifiers:
            return {}
        return self._destination.observations.get_many(identifiers)

    def _indirect_references(self, record_id: UUID) -> int:
        rows = self._destination.store.connection.execute(
            "SELECT count(*) FROM normalized_observations_v2 "
            "WHERE raw_record_id <> ? AND source_record_key LIKE ?",
            [str(record_id), f"%{record_id}%"],
        ).fetchone()
        return int(rows[0]) if rows else 0

    def _source_record(self, record_id: UUID) -> RawRecord | None:
        if self._source is None:
            return None
        try:
            return self._source.raw_records.get(record_id)
        except Exception:
            return None

    def _source_observation(self, observation_id: UUID) -> object | None:
        if self._source is None:
            return None
        try:
            return self._source.observations.get(observation_id)
        except Exception:
            return None

    def _configurations_of(self, asset_id: str) -> SecAssetConfiguration:
        for configuration in self._configurations:
            if configuration.asset_id == asset_id:
                return configuration
        raise SecProvenanceRepairScopeError(f"asset {asset_id} is outside the declared scope")

    def _verify_plan_identity(self, plan: SecProvenanceEvidencePlan) -> None:
        if plan.executor_sha != self._executor_sha:
            raise SecProvenanceRepairScopeError("sealed plan executor does not match this executor")
        if plan.base_sha != self._base_sha:
            raise SecProvenanceRepairScopeError("sealed plan base does not match the declared base")
        if plan.destination_workspace_id != self._destination_workspace_id:
            raise SecProvenanceRepairScopeError("sealed plan destination workspace does not match")
        if plan.source_workspace_id != self._source_workspace_id:
            raise SecProvenanceRepairScopeError("sealed plan source workspace does not match")
        if plan.catalog_assets != tuple(item.asset_id for item in self._configurations):
            raise SecProvenanceRepairScopeError("sealed plan issuers do not match this scope")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise SecProvenanceEvidenceError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _evidence_group(
    record: RawRecord,
    classification: SecProvenanceClassification,
    *,
    dependent_observations: int,
    current_reconstructing: int = 0,
    historical_required: int = 0,
    indirect_references: int = 0,
    detail: str = "",
) -> SecProvenanceEvidenceGroup:
    return SecProvenanceEvidenceGroup(
        raw_record_id=record.record_id,
        asset_id=record.asset_id,
        source_id=record.source.source_id,
        raw_schema_version=record.schema_version,
        classification=classification,
        dependent_observations=dependent_observations,
        current_reconstructing=current_reconstructing,
        historical_required=historical_required,
        indirect_references=indirect_references,
        detail=detail,
    )


def _evidence_conflict(
    record: RawRecord,
    detail: str,
    dependent_observations: int,
    current_reconstructing: int,
    differences: tuple[str, ...],
) -> SecProvenanceEvidenceConflict:
    return SecProvenanceEvidenceConflict(
        raw_record_id=record.record_id,
        asset_id=record.asset_id,
        source_id=record.source.source_id,
        raw_schema_version=record.schema_version,
        detail=detail or "unresolved",
        dependent_observations=dependent_observations,
        current_reconstructing=current_reconstructing,
        field_differences=differences,
    )


def _evidence_budget(
    entries: Sequence[SecProvenanceEvidenceEntry],
) -> SecProvenanceEvidenceBudget:
    files = 0
    size = 0
    bindings = 0
    for entry in entries:
        entry_files, entry_bytes = SecProvenanceEvidenceStore.measure(entry.artifact)
        files += entry_files
        size += entry_bytes
        bindings += len(entry.artifact.bindings)
    return SecProvenanceEvidenceBudget(
        artifacts=len(entries),
        bindings=bindings,
        bytes_new=size,
    )


def _payload_differences(recorded: RawRecord, original: RawRecord) -> list[str]:
    differences: list[str] = []
    if recorded.asset_id != original.asset_id:
        differences.append("raw:asset")
    if recorded.source.source_id != original.source.source_id:
        differences.append("raw:source_id")
    if recorded.schema_version != original.schema_version:
        differences.append("raw:schema_version")
    if recorded.source.checksum_sha256 != original.source.checksum_sha256:
        differences.append("raw:payload_checksum")
    if recorded.source.record_key != original.source.record_key:
        differences.append("raw:record_key")
    if recorded.payload != original.payload:
        differences.append("raw:payload")
    return differences


def _observation_digest(observation: object) -> str:
    return sha256_hex(canonical_json_bytes(observation))


def _reconstruct_observation(
    record: RawRecord,
    submissions: RawRecord,
    stored: object,
    configuration: SecAssetConfiguration,
    normalizer: SecCompanyFactsNormalizer,
) -> object | None:
    extraction = normalizer.extract(record, submissions, normalized_at=stored.normalized_at)
    rebuilt = {
        item.observation_id: item
        for item in (
            sec_fact_to_observation(
                fact,
                record,
                submissions,
                normalized_at=stored.normalized_at,
                configuration=configuration,
            )
            for fact in extraction.facts
        )
    }
    return rebuilt.get(stored.observation_id)


__all__ = [
    "PLAN_SCHEMA",
    "RECEIPT_SCHEMA",
    "SecProvenanceConflictReason",
    "SecProvenanceReference",
    "SecProvenanceRepairConflict",
    "SecProvenanceRepairConflictError",
    "SecProvenanceRepairEntry",
    "SecProvenanceRepairError",
    "SecProvenanceRepairPlan",
    "SecProvenanceRepairReceipt",
    "SecProvenanceRepairReceiptEntry",
    "SecProvenanceRepairScopeError",
    "SecProvenanceRepairService",
    "SecProvenanceEvidenceError",
    "SecProvenanceEvidenceService",
    "load_evidence_plan",
    "SecProvenanceRepairVerification",
    "corrected_record",
    "iter_conflict_ids",
    "load_plan",
    "stamp_of",
    "write_evidence_plan",
    "write_plan",
]
