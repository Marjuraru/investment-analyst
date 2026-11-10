"""Typed requests and accounting for the institutional-observation service."""

from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.observation import NormalizedObservation
from investment_analyst.evidence.instrument_correspondence.models import InstrumentCorrespondence
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_correspondence.models import (
    SecInstitutionalRowCorrespondence,
)
from investment_analyst.evidence.sec_institutional_holdings.models import (
    InstitutionalHoldingsReport,
)
from investment_analyst.evidence.sec_institutional_semantics.models import (
    InstitutionalHoldingsSemantics,
    InstitutionalSemanticsRow,
)


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class InstitutionalObservationRequest(_Strict):
    asset_id: NonEmptyStr
    manager_cik: NonEmptyStr
    report_ids: tuple[UUID, ...]
    known_at: UTCDateTime

    @field_validator("manager_cik")
    @classmethod
    def _normalize_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def ids(self) -> "InstitutionalObservationRequest":
        if (
            not self.report_ids
            or len(self.report_ids) > 20
            or len(set(self.report_ids)) != len(self.report_ids)
        ):
            raise ValueError("report_ids must contain one to twenty unique values")
        return self


class InstitutionalObservationSummary(_Strict):
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    normalized_at: UTCDateTime
    reports_examined: int = Field(ge=0)
    reports_missing: int = Field(ge=0)
    reports_not_enriched: int = Field(ge=0)
    rows_examined: int = Field(ge=0)
    rows_linked: int = Field(ge=0)
    rows_unlinked: int = Field(ge=0)
    values_examined: int = Field(ge=0)
    observations_generated: int = Field(ge=0)
    observations_created: int = Field(ge=0)
    observations_reused: int = Field(ge=0)
    skipped_by_reason: dict[NonEmptyStr, int] = Field(default_factory=dict)
    observation_witnesses: tuple["InstitutionalObservationWitness", ...] = ()
    observation_references: tuple["InstitutionalObservationReference", ...] = ()

    @model_validator(mode="after")
    def counts(self) -> "InstitutionalObservationSummary":
        if self.observations_created + self.observations_reused != self.observations_generated:
            raise ValueError("observation counts are inconsistent")
        if self.reports_missing + self.reports_not_enriched > self.reports_examined:
            raise ValueError("report counts are inconsistent")
        if self.rows_linked + self.rows_unlinked != self.rows_examined:
            raise ValueError("row counts are inconsistent")
        if self.values_examined != self.rows_linked:
            raise ValueError("values examined must equal linked rows")
        if any(count < 0 for count in self.skipped_by_reason.values()):
            raise ValueError("skipped_by_reason counts must not be negative")
        if any(item.verified_at_cut != self.known_at for item in self.observation_witnesses):
            raise ValueError("observation witness cuts must equal the requested cut")
        if any(item.available_at > self.known_at for item in self.observation_references):
            raise ValueError("observation references must be visible at the requested cut")
        reference_keys = {
            (item.observation_id, item.claim_id, item.field_name)
            for item in self.observation_references
        }
        if len(reference_keys) != len(self.observation_references):
            raise ValueError("observation references must be unique")
        witness_keys = {
            (item.observation_id, item.current_claim_id) for item in self.observation_witnesses
        }
        if len(witness_keys) != len(self.observation_witnesses):
            raise ValueError("observation witnesses must be unique")
        return self


class InstitutionalObservationQuery(_Strict):
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    manager_cik: NonEmptyStr | None = None
    report_id: UUID | None = None
    cusip: NonEmptyStr | None = None
    field_name: NonEmptyStr | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=1000, ge=1, le=10_000)

    @field_validator("manager_cik")
    @classmethod
    def query_cik(cls, value: str | None) -> str | None:
        return normalize_cik(value) if value is not None else None


class InstitutionalObservationWitness(_Strict):
    """Ephemeral proof linking a current verified claim to an older observation.

    The witness is never persisted and never certifies the original claim's historical
    availability. It only records that a current, strictly verified proof covers the same row and
    fields at ``verified_at_cut``.
    """

    current_claim_id: UUID
    observation_id: UUID
    original_claim_id: UUID
    asset_id: NonEmptyStr
    manager_cik: NonEmptyStr
    report_id: UUID
    artifact_id: UUID
    row_id: UUID
    field_name: NonEmptyStr
    verified_at_cut: UTCDateTime
    effective_available_at: UTCDateTime

    @field_validator("manager_cik")
    @classmethod
    def witness_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def witness_shape(self) -> "InstitutionalObservationWitness":
        if self.current_claim_id == self.original_claim_id:
            raise ValueError("a witness requires a distinct current and original claim")
        if self.effective_available_at > self.verified_at_cut:
            raise ValueError("witness effective availability must not exceed its cut")
        return self


class InstitutionalObservationReference(_Strict):
    """Concrete link from one expected field to an observation and selected claim."""

    observation_id: UUID
    claim_id: UUID
    asset_id: NonEmptyStr
    manager_cik: NonEmptyStr
    report_id: UUID
    artifact_id: UUID
    row_id: UUID
    field_name: NonEmptyStr
    available_at: UTCDateTime
    persisted: bool = True

    @field_validator("manager_cik")
    @classmethod
    def reference_cik(cls, value: str) -> str:
        return normalize_cik(value)


class InstitutionalObservationEquivalentReuse(_Strict):
    """Reuse of an observation whose original claim is verified and equivalent at the cut."""

    observation_id: UUID
    current_claim_id: UUID
    original_claim_id: UUID
    asset_id: NonEmptyStr
    manager_cik: NonEmptyStr
    report_id: UUID
    artifact_id: UUID
    row_id: UUID
    field_name: NonEmptyStr
    available_at: UTCDateTime

    @field_validator("manager_cik")
    @classmethod
    def equivalent_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def equivalent_shape(self) -> "InstitutionalObservationEquivalentReuse":
        if self.current_claim_id == self.original_claim_id:
            raise ValueError("an equivalent reuse requires a distinct original claim")
        return self


class InstitutionalObservationClaimSelection(_Strict):
    """One explicitly verified current claim selected for a materialization row."""

    asset_id: NonEmptyStr
    manager_cik: NonEmptyStr
    report_id: UUID
    artifact_id: UUID
    row_id: UUID
    correspondence: SecInstitutionalRowCorrespondence
    verified_claim_ids: tuple[UUID, ...] = ()

    @field_validator("manager_cik")
    @classmethod
    def selection_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def selection_shape(self) -> "InstitutionalObservationClaimSelection":
        claim = self.correspondence
        if (
            claim.asset_id != self.asset_id
            or claim.manager_cik != self.manager_cik
            or claim.report_id != self.report_id
            or claim.artifact_id != self.artifact_id
            or claim.row_id != self.row_id
        ):
            raise ValueError("selected claim does not match the row selection")
        if len(set(self.verified_claim_ids)) != len(self.verified_claim_ids):
            raise ValueError("verified equivalent claims must be unique")
        if self.verified_claim_ids and claim.correspondence_id not in self.verified_claim_ids:
            raise ValueError("selected claim must belong to its verified equivalent set")
        return self


class InstitutionalObservationParentContext(_Strict):
    """Already-loaded parents for one selected row, reused by planning and normalization."""

    report: InstitutionalHoldingsReport
    artifact: InstitutionalHoldingsSemantics
    row: InstitutionalSemanticsRow

    @model_validator(mode="after")
    def parent_shape(self) -> "InstitutionalObservationParentContext":
        if (
            self.artifact.parent_report_id != self.report.report_id
            or self.artifact.manager_cik != self.report.manager_cik
            or self.artifact.cover_revision != self.report.cover_revision
            or self.artifact.information_table_revision != self.report.information_table_revision
            or self.row not in self.artifact.rows
        ):
            raise ValueError("observation parent context is inconsistent")
        return self


class InstitutionalObservationNormalizationContext(_Strict):
    """Bounded, typed context shared by materialization, normalization and traceability."""

    known_at: UTCDateTime
    selections: tuple[InstitutionalObservationClaimSelection, ...]
    parents: tuple[InstitutionalObservationParentContext, ...] = ()
    witnesses: tuple[InstitutionalObservationWitness, ...] = ()
    equivalent_reuses: tuple[InstitutionalObservationEquivalentReuse, ...] = ()
    write_allowed: bool = True

    @model_validator(mode="after")
    def context_shape(self) -> "InstitutionalObservationNormalizationContext":
        selection_keys = {
            (
                item.asset_id,
                item.manager_cik,
                item.report_id,
                item.artifact_id,
                item.row_id,
            )
            for item in self.selections
        }
        if len(selection_keys) != len(self.selections):
            raise ValueError("claim selections must be unique per row")
        claim_ids = {item.correspondence.correspondence_id for item in self.selections}
        if len(claim_ids) != len(self.selections):
            raise ValueError("a selected claim cannot serve multiple rows")
        selected = {
            (
                item.asset_id,
                item.manager_cik,
                item.report_id,
                item.artifact_id,
                item.row_id,
            ): item.correspondence.correspondence_id
            for item in self.selections
        }
        parent_keys = {(item.artifact.artifact_id, item.row.row_id) for item in self.parents}
        if len(parent_keys) != len(self.parents):
            raise ValueError("parent contexts must be unique per row")
        if self.selections and not parent_keys:
            raise ValueError("normalization selections require their parent context")
        for selection in self.selections:
            if (selection.artifact_id, selection.row_id) not in parent_keys:
                raise ValueError("every selection requires its parent context")
        for parent in self.parents:
            if parent.report.report_id != parent.artifact.parent_report_id:
                raise ValueError("parent report and artifact disagree")
        witness_keys = {
            (
                item.asset_id,
                item.manager_cik,
                item.report_id,
                item.artifact_id,
                item.row_id,
                item.field_name,
            )
            for item in self.witnesses
        }
        if len(witness_keys) != len(self.witnesses):
            raise ValueError("witnesses must be unique per row and field")
        for witness in self.witnesses:
            key = (
                witness.asset_id,
                witness.manager_cik,
                witness.report_id,
                witness.artifact_id,
                witness.row_id,
            )
            if selected.get(key) != witness.current_claim_id:
                raise ValueError("witness current claim must match its row selection")
            if witness.verified_at_cut != self.known_at:
                raise ValueError("witness cut must equal the normalization cut")
            if witness.effective_available_at > self.known_at:
                raise ValueError("witness effective availability must not exceed the cut")
        equivalent_keys = {
            (
                item.asset_id,
                item.manager_cik,
                item.report_id,
                item.artifact_id,
                item.row_id,
                item.field_name,
            )
            for item in self.equivalent_reuses
        }
        if len(equivalent_keys) != len(self.equivalent_reuses):
            raise ValueError("equivalent reuses must be unique per row and field")
        for reuse in self.equivalent_reuses:
            key = (
                reuse.asset_id,
                reuse.manager_cik,
                reuse.report_id,
                reuse.artifact_id,
                reuse.row_id,
            )
            if selected.get(key) != reuse.current_claim_id:
                raise ValueError("equivalent reuse current claim must match its selection")
            selection = next(
                (
                    item
                    for item in self.selections
                    if (
                        item.asset_id,
                        item.manager_cik,
                        item.report_id,
                        item.artifact_id,
                        item.row_id,
                    )
                    == key
                ),
                None,
            )
            if selection is None or reuse.original_claim_id not in selection.verified_claim_ids:
                raise ValueError("equivalent reuse original claim must be verified")
            if reuse.available_at > self.known_at:
                raise ValueError("equivalent reuse must be visible at the cut")
        return self


class InstitutionalObservationView(_Strict):
    """One observation with every persisted institutional parent verified.

    ``correspondence`` is the typed union of the two admissible proofs: the manual
    ``instrument-correspondence-v1`` declaration or the row-scoped
    ``sec-institutional-row-correspondence-v1`` claim. Their meaning is never merged.
    """

    observation: NormalizedObservation
    report: InstitutionalHoldingsReport
    artifact: InstitutionalHoldingsSemantics
    row: InstitutionalSemanticsRow
    correspondence: InstrumentCorrespondence | SecInstitutionalRowCorrespondence
    compatibility_notes: tuple[
        Literal["replayed_retrieval_time", "legacy_acceptance_timezone"], ...
    ] = ()

    @field_validator("compatibility_notes")
    @classmethod
    def ordered_unique_notes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("compatibility notes must not repeat")
        expected = tuple(
            note
            for note in ("replayed_retrieval_time", "legacy_acceptance_timezone")
            if note in value
        )
        if tuple(value) != expected:
            raise ValueError("compatibility notes must preserve canonical order")
        return value


class InstitutionalObservationQueryResult(_Strict):
    observations: tuple[InstitutionalObservationView, ...]
    total_matching: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def page(self) -> "InstitutionalObservationQueryResult":
        if self.total_matching < len(self.observations):
            raise ValueError("query counts are invalid")
        return self


InstitutionalObservationSummary.model_rebuild()
