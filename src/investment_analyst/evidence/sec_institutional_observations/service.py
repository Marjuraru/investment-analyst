"""PIT persistence for linked institutional observations."""

from collections import Counter
from collections.abc import Collection
from datetime import UTC, datetime
from uuid import UUID

from investment_analyst.core.models.observation import NormalizedObservation
from investment_analyst.evidence.instrument_correspondence.models import (
    InstrumentCorrespondence,
)
from investment_analyst.evidence.instrument_correspondence.repository import (
    InstrumentCorrespondenceRepository,
)
from investment_analyst.evidence.sec_institutional_correspondence.models import (
    SecInstitutionalRowCorrespondence,
)
from investment_analyst.evidence.sec_institutional_correspondence.repository import (
    SecInstitutionalRowCorrespondenceRepository,
)
from investment_analyst.evidence.sec_institutional_correspondence.service import (
    SecInstitutionalRowCorrespondenceService,
    resolve_visible_claims,
)
from investment_analyst.evidence.sec_institutional_holdings.models import (
    InstitutionalHoldingsReport,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
)
from investment_analyst.evidence.sec_institutional_semantics.models import (
    InstitutionalHoldingsSemantics,
    InstitutionalSemanticsRow,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    InstitutionalSemanticsRepository,
)
from investment_analyst.storage import RecordNotFoundError, StorageError

from .compatibility import (
    InstitutionalObservationCompatibilityError,
    compare_equivalent_reconstruction,
    compare_reconstruction,
    compare_witnessed_reconstruction,
    parse_record_key,
)
from .definitions import SOURCE_ID
from .models import (
    InstitutionalObservationClaimSelection,
    InstitutionalObservationEquivalentReuse,
    InstitutionalObservationNormalizationContext,
    InstitutionalObservationParentContext,
    InstitutionalObservationQuery,
    InstitutionalObservationQueryResult,
    InstitutionalObservationReference,
    InstitutionalObservationRequest,
    InstitutionalObservationSummary,
    InstitutionalObservationView,
    InstitutionalObservationWitness,
)
from .normalizer import normalize_row

_OBSERVATION_SELECTION_BATCH_SIZE = 256


class InstitutionalObservationLineageError(StorageError):
    """An observation cannot be reconstructed to its immutable parents."""


class InstitutionalObservationService:
    def __init__(self, storage, *, clock=lambda: datetime.now(UTC)) -> None:
        self._storage = storage
        self._clock = clock

    def normalize(
        self,
        request: InstitutionalObservationRequest,
        *,
        context: InstitutionalObservationNormalizationContext | None = None,
        claim_service: SecInstitutionalRowCorrespondenceService | None = None,
    ) -> InstitutionalObservationSummary:
        writable = not self._storage.read_only
        if not writable and (context is None or context.write_allowed):
            raise StorageError("institutional observation normalization requires writable storage")
        if context is not None:
            if context.known_at != request.known_at:
                raise StorageError("institutional observation context cut differs from the request")
            if not writable and context.write_allowed:
                raise StorageError("preflight normalization cannot write")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise StorageError("institutional observation clock must include timezone")
        now = now.astimezone(UTC)
        holdings = InstitutionalHoldingsRepository(self._storage.raw_records)
        semantics = InstitutionalSemanticsRepository(self._storage.raw_records)
        row_claims = SecInstitutionalRowCorrespondenceRepository(self._storage.raw_records)
        selections: dict[tuple[object, ...], InstitutionalObservationClaimSelection] = {}
        witnesses: dict[tuple[object, ...], InstitutionalObservationWitness] = {}
        equivalent_reuses: dict[tuple[object, ...], InstitutionalObservationEquivalentReuse] = {}
        if context is not None:
            selections = {
                (
                    item.asset_id,
                    item.manager_cik,
                    item.report_id,
                    item.artifact_id,
                    item.row_id,
                ): item
                for item in context.selections
            }
            witnesses = {
                (
                    item.asset_id,
                    item.manager_cik,
                    item.report_id,
                    item.artifact_id,
                    item.row_id,
                    item.field_name,
                ): item
                for item in context.witnesses
            }
            equivalent_reuses = {
                (
                    item.asset_id,
                    item.manager_cik,
                    item.report_id,
                    item.artifact_id,
                    item.row_id,
                    item.field_name,
                ): item
                for item in context.equivalent_reuses
            }
            correspondences = ()
        else:
            manual = InstrumentCorrespondenceRepository(self._storage.raw_records)
            correspondences = manual.list(known_at=request.known_at, asset_id=request.asset_id)
        created = reused = rows = linked = unlinked = values = generated = 0
        reports_missing = reports_not_enriched = 0
        skips: Counter[str] = Counter()
        used_witnesses: list[InstitutionalObservationWitness] = []
        references: list[InstitutionalObservationReference] = []
        if context is not None:
            return self._normalize_selected(
                request=request,
                context=context,
                now=now,
                witnesses=witnesses,
                equivalent_reuses=equivalent_reuses,
                used_witnesses=used_witnesses,
                references=references,
                claim_service=claim_service,
            )
        for report_id in sorted(request.report_ids, key=str):
            parent = holdings.get_report(report_id)
            if (
                parent is None
                or parent.manager_cik != request.manager_cik
                or parent.available_at > request.known_at
            ):
                reports_missing += 1
                skips["missing_report"] += 1
                continue
            item = semantics.get_for_parent(parent)
            if item is None:
                reports_not_enriched += 1
                skips["not_enriched"] += 1
                continue
            claims_by_row: dict[UUID, list] = {}
            if context is None:
                for claim in row_claims.list(
                    known_at=request.known_at,
                    artifact_id=item.artifact_id,
                    manager_cik=item.manager_cik,
                ):
                    claims_by_row.setdefault(claim.row_id, []).append(claim)
            for row in item.rows:
                rows += 1
                if item.report_period is None:
                    unlinked += 1
                    skips["missing_report_period"] += 1
                    continue
                if context is not None:
                    selection = selections.get(
                        (
                            request.asset_id,
                            request.manager_cik,
                            report_id,
                            item.artifact_id,
                            row.row_id,
                        )
                    )
                    if selection is None:
                        unlinked += 1
                        skips["row_not_selected"] += 1
                        continue
                    correspondence = selection.correspondence
                    if (
                        correspondence.asset_id != request.asset_id
                        or correspondence.manager_cik != request.manager_cik
                        or correspondence.report_id != report_id
                        or correspondence.artifact_id != item.artifact_id
                        or correspondence.row_id != row.row_id
                    ):
                        raise StorageError("normalization selection contradicts the visible row")
                else:
                    resolution = resolve_visible_claims(claims_by_row.get(row.row_id, ()))
                    if resolution.state == "resolved":
                        correspondence = resolution.correspondence
                    elif resolution.state != "absent":
                        unlinked += 1
                        skips[f"row_{resolution.state}"] += 1
                        continue
                    else:
                        same_cusip = [c for c in correspondences if c.cusip == row.cusip]
                        exact = [
                            c
                            for c in correspondences
                            if c.cusip == row.cusip
                            and c.title_of_class == row.title_of_class
                            and c.is_effective_on(item.report_period)
                        ]
                        if len(exact) != 1:
                            if len(exact) > 1:
                                reason = "ambiguous_correspondence"
                            elif any(c.is_effective_on(item.report_period) for c in same_cusip):
                                reason = "class_mismatch"
                            elif same_cusip:
                                reason = "outside_effective_period"
                            else:
                                reason = "missing_correspondence"
                            unlinked += 1
                            skips[reason] += 1
                            continue
                        correspondence = exact[0]
                linked += 1
                values += 1
                candidates = normalize_row(item, row, correspondence, normalized_at=now)
                if not candidates:
                    skips["unsupported_row"] += 1
                for candidate in candidates:
                    generated += 1
                    witness = (
                        witnesses.get(
                            (
                                request.asset_id,
                                request.manager_cik,
                                report_id,
                                item.artifact_id,
                                row.row_id,
                                candidate.field_name,
                            )
                        )
                        if context is not None
                        else None
                    )
                    if witness is not None:
                        try:
                            existing = self._witnessed_existing(
                                witness=witness,
                                candidate=candidate,
                                artifact=item,
                                report=parent,
                                row=row,
                                correspondence=correspondence,
                                request=request,
                            )
                        except InstitutionalObservationCompatibilityError as error:
                            raise StorageError(
                                "institutional observation witness conflicts"
                            ) from error
                        reused += 1
                        used_witnesses.append(witness)
                        references.append(
                            InstitutionalObservationReference(
                                observation_id=existing.observation_id,
                                claim_id=correspondence.correspondence_id,
                                asset_id=request.asset_id,
                                manager_cik=request.manager_cik,
                                report_id=report_id,
                                artifact_id=item.artifact_id,
                                row_id=row.row_id,
                                field_name=candidate.field_name,
                                available_at=existing.available_at,
                                persisted=True,
                            )
                        )
                        continue
                    try:
                        existing = self._storage.observations.get(candidate.observation_id)
                    except RecordNotFoundError:
                        if context is not None and not context.write_allowed:
                            created += 1
                            references.append(
                                InstitutionalObservationReference(
                                    observation_id=candidate.observation_id,
                                    claim_id=correspondence.correspondence_id,
                                    asset_id=request.asset_id,
                                    manager_cik=request.manager_cik,
                                    report_id=report_id,
                                    artifact_id=item.artifact_id,
                                    row_id=row.row_id,
                                    field_name=candidate.field_name,
                                    available_at=candidate.available_at,
                                    persisted=False,
                                )
                            )
                            continue
                        self._storage.observations.save(candidate)
                        created += 1
                        if context is not None:
                            references.append(
                                InstitutionalObservationReference(
                                    observation_id=candidate.observation_id,
                                    claim_id=correspondence.correspondence_id,
                                    asset_id=request.asset_id,
                                    manager_cik=request.manager_cik,
                                    report_id=report_id,
                                    artifact_id=item.artifact_id,
                                    row_id=row.row_id,
                                    field_name=candidate.field_name,
                                    available_at=candidate.available_at,
                                    persisted=True,
                                )
                            )
                    else:
                        try:
                            compare_reconstruction(
                                existing,
                                candidate,
                                existing_key=_record_key(existing),
                                candidate_key=_record_key(candidate),
                                artifact=item,
                                report=parent,
                                row=row,
                                correspondence=correspondence,
                            )
                        except InstitutionalObservationCompatibilityError as error:
                            raise StorageError(
                                "institutional observation identity conflicts"
                            ) from error
                        reused += 1
                        if context is not None:
                            references.append(
                                InstitutionalObservationReference(
                                    observation_id=existing.observation_id,
                                    claim_id=correspondence.correspondence_id,
                                    asset_id=request.asset_id,
                                    manager_cik=request.manager_cik,
                                    report_id=report_id,
                                    artifact_id=item.artifact_id,
                                    row_id=row.row_id,
                                    field_name=candidate.field_name,
                                    available_at=existing.available_at,
                                    persisted=True,
                                )
                            )
        return InstitutionalObservationSummary(
            asset_id=request.asset_id,
            known_at=request.known_at,
            normalized_at=now,
            reports_examined=len(request.report_ids),
            reports_missing=reports_missing,
            reports_not_enriched=reports_not_enriched,
            rows_examined=rows,
            rows_linked=linked,
            rows_unlinked=unlinked,
            values_examined=values,
            observations_generated=generated,
            observations_created=created,
            observations_reused=reused,
            skipped_by_reason=dict(skips),
            observation_witnesses=tuple(used_witnesses),
            observation_references=tuple(references),
        )

    def _witnessed_existing(
        self,
        *,
        witness: InstitutionalObservationWitness,
        candidate: NormalizedObservation,
        artifact: InstitutionalHoldingsSemantics,
        report: InstitutionalHoldingsReport,
        row: InstitutionalSemanticsRow,
        correspondence: InstrumentCorrespondence | SecInstitutionalRowCorrespondence,
        request: InstitutionalObservationRequest,
        claim_service: SecInstitutionalRowCorrespondenceService | None,
    ) -> NormalizedObservation:
        if not isinstance(correspondence, SecInstitutionalRowCorrespondence):
            raise StorageError("institutional observation witness requires a row claim")
        try:
            existing = self._storage.observations.get(witness.observation_id)
        except RecordNotFoundError as error:
            raise StorageError("witnessed observation is missing") from error
        if (
            existing.observation_id != witness.observation_id
            or existing.asset_id != witness.asset_id
            or existing.field_name != witness.field_name
            or existing.available_at > request.known_at
        ):
            raise StorageError("witnessed observation identity or cut conflicts")
        existing_key = _record_key(existing)
        candidate_key = _record_key(candidate)
        if existing_key["correspondence_id"] != str(witness.original_claim_id):
            raise StorageError("witnessed observation references another original claim")
        if candidate_key["correspondence_id"] != str(correspondence.correspondence_id):
            raise StorageError("witness current claim differs from its selection")
        row_claims = SecInstitutionalRowCorrespondenceRepository(self._storage.raw_records)
        original_claim = row_claims.get(witness.original_claim_id)
        if original_claim is None:
            raise StorageError("witness original claim is missing")
        service = claim_service or SecInstitutionalRowCorrespondenceService(self._storage)
        service.verify_lineage(correspondence)
        original = service.classify_lineage(original_claim)
        if original.state != "stale_availability":
            raise StorageError("witness original claim is not temporally stale")
        compare_witnessed_reconstruction(
            existing,
            candidate,
            existing_key=existing_key,
            candidate_key=candidate_key,
            original_claim=original_claim,
            current_claim=correspondence,
            artifact=artifact,
            report=report,
            row=row,
        )
        effective = max(correspondence.available_at, artifact.available_at, existing.available_at)
        if effective != witness.effective_available_at or effective > request.known_at:
            raise StorageError("witness effective availability conflicts")
        return existing

    def _equivalent_existing(
        self,
        *,
        reuse: InstitutionalObservationEquivalentReuse,
        candidate: NormalizedObservation,
        artifact: InstitutionalHoldingsSemantics,
        report: InstitutionalHoldingsReport,
        row: InstitutionalSemanticsRow,
        correspondence: SecInstitutionalRowCorrespondence,
        request: InstitutionalObservationRequest,
        claim_service: SecInstitutionalRowCorrespondenceService | None,
    ) -> NormalizedObservation:
        try:
            existing = self._storage.observations.get(reuse.observation_id)
        except RecordNotFoundError as error:
            raise StorageError("equivalent observation is missing") from error
        if (
            existing.observation_id != reuse.observation_id
            or existing.asset_id != reuse.asset_id
            or existing.field_name != reuse.field_name
            or existing.available_at > request.known_at
        ):
            raise StorageError("equivalent observation identity or cut conflicts")
        existing_key = _record_key(existing)
        candidate_key = _record_key(candidate)
        if existing_key["correspondence_id"] != str(reuse.original_claim_id):
            raise StorageError("equivalent observation references another original claim")
        if candidate_key["correspondence_id"] != str(correspondence.correspondence_id):
            raise StorageError("equivalent current claim differs from its selection")
        row_claims = SecInstitutionalRowCorrespondenceRepository(self._storage.raw_records)
        original_claim = row_claims.get(reuse.original_claim_id)
        if original_claim is None:
            raise StorageError("equivalent original claim is missing")
        service = claim_service or SecInstitutionalRowCorrespondenceService(self._storage)
        service.verify_lineage(correspondence)
        if service.classify_lineage(original_claim).state != "verified":
            raise StorageError("equivalent original claim is not strictly verified")
        compare_equivalent_reconstruction(
            existing,
            candidate,
            existing_key=existing_key,
            candidate_key=candidate_key,
            original_claim=original_claim,
            current_claim=correspondence,
            artifact=artifact,
            report=report,
            row=row,
        )
        return existing

    def _normalize_selected(
        self,
        *,
        request: InstitutionalObservationRequest,
        context: InstitutionalObservationNormalizationContext,
        now: datetime,
        witnesses: dict[tuple[object, ...], InstitutionalObservationWitness],
        equivalent_reuses: dict[tuple[object, ...], InstitutionalObservationEquivalentReuse],
        used_witnesses: list[InstitutionalObservationWitness],
        references: list[InstitutionalObservationReference],
        claim_service: SecInstitutionalRowCorrespondenceService | None,
    ) -> InstitutionalObservationSummary:
        """Normalize only the explicitly selected rows using their supplied parents."""
        parents = {(item.artifact.artifact_id, item.row.row_id): item for item in context.parents}
        created = reused = rows = linked = values = generated = 0
        skips: Counter[str] = Counter()
        reports: set[UUID] = set()
        for selection in context.selections:
            if (
                selection.asset_id != request.asset_id
                or selection.manager_cik != request.manager_cik
                or selection.report_id not in request.report_ids
            ):
                raise StorageError("normalization selection is outside the request")
            parent = parents.get((selection.artifact_id, selection.row_id))
            if parent is None:
                raise StorageError("normalization selection parent is missing")
            reports.add(selection.report_id)
            rows += 1
            linked += 1
            values += 1
            candidates = normalize_row(
                parent.artifact,
                parent.row,
                selection.correspondence,
                normalized_at=now,
            )
            if not candidates:
                skips["unsupported_row"] += 1
            for candidate in candidates:
                generated += 1
                witness = witnesses.get(
                    (
                        selection.asset_id,
                        request.manager_cik,
                        selection.report_id,
                        selection.artifact_id,
                        selection.row_id,
                        candidate.field_name,
                    )
                )
                equivalent_reuse = equivalent_reuses.get(
                    (
                        selection.asset_id,
                        request.manager_cik,
                        selection.report_id,
                        selection.artifact_id,
                        selection.row_id,
                        candidate.field_name,
                    )
                )
                action = self._process_candidate(
                    request=request,
                    candidate=candidate,
                    artifact=parent.artifact,
                    report=parent.report,
                    row=parent.row,
                    correspondence=selection.correspondence,
                    context=context,
                    witness=witness,
                    equivalent_reuse=equivalent_reuse,
                    used_witnesses=used_witnesses,
                    references=references,
                    claim_service=claim_service,
                )
                if action == "created":
                    created += 1
                else:
                    reused += 1
        return InstitutionalObservationSummary(
            asset_id=request.asset_id,
            known_at=request.known_at,
            normalized_at=now,
            reports_examined=len(reports),
            reports_missing=0,
            reports_not_enriched=0,
            rows_examined=rows,
            rows_linked=linked,
            rows_unlinked=0,
            values_examined=values,
            observations_generated=generated,
            observations_created=created,
            observations_reused=reused,
            skipped_by_reason=dict(skips),
            observation_witnesses=tuple(used_witnesses),
            observation_references=tuple(references),
        )

    def _process_candidate(
        self,
        *,
        request: InstitutionalObservationRequest,
        candidate: NormalizedObservation,
        artifact: InstitutionalHoldingsSemantics,
        report: InstitutionalHoldingsReport,
        row: InstitutionalSemanticsRow,
        correspondence: SecInstitutionalRowCorrespondence,
        context: InstitutionalObservationNormalizationContext,
        witness: InstitutionalObservationWitness | None,
        equivalent_reuse: InstitutionalObservationEquivalentReuse | None,
        used_witnesses: list[InstitutionalObservationWitness],
        references: list[InstitutionalObservationReference],
        claim_service: SecInstitutionalRowCorrespondenceService | None,
    ) -> str:
        if equivalent_reuse is not None:
            existing = self._equivalent_existing(
                reuse=equivalent_reuse,
                candidate=candidate,
                artifact=artifact,
                report=report,
                row=row,
                correspondence=correspondence,
                request=request,
                claim_service=claim_service,
            )
            references.append(
                InstitutionalObservationReference(
                    observation_id=existing.observation_id,
                    claim_id=equivalent_reuse.original_claim_id,
                    asset_id=request.asset_id,
                    manager_cik=request.manager_cik,
                    report_id=report.report_id,
                    artifact_id=artifact.artifact_id,
                    row_id=row.row_id,
                    field_name=candidate.field_name,
                    available_at=existing.available_at,
                    persisted=True,
                )
            )
            return "reused"
        if witness is not None:
            existing = self._witnessed_existing(
                witness=witness,
                candidate=candidate,
                artifact=artifact,
                report=report,
                row=row,
                correspondence=correspondence,
                request=request,
                claim_service=claim_service,
            )
            used_witnesses.append(witness)
            references.append(
                InstitutionalObservationReference(
                    observation_id=existing.observation_id,
                    claim_id=correspondence.correspondence_id,
                    asset_id=request.asset_id,
                    manager_cik=request.manager_cik,
                    report_id=report.report_id,
                    artifact_id=artifact.artifact_id,
                    row_id=row.row_id,
                    field_name=candidate.field_name,
                    available_at=existing.available_at,
                    persisted=True,
                )
            )
            return "reused"
        try:
            existing = self._storage.observations.get(candidate.observation_id)
        except RecordNotFoundError:
            if not context.write_allowed:
                references.append(
                    InstitutionalObservationReference(
                        observation_id=candidate.observation_id,
                        claim_id=correspondence.correspondence_id,
                        asset_id=request.asset_id,
                        manager_cik=request.manager_cik,
                        report_id=report.report_id,
                        artifact_id=artifact.artifact_id,
                        row_id=row.row_id,
                        field_name=candidate.field_name,
                        available_at=candidate.available_at,
                        persisted=False,
                    )
                )
                return "created"
            self._storage.observations.save(candidate)
            references.append(
                InstitutionalObservationReference(
                    observation_id=candidate.observation_id,
                    claim_id=correspondence.correspondence_id,
                    asset_id=request.asset_id,
                    manager_cik=request.manager_cik,
                    report_id=report.report_id,
                    artifact_id=artifact.artifact_id,
                    row_id=row.row_id,
                    field_name=candidate.field_name,
                    available_at=candidate.available_at,
                    persisted=True,
                )
            )
            return "created"
        try:
            compare_reconstruction(
                existing,
                candidate,
                existing_key=_record_key(existing),
                candidate_key=_record_key(candidate),
                artifact=artifact,
                report=report,
                row=row,
                correspondence=correspondence,
            )
        except InstitutionalObservationCompatibilityError as error:
            raise StorageError("institutional observation identity conflicts") from error
        references.append(
            InstitutionalObservationReference(
                observation_id=existing.observation_id,
                claim_id=correspondence.correspondence_id,
                asset_id=request.asset_id,
                manager_cik=request.manager_cik,
                report_id=report.report_id,
                artifact_id=artifact.artifact_id,
                row_id=row.row_id,
                field_name=candidate.field_name,
                available_at=existing.available_at,
                persisted=True,
            )
        )
        return "reused"

    def plan_normalization(
        self,
        request: InstitutionalObservationRequest,
        selections: tuple[InstitutionalObservationClaimSelection, ...],
        *,
        write_allowed: bool,
        parents: tuple[InstitutionalObservationParentContext, ...] | None = None,
        claim_service: SecInstitutionalRowCorrespondenceService | None = None,
    ) -> InstitutionalObservationNormalizationContext:
        """Plan witnesses and direct links without writing or altering any persisted object."""
        holdings = InstitutionalHoldingsRepository(self._storage.raw_records)
        semantics = InstitutionalSemanticsRepository(self._storage.raw_records)
        row_claims = SecInstitutionalRowCorrespondenceRepository(self._storage.raw_records)
        claim_service = claim_service or SecInstitutionalRowCorrespondenceService(self._storage)
        observation_cache: dict[tuple[str, str], tuple[NormalizedObservation, ...]] = {}
        parents_by_key = {
            (item.artifact.artifact_id, item.row.row_id): item for item in (parents or ())
        }
        parents_used: list[InstitutionalObservationParentContext] = []
        witnesses: list[InstitutionalObservationWitness] = []
        equivalent_reuses: list[InstitutionalObservationEquivalentReuse] = []
        for selection in selections:
            if (
                selection.asset_id != request.asset_id
                or selection.manager_cik != request.manager_cik
                or selection.report_id not in request.report_ids
            ):
                raise StorageError("normalization plan selection is outside the request")
            parent = parents_by_key.get((selection.artifact_id, selection.row_id))
            if parent is None:
                report = holdings.get_report(selection.report_id)
                artifact = semantics.get(selection.artifact_id)
                if report is None or artifact is None:
                    raise StorageError("normalization plan parent is missing")
                row = next(
                    (item for item in artifact.rows if item.row_id == selection.row_id),
                    None,
                )
                if row is None:
                    raise StorageError("normalization plan row is missing")
                parent = InstitutionalObservationParentContext(
                    report=report, artifact=artifact, row=row
                )
            parents_used.append(parent)
            report = parent.report
            artifact = parent.artifact
            row = parent.row
            claim_service.verify_lineage(selection.correspondence)
            candidates = normalize_row(
                artifact,
                row,
                selection.correspondence,
                normalized_at=request.known_at,
            )
            for candidate in candidates:
                cache_key = (selection.asset_id, candidate.field_name)
                if cache_key not in observation_cache:
                    observation_cache[cache_key] = tuple(
                        self._storage.observations.list(
                            asset_id=selection.asset_id,
                            source_id=SOURCE_ID,
                            field_name=candidate.field_name,
                            available_to=request.known_at,
                        )
                    )
                direct = False
                stale_compatible: list[
                    tuple[
                        datetime,
                        str,
                        NormalizedObservation,
                        SecInstitutionalRowCorrespondence,
                        datetime,
                    ]
                ] = []
                verified_compatible: list[
                    tuple[
                        datetime,
                        str,
                        NormalizedObservation,
                        SecInstitutionalRowCorrespondence,
                    ]
                ] = []
                for existing in observation_cache[cache_key]:
                    existing_key = _record_key(existing)
                    if (
                        existing_key["artifact_id"] != str(artifact.artifact_id)
                        or existing_key["row_id"] != str(row.row_id)
                        or existing_key["report_id"] != str(selection.report_id)
                        or existing_key["manager_cik"] != request.manager_cik
                        or existing_key["field_name"] != candidate.field_name
                    ):
                        continue
                    if existing_key["correspondence_id"] == str(
                        selection.correspondence.correspondence_id
                    ):
                        direct = True
                        continue
                    original_claim = row_claims.get(UUID(existing_key["correspondence_id"]))
                    if original_claim is None:
                        raise StorageError("normalization plan observation claim is missing")
                    classification = claim_service.classify_lineage(original_claim)
                    if classification.state == "invalid":
                        raise StorageError("normalization plan observation claim is invalid")
                    if classification.state == "verified":
                        if (
                            selection.verified_claim_ids
                            and original_claim.correspondence_id not in selection.verified_claim_ids
                        ):
                            raise StorageError(
                                "normalization plan verified claim is outside the selected set"
                            )
                        compare_equivalent_reconstruction(
                            existing,
                            candidate,
                            existing_key=existing_key,
                            candidate_key=_record_key(candidate),
                            original_claim=original_claim,
                            current_claim=selection.correspondence,
                            artifact=artifact,
                            report=report,
                            row=row,
                        )
                        verified_compatible.append(
                            (
                                existing.available_at,
                                str(existing.observation_id),
                                existing,
                                original_claim,
                            )
                        )
                        continue
                    compare_witnessed_reconstruction(
                        existing,
                        candidate,
                        existing_key=existing_key,
                        candidate_key=_record_key(candidate),
                        original_claim=original_claim,
                        current_claim=selection.correspondence,
                        artifact=artifact,
                        report=report,
                        row=row,
                    )
                    effective = max(
                        selection.correspondence.available_at,
                        artifact.available_at,
                        existing.available_at,
                    )
                    if effective > request.known_at:
                        raise StorageError("normalization plan witness exceeds the cut")
                    stale_compatible.append(
                        (
                            existing.available_at,
                            str(existing.observation_id),
                            existing,
                            original_claim,
                            effective,
                        )
                    )
                if direct:
                    continue
                if verified_compatible:
                    verified_compatible.sort(key=lambda item: (item[0], item[1]))
                    _, _, existing, original_claim = verified_compatible[0]
                    equivalent_reuses.append(
                        InstitutionalObservationEquivalentReuse(
                            observation_id=existing.observation_id,
                            current_claim_id=selection.correspondence.correspondence_id,
                            original_claim_id=original_claim.correspondence_id,
                            asset_id=selection.asset_id,
                            manager_cik=selection.manager_cik,
                            report_id=selection.report_id,
                            artifact_id=selection.artifact_id,
                            row_id=selection.row_id,
                            field_name=candidate.field_name,
                            available_at=existing.available_at,
                        )
                    )
                    continue
                if not stale_compatible:
                    continue
                stale_compatible.sort(key=lambda item: (item[0], item[1]))
                _, _, existing, original_claim, effective = stale_compatible[0]
                witnesses.append(
                    InstitutionalObservationWitness(
                        current_claim_id=selection.correspondence.correspondence_id,
                        observation_id=existing.observation_id,
                        original_claim_id=original_claim.correspondence_id,
                        asset_id=selection.asset_id,
                        manager_cik=selection.manager_cik,
                        report_id=selection.report_id,
                        artifact_id=selection.artifact_id,
                        row_id=selection.row_id,
                        field_name=candidate.field_name,
                        verified_at_cut=request.known_at,
                        effective_available_at=effective,
                    )
                )
        return InstitutionalObservationNormalizationContext(
            known_at=request.known_at,
            selections=selections,
            parents=tuple(parents_used),
            witnesses=tuple(witnesses),
            equivalent_reuses=tuple(equivalent_reuses),
            write_allowed=write_allowed,
        )

    def list_for_manager(
        self,
        *,
        asset_id: str,
        manager_cik: str,
        known_at: datetime,
        field_name: str | None = None,
    ) -> tuple[object, ...]:
        """Return exactly the manager's observations at one cut before hydrating models.

        Identity selection by the closed-set ``manager_cik`` precedes hydration:
        only candidates of this manager are parsed into models.
        """
        from investment_analyst.evidence.sec_documents.models import normalize_cik

        manager = normalize_cik(manager_cik)
        candidate_ids = self._storage.observations.list_ids_for_manager_observation_references(
            asset_id=asset_id, available_to=known_at
        )
        selected_ids = self._select_manager_observation_ids(candidate_ids, manager=manager)
        if field_name is not None:
            selected_ids = [
                observation_id
                for observation_id in selected_ids
                if self._observation_field_name(observation_id) == field_name
            ]
        return tuple(self.observation_ids_for_references(selected_ids).values())

    def _observation_field_name(self, observation_id: UUID) -> str | None:
        return self._storage.observations.field_name(observation_id)

    def _select_manager_observation_ids(
        self, candidate_ids: list[UUID], *, manager: str
    ) -> list[UUID]:
        """Select candidate IDs whose persisted lineage declares exactly this manager."""
        try:
            return self._storage.observations.select_ids_for_manager_observation_references(
                candidate_ids, manager=manager
            )
        except RecordNotFoundError as error:
            raise InstitutionalObservationLineageError(
                "selected institutional observation is absent"
            ) from error

    def observation_ids_for_references(
        self,
        observation_ids: Collection[UUID],
    ) -> dict[UUID, object]:
        """Hydrate exactly the declared observation references without scanning history."""
        ordered_ids = tuple(dict.fromkeys(observation_ids))
        if not ordered_ids:
            return {}
        from investment_analyst.storage.errors import RecordNotFoundError

        resolved: dict[UUID, object] = {}
        for offset in range(0, len(ordered_ids), _OBSERVATION_SELECTION_BATCH_SIZE):
            batch_ids = ordered_ids[offset : offset + _OBSERVATION_SELECTION_BATCH_SIZE]
            try:
                records = self._storage.observations.get_many(batch_ids)
            except RecordNotFoundError as error:
                raise InstitutionalObservationLineageError(
                    "selected institutional observation is absent"
                ) from error
            for observation_id in batch_ids:
                try:
                    resolved[observation_id] = records[observation_id]
                except KeyError as error:
                    raise InstitutionalObservationLineageError(
                        "selected institutional observation is absent"
                    ) from error
        return resolved

    def query(self, query: InstitutionalObservationQuery) -> InstitutionalObservationQueryResult:
        if not self._storage.read_only:
            raise StorageError("institutional observation query requires read-only storage")
        if query.manager_cik is not None:
            observations = tuple(
                self.list_for_manager(
                    asset_id=query.asset_id,
                    manager_cik=query.manager_cik,
                    known_at=query.known_at,
                    field_name=query.field_name,
                )
            )
        else:
            observations = tuple(
                self._storage.observations.list(
                    asset_id=query.asset_id,
                    source_id=SOURCE_ID,
                    field_name=query.field_name,
                    available_to=query.known_at,
                )
            )
        keys = {item.observation_id: _record_key(item) for item in observations}
        artifact_ids = {UUID(key["artifact_id"]) for key in keys.values()}
        correspondence_ids = {UUID(key["correspondence_id"]) for key in keys.values()}
        report_ids = {UUID(key["report_id"]) for key in keys.values()}
        artifacts = _resolve_all(
            artifact_ids,
            InstitutionalSemanticsRepository(self._storage.raw_records).get,
            "artifact",
        )
        correspondences = self._resolve_correspondences(correspondence_ids)
        reports = _resolve_all(
            report_ids,
            InstitutionalHoldingsRepository(self._storage.raw_records).get_report,
            "report",
        )
        verified = tuple(
            _verified_view(
                observation,
                keys[observation.observation_id],
                artifacts,
                reports,
                correspondences,
            )
            for observation in observations
        )
        matching = tuple(
            view
            for view in verified
            if (query.report_id is None or view.report.report_id == query.report_id)
            and (query.manager_cik is None or view.artifact.manager_cik == query.manager_cik)
            and (query.cusip is None or view.row.cusip == query.cusip)
        )
        page = matching[query.offset : query.offset + query.limit]
        return InstitutionalObservationQueryResult(
            observations=page,
            total_matching=len(matching),
            truncated=query.offset + len(page) < len(matching),
        )

    def _resolve_correspondences(self, ids) -> dict:
        """Resolve each identifier against both repositories, requiring exactly one parent."""
        manual = InstrumentCorrespondenceRepository(self._storage.raw_records)
        row_claims = SecInstitutionalRowCorrespondenceRepository(self._storage.raw_records)
        resolved = {}
        for identifier in ids:
            declared = manual.get(identifier)
            scoped = row_claims.get(identifier)
            if (declared is None) == (scoped is None):
                raise InstitutionalObservationLineageError(
                    "institutional observation correspondence parent is missing or duplicated"
                )
            resolved[identifier] = declared if declared is not None else scoped
        return resolved


def observation_lineage_key(observation) -> dict[str, str | None]:
    """Return the canonical lineage key of one institutional observation for verification."""
    return _record_key(observation)


def _record_key(observation) -> dict[str, str | None]:
    if observation.source.source_id != SOURCE_ID:
        raise InstitutionalObservationLineageError("institutional observation source is invalid")
    try:
        return parse_record_key(observation)
    except InstitutionalObservationCompatibilityError as error:
        raise InstitutionalObservationLineageError(str(error)) from error


def _resolve_all(ids, resolver, kind: str):
    resolved = {}
    for identifier in ids:
        item = resolver(identifier)
        if item is None:
            raise InstitutionalObservationLineageError(
                f"institutional observation {kind} is missing"
            )
        resolved[identifier] = item
    return resolved


def _verified_view(observation, key, artifacts, reports, correspondences):
    artifact = artifacts[UUID(key["artifact_id"])]
    report = reports[UUID(key["report_id"])]
    correspondence = correspondences[UUID(key["correspondence_id"])]
    row = next((item for item in artifact.rows if str(item.row_id) == key["row_id"]), None)
    if row is None:
        raise InstitutionalObservationLineageError("institutional observation row is missing")
    if (
        observation.raw_record_id != artifact.raw_record_id
        or observation.asset_id != correspondence.asset_id
        or artifact.parent_report_id != report.report_id
        or artifact.manager_cik != report.manager_cik
        or artifact.cover_revision != report.cover_revision
        or artifact.information_table_revision != report.information_table_revision
        or key["report_id"] != str(report.report_id)
        or key["manager_cik"] != artifact.manager_cik
        or key["correspondence_id"] != str(correspondence.correspondence_id)
        or key["cover_revision_id"] != str(artifact.cover_revision.revision_id)
        or key["cover_content_sha256"] != artifact.cover_revision.content_sha256
        or key["information_table_revision_id"]
        != str(artifact.information_table_revision.revision_id)
        or key["information_table_content_sha256"]
        != artifact.information_table_revision.content_sha256
        or key["cusip"] != row.cusip
        or key["title_of_class"] != row.title_of_class
        or key["put_call"] != row.put_call
        or key["quantity_type"] != row.quantity_type
    ):
        raise InstitutionalObservationLineageError("institutional observation lineage conflicts")
    expected = {
        candidate.field_name: candidate
        for candidate in normalize_row(
            artifact, row, correspondence, normalized_at=observation.normalized_at
        )
    }.get(observation.field_name)
    if expected is None or key["field_name"] != observation.field_name:
        raise InstitutionalObservationLineageError("institutional observation content conflicts")
    try:
        compatibility_notes = compare_reconstruction(
            observation,
            expected,
            existing_key=key,
            candidate_key=_record_key(expected),
            artifact=artifact,
            report=report,
            row=row,
            correspondence=correspondence,
        )
    except InstitutionalObservationCompatibilityError as error:
        raise InstitutionalObservationLineageError(str(error)) from error
    return InstitutionalObservationView(
        observation=observation,
        report=report,
        artifact=artifact,
        row=row,
        correspondence=correspondence,
        compatibility_notes=compatibility_notes,
    )
