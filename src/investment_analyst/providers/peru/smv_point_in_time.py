"""Local point-in-time reconstruction of persisted SMV registry evidence."""

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.peru.smv_open_data import (
    SmvOpenDataDataset,
    SmvRegisteredCompany,
    SmvRegisteredSecurity,
    validate_legal_name,
)
from investment_analyst.providers.peru.smv_raw_records import (
    StoredSmvRegistrySnapshot,
    create_smv_source,
    smv_source_id,
    stored_smv_snapshot_from_raw_record,
)
from investment_analyst.storage import LocalStorage, RecordNotFoundError, StorageError


class SmvPointInTimeError(RuntimeError):
    """Base failure for local SMV registry reconstruction."""


class AmbiguousSmvRevisionError(SmvPointInTimeError):
    """Raised when equally available registry revisions disagree."""


class SmvPointInTimeQuery(ContractModel):
    """Request a local registry view at one explicit information cut."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    known_at: UTCDateTime
    legal_names: tuple[NonEmptyStr, ...] = ()

    @field_validator("legal_names")
    @classmethod
    def validate_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Require exact, unique, sorted names for deterministic filtering."""
        for value in values:
            validate_legal_name(value)
        if values != tuple(sorted(set(values))):
            raise ValueError("SMV legal_names must be unique and sorted")
        return values


class SmvIssuerRegistryView(ContractModel):
    """Latest independently selected company and security evidence for one issuer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    legal_name: NonEmptyStr
    companies: tuple[SmvRegisteredCompany, ...]
    securities: tuple[SmvRegisteredSecurity, ...]
    company_available_at: UTCDateTime | None = None
    securities_available_at: UTCDateTime | None = None
    company_raw_record_ids: tuple[UUID, ...]
    securities_raw_record_ids: tuple[UUID, ...]
    company_revisions_superseded: int = Field(ge=0)
    security_revisions_superseded: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_evidence(self) -> "SmvIssuerRegistryView":
        """Keep dataset payloads, timestamps, and evidence IDs aligned."""
        validate_legal_name(self.legal_name)
        _validate_side(
            self.companies,
            self.company_available_at,
            self.company_raw_record_ids,
            label="company",
        )
        _validate_side(
            self.securities,
            self.securities_available_at,
            self.securities_raw_record_ids,
            label="securities",
        )
        if any(item.legal_name != self.legal_name for item in self.companies):
            raise ValueError("company evidence belongs to a different legal name")
        if any(item.legal_name != self.legal_name for item in self.securities):
            raise ValueError("security evidence belongs to a different legal name")
        return self


class SmvPointInTimeResult(ContractModel):
    """Auditable registry universe reconstructed only from eligible raw records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: NonEmptyStr = "smv-registry-point-in-time-v1"
    query: SmvPointInTimeQuery
    issuers: tuple[SmvIssuerRegistryView, ...]
    raw_records_examined: int = Field(ge=0)
    raw_records_eligible: int = Field(ge=0)
    revisions_superseded: int = Field(ge=0)
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_result(self) -> "SmvPointInTimeResult":
        """Validate ordering, filtering, counts, and point-in-time safety."""
        names = tuple(item.legal_name for item in self.issuers)
        if names != tuple(sorted(set(names))):
            raise ValueError("SMV issuer views must be unique and sorted")
        if self.query.legal_names and any(name not in self.query.legal_names for name in names):
            raise ValueError("SMV result contains an issuer outside the requested filter")
        if self.raw_records_eligible > self.raw_records_examined:
            raise ValueError("eligible SMV raw record count exceeds examined records")
        expected_superseded = sum(
            item.company_revisions_superseded + item.security_revisions_superseded
            for item in self.issuers
        )
        if self.revisions_superseded != expected_superseded:
            raise ValueError("SMV superseded revision count is inconsistent")
        if any(
            timestamp is not None and timestamp > self.query.known_at
            for item in self.issuers
            for timestamp in (item.company_available_at, item.securities_available_at)
        ):
            raise ValueError("SMV result uses registry evidence unavailable at known_at")
        if not self.traceability_verified:
            raise ValueError("SMV point-in-time result must have verified traceability")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return deterministic JSON primitives for local clients."""
        return self.model_dump(mode="json")


class SmvPointInTimeService:
    """Select the latest non-ambiguous revision of each exact-name SMV query."""

    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def query(self, request: SmvPointInTimeQuery) -> SmvPointInTimeResult:
        """Reconstruct company and security evidence independently at known_at."""
        self._storage.require_open()
        records_examined = 0
        eligible: dict[
            tuple[SmvOpenDataDataset, str],
            list[StoredSmvRegistrySnapshot],
        ] = defaultdict(list)
        for dataset in SmvOpenDataDataset:
            snapshots, examined = self._load_dataset(dataset)
            records_examined += examined
            for snapshot in snapshots:
                name = snapshot.metadata.query_legal_name
                if request.legal_names and name not in request.legal_names:
                    continue
                if snapshot.record.available_at <= request.known_at:
                    eligible[(dataset, name)].append(snapshot)

        selected: dict[
            tuple[SmvOpenDataDataset, str],
            tuple[StoredSmvRegistrySnapshot, tuple[UUID, ...], int],
        ] = {}
        for key, snapshots in eligible.items():
            selected[key] = _select_latest(snapshots, dataset=key[0], legal_name=key[1])

        names = tuple(
            sorted(
                {legal_name for _, legal_name in selected}
                | (set(request.legal_names) if request.legal_names else set())
            )
        )
        issuers: list[SmvIssuerRegistryView] = []
        for legal_name in names:
            company_selection = selected.get((SmvOpenDataDataset.REGISTERED_COMPANIES, legal_name))
            security_selection = selected.get(
                (SmvOpenDataDataset.REGISTERED_SECURITIES, legal_name)
            )
            if company_selection is None and security_selection is None:
                continue
            company_snapshot, company_ids, company_superseded = _unpack(company_selection)
            security_snapshot, security_ids, security_superseded = _unpack(security_selection)
            issuers.append(
                SmvIssuerRegistryView(
                    legal_name=legal_name,
                    companies=(
                        company_snapshot.snapshot.companies if company_snapshot is not None else ()
                    ),
                    securities=(
                        security_snapshot.snapshot.securities
                        if security_snapshot is not None
                        else ()
                    ),
                    company_available_at=(
                        company_snapshot.record.available_at
                        if company_snapshot is not None
                        else None
                    ),
                    securities_available_at=(
                        security_snapshot.record.available_at
                        if security_snapshot is not None
                        else None
                    ),
                    company_raw_record_ids=company_ids,
                    securities_raw_record_ids=security_ids,
                    company_revisions_superseded=company_superseded,
                    security_revisions_superseded=security_superseded,
                )
            )
        result = SmvPointInTimeResult(
            query=request,
            issuers=tuple(issuers),
            raw_records_examined=records_examined,
            raw_records_eligible=sum(len(items) for items in eligible.values()),
            revisions_superseded=sum(
                item.company_revisions_superseded + item.security_revisions_superseded
                for item in issuers
            ),
            traceability_verified=True,
        )
        self._verify_traceability(result)
        return result

    def _load_dataset(
        self,
        dataset: SmvOpenDataDataset,
    ) -> tuple[list[StoredSmvRegistrySnapshot], int]:
        source_id = smv_source_id(dataset)
        records = self._storage.raw_records.list(source_id=source_id)
        if not records:
            try:
                self._storage.sources.get(source_id)
            except RecordNotFoundError:
                return [], 0
        try:
            if self._storage.sources.get(source_id) != create_smv_source(dataset):
                raise SmvPointInTimeError("stored SMV source definition is inconsistent")
        except RecordNotFoundError as error:
            raise SmvPointInTimeError(
                "SMV raw records exist without a source definition"
            ) from error
        snapshots: list[StoredSmvRegistrySnapshot] = []
        for record in records:
            try:
                snapshot = stored_smv_snapshot_from_raw_record(record)
            except (StorageError, ValueError) as error:
                raise SmvPointInTimeError(
                    f"stored SMV record {record.record_id} is invalid"
                ) from error
            if snapshot.metadata.dataset is not dataset:
                raise SmvPointInTimeError("stored SMV dataset identity is inconsistent")
            snapshots.append(snapshot)
        return snapshots, len(records)

    def _verify_traceability(self, result: SmvPointInTimeResult) -> None:
        evidence_ids = {
            record_id
            for issuer in result.issuers
            for record_id in (
                *issuer.company_raw_record_ids,
                *issuer.securities_raw_record_ids,
            )
        }
        records = self._storage.raw_records.get_many(evidence_ids)
        if set(records) != evidence_ids:
            raise SmvPointInTimeError("SMV point-in-time evidence is incomplete")
        for issuer in result.issuers:
            for record_id in issuer.company_raw_record_ids:
                snapshot = stored_smv_snapshot_from_raw_record(records[record_id])
                if (
                    snapshot.metadata.dataset is not SmvOpenDataDataset.REGISTERED_COMPANIES
                    or snapshot.metadata.query_legal_name != issuer.legal_name
                ):
                    raise SmvPointInTimeError("SMV company evidence identity is inconsistent")
            for record_id in issuer.securities_raw_record_ids:
                snapshot = stored_smv_snapshot_from_raw_record(records[record_id])
                if (
                    snapshot.metadata.dataset is not SmvOpenDataDataset.REGISTERED_SECURITIES
                    or snapshot.metadata.query_legal_name != issuer.legal_name
                ):
                    raise SmvPointInTimeError("SMV security evidence identity is inconsistent")


def _select_latest(
    snapshots: list[StoredSmvRegistrySnapshot],
    *,
    dataset: SmvOpenDataDataset,
    legal_name: str,
) -> tuple[StoredSmvRegistrySnapshot, tuple[UUID, ...], int]:
    latest_at = max(item.record.available_at for item in snapshots)
    latest = tuple(item for item in snapshots if item.record.available_at == latest_at)
    semantic_hashes = {item.metadata.semantic_sha256 for item in latest}
    if len(semantic_hashes) != 1:
        raise AmbiguousSmvRevisionError(
            f"conflicting {dataset.value} revisions for {legal_name} at {latest_at.isoformat()}"
        )
    reference = min(latest, key=lambda item: str(item.record.record_id))
    if any(item.snapshot != reference.snapshot for item in latest):
        raise AmbiguousSmvRevisionError(
            f"conflicting {dataset.value} payloads for {legal_name} at {latest_at.isoformat()}"
        )
    evidence_ids = tuple(sorted((item.record.record_id for item in latest), key=str))
    return reference, evidence_ids, len(snapshots) - len(latest)


def _unpack(
    selection: tuple[StoredSmvRegistrySnapshot, tuple[UUID, ...], int] | None,
) -> tuple[StoredSmvRegistrySnapshot | None, tuple[UUID, ...], int]:
    if selection is None:
        return None, (), 0
    return selection


def _validate_side(
    values: tuple[object, ...],
    available_at: datetime | None,
    evidence_ids: tuple[UUID, ...],
    *,
    label: str,
) -> None:
    populated = bool(values)
    if populated != (available_at is not None) or populated != bool(evidence_ids):
        raise ValueError(f"SMV {label} payload, availability, and evidence must align")
    if evidence_ids != tuple(sorted(set(evidence_ids), key=str)):
        raise ValueError(f"SMV {label} evidence IDs must be unique and sorted")


__all__ = [
    "AmbiguousSmvRevisionError",
    "SmvIssuerRegistryView",
    "SmvPointInTimeError",
    "SmvPointInTimeQuery",
    "SmvPointInTimeResult",
    "SmvPointInTimeService",
]
