"""Directed, resumable materialization of institutional observations from the manager universe.

One execution resolves the latest manager-universe snapshot available at the requested cut, selects
the same deterministic page of already selected managers as ``SEC-CORPUS-28`` while preserving each
candidate's own ``(asset_id, cusip, manager_cik, report_period)`` tuple, selects strictly verified
row-scoped correspondences, and completes the integrated observation layer for every asset present
in the page. No network call is performed and the workspace is opened exactly once.

The preflight mode executes the same selection and all traceability checks without writing. A
witness only links a current verified claim to an older observation of the same row; it never
certifies the old claim's historical availability.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.application.sec_institutional_observation_materialization_models import (
    MaterializationObservationWitness,
    SecInstitutionalMaterializationCandidateSummary,
    SecInstitutionalMaterializationRunSummary,
    SecInstitutionalObservationMaterializationRequest,
    SecInstitutionalObservationMaterializationSummary,
)
from investment_analyst.application.sec_institutional_universe import (
    resolve_catalog_sec_cusip_mappings,
)
from investment_analyst.evidence.sec_institutional_correspondence.models import (
    SecInstitutionalRowCorrespondence,
    same_evidence,
)
from investment_analyst.evidence.sec_institutional_correspondence.repository import (
    SecInstitutionalRowCorrespondenceRepository,
)
from investment_analyst.evidence.sec_institutional_correspondence.service import (
    SecInstitutionalRowCorrespondenceService,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
)
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationClaimSelection,
    InstitutionalObservationParentContext,
    InstitutionalObservationRequest,
    InstitutionalObservationSummary,
)
from investment_analyst.evidence.sec_institutional_observations.normalizer import normalize_row
from investment_analyst.evidence.sec_institutional_observations.service import (
    InstitutionalObservationService,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    InstitutionalSemanticsRepository,
)
from investment_analyst.evidence.sec_institutional_universe.models import (
    Sec13FDataSetRevision,
    Sec13FManagerCandidate,
    Sec13FManagerUniverseSnapshot,
)
from investment_analyst.evidence.sec_institutional_universe.repository import (
    SecInstitutionalUniverseRepository,
)
from investment_analyst.workspace.models import WorkspaceAccessMode

MAX_REPORT_IDS_PER_OBSERVATION_REQUEST = 20
"""The integrated observation service accepts at most twenty report identifiers per request."""

MISSING_UNIVERSE_INSTRUCTION = (
    "no Form 13F manager universe snapshot is available at the requested cut; "
    "run scripts/refresh_sec_institutional_manager_universe.py first"
)


class SecInstitutionalObservationMaterializationError(RuntimeError):
    """A directed materialization cannot preserve its evidence contract."""


@dataclass(frozen=True, slots=True)
class MaterializationTarget:
    """One manager and reported period of the page with its exact candidate tuples."""

    manager_cik: str
    manager_name: str
    report_period: date
    candidates: tuple[Sec13FManagerCandidate, ...]


def plan_materialization_page(
    snapshot: Sec13FManagerUniverseSnapshot, *, offset: int, limit: int
) -> tuple[MaterializationTarget, ...]:
    """Select the deterministic manager page while preserving candidate tuples exactly.

    The ordering ``(selection_rank, asset_id, manager_cik, report_period)`` and the deduplication
    key ``(manager_cik, report_period)`` are the ones already integrated by ``SEC-CORPUS-28``; each
    manager and period keeps its own candidates, so no cartesian product is ever formed.
    """
    if offset < 0 or limit < 1:
        raise SecInstitutionalObservationMaterializationError("invalid materialization page bounds")
    ordered = sorted(
        (item for item in snapshot.candidates if item.is_selected),
        key=lambda item: (
            item.selection_rank,
            item.asset_id,
            item.manager_cik,
            item.report_period,
        ),
    )
    grouped: dict[tuple[str, date], list[Sec13FManagerCandidate]] = {}
    for candidate in ordered:
        grouped.setdefault((candidate.manager_cik, candidate.report_period), []).append(candidate)
    targets = tuple(
        MaterializationTarget(
            manager_cik=candidates[0].manager_cik,
            manager_name=candidates[0].manager_name,
            report_period=report_period,
            candidates=tuple(candidates),
        )
        for (_, report_period), candidates in grouped.items()
    )
    return targets[offset : offset + limit]


class SecInstitutionalObservationMaterializationApplication:
    """Isolated application edge for directed institutional-observation materialization."""

    def __init__(
        self,
        runtime: ApplicationRuntime,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._runtime = runtime
        self._clock = clock

    @classmethod
    def create_default(cls) -> SecInstitutionalObservationMaterializationApplication:
        return cls(ApplicationRuntime.create_default())

    def materialize_with_storage(
        self,
        storage,
        request: SecInstitutionalObservationMaterializationRequest,
        *,
        snapshot: Sec13FManagerUniverseSnapshot | None = None,
        revision: Sec13FDataSetRevision | None = None,
        preflight: bool = False,
    ) -> SecInstitutionalObservationMaterializationSummary:
        """Select, verify and optionally write one page under an open storage connection."""
        if not preflight and storage.read_only:
            raise SecInstitutionalObservationMaterializationError(
                "materialization requires writable storage"
            )
        catalog_cusips = resolve_catalog_sec_cusip_mappings(self._runtime.catalog)
        if snapshot is None or revision is None:
            snapshot, revision = _resolve_snapshot(storage, request.known_at)
        targets = plan_materialization_page(
            snapshot, offset=request.manager_offset, limit=request.manager_limit
        )
        correspondences = SecInstitutionalRowCorrespondenceService(storage)
        repository = SecInstitutionalRowCorrespondenceRepository(storage.raw_records)
        holdings = InstitutionalHoldingsRepository(storage.raw_records)
        semantics = InstitutionalSemanticsRepository(storage.raw_records)
        observations = InstitutionalObservationService(storage, clock=self._clock)
        recorded_at = self._now()
        candidates: list[SecInstitutionalMaterializationCandidateSummary] = []
        runs: list[SecInstitutionalMaterializationRunSummary] = []
        selections: list[InstitutionalObservationClaimSelection] = []
        outcomes: list[InstitutionalObservationSummary] = []
        for target in targets:
            target_candidates, target_runs, target_selections, target_outcomes = self._run_target(
                target=target,
                request=request,
                snapshot_id=snapshot.snapshot_id,
                dataset_revision_id=revision.revision_id,
                snapshot_available_at=snapshot.available_at,
                holdings=holdings,
                semantics=semantics,
                repository=repository,
                correspondences=correspondences,
                observations=observations,
                catalog_cusips=catalog_cusips,
                recorded_at=recorded_at,
                preflight=preflight,
            )
            candidates.extend(target_candidates)
            runs.extend(target_runs)
            selections.extend(target_selections)
            outcomes.extend(target_outcomes)
        witnesses = tuple(
            MaterializationObservationWitness(**item.model_dump())
            for outcome in outcomes
            for item in outcome.observation_witnesses
        )
        failed = any(item.state == "failed" for item in candidates) or any(
            item.claims_ambiguous for item in candidates
        )
        traceability = (
            False
            if failed
            else _verify_context_traceability(
                storage=storage,
                selections=tuple(selections),
                outcomes=tuple(outcomes),
                known_at=request.known_at,
                preflight=preflight,
            )
        )
        references = tuple(item for outcome in outcomes for item in outcome.observation_references)
        return SecInstitutionalObservationMaterializationSummary(
            mode="preflight" if preflight else "materialized",
            request=request,
            effective_known_at=request.known_at,
            snapshot_id=snapshot.snapshot_id,
            snapshot_raw_record_id=snapshot.raw_record_id,
            dataset_revision_id=revision.revision_id,
            dataset_sha256=snapshot.dataset_sha256,
            snapshot_period_start=snapshot.period_start,
            snapshot_period_end=snapshot.period_end,
            snapshot_available_at=snapshot.available_at,
            universe_selected_manager_count=snapshot.selected_manager_count,
            universe_coverage_complete=snapshot.coverage_complete,
            page_manager_count=len(targets),
            candidate_count=len(candidates),
            candidates=tuple(candidates),
            runs=tuple(runs),
            claims_created=sum(item.claims_created for item in candidates),
            claims_reused=sum(item.claims_reused for item in candidates),
            claims_ambiguous=sum(item.claims_ambiguous for item in candidates),
            observations_created=sum(item.observations_created for item in runs),
            observations_reused=sum(item.observations_reused for item in runs),
            failed_candidates=sum(item.state == "failed" for item in candidates),
            failed_runs=sum(item.state == "failed" for item in runs),
            observation_witnesses=witnesses,
            observation_references=references,
            traceability_verified=traceability,
        )

    def materialize(
        self,
        request: SecInstitutionalObservationMaterializationRequest,
        *,
        location: StorageLocationRequest | None = None,
        preflight: bool = False,
    ) -> SecInstitutionalObservationMaterializationSummary:
        """Run one bounded page under a single read-only or read-write connection."""
        storage_request = location or StorageLocationRequest()
        access_mode = WorkspaceAccessMode.READ_ONLY if preflight else WorkspaceAccessMode.READ_WRITE
        with self._runtime.open_storage(storage_request, access_mode=access_mode) as storage:
            return self.materialize_with_storage(
                storage,
                request,
                preflight=preflight,
            )

    def _run_target(
        self,
        *,
        target: MaterializationTarget,
        request: SecInstitutionalObservationMaterializationRequest,
        snapshot_id: UUID,
        dataset_revision_id: UUID,
        snapshot_available_at: datetime,
        holdings: InstitutionalHoldingsRepository,
        semantics: InstitutionalSemanticsRepository,
        repository: SecInstitutionalRowCorrespondenceRepository,
        correspondences: SecInstitutionalRowCorrespondenceService,
        observations: InstitutionalObservationService,
        catalog_cusips: dict[str, str],
        recorded_at: datetime,
        preflight: bool,
    ) -> tuple[
        list[SecInstitutionalMaterializationCandidateSummary],
        list[SecInstitutionalMaterializationRunSummary],
        list[InstitutionalObservationClaimSelection],
        list[InstitutionalObservationSummary],
    ]:
        """Select verified claims for one manager/period page and complete its observations."""
        reports = [
            report
            for report in holdings.list_reports(
                manager_cik=target.manager_cik, known_at=request.known_at
            )
            if report.report_period == target.report_period
        ]
        artifacts = {
            report.report_id: artifact
            for report in reports
            if (artifact := semantics.get_for_parent(report)) is not None
        }
        artifact_ids = tuple(dict.fromkeys(artifact.artifact_id for artifact in artifacts.values()))
        claims_by_row: dict[tuple[UUID, UUID], list[SecInstitutionalRowCorrespondence]] = {}
        for claim in repository.list_for_artifacts(
            known_at=request.known_at,
            artifact_ids=artifact_ids,
            manager_cik=target.manager_cik,
        ):
            claims_by_row.setdefault((claim.artifact_id, claim.row_id), []).append(claim)
        summaries: list[SecInstitutionalMaterializationCandidateSummary] = []
        selections: list[InstitutionalObservationClaimSelection] = []
        run_report_ids: dict[str, set[UUID]] = {}
        for candidate in target.candidates:
            summary, candidate_selections = self._claim_candidate(
                candidate=candidate,
                target=target,
                reports=reports,
                artifacts=artifacts,
                claims_by_row=claims_by_row,
                snapshot_id=snapshot_id,
                dataset_revision_id=dataset_revision_id,
                snapshot_available_at=snapshot_available_at,
                repository=repository,
                service=correspondences,
                catalog_cusips=catalog_cusips,
                recorded_at=recorded_at,
                preflight=preflight,
            )
            summaries.append(summary)
            selections.extend(candidate_selections)
            if summary.state == "processed":
                run_report_ids.setdefault(candidate.asset_id, set()).update(summary.report_ids)
        selections_by_asset: dict[str, list[InstitutionalObservationClaimSelection]] = {}
        for selection in selections:
            selections_by_asset.setdefault(selection.asset_id, []).append(selection)
        reports_by_id = {report.report_id: report for report in reports}
        parents: list[InstitutionalObservationParentContext] = []
        for selection in selections:
            report = reports_by_id.get(selection.report_id)
            artifact = artifacts.get(selection.report_id)
            row = (
                next(
                    (item for item in artifact.rows if item.row_id == selection.row_id),
                    None,
                )
                if artifact is not None
                else None
            )
            if report is None or artifact is None or row is None:
                raise SecInstitutionalObservationMaterializationError(
                    "selected claim parent is not available in the page"
                )
            parents.append(
                InstitutionalObservationParentContext(report=report, artifact=artifact, row=row)
            )
        runs: list[SecInstitutionalMaterializationRunSummary] = []
        outcomes: list[InstitutionalObservationSummary] = []
        for asset_id, report_ids in sorted(run_report_ids.items()):
            run, outcome = self._observe_asset(
                asset_id=asset_id,
                manager_cik=target.manager_cik,
                report_ids=tuple(sorted(report_ids, key=str)),
                selections=tuple(selections_by_asset.get(asset_id, ())),
                parents=tuple(parents),
                known_at=request.known_at,
                observations=observations,
                claim_service=correspondences,
                preflight=preflight,
            )
            runs.append(run)
            outcomes.append(outcome)
        return summaries, runs, selections, outcomes

    def _claim_candidate(
        self,
        *,
        candidate: Sec13FManagerCandidate,
        target: MaterializationTarget,
        reports,
        artifacts,
        claims_by_row: dict[tuple[UUID, UUID], list[SecInstitutionalRowCorrespondence]],
        snapshot_id: UUID,
        dataset_revision_id: UUID,
        snapshot_available_at: datetime,
        repository: SecInstitutionalRowCorrespondenceRepository,
        service: SecInstitutionalRowCorrespondenceService,
        catalog_cusips: dict[str, str],
        recorded_at: datetime,
        preflight: bool,
    ) -> tuple[
        SecInstitutionalMaterializationCandidateSummary,
        tuple[InstitutionalObservationClaimSelection, ...],
    ]:
        """Select one strictly verified claim per exact CUSIP row and report."""
        base = {
            "candidate_id": candidate.candidate_id,
            "asset_id": candidate.asset_id,
            "cusip": candidate.cusip,
            "manager_cik": target.manager_cik,
            "manager_name": candidate.manager_name,
            "report_period": target.report_period,
        }
        if not reports:
            return (
                SecInstitutionalMaterializationCandidateSummary(
                    **base, state="missing_report", reason_code=None, skipped_by_reason={}
                ),
                (),
            )
        if not artifacts:
            return (
                SecInstitutionalMaterializationCandidateSummary(
                    **base,
                    state="not_enriched",
                    report_ids=tuple(report.report_id for report in reports),
                    skipped_by_reason={},
                ),
                (),
            )
        created = reused = ambiguous = matched = unmatched = examined = 0
        claim_ids: list[UUID] = []
        artifact_ids: list[UUID] = []
        selections: list[InstitutionalObservationClaimSelection] = []
        skipped: dict[str, int] = {}
        failure: str | None = None
        try:
            for report in reports:
                artifact = artifacts.get(report.report_id)
                if artifact is None:
                    continue
                artifact_ids.append(artifact.artifact_id)
                for row in artifact.rows:
                    if row.cusip != candidate.cusip:
                        continue
                    examined += 1
                    claims = claims_by_row.get((artifact.artifact_id, row.row_id), ())
                    resolution = service.resolve_verified_claims(claims)
                    if resolution.state in {"conflicting_content", "invalid"}:
                        ambiguous += 1
                        unmatched += 1
                        failure = failure or f"claim_{resolution.state}"
                        skipped[f"claim_{resolution.state}"] = (
                            skipped.get(f"claim_{resolution.state}", 0) + 1
                        )
                        continue
                    if resolution.state == "verified":
                        verified_ids = tuple(item.correspondence_id for item in resolution.verified)
                        selected = resolution.selected
                        service.verify_catalog_binding(selected, catalog_cusips=catalog_cusips)
                        reused += 1
                    else:
                        proposed = SecInstitutionalRowCorrespondence.claim(
                            asset_id=candidate.asset_id,
                            cusip=row.cusip,
                            title_of_class=row.title_of_class,
                            report_period=target.report_period,
                            manager_cik=target.manager_cik,
                            report_id=report.report_id,
                            artifact_id=artifact.artifact_id,
                            row_id=row.row_id,
                            universe_snapshot_id=snapshot_id,
                            dataset_revision_id=dataset_revision_id,
                            candidate_id=candidate.candidate_id,
                            available_at=max(snapshot_available_at, artifact.available_at),
                            recorded_at=recorded_at,
                        )
                        service.verify_lineage(proposed)
                        service.verify_catalog_binding(proposed, catalog_cusips=catalog_cusips)
                        if preflight:
                            selected = proposed
                            created += 1
                        else:
                            existing = repository.get(proposed.correspondence_id)
                            if existing is not None:
                                if not same_evidence(existing, proposed):
                                    raise SecInstitutionalObservationMaterializationError(
                                        "row correspondence identity conflicts"
                                    )
                                if service.classify_lineage(existing).state != "verified":
                                    raise SecInstitutionalObservationMaterializationError(
                                        "persisted row correspondence is not verified"
                                    )
                                selected = existing
                                reused += 1
                            else:
                                repository.save(proposed)
                                selected = proposed
                                created += 1
                        verified_ids = (selected.correspondence_id,)
                    matched += 1
                    if selected.correspondence_id not in claim_ids:
                        claim_ids.append(selected.correspondence_id)
                    selections.append(
                        InstitutionalObservationClaimSelection(
                            asset_id=candidate.asset_id,
                            manager_cik=target.manager_cik,
                            report_id=report.report_id,
                            artifact_id=artifact.artifact_id,
                            row_id=row.row_id,
                            correspondence=selected,
                            verified_claim_ids=verified_ids,
                        )
                    )
        except Exception as error:
            return (
                SecInstitutionalMaterializationCandidateSummary(
                    **base,
                    state="failed",
                    reason_code=f"{type(error).__name__}: {error}",
                    report_ids=tuple(report.report_id for report in reports),
                    artifact_ids=tuple(artifact_ids),
                    rows_examined=examined,
                    rows_matched=matched,
                    rows_unmatched=examined - matched,
                    claims_created=created,
                    claims_reused=reused,
                    claims_ambiguous=ambiguous,
                    claim_ids=tuple(claim_ids),
                    skipped_by_reason=skipped,
                ),
                tuple(selections),
            )
        unmatched = examined - matched
        if failure is not None:
            state = "failed"
        elif matched:
            state = "processed"
        else:
            state = "missing_rows"
        if unmatched and failure is None:
            skipped["cusip_mismatch"] = unmatched
        return (
            SecInstitutionalMaterializationCandidateSummary(
                **base,
                state=state,
                reason_code=failure,
                report_ids=tuple(report.report_id for report in reports),
                artifact_ids=tuple(artifact_ids),
                rows_examined=examined,
                rows_matched=matched,
                rows_unmatched=unmatched,
                claims_created=created,
                claims_reused=reused,
                claims_ambiguous=ambiguous,
                claim_ids=tuple(claim_ids),
                skipped_by_reason=skipped,
            ),
            tuple(selections),
        )

    def _observe_asset(
        self,
        *,
        asset_id: str,
        manager_cik: str,
        report_ids: tuple[UUID, ...],
        selections: tuple[InstitutionalObservationClaimSelection, ...],
        parents: tuple[InstitutionalObservationParentContext, ...],
        known_at: datetime,
        observations: InstitutionalObservationService,
        claim_service: SecInstitutionalRowCorrespondenceService,
        preflight: bool,
    ) -> tuple[SecInstitutionalMaterializationRunSummary, InstitutionalObservationSummary]:
        """Complete the integrated observation layer for one asset and manager in batches."""
        created = reused = linked = unlinked = 0
        skipped: dict[str, int] = {}
        witnesses: list[MaterializationObservationWitness] = []
        try:
            for start in range(0, len(report_ids), MAX_REPORT_IDS_PER_OBSERVATION_REQUEST):
                batch = report_ids[start : start + MAX_REPORT_IDS_PER_OBSERVATION_REQUEST]
                request = InstitutionalObservationRequest(
                    asset_id=asset_id,
                    manager_cik=manager_cik,
                    report_ids=batch,
                    known_at=known_at,
                )
                context = observations.plan_normalization(
                    request,
                    tuple(item for item in selections if item.report_id in set(batch)),
                    write_allowed=not preflight,
                    parents=parents,
                    claim_service=claim_service,
                )
                outcome = observations.normalize(
                    request, context=context, claim_service=claim_service
                )
                created += outcome.observations_created
                reused += outcome.observations_reused
                linked += outcome.rows_linked
                unlinked += outcome.rows_unlinked
                for reason, count in outcome.skipped_by_reason.items():
                    skipped[reason] = skipped.get(reason, 0) + count
                witnesses.extend(
                    MaterializationObservationWitness(**item.model_dump())
                    for item in outcome.observation_witnesses
                )
        except Exception as error:
            return (
                SecInstitutionalMaterializationRunSummary(
                    asset_id=asset_id,
                    manager_cik=manager_cik,
                    report_ids=report_ids,
                    state="failed",
                    reason_code=f"{type(error).__name__}: {error}",
                    rows_linked=linked,
                    rows_unlinked=unlinked,
                    observations_created=created,
                    observations_reused=reused,
                    skipped_by_reason=skipped,
                    observation_witnesses=tuple(witnesses),
                ),
                InstitutionalObservationSummary(
                    asset_id=asset_id,
                    known_at=known_at,
                    normalized_at=known_at,
                    reports_examined=len(report_ids),
                    reports_missing=0,
                    reports_not_enriched=0,
                    rows_examined=linked + unlinked,
                    rows_linked=linked,
                    rows_unlinked=unlinked,
                    values_examined=linked,
                    observations_generated=created + reused,
                    observations_created=created,
                    observations_reused=reused,
                    skipped_by_reason=skipped,
                    observation_witnesses=tuple(),
                    observation_references=tuple(),
                ),
            )
        return (
            SecInstitutionalMaterializationRunSummary(
                asset_id=asset_id,
                manager_cik=manager_cik,
                report_ids=report_ids,
                state="processed",
                rows_linked=linked,
                rows_unlinked=unlinked,
                observations_created=created,
                observations_reused=reused,
                skipped_by_reason=skipped,
                observation_witnesses=tuple(witnesses),
            ),
            outcome,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise SecInstitutionalObservationMaterializationError(
                "materialization clock must be timezone-aware"
            )
        return value.astimezone(UTC)


def _resolve_snapshot(storage, known_at: datetime):
    """Resolve the latest available universe snapshot and verify its dataset lineage."""
    repository = SecInstitutionalUniverseRepository(storage.raw_records, storage.documents)
    snapshot = repository.find_latest_snapshot(known_at=known_at)
    if snapshot is None:
        raise SecInstitutionalObservationMaterializationError(MISSING_UNIVERSE_INSTRUCTION)
    revision: Sec13FDataSetRevision | None = repository.get_dataset_revision(
        snapshot.dataset_revision_id
    )
    if revision is None or revision.content_sha256 != snapshot.dataset_sha256:
        raise SecInstitutionalObservationMaterializationError(
            "manager universe snapshot lineage does not verify"
        )
    if (revision.period_start, revision.period_end) != (
        snapshot.period_start,
        snapshot.period_end,
    ):
        raise SecInstitutionalObservationMaterializationError(
            "manager universe snapshot period conflicts with its dataset revision"
        )
    if revision.available_at > known_at:
        raise SecInstitutionalObservationMaterializationError(
            "manager universe dataset revision is not available at the requested cut"
        )
    return snapshot, revision


def _verify_context_traceability(
    *,
    storage,
    selections: tuple[InstitutionalObservationClaimSelection, ...],
    outcomes: tuple[InstitutionalObservationSummary, ...],
    known_at: datetime,
    preflight: bool,
) -> bool:
    """Verify concrete field-to-observation links for every selected row at the cut."""
    references: dict[tuple[UUID, UUID, str], list] = {}
    for outcome in outcomes:
        for item in outcome.observation_references:
            references.setdefault((item.artifact_id, item.row_id, item.field_name), []).append(item)
    semantics = InstitutionalSemanticsRepository(storage.raw_records)
    for selection in selections:
        artifact = semantics.get(selection.artifact_id)
        if artifact is None:
            return False
        row = next((item for item in artifact.rows if item.row_id == selection.row_id), None)
        if row is None:
            return False
        verified_ids = set(selection.verified_claim_ids) or {
            selection.correspondence.correspondence_id
        }
        candidates = normalize_row(
            artifact,
            row,
            selection.correspondence,
            normalized_at=known_at,
        )
        for candidate in candidates:
            compatible = [
                item
                for item in references.get(
                    (
                        selection.artifact_id,
                        selection.row_id,
                        candidate.field_name,
                    ),
                    (),
                )
                if item.claim_id in verified_ids
                and item.asset_id == selection.asset_id
                and item.manager_cik == selection.manager_cik
                and item.report_id == selection.report_id
            ]
            if not compatible:
                return False
            if not preflight and not any(item.persisted for item in compatible):
                return False
    return True


def _verify_traceability(
    *,
    repository: SecInstitutionalRowCorrespondenceRepository,
    storage,
    candidates: tuple[SecInstitutionalMaterializationCandidateSummary, ...],
    known_at: datetime,
) -> bool:
    """Re-read the persisted claims and observations and confirm every proof is still visible."""
    from investment_analyst.evidence.sec_institutional_observations.definitions import (
        SOURCE_ID as _OBSERVATION_SOURCE_ID,
    )
    from investment_analyst.evidence.sec_institutional_observations.service import (
        observation_lineage_key,
    )

    observed: dict[str, set[str]] = {}
    for candidate in candidates:
        if candidate.state == "failed" or candidate.claims_ambiguous:
            return False
        candidate_claim_ids = set(map(str, candidate.claim_ids))
        persisted_ids = repository.selected_claim_ids_for_candidate(
            known_at=known_at,
            claim_ids=tuple(candidate.claim_ids),
        )
        if not candidate_claim_ids.issubset({str(value) for value in persisted_ids}):
            return False
        key = f"{candidate.asset_id}|{candidate.manager_cik}"
        if key not in observed:
            candidate_ids = storage.observations.list_ids_for_manager_observation_references(
                asset_id=candidate.asset_id,
                available_to=known_at,
            )
            observed[key] = {
                str(observation_lineage_key(item)["correspondence_id"])
                for item in storage.observations.get_many(candidate_ids).values()
                if observation_lineage_key(item)["manager_cik"] == candidate.manager_cik
                and item.source.source_id == _OBSERVATION_SOURCE_ID
            }
        if not set(map(str, candidate.claim_ids)).issubset(observed[key]):
            return False
    return True


__all__ = [
    "MISSING_UNIVERSE_INSTRUCTION",
    "MAX_REPORT_IDS_PER_OBSERVATION_REQUEST",
    "MaterializationTarget",
    "SecInstitutionalObservationMaterializationApplication",
    "SecInstitutionalObservationMaterializationError",
    "plan_materialization_page",
]
