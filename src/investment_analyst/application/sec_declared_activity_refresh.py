"""Incremental SEC declared-activity refresh under one versioned selection policy.

One run performs exactly one Submissions GET, selects at most one baseline accession per exact
form or the bounded post-watermark delta of a form that already has terminal evidence, imports
both families through the integrated pipelines, and then completes the integrated observation
and metric layers at the same point-in-time cut. Nothing is deleted, rewritten, or converted
into a signal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from investment_analyst.analytics.cazatiburones.activity_metric_models import (
    ActivityMetricRunSummary,
)
from investment_analyst.analytics.cazatiburones.activity_metric_pipeline import (
    ActivityMetricPipeline,
)
from investment_analyst.application.sec_declared_activity_refresh_models import (
    SecDeclaredActivityFamilySummary,
    SecDeclaredActivityRefreshRequest,
    SecDeclaredActivityRefreshSummary,
)
from investment_analyst.application.sec_submissions_refresh import (
    SecSubmissionsRefreshError,
    SecSubmissionsRefreshService,
    SecSubmissionsSnapshot,
)
from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BENEFICIAL_OWNERSHIP_SOURCE_ID,
)
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    BeneficialOwnershipAccessionState,
    BeneficialOwnershipRepository,
)
from investment_analyst.evidence.sec_declared_activity_observations.models import (
    DeclaredActivityObservationRunSummary,
)
from investment_analyst.evidence.sec_declared_activity_observations.service import (
    DeclaredActivityObservationService,
)
from investment_analyst.evidence.sec_documents.models import BENEFICIAL_OWNERSHIP_FORMS
from investment_analyst.evidence.sec_ownership.models import OWNERSHIP_FORMS, OWNERSHIP_SOURCE_ID
from investment_analyst.evidence.sec_ownership.repository import (
    OwnershipAccessionState,
    OwnershipRepository,
)
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.beneficial_ownership.sec_beneficial_ownership_index import (
    beneficial_ownership_filings,
)
from investment_analyst.providers.beneficial_ownership.sec_beneficial_ownership_pipeline import (
    SecBeneficialOwnershipImportRequest,
    SecBeneficialOwnershipPipeline,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarError
from investment_analyst.providers.http import HttpRequestError
from investment_analyst.providers.ownership.sec_ownership_index import ownership_filings
from investment_analyst.providers.ownership.sec_ownership_pipeline import (
    SecOwnershipImportRequest,
    SecOwnershipPipeline,
)
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import StorageError

MAX_ACCESSIONS_PER_FAMILY_PER_RUN = 25
"""Global per-family bound on selected accessions for one asset and one execution."""


class SecDeclaredActivityRefreshError(RuntimeError):
    """A declared-activity refresh cannot preserve its evidence contract."""


class _Filing(Protocol):
    accession: str
    form: str
    accepted_at: datetime


class _FamilyState(Protocol):
    accession: str
    form: str
    accepted_at: datetime

    @property
    def terminal(self) -> bool: ...


_FamilyAttempt = Callable[[tuple[str, ...]], None]
_FamilyStates = Callable[[], tuple[_FamilyState, ...]]


class _OwnershipPipeline(Protocol):
    def run(self, request: SecOwnershipImportRequest) -> tuple[object, ...]:
        """Import selected Section 16 accessions append-only."""
        ...


class _BeneficialOwnershipPipeline(Protocol):
    def run(self, request: SecBeneficialOwnershipImportRequest) -> tuple[object, ...]:
        """Import selected Schedule 13D/13G accessions append-only."""
        ...


class _ObservationService(Protocol):
    def normalize(
        self, *, asset_id: str, known_at: datetime
    ) -> DeclaredActivityObservationRunSummary:
        """Persist the integrated layer-2 observations for one PIT cut."""
        ...


class _MetricPipeline(Protocol):
    def compute(self, *, asset_id: str, known_at: datetime) -> ActivityMetricRunSummary:
        """Persist the integrated layer-3 metrics for one PIT cut."""
        ...


@dataclass(frozen=True, slots=True)
class _FamilyPlan:
    forms_evaluated: tuple[str, ...]
    forms_missing: tuple[str, ...]
    selected: tuple[str, ...]
    backlog_count: int


@dataclass(frozen=True, slots=True)
class _FamilyOutcome:
    imported: tuple[str, ...]
    reused: tuple[str, ...]
    rejected: tuple[str, ...]
    incomplete: tuple[str, ...]


def _plan_family(
    *,
    forms: frozenset[str],
    filings: tuple[_Filing, ...],
    states: tuple[_FamilyState, ...],
    limit: int = MAX_ACCESSIONS_PER_FAMILY_PER_RUN,
) -> _FamilyPlan:
    """Select the oldest-first delta of every exact form under the versioned policy."""
    by_form: dict[str, list[_Filing]] = {}
    for filing in filings:
        if filing.form not in forms:
            raise SecDeclaredActivityRefreshError("filing form is outside the declared policy")
        by_form.setdefault(filing.form, []).append(filing)
    for form_filings in by_form.values():
        form_filings.sort(key=lambda item: (item.accepted_at, item.accession))
    state_by_accession = {state.accession: state for state in states}
    candidates: dict[str, _Filing] = {}
    for form in sorted(forms):
        form_filings = by_form.get(form, ())
        if not form_filings:
            continue
        terminal = tuple(state for state in states if state.form == form and state.terminal)
        if not terminal:
            # No completed form evidence: only the most recent eligible accession is in scope.
            # The older historical baseline stays explicitly excluded and is never backlog.
            delta = (form_filings[-1],)
        else:
            watermark = max(state.accepted_at for state in terminal)
            delta = tuple(
                filing
                for filing in form_filings
                if filing.accepted_at >= watermark
                and not _terminal(state_by_accession.get(filing.accession))
            )
        for filing in delta:
            candidates[filing.accession] = filing
        # Interrupted work resumes regardless of the watermark; terminal rejections never do.
        for filing in form_filings:
            state = state_by_accession.get(filing.accession)
            if state is not None and not state.terminal:
                candidates[filing.accession] = filing
    ordered = sorted(candidates.values(), key=lambda item: (item.accepted_at, item.accession))
    selected = ordered[:limit]
    return _FamilyPlan(
        forms_evaluated=tuple(sorted(forms)),
        forms_missing=tuple(sorted(form for form in forms if form not in by_form)),
        selected=tuple(filing.accession for filing in selected),
        backlog_count=len(ordered) - len(selected),
    )


def _terminal(state: _FamilyState | None) -> bool:
    return state is not None and state.terminal


def _classify_outcome(
    *,
    selected: tuple[str, ...],
    accepted_before: frozenset[str],
    states: tuple[_FamilyState, ...],
) -> _FamilyOutcome:
    """Derive exact imported, reused, rejected, and still-incomplete accessions."""
    by_accession = {state.accession: state for state in states}
    imported: list[str] = []
    reused: list[str] = []
    rejected: list[str] = []
    incomplete: list[str] = []
    for accession in selected:
        state = by_accession.get(accession)
        if state is None or state.resolution == "partial":
            incomplete.append(accession)
        elif state.resolution == "rejected":
            rejected.append(accession)
        elif accession in accepted_before:
            reused.append(accession)
        else:
            imported.append(accession)
    return _FamilyOutcome(
        imported=tuple(sorted(imported)),
        reused=tuple(sorted(reused)),
        rejected=tuple(sorted(rejected)),
        incomplete=tuple(sorted(incomplete)),
    )


class SecDeclaredActivityRefreshService:
    """Refresh declared SEC activity and its integrated derived layers for one issuer."""

    def __init__(
        self,
        storage: LocalStorage,
        *,
        configuration: SecAssetConfiguration,
        submissions_service: SecSubmissionsRefreshService,
        ownership_pipeline: _OwnershipPipeline,
        beneficial_pipeline: _BeneficialOwnershipPipeline,
        observation_service: _ObservationService,
        metric_pipeline: _MetricPipeline,
    ) -> None:
        storage.require_open()
        self._storage = storage
        self._configuration = configuration
        self._submissions_service = submissions_service
        self._ownership_pipeline = ownership_pipeline
        self._beneficial_pipeline = beneficial_pipeline
        self._observation_service = observation_service
        self._metric_pipeline = metric_pipeline

    def run(self, request: SecDeclaredActivityRefreshRequest) -> SecDeclaredActivityRefreshSummary:
        """Import the selected accessions of both families and complete their derived layers."""
        self._storage.require_open()
        if request.asset_id != self._configuration.asset_id:
            raise SecDeclaredActivityRefreshError("request asset_id does not match SEC issuer")
        snapshot = self._persist_fresh_submissions()
        known_at = snapshot.checked_at

        insider_plan, insider_states = self._plan_insider(snapshot, known_at=known_at)
        beneficial_plan, beneficial_states = self._plan_beneficial(snapshot, known_at=known_at)
        insider_outcome = self._run_insider(
            insider_plan.selected,
            accepted_before=frozenset(
                state.accession for state in insider_states if state.resolution == "accepted"
            ),
            known_at=known_at,
        )
        beneficial_outcome = self._run_beneficial(
            beneficial_plan.selected,
            accepted_before=frozenset(
                state.accession for state in beneficial_states if state.resolution == "accepted"
            ),
            known_at=known_at,
        )
        observations = self._normalize_observations(known_at=known_at)
        metrics = self._compute_metrics(known_at=known_at)

        insider = SecDeclaredActivityFamilySummary(
            family="insider",
            source_id=OWNERSHIP_SOURCE_ID,
            forms_evaluated=insider_plan.forms_evaluated,
            forms_missing=insider_plan.forms_missing,
            accessions_selected=insider_plan.selected,
            accessions_imported=insider_outcome.imported,
            accessions_reused=insider_outcome.reused,
            accessions_rejected=insider_outcome.rejected,
            accessions_incomplete=insider_outcome.incomplete,
            backlog_count=insider_plan.backlog_count,
            statements_created=len(insider_outcome.imported),
            statements_reused=len(insider_outcome.reused),
        )
        beneficial = SecDeclaredActivityFamilySummary(
            family="beneficial",
            source_id=BENEFICIAL_OWNERSHIP_SOURCE_ID,
            forms_evaluated=beneficial_plan.forms_evaluated,
            forms_missing=beneficial_plan.forms_missing,
            accessions_selected=beneficial_plan.selected,
            accessions_imported=beneficial_outcome.imported,
            accessions_reused=beneficial_outcome.reused,
            accessions_rejected=beneficial_outcome.rejected,
            accessions_incomplete=beneficial_outcome.incomplete,
            backlog_count=beneficial_plan.backlog_count,
            statements_created=len(beneficial_outcome.imported),
            statements_reused=len(beneficial_outcome.reused),
        )
        traceability_verified = not insider_outcome.incomplete and not beneficial_outcome.incomplete
        return SecDeclaredActivityRefreshSummary(
            asset_id=self._configuration.asset_id,
            request=request,
            submissions_source_id=self._configuration.submissions_source_id,
            submissions_raw_record_id=str(snapshot.record.record_id),
            submissions_checked_at=snapshot.checked_at,
            submissions_record_available_at=snapshot.record.available_at,
            submissions_created=snapshot.created,
            submissions_reused=snapshot.reused,
            insider=insider,
            beneficial=beneficial,
            observations_created=observations.observations_created,
            observations_reused=observations.observations_reused,
            observations_skipped=observations.skipped_total,
            metrics_created=metrics.metrics_created,
            metrics_reused=metrics.metrics_reused,
            metrics_skipped=metrics.skipped_total,
            backlog_count=insider_plan.backlog_count + beneficial_plan.backlog_count,
            coverage_complete=(
                insider_plan.backlog_count == 0
                and beneficial_plan.backlog_count == 0
                and traceability_verified
            ),
            traceability_verified=traceability_verified,
        )

    def _persist_fresh_submissions(self) -> SecSubmissionsSnapshot:
        try:
            return self._submissions_service.persist_fresh_snapshot()
        except SecSubmissionsRefreshError as error:
            raise SecDeclaredActivityRefreshError(str(error)) from error

    def _plan_insider(
        self, snapshot: SecSubmissionsSnapshot, *, known_at: datetime
    ) -> tuple[_FamilyPlan, tuple[OwnershipAccessionState, ...]]:
        try:
            filings = ownership_filings(snapshot.record, self._configuration)
        except (TypeError, ValueError) as error:
            raise SecDeclaredActivityRefreshError("Submissions snapshot is not eligible") from error
        states = self._ownership_repository().list_accession_states(
            asset_id=self._configuration.asset_id,
            known_at=known_at,
        )
        return _plan_family(forms=OWNERSHIP_FORMS, filings=filings, states=states), states

    def _plan_beneficial(
        self, snapshot: SecSubmissionsSnapshot, *, known_at: datetime
    ) -> tuple[_FamilyPlan, tuple[BeneficialOwnershipAccessionState, ...]]:
        try:
            filings = beneficial_ownership_filings(snapshot.record, self._configuration)
        except (TypeError, ValueError) as error:
            raise SecDeclaredActivityRefreshError("Submissions snapshot is not eligible") from error
        states = self._beneficial_repository().list_accession_states(
            asset_id=self._configuration.asset_id,
            known_at=known_at,
        )
        return (
            _plan_family(forms=BENEFICIAL_OWNERSHIP_FORMS, filings=filings, states=states),
            states,
        )

    def _run_insider(
        self,
        selected: tuple[str, ...],
        *,
        accepted_before: frozenset[str],
        known_at: datetime,
    ) -> _FamilyOutcome:
        def attempt(accessions: tuple[str, ...]) -> None:
            self._ownership_pipeline.run(SecOwnershipImportRequest(accessions=accessions))

        return self._run_family(
            selected=selected,
            accepted_before=accepted_before,
            known_at=known_at,
            attempt=attempt,
            read_states=lambda: self._ownership_repository().list_accession_states(
                asset_id=self._configuration.asset_id,
                known_at=known_at,
            ),
            message="declared insider activity could not be imported",
        )

    def _run_beneficial(
        self,
        selected: tuple[str, ...],
        *,
        accepted_before: frozenset[str],
        known_at: datetime,
    ) -> _FamilyOutcome:
        def attempt(accessions: tuple[str, ...]) -> None:
            self._beneficial_pipeline.run(
                SecBeneficialOwnershipImportRequest(accessions=accessions)
            )

        return self._run_family(
            selected=selected,
            accepted_before=accepted_before,
            known_at=known_at,
            attempt=attempt,
            read_states=lambda: self._beneficial_repository().list_accession_states(
                asset_id=self._configuration.asset_id,
                known_at=known_at,
            ),
            message="declared beneficial-ownership activity could not be imported",
        )

    def _run_family(
        self,
        *,
        selected: tuple[str, ...],
        accepted_before: frozenset[str],
        known_at: datetime,
        attempt: _FamilyAttempt,
        read_states: _FamilyStates,
        message: str,
    ) -> _FamilyOutcome:
        """Import one family, resuming after a hard accession failure.

        The integrated importers abort their own loop when one accession raises. The refresh
        keeps a single attempt per selected accession and resumes with the remaining ones, so
        one transient or corrupt accession cannot stall the rest of the family: it is simply
        left unresolved, declared as incomplete, and retried by the next run. A family that
        produced no terminal progress at all still fails with its typed cause so the scheduler
        can classify and retry it.
        """
        remaining = list(selected)
        first_error: Exception | None = None
        while remaining:
            try:
                attempt(tuple(remaining))
                break
            except (StorageError, ValueError, SecEdgarError, HttpRequestError) as error:
                if first_error is None:
                    first_error = error
                states_by_accession = {state.accession: state for state in read_states()}
                failed_index = next(
                    (
                        index
                        for index, accession in enumerate(remaining)
                        if not _terminal(states_by_accession.get(accession))
                    ),
                    None,
                )
                if failed_index is None:
                    raise SecDeclaredActivityRefreshError(message) from error
                remaining = remaining[failed_index + 1 :]
        outcome = _classify_outcome(
            selected=selected,
            accepted_before=accepted_before,
            states=read_states(),
        )
        if first_error is not None and not (outcome.imported or outcome.reused or outcome.rejected):
            raise SecDeclaredActivityRefreshError(message) from first_error
        return outcome

    def _normalize_observations(
        self, *, known_at: datetime
    ) -> DeclaredActivityObservationRunSummary:
        try:
            return self._observation_service.normalize(
                asset_id=self._configuration.asset_id,
                known_at=known_at,
            )
        except (StorageError, ValueError) as error:
            raise SecDeclaredActivityRefreshError(
                "declared-activity observations could not be normalized"
            ) from error

    def _compute_metrics(self, *, known_at: datetime) -> ActivityMetricRunSummary:
        try:
            return self._metric_pipeline.compute(
                asset_id=self._configuration.asset_id,
                known_at=known_at,
            )
        except (StorageError, ValueError) as error:
            raise SecDeclaredActivityRefreshError(
                "declared-activity metrics could not be computed"
            ) from error

    def _ownership_repository(self) -> OwnershipRepository:
        return OwnershipRepository(self._storage.raw_records)

    def _beneficial_repository(self) -> BeneficialOwnershipRepository:
        return BeneficialOwnershipRepository(self._storage.raw_records)


def build_sec_declared_activity_refresh_service(
    storage: LocalStorage,
    *,
    configuration: SecAssetConfiguration,
    submissions_service: SecSubmissionsRefreshService,
    ownership_pipeline: SecOwnershipPipeline,
    beneficial_pipeline: SecBeneficialOwnershipPipeline,
    observation_service: DeclaredActivityObservationService,
    metric_pipeline: ActivityMetricPipeline,
) -> SecDeclaredActivityRefreshService:
    """Keep the facade composition concise while retaining typed dependencies."""
    return SecDeclaredActivityRefreshService(
        storage,
        configuration=configuration,
        submissions_service=submissions_service,
        ownership_pipeline=ownership_pipeline,
        beneficial_pipeline=beneficial_pipeline,
        observation_service=observation_service,
        metric_pipeline=metric_pipeline,
    )


__all__ = [
    "MAX_ACCESSIONS_PER_FAMILY_PER_RUN",
    "SecDeclaredActivityRefreshError",
    "SecDeclaredActivityRefreshService",
    "build_sec_declared_activity_refresh_service",
]
