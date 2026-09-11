"""Directed, resumable materialization of institutional observations from the manager universe.

One execution resolves the latest manager-universe snapshot available at the requested cut, selects
the same deterministic page of already selected managers as ``SEC-CORPUS-28`` while preserving each
candidate's own ``(asset_id, cusip, manager_cik, report_period)`` tuple, proves one row-scoped
correspondence per exact CUSIP match against the persisted 13F evidence, and then completes the
integrated observation layer for every asset present in the page. No network call is performed and
the workspace is opened exactly once.
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
from investment_analyst.evidence.sec_institutional_observations.definitions import (
    SOURCE_ID as OBSERVATION_SOURCE_ID,
)
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationRequest,
)
from investment_analyst.evidence.sec_institutional_observations.service import (
    InstitutionalObservationService,
    observation_lineage_key,
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

    def materialize(
        self,
        request: SecInstitutionalObservationMaterializationRequest,
        *,
        location: StorageLocationRequest | None = None,
    ) -> SecInstitutionalObservationMaterializationSummary:
        """Materialize claims and observations for one bounded page under a single writer."""
        storage_request = location or StorageLocationRequest()
        catalog_cusips = resolve_catalog_sec_cusip_mappings(self._runtime.catalog)
        with self._runtime.open_storage(
            storage_request, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
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
            for target in targets:
                target_candidates, target_runs = self._run_target(
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
                )
                candidates.extend(target_candidates)
                runs.extend(target_runs)
            return SecInstitutionalObservationMaterializationSummary(
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
                traceability_verified=_verify_traceability(
                    repository=repository,
                    storage=storage,
                    candidates=tuple(candidates),
                    known_at=request.known_at,
                ),
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
    ) -> tuple[
        list[SecInstitutionalMaterializationCandidateSummary],
        list[SecInstitutionalMaterializationRunSummary],
    ]:
        """Prove claims for every candidate of one manager and period, then observe each asset."""
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
        summaries: list[SecInstitutionalMaterializationCandidateSummary] = []
        run_report_ids: dict[str, list[UUID]] = {}
        for candidate in target.candidates:
            summary = self._claim_candidate(
                candidate=candidate,
                target=target,
                reports=reports,
                artifacts=artifacts,
                snapshot_id=snapshot_id,
                dataset_revision_id=dataset_revision_id,
                snapshot_available_at=snapshot_available_at,
                repository=repository,
                service=correspondences,
                catalog_cusips=catalog_cusips,
                recorded_at=recorded_at,
            )
            summaries.append(summary)
            if summary.state == "processed":
                run_report_ids.setdefault(candidate.asset_id, list(summary.report_ids))
        runs = [
            self._observe_asset(
                asset_id=asset_id,
                manager_cik=target.manager_cik,
                report_ids=tuple(dict.fromkeys(report_ids)),
                known_at=request.known_at,
                observations=observations,
            )
            for asset_id, report_ids in sorted(run_report_ids.items())
        ]
        return summaries, runs

    def _claim_candidate(
        self,
        *,
        candidate: Sec13FManagerCandidate,
        target: MaterializationTarget,
        reports,
        artifacts,
        snapshot_id: UUID,
        dataset_revision_id: UUID,
        snapshot_available_at: datetime,
        repository: SecInstitutionalRowCorrespondenceRepository,
        service: SecInstitutionalRowCorrespondenceService,
        catalog_cusips: dict[str, str],
        recorded_at: datetime,
    ) -> SecInstitutionalMaterializationCandidateSummary:
        """Create or reuse one claim per exact CUSIP row of every visible report."""
        base = {
            "candidate_id": candidate.candidate_id,
            "asset_id": candidate.asset_id,
            "cusip": candidate.cusip,
            "manager_cik": target.manager_cik,
            "manager_name": candidate.manager_name,
            "report_period": target.report_period,
        }
        if not reports:
            return SecInstitutionalMaterializationCandidateSummary(
                **base, state="missing_report", reason_code=None, skipped_by_reason={}
            )
        if not artifacts:
            return SecInstitutionalMaterializationCandidateSummary(
                **base,
                state="not_enriched",
                report_ids=tuple(report.report_id for report in reports),
                skipped_by_reason={},
            )
        created = reused = ambiguous = matched = unmatched = examined = 0
        claim_ids: list[UUID] = []
        artifact_ids: list[UUID] = []
        skipped: dict[str, int] = {}
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
                    claim = SecInstitutionalRowCorrespondence.claim(
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
                    service.verify_lineage(claim)
                    service.verify_catalog_binding(claim, catalog_cusips=catalog_cusips)
                    existing = repository.get(claim.correspondence_id)
                    if existing is None:
                        repository.save(claim)
                        created += 1
                    elif not same_evidence(existing, claim):
                        ambiguous += 1
                        skipped["claim_identity_conflicts"] = (
                            skipped.get("claim_identity_conflicts", 0) + 1
                        )
                        continue
                    else:
                        reused += 1
                    matched += 1
                    claim_ids.append(claim.correspondence_id)
        except Exception as error:
            return SecInstitutionalMaterializationCandidateSummary(
                **base,
                state="failed",
                reason_code=type(error).__name__,
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
            )
        unmatched = examined - matched
        state = "processed" if matched else "missing_rows"
        if unmatched:
            skipped["cusip_mismatch"] = unmatched
        return SecInstitutionalMaterializationCandidateSummary(
            **base,
            state=state,
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
        )

    def _observe_asset(
        self,
        *,
        asset_id: str,
        manager_cik: str,
        report_ids: tuple[UUID, ...],
        known_at: datetime,
        observations: InstitutionalObservationService,
    ) -> SecInstitutionalMaterializationRunSummary:
        """Complete the integrated observation layer for one asset and manager in batches."""
        created = reused = linked = unlinked = 0
        skipped: dict[str, int] = {}
        try:
            for start in range(0, len(report_ids), MAX_REPORT_IDS_PER_OBSERVATION_REQUEST):
                batch = report_ids[start : start + MAX_REPORT_IDS_PER_OBSERVATION_REQUEST]
                outcome = observations.normalize(
                    InstitutionalObservationRequest(
                        asset_id=asset_id,
                        manager_cik=manager_cik,
                        report_ids=batch,
                        known_at=known_at,
                    )
                )
                created += outcome.observations_created
                reused += outcome.observations_reused
                linked += outcome.rows_linked
                unlinked += outcome.rows_unlinked
                for reason, count in outcome.skipped_by_reason.items():
                    skipped[reason] = skipped.get(reason, 0) + count
        except Exception as error:
            return SecInstitutionalMaterializationRunSummary(
                asset_id=asset_id,
                manager_cik=manager_cik,
                report_ids=report_ids,
                state="failed",
                reason_code=type(error).__name__,
                rows_linked=linked,
                rows_unlinked=unlinked,
                observations_created=created,
                observations_reused=reused,
                skipped_by_reason=skipped,
            )
        return SecInstitutionalMaterializationRunSummary(
            asset_id=asset_id,
            manager_cik=manager_cik,
            report_ids=report_ids,
            state="processed",
            rows_linked=linked,
            rows_unlinked=unlinked,
            observations_created=created,
            observations_reused=reused,
            skipped_by_reason=skipped,
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


def _verify_traceability(
    *,
    repository: SecInstitutionalRowCorrespondenceRepository,
    storage,
    candidates: tuple[SecInstitutionalMaterializationCandidateSummary, ...],
    known_at: datetime,
) -> bool:
    """Re-read the persisted claims and observations and confirm every proof is still visible."""
    observed: dict[str, set[str]] = {}
    for candidate in candidates:
        if candidate.state == "failed" or candidate.claims_ambiguous:
            return False
        persisted = {
            str(claim.correspondence_id)
            for claim in repository.list(known_at=known_at, asset_id=candidate.asset_id)
            if claim.manager_cik == candidate.manager_cik
            and claim.report_period == candidate.report_period
        }
        if not set(map(str, candidate.claim_ids)).issubset(persisted):
            return False
        key = f"{candidate.asset_id}|{candidate.manager_cik}"
        if key not in observed:
            observed[key] = {
                str(observation_lineage_key(item)["correspondence_id"])
                for item in storage.observations.list(
                    asset_id=candidate.asset_id,
                    source_id=OBSERVATION_SOURCE_ID,
                    available_to=known_at,
                )
                if observation_lineage_key(item)["manager_cik"] == candidate.manager_cik
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
