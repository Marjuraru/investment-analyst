"""Directed, resumable acquisition of Form 13F filings from the persisted manager universe.

One execution resolves the latest manager-universe snapshot available at the requested cut,
selects a deterministic page of already selected managers, performs exactly one fresh Submissions
GET per manager, imports only the pending ``13F-HR``/``13F-HR/A`` accessions of the exact report
period discovered there, and then completes the integrated semantic layer for the reports visible
at the same cut. Discovery accessions are lineage, never import authority, and SEC Archives is
never revisited for an accession that is already materialized.
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
from investment_analyst.application.sec_institutional_holdings_refresh_models import (
    SEC_INSTITUTIONAL_HOLDINGS_DIRECTED_REFRESH_POLICY,
    SecInstitutionalHoldingsDirectedManagerSummary,
    SecInstitutionalHoldingsDirectedRefreshRequest,
    SecInstitutionalHoldingsDirectedRefreshSummary,
)
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
)
from investment_analyst.evidence.sec_institutional_semantics.service import (
    InstitutionalHoldingsSemanticsService,
    InstitutionalSemanticsEnrichRequest,
)
from investment_analyst.evidence.sec_institutional_universe.models import (
    Sec13FDataSetRevision,
    Sec13FManagerCandidate,
    Sec13FManagerUniverseSnapshot,
)
from investment_analyst.evidence.sec_institutional_universe.repository import (
    SecInstitutionalUniverseRepository,
)
from investment_analyst.providers.fundamentals.sec_document_client import SecDocumentClient
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpTransport, UrlLibHttpTransport
from investment_analyst.providers.institutional_holdings.sec_institutional_holdings_pipeline import (  # noqa: E501
    SecInstitutionalHoldingsPeriodImportRequest,
    SecInstitutionalHoldingsPeriodImportResult,
    SecInstitutionalHoldingsPipeline,
)
from investment_analyst.providers.institutional_holdings.sec_manager_submissions import (
    SecManagerSubmissionsClient,
)
from investment_analyst.workspace.models import WorkspaceAccessMode

MAX_SEMANTIC_REPORTS_PER_REQUEST = 20
"""The integrated semantic service accepts at most twenty report identifiers per request."""

MISSING_UNIVERSE_INSTRUCTION = (
    "no Form 13F manager universe snapshot is available at the requested cut; "
    "run scripts/refresh_sec_institutional_manager_universe.py first"
)


class SecInstitutionalHoldingsDirectedRefreshError(RuntimeError):
    """A directed 13F refresh cannot preserve its evidence contract."""


@dataclass(frozen=True, slots=True)
class DirectedManagerTarget:
    """One manager and report period selected by the persisted universe policy."""

    manager_cik: str
    manager_name: str
    report_period: date
    candidate_ids: tuple[UUID, ...]
    asset_ids: tuple[str, ...]
    cusips: tuple[str, ...]
    dataset_accession_hints: tuple[str, ...]
    accession_lineage: tuple[str, ...]


@dataclass(slots=True)
class _ProviderCallCounters:
    """Count the provider calls performed by one directed page, even when a call fails."""

    submissions_calls: int = 0
    archives_calls: int = 0

    def snapshot(self) -> tuple[int, int]:
        return self.submissions_calls, self.archives_calls

    def delta(self, baseline: tuple[int, int]) -> tuple[int, int]:
        return self.submissions_calls - baseline[0], self.archives_calls - baseline[1]


class _CountingSubmissionsClient:
    def __init__(self, client, counters: _ProviderCallCounters) -> None:
        self._client = client
        self._counters = counters

    def fetch(self, filer_cik: str):
        self._counters.submissions_calls += 1
        return self._client.fetch(filer_cik)


class _CountingDocumentClient:
    def __init__(self, client, counters: _ProviderCallCounters) -> None:
        self._client = client
        self._counters = counters

    def fetch_manifest(self, document):
        self._counters.archives_calls += 1
        return self._client.fetch_manifest(document)

    def fetch(self, document):
        self._counters.archives_calls += 1
        return self._client.fetch(document)


@dataclass(slots=True)
class _SemanticCounts:
    examined: int = 0
    created: int = 0
    reused: int = 0
    not_visible: int = 0
    rejected: int = 0


def plan_directed_manager_page(
    snapshot: Sec13FManagerUniverseSnapshot, *, offset: int, limit: int
) -> tuple[DirectedManagerTarget, ...]:
    """Select one deterministic page of already selected managers.

    Ordering is ``(selection_rank, asset_id, manager_cik, report_period)`` and the deduplication
    key is ``(manager_cik, report_period)``; the page bound is an operational budget and never an
    analytical ranking.
    """
    if offset < 0 or limit < 1:
        raise SecInstitutionalHoldingsDirectedRefreshError("invalid directed manager page bounds")
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
        DirectedManagerTarget(
            manager_cik=normalize_cik(manager_cik),
            manager_name=candidates[0].manager_name,
            report_period=report_period,
            candidate_ids=tuple(item.candidate_id for item in candidates),
            asset_ids=tuple(sorted({item.asset_id for item in candidates})),
            cusips=tuple(sorted({item.cusip for item in candidates})),
            dataset_accession_hints=tuple(sorted({item.accession for item in candidates})),
            accession_lineage=tuple(
                sorted({accession for item in candidates for accession in item.accession_lineage})
            ),
        )
        for (manager_cik, report_period), candidates in grouped.items()
    )
    return targets[offset : offset + limit]


class SecInstitutionalHoldingsDirectedRefreshApplication:
    """Isolated application edge for the directed Form 13F acquisition."""

    def __init__(
        self,
        runtime: ApplicationRuntime,
        *,
        transport_factory: Callable[[], HttpTransport] = UrlLibHttpTransport,
        submissions_client_factory: Callable[..., object] | None = None,
        document_client_factory: Callable[..., object] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._runtime = runtime
        self._transport_factory = transport_factory
        self._submissions_client_factory = submissions_client_factory
        self._document_client_factory = document_client_factory
        self._clock = clock

    @classmethod
    def create_default(cls) -> SecInstitutionalHoldingsDirectedRefreshApplication:
        return cls(ApplicationRuntime.create_default())

    def refresh(
        self,
        request: SecInstitutionalHoldingsDirectedRefreshRequest,
        *,
        sec_identity: SecEdgarIdentity,
        location: StorageLocationRequest | None = None,
    ) -> SecInstitutionalHoldingsDirectedRefreshSummary:
        """Acquire one bounded page of pending Form 13F filings under a single writer."""
        storage_request = location or StorageLocationRequest()
        with self._runtime.open_storage(
            storage_request, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            snapshot, revision = self._resolve_snapshot(storage, request.known_at)
            targets = plan_directed_manager_page(
                snapshot, offset=request.manager_offset, limit=request.manager_limit
            )
            transport = self._transport_factory()
            counters = _ProviderCallCounters()
            submissions_client = _CountingSubmissionsClient(
                (
                    self._submissions_client_factory(transport, sec_identity, self._clock)
                    if self._submissions_client_factory is not None
                    else SecManagerSubmissionsClient(transport, sec_identity, clock=self._clock)
                ),
                counters,
            )
            document_client = _CountingDocumentClient(
                (
                    self._document_client_factory(transport, sec_identity)
                    if self._document_client_factory is not None
                    else SecDocumentClient(transport, sec_identity)
                ),
                counters,
            )
            pipeline = SecInstitutionalHoldingsPipeline(
                storage, submissions_client, document_client
            )
            holdings = InstitutionalHoldingsRepository(storage.raw_records)
            semantics = InstitutionalHoldingsSemanticsService(storage, clock=self._clock)
            managers = tuple(
                self._run_manager(
                    pipeline=pipeline,
                    holdings=holdings,
                    semantics=semantics,
                    counters=counters,
                    target=target,
                    request=request,
                )
                for target in targets
            )
            traceability = self._verify_traceability(
                holdings=holdings, managers=managers, known_at=request.known_at
            )
            return SecInstitutionalHoldingsDirectedRefreshSummary(
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
                page_manager_count=len(managers),
                managers=managers,
                submissions_calls=counters.submissions_calls,
                archives_calls=counters.archives_calls,
                created=sum(len(item.created_accessions) for item in managers),
                reused=sum(len(item.reused_accessions) for item in managers),
                rejected_or_failed=sum(
                    len(item.rejected_accessions) + len(item.failed_accessions) for item in managers
                ),
                backlog_after=sum(item.backlog_after for item in managers),
                semantics_created=sum(item.semantics_created for item in managers),
                semantics_reused=sum(item.semantics_reused for item in managers),
                traceability_verified=traceability,
            )

    def _resolve_snapshot(
        self, storage, known_at: datetime
    ) -> tuple[Sec13FManagerUniverseSnapshot, Sec13FDataSetRevision]:
        """Resolve the latest available universe snapshot and verify its dataset lineage."""
        repository = SecInstitutionalUniverseRepository(storage.raw_records, storage.documents)
        snapshot = repository.find_latest_snapshot(known_at=known_at)
        if snapshot is None:
            raise SecInstitutionalHoldingsDirectedRefreshError(MISSING_UNIVERSE_INSTRUCTION)
        revision = repository.get_dataset_revision(snapshot.dataset_revision_id)
        if revision is None or revision.content_sha256 != snapshot.dataset_sha256:
            raise SecInstitutionalHoldingsDirectedRefreshError(
                "manager universe snapshot lineage does not verify"
            )
        if (revision.period_start, revision.period_end) != (
            snapshot.period_start,
            snapshot.period_end,
        ):
            raise SecInstitutionalHoldingsDirectedRefreshError(
                "manager universe snapshot period conflicts with its dataset revision"
            )
        if revision.available_at > known_at:
            raise SecInstitutionalHoldingsDirectedRefreshError(
                "manager universe dataset revision is not available at the requested cut"
            )
        return snapshot, revision

    def _run_manager(
        self,
        *,
        pipeline: SecInstitutionalHoldingsPipeline,
        holdings: InstitutionalHoldingsRepository,
        semantics: InstitutionalHoldingsSemanticsService,
        counters: _ProviderCallCounters,
        target: DirectedManagerTarget,
        request: SecInstitutionalHoldingsDirectedRefreshRequest,
    ) -> SecInstitutionalHoldingsDirectedManagerSummary:
        baseline = counters.snapshot()
        state = "processed"
        reason_code: str | None = None
        result: SecInstitutionalHoldingsPeriodImportResult | None = None
        try:
            result = pipeline.run_period(
                SecInstitutionalHoldingsPeriodImportRequest(
                    filer_cik=target.manager_cik,
                    report_period=target.report_period,
                    known_at=request.known_at,
                    accessions_per_manager=request.accessions_per_manager,
                )
            )
        except Exception as error:
            state = "failed"
            reason_code = _failure_code(error)
        submissions_calls, archives_calls = counters.delta(baseline)
        report_ids = (
            () if result is None else self._visible_report_ids(holdings, target, request.known_at)
        )
        counts = _SemanticCounts()
        try:
            counts = self._enrich_period(
                semantics=semantics,
                manager_cik=target.manager_cik,
                report_ids=report_ids,
                known_at=request.known_at,
            )
        except Exception as error:
            if state == "processed":
                state = "failed"
                reason_code = _failure_code(error)
        return SecInstitutionalHoldingsDirectedManagerSummary(
            manager_cik=target.manager_cik,
            manager_name=target.manager_name,
            report_period=target.report_period,
            state=state,
            reason_code=reason_code,
            candidate_ids=target.candidate_ids,
            candidate_asset_ids=target.asset_ids,
            candidate_cusips=target.cusips,
            dataset_accession_hints=target.dataset_accession_hints,
            dataset_accession_lineage=target.accession_lineage,
            submissions_calls=submissions_calls,
            submissions_created=0 if result is None else result.submissions_created,
            submissions_reused=0 if result is None else result.submissions_reused,
            submissions_raw_record_id=None if result is None else result.submissions_raw_record_id,
            submissions_checked_at=None if result is None else result.submissions_checked_at,
            eligible_accessions=() if result is None else result.eligible_accessions,
            reused_accessions=() if result is None else result.reused_accessions,
            attempted_accessions=() if result is None else result.attempted_accessions,
            created_accessions=() if result is None else result.created_accessions,
            rejected_accessions=() if result is None else result.rejected_accessions,
            failed_accessions=() if result is None else result.failed_accessions,
            failure_codes=() if result is None else result.failure_codes,
            report_ids=report_ids,
            pending_before=0 if result is None else result.pending_before,
            backlog_after=0 if result is None else result.backlog_after,
            archives_calls=archives_calls,
            semantics_examined=counts.examined,
            semantics_created=counts.created,
            semantics_reused=counts.reused,
            semantics_not_visible=counts.not_visible,
            semantics_rejected=counts.rejected,
        )

    def _visible_report_ids(
        self,
        holdings: InstitutionalHoldingsRepository,
        target: DirectedManagerTarget,
        known_at: datetime,
    ) -> tuple[UUID, ...]:
        """List every report of the target period visible at the cut for one manager."""
        return tuple(
            report.report_id
            for report in holdings.list_reports(manager_cik=target.manager_cik, known_at=known_at)
            if report.report_period == target.report_period
        )

    def _enrich_period(
        self,
        *,
        semantics: InstitutionalHoldingsSemanticsService,
        manager_cik: str,
        report_ids: tuple[UUID, ...],
        known_at: datetime,
    ) -> _SemanticCounts:
        """Enrich every report of the target period visible at the cut, in bounded batches."""
        counts = _SemanticCounts()
        for start in range(0, len(report_ids), MAX_SEMANTIC_REPORTS_PER_REQUEST):
            batch = tuple(report_ids[start : start + MAX_SEMANTIC_REPORTS_PER_REQUEST])
            outcome = semantics.enrich(
                InstitutionalSemanticsEnrichRequest(
                    manager_cik=manager_cik, report_ids=batch, known_at=known_at
                )
            )
            counts.examined += outcome.examined
            counts.created += outcome.created
            counts.reused += outcome.reused
            counts.not_visible += outcome.not_visible
            counts.rejected += outcome.rejected
        return counts

    def _verify_traceability(
        self,
        *,
        holdings: InstitutionalHoldingsRepository,
        managers: tuple[SecInstitutionalHoldingsDirectedManagerSummary, ...],
        known_at: datetime,
    ) -> bool:
        """Re-read the persisted evidence and confirm every materialized accession is visible."""
        for manager in managers:
            if manager.state != "processed":
                return False
            materialized = {
                report.cover_revision.document.filing.accession
                for report in holdings.list_reports(
                    manager_cik=manager.manager_cik, known_at=known_at
                )
                if report.report_period == manager.report_period
            }
            expected = set(manager.created_accessions) | set(manager.reused_accessions)
            if not expected.issubset(materialized):
                return False
        return True


def _failure_code(error: BaseException) -> str:
    """Return a compact, secret-free code for one bounded per-manager failure."""
    return type(error).__name__


__all__ = [
    "MISSING_UNIVERSE_INSTRUCTION",
    "SEC_INSTITUTIONAL_HOLDINGS_DIRECTED_REFRESH_POLICY",
    "DirectedManagerTarget",
    "SecInstitutionalHoldingsDirectedRefreshApplication",
    "SecInstitutionalHoldingsDirectedRefreshError",
    "SecInstitutionalHoldingsDirectedRefreshRequest",
    "SecInstitutionalHoldingsDirectedRefreshSummary",
    "plan_directed_manager_page",
]
