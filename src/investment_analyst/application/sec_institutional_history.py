"""Application service for the two-close institutional 13F history window.

One attempt probes the official Form 13F Data Sets catalog, resolves exactly the two most recent
adjacent periods, reuses any verifiable persisted revision/snapshot pair, and downloads at most one
missing ZIP per attempt. Once both closes are persisted it intersects the selected managers common
to both, processes exactly one common manager per attempt with a single shared Submissions revision,
materializes both candidate pages through the integrated observation materialization, runs the
already persisted institutional metrics, weights and events, and reconciles the local Cazatiburones
outbox before advancing its cursor. The cursor only orders targets: it never represents a portfolio,
a signal, or proof of ownership.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from investment_analyst.alerts.cazatiburones_notification_models import (
    CazatiburonesNotificationReconciliationSummary,
)
from investment_analyst.analytics.cazatiburones.institutional_event_service import (
    InstitutionalEventService,
)
from investment_analyst.analytics.cazatiburones.institutional_metric_pipeline import (
    InstitutionalMetricPipeline,
)
from investment_analyst.analytics.cazatiburones.institutional_weight_pipeline import (
    InstitutionalWeightPipeline,
)
from investment_analyst.application.cazatiburones_notifications import (
    CazatiburonesNotificationsApplication,
)
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.application.sec_institutional_history_models import (
    SEC_INSTITUTIONAL_HISTORY_WINDOW_SIZE,
    SecInstitutionalHistoryPeriodSummary,
    SecInstitutionalHistoryRequest,
    SecInstitutionalHistorySummary,
    SecInstitutionalHistoryTargetSummary,
)
from investment_analyst.application.sec_institutional_history_state import (
    SEC_INSTITUTIONAL_HISTORY_STATE_FILE_NAME,
    SecInstitutionalHistoryDatasetState,
    SecInstitutionalHistoryState,
    SecInstitutionalHistoryStateStore,
)
from investment_analyst.application.sec_institutional_holdings_refresh import (
    SecInstitutionalHoldingsDirectedPeriodsRefreshRequest,
    SecInstitutionalHoldingsDirectedPeriodsRefreshSummary,
    SecInstitutionalHoldingsDirectedRefreshApplication,
)
from investment_analyst.application.sec_institutional_observation_materialization import (
    SecInstitutionalObservationMaterializationApplication,
    plan_materialization_page,
)
from investment_analyst.application.sec_institutional_observation_materialization_models import (
    SecInstitutionalObservationMaterializationRequest,
)
from investment_analyst.application.sec_institutional_universe import (
    SecInstitutionalUniverseApplication,
)
from investment_analyst.application.sec_institutional_universe_models import (
    SecInstitutionalUniverseRefreshRequest,
)
from investment_analyst.evidence.sec_institutional_holdings.models import (
    INSTITUTIONAL_HOLDINGS_SOURCE_ID,
)
from investment_analyst.evidence.sec_institutional_observations.definitions import (
    SOURCE_ID as OBSERVATIONS_SOURCE_ID,
)
from investment_analyst.evidence.sec_institutional_universe.models import (
    Sec13FDataSetRevision,
    Sec13FManagerCandidate,
    Sec13FManagerUniverseSnapshot,
)
from investment_analyst.evidence.sec_institutional_universe.repository import (
    SecInstitutionalUniverseRepository,
)
from investment_analyst.evidence.sec_institutional_universe.service import (
    SecInstitutionalUniverseService,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpTransport, UrlLibHttpTransport
from investment_analyst.providers.institutional_holdings.sec_13f_data_sets import (
    Sec13FDataSetLink,
    Sec13FDataSetsClient,
)
from investment_analyst.storage import StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode

SEC_INSTITUTIONAL_HISTORY_CATALOG_SOURCE_ID = "sec-edgar:form-13f-data-sets"
CAZATIBURONES_NOTIFICATION_OUTBOX_STATE_FILE_NAME = (
    "cazatiburones_notification_outbox_state_v1.json"
)
_MAX_PLANNED_MANAGERS = 100_000


class SecInstitutionalHistoryError(RuntimeError):
    """Failure executing two-close institutional 13F history operations."""


@dataclass(frozen=True, slots=True)
class HistoryTarget:
    """One manager common to both closes with its exact per-side candidate tuples."""

    asset_id: str
    manager_cik: str
    manager_name: str
    older_report_period: date
    newer_report_period: date
    older_candidates: tuple[Sec13FManagerCandidate, ...]
    newer_candidates: tuple[Sec13FManagerCandidate, ...]


@dataclass(frozen=True, slots=True)
class HistoryWindowPlan:
    """Deterministic intersection of the two selected closes."""

    targets: tuple[HistoryTarget, ...]
    excluded_manager_count: int
    non_comparable_manager_count: int


def plan_history_window(
    older: Sec13FManagerUniverseSnapshot, newer: Sec13FManagerUniverseSnapshot
) -> HistoryWindowPlan:
    """Intersect the selected ``(asset_id, manager_cik)`` pairs present in both closes.

    Ordering is ``(asset_id, manager_cik)`` and every target keeps its own exact candidates, so no
    cartesian product is ever formed and a manager present in a single close is counted as excluded
    instead of being turned into a synthetic prior or current close.
    """
    older_by_key = _selected_candidates(older)
    newer_by_key = _selected_candidates(newer)
    targets: list[HistoryTarget] = []
    excluded = 0
    non_comparable = 0
    for key in sorted(set(older_by_key) | set(newer_by_key)):
        older_candidates = older_by_key.get(key)
        newer_candidates = newer_by_key.get(key)
        if older_candidates is None or newer_candidates is None:
            excluded += 1
            continue
        older_period = older_candidates[0].report_period
        newer_period = newer_candidates[0].report_period
        if older_period >= newer_period:
            non_comparable += 1
            continue
        targets.append(
            HistoryTarget(
                asset_id=key[0],
                manager_cik=key[1],
                manager_name=older_candidates[0].manager_name,
                older_report_period=older_period,
                newer_report_period=newer_period,
                older_candidates=older_candidates,
                newer_candidates=newer_candidates,
            )
        )
    return HistoryWindowPlan(
        targets=tuple(targets),
        excluded_manager_count=excluded,
        non_comparable_manager_count=non_comparable,
    )


def _selected_candidates(
    snapshot: Sec13FManagerUniverseSnapshot,
) -> dict[tuple[str, str], tuple[Sec13FManagerCandidate, ...]]:
    grouped: dict[tuple[str, str], list[Sec13FManagerCandidate]] = {}
    for candidate in snapshot.candidates:
        if not candidate.is_selected:
            continue
        grouped.setdefault((candidate.asset_id, candidate.manager_cik), []).append(candidate)
    return {
        key: tuple(sorted(values, key=lambda item: str(item.candidate_id)))
        for key, values in grouped.items()
    }


def _dataset_state(
    snapshot: Sec13FManagerUniverseSnapshot, link: Sec13FDataSetLink
) -> SecInstitutionalHistoryDatasetState:
    return SecInstitutionalHistoryDatasetState(
        period_start=link.period_start,
        period_end=link.period_end,
        dataset_url=link.url,
        dataset_sha256=snapshot.dataset_sha256,
        snapshot_id=snapshot.snapshot_id,
    )


def _materialization_offsets(
    snapshot: Sec13FManagerUniverseSnapshot,
) -> dict[tuple[str, date], int]:
    page = plan_materialization_page(snapshot, offset=0, limit=_MAX_PLANNED_MANAGERS)
    return {(item.manager_cik, item.report_period): index for index, item in enumerate(page)}


def _period_from_state(
    role: str, side: SecInstitutionalHistoryDatasetState | None, *, reused: bool
) -> SecInstitutionalHistoryPeriodSummary | None:
    if side is None:
        return None
    return SecInstitutionalHistoryPeriodSummary(
        role=role,
        period_start=side.period_start,
        period_end=side.period_end,
        dataset_url=side.dataset_url,
        dataset_sha256=side.dataset_sha256,
        snapshot_id=side.snapshot_id,
        zip_downloaded=False,
        snapshot_reused=reused,
    )


def _period_from_link(
    role: str,
    link: Sec13FDataSetLink,
    snapshot: Sec13FManagerUniverseSnapshot,
    *,
    downloaded: bool,
) -> SecInstitutionalHistoryPeriodSummary:
    return SecInstitutionalHistoryPeriodSummary(
        role=role,
        period_start=link.period_start,
        period_end=link.period_end,
        dataset_url=link.url,
        dataset_sha256=snapshot.dataset_sha256,
        snapshot_id=snapshot.snapshot_id,
        zip_downloaded=downloaded,
        snapshot_reused=not downloaded,
    )


class SecInstitutionalHistoryApplication:
    """Orchestrate one bounded step of the two-close history window under a single writer."""

    def __init__(
        self,
        runtime: ApplicationRuntime,
        *,
        transport_factory: Callable[[], HttpTransport] = UrlLibHttpTransport,
        submissions_client_factory: Callable[..., object] | None = None,
        document_client_factory: Callable[..., object] | None = None,
        state_store: SecInstitutionalHistoryStateStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        service: SecInstitutionalUniverseService | None = None,
    ) -> None:
        self._runtime = runtime
        self._transport_factory = transport_factory
        self._submissions_client_factory = submissions_client_factory
        self._document_client_factory = document_client_factory
        self._state_store = state_store
        self._clock = clock
        self._service = service or SecInstitutionalUniverseService()

    @classmethod
    def create_default(cls) -> SecInstitutionalHistoryApplication:
        return cls(ApplicationRuntime.create_default())

    def run_cycle(
        self,
        request: SecInstitutionalHistoryRequest,
        *,
        sec_identity: SecEdgarIdentity,
        location: StorageLocationRequest | None = None,
        state_root: Path | None = None,
        outbox_state: Path | None = None,
    ) -> SecInstitutionalHistorySummary:
        """Run one bounded step of the two-close history window under one writer connection."""
        storage_req = location or StorageLocationRequest()
        state_store, outbox_path = self._resolve_paths(
            location=storage_req, state_root=state_root, outbox_state=outbox_state
        )
        state = state_store.load()
        effective_known_at = max(request.known_at, self._now())

        client = Sec13FDataSetsClient(
            identity=sec_identity,
            transport=self._transport_factory(),
            clock=self._clock,
        )
        catalog_calls = 0
        try:
            catalog_html = client.fetch_catalog_page()
            catalog_calls += 1
            links = client.discover_catalog_links(catalog_html)
        except Exception as error:
            self._persist_failure(
                state_store=state_store,
                state=state,
                reason_code=type(error).__name__,
                effective_known_at=effective_known_at,
            )
            raise SecInstitutionalHistoryError(f"Catalog probe failed: {error}") from error
        if len(links) < SEC_INSTITUTIONAL_HISTORY_WINDOW_SIZE:
            self._persist_failure(
                state_store=state_store,
                state=state,
                reason_code="insufficient_catalog_periods",
                effective_known_at=effective_known_at,
            )
            raise SecInstitutionalHistoryError(
                "the official catalog exposes fewer than two datasets"
            )
        newer_link, older_link = links[0], links[1]
        if older_link.period_end + timedelta(days=1) != newer_link.period_start:
            self._persist_failure(
                state_store=state_store,
                state=state,
                reason_code="catalog_periods_not_adjacent",
                effective_known_at=effective_known_at,
            )
            raise SecInstitutionalHistoryError(
                "the two most recent official datasets are not adjacent periods"
            )

        state = self._reconcile_declared_window(state, older_link=older_link, newer_link=newer_link)
        zip_calls = 0
        with self._runtime.open_storage(
            storage_req, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            repository = SecInstitutionalUniverseRepository(storage.raw_records, storage.documents)
            older_side = self._resolve_side(
                repository, declared=state.older, link=older_link, known_at=effective_known_at
            )
            newer_side = self._resolve_side(
                repository, declared=state.newer, link=newer_link, known_at=effective_known_at
            )
            pending_link = older_link if older_side is None else None
            if pending_link is None and newer_side is None:
                pending_link = newer_link
            if pending_link is not None:
                try:
                    download = client.fetch_dataset_archive(pending_link)
                    zip_calls += 1
                    universe_app = SecInstitutionalUniverseApplication(
                        self._runtime,
                        transport_factory=self._transport_factory,
                        service=self._service,
                    )
                    universe_result = universe_app.refresh_with_storage(
                        storage,
                        SecInstitutionalUniverseRefreshRequest(),
                        sec_identity=sec_identity,
                        download=download,
                    )
                except Exception as error:
                    self._persist_failure(
                        state_store=state_store,
                        state=state,
                        reason_code=type(error).__name__,
                        effective_known_at=effective_known_at,
                    )
                    raise SecInstitutionalHistoryError(
                        f"Dataset archive refresh failed: {error}"
                    ) from error
                refreshed = repository.get_snapshot(universe_result.snapshot_id)
                if refreshed is None:
                    raise SecInstitutionalHistoryError(
                        "refreshed universe snapshot is absent from storage"
                    )
                refreshed_side = self._refreshed_side(
                    repository, link=pending_link, snapshot=refreshed
                )
                if pending_link is older_link:
                    older_side = refreshed_side
                    role = "older"
                else:
                    newer_side = refreshed_side
                    role = "newer"
                state = self._state_with_side(
                    state,
                    role=role,
                    snapshot=refreshed,
                    link=pending_link,
                    effective_known_at=effective_known_at,
                )
                state = self._mark_state(state, "preparing", None, effective_known_at)
                state_store.write(state)
                return self._preparing_summary(
                    state=state,
                    catalog_calls=catalog_calls,
                    zip_calls=zip_calls,
                    downloaded_role=role,
                    downloaded_link=pending_link,
                    downloaded_snapshot=refreshed,
                )

            state = self._state_with_side(
                state,
                role="older",
                snapshot=older_side[1],
                link=older_link,
                effective_known_at=effective_known_at,
            )
            state = self._state_with_side(
                state,
                role="newer",
                snapshot=newer_side[1],
                link=newer_link,
                effective_known_at=effective_known_at,
            )
            plan = plan_history_window(older_side[1], newer_side[1])
            total_targets = len(plan.targets)
            cursor_before = state.target_cursor if state.total_targets == total_targets else 0
            state = state.model_copy(
                update={
                    "older": _dataset_state(older_side[1], older_link),
                    "newer": _dataset_state(newer_side[1], newer_link),
                    "total_targets": total_targets,
                    "target_cursor": cursor_before,
                    "phase": "completed" if cursor_before >= total_targets else "ready",
                    "updated_at": effective_known_at,
                }
            )
            periods = self._period_summaries(
                older_link=older_link,
                newer_link=newer_link,
                older_snapshot=older_side[1],
                newer_snapshot=newer_side[1],
                reused=True,
                downloaded_role=None,
            )
            source_ids = tuple(
                sorted(
                    {
                        SEC_INSTITUTIONAL_HISTORY_CATALOG_SOURCE_ID,
                        INSTITUTIONAL_HOLDINGS_SOURCE_ID,
                        OBSERVATIONS_SOURCE_ID,
                    }
                )
            )
            if cursor_before >= total_targets:
                state = self._mark_state(state, "completed", None, effective_known_at)
                state_store.write(state)
                return self._window_summary(
                    state=state,
                    status="completed",
                    catalog_calls=catalog_calls,
                    zip_calls=zip_calls,
                    periods=periods,
                    plan=plan,
                    total_targets=total_targets,
                    cursor_before=cursor_before,
                    cursor_after=cursor_before,
                    source_ids=source_ids,
                )
            target = plan.targets[cursor_before]
            target_summary, failure_reason = self._prepare_target(
                storage=storage,
                target=target,
                request=request,
                sec_identity=sec_identity,
                known_at=effective_known_at,
                older_snapshot=older_side[1],
                older_revision=older_side[0],
                newer_snapshot=newer_side[1],
                newer_revision=newer_side[0],
            )
            if failure_reason is not None:
                return self._failed_window_summary(
                    state_store=state_store,
                    state=state,
                    effective_known_at=effective_known_at,
                    reason_code=failure_reason,
                    catalog_calls=catalog_calls,
                    zip_calls=zip_calls,
                    periods=periods,
                    plan=plan,
                    total_targets=total_targets,
                    cursor_before=cursor_before,
                    target=target,
                    target_summary=target_summary,
                    source_ids=source_ids,
                )

        notifications = self._reconcile_outbox(location=storage_req, outbox_path=outbox_path)
        if notifications is None:
            return self._failed_window_summary(
                state_store=state_store,
                state=state,
                effective_known_at=effective_known_at,
                reason_code="outbox_reconciliation_failed",
                catalog_calls=catalog_calls,
                zip_calls=zip_calls,
                periods=periods,
                plan=plan,
                total_targets=total_targets,
                cursor_before=cursor_before,
                target=target,
                target_summary=target_summary,
                source_ids=source_ids,
            )
        cursor_after = cursor_before + 1
        completed = cursor_after >= total_targets
        state = state.model_copy(
            update={
                "target_cursor": cursor_after,
                "total_targets": total_targets,
                "cycle_count": state.cycle_count + 1,
                "phase": "completed" if completed else "ready",
                "last_processed_cik": target.manager_cik,
                "last_status": "completed" if completed else "success",
                "last_error_code": None,
                "updated_at": max(effective_known_at, self._now()),
            }
        )
        state_store.write(state)
        return self._window_summary(
            state=state,
            status="completed" if completed else "processed",
            catalog_calls=catalog_calls,
            zip_calls=zip_calls,
            periods=periods,
            plan=plan,
            total_targets=total_targets,
            cursor_before=cursor_before,
            cursor_after=cursor_after,
            source_ids=source_ids,
            target_summary=target_summary,
            notifications=notifications,
        )

    def _prepare_target(
        self,
        *,
        storage,
        target: HistoryTarget,
        request: SecInstitutionalHistoryRequest,
        sec_identity: SecEdgarIdentity,
        known_at: datetime,
        older_snapshot: Sec13FManagerUniverseSnapshot,
        older_revision: Sec13FDataSetRevision,
        newer_snapshot: Sec13FManagerUniverseSnapshot,
        newer_revision: Sec13FDataSetRevision,
    ) -> tuple[SecInstitutionalHistoryTargetSummary, str | None]:
        """Acquire, materialize and derive one common manager, or return a failure reason."""
        refresh_app = SecInstitutionalHoldingsDirectedRefreshApplication(
            self._runtime,
            transport_factory=self._transport_factory,
            submissions_client_factory=self._submissions_client_factory,
            document_client_factory=self._document_client_factory,
            clock=self._clock,
        )
        try:
            refresh_summary = refresh_app.refresh_periods_with_storage(
                storage,
                SecInstitutionalHoldingsDirectedPeriodsRefreshRequest(
                    known_at=known_at,
                    manager_cik=target.manager_cik,
                    report_periods=(target.older_report_period, target.newer_report_period),
                    accessions_per_period=request.accessions_per_period,
                ),
                sec_identity=sec_identity,
            )
        except Exception as error:
            return (
                self._empty_target_summary(
                    target=target, state="failed", reason_code=type(error).__name__
                ),
                type(error).__name__,
            )
        target_summary = SecInstitutionalHistoryTargetSummary(
            asset_id=target.asset_id,
            manager_cik=target.manager_cik,
            manager_name=target.manager_name,
            older_report_period=target.older_report_period,
            newer_report_period=target.newer_report_period,
            older_candidate_ids=tuple(item.candidate_id for item in target.older_candidates),
            newer_candidate_ids=tuple(item.candidate_id for item in target.newer_candidates),
            state="processed",
            submissions_calls=refresh_summary.submissions_calls,
            archives_calls=refresh_summary.archives_calls,
            created_accessions=refresh_summary.created_accessions,
            reused_accessions=refresh_summary.reused_accessions,
            rejected_accessions=refresh_summary.rejected_accessions,
            failed_accessions=refresh_summary.failed_accessions,
            non_evaluable=_semantic_non_evaluable(refresh_summary),
        )
        if refresh_summary.state == "failed" or refresh_summary.failed_accessions:
            return (
                target_summary.model_copy(
                    update={
                        "state": "failed",
                        "reason_code": _refresh_reason(refresh_summary),
                    }
                ),
                _refresh_reason(refresh_summary),
            )
        materialize_app = SecInstitutionalObservationMaterializationApplication(
            self._runtime, clock=self._clock
        )
        observations: dict[str, tuple[int, int]] = {}
        for role, snapshot, revision, period in (
            ("older", older_snapshot, older_revision, target.older_report_period),
            ("newer", newer_snapshot, newer_revision, target.newer_report_period),
        ):
            offset = _materialization_offsets(snapshot).get((target.manager_cik, period))
            if offset is None:
                reason = f"{role}_target_not_planned"
                return (
                    target_summary.model_copy(update={"state": "failed", "reason_code": reason}),
                    reason,
                )
            try:
                materialization = materialize_app.materialize_with_storage(
                    storage,
                    SecInstitutionalObservationMaterializationRequest(
                        known_at=known_at, manager_offset=offset, manager_limit=1
                    ),
                    snapshot=snapshot,
                    revision=revision,
                )
            except Exception as error:
                reason = type(error).__name__
                return (
                    target_summary.model_copy(update={"state": "failed", "reason_code": reason}),
                    reason,
                )
            if (
                materialization.failed_candidates > 0
                or materialization.failed_runs > 0
                or not materialization.traceability_verified
            ):
                reason = f"{role}_materialization_unverified"
                return (
                    target_summary.model_copy(update={"state": "failed", "reason_code": reason}),
                    reason,
                )
            observations[role] = (
                materialization.observations_created,
                materialization.observations_reused,
            )
        try:
            derived = self._derive_target(
                storage=storage,
                target=target,
                target_summary=target_summary,
                known_at=max(known_at, self._now()),
                observations=observations,
            )
        except Exception as error:
            reason = type(error).__name__
            return (
                target_summary.model_copy(update={"state": "failed", "reason_code": reason}),
                reason,
            )
        return derived, None

    def _derive_target(
        self,
        *,
        storage,
        target: HistoryTarget,
        target_summary: SecInstitutionalHistoryTargetSummary,
        known_at: datetime,
        observations: dict[str, tuple[int, int]],
    ) -> SecInstitutionalHistoryTargetSummary:
        """Run the integrated institutional chain without changing any formula or threshold."""
        metrics = InstitutionalMetricPipeline(storage, clock=self._clock).compute(
            asset_id=target.asset_id,
            manager_cik=target.manager_cik,
            known_at=known_at,
        )
        weights = InstitutionalWeightPipeline(storage, clock=self._clock).compute(
            asset_id=target.asset_id,
            manager_cik=target.manager_cik,
            known_at=known_at,
        )
        events = InstitutionalEventService(storage, clock=self._clock).materialize(
            asset_id=target.asset_id,
            manager_cik=target.manager_cik,
            known_at=known_at,
        )
        non_evaluable = dict(target_summary.non_evaluable)
        for name, counts in (
            ("metric", metrics.skipped_by_reason),
            ("weight", weights.skipped_by_reason),
        ):
            for reason, count in counts.items():
                key = f"{name}:{reason}"
                non_evaluable[key] = non_evaluable.get(key, 0) + count
        return target_summary.model_copy(
            update={
                "older_observations_created": observations.get("older", (0, 0))[0],
                "older_observations_reused": observations.get("older", (0, 0))[1],
                "newer_observations_created": observations.get("newer", (0, 0))[0],
                "newer_observations_reused": observations.get("newer", (0, 0))[1],
                "metrics_created": metrics.metrics_created,
                "metrics_reused": metrics.metrics_reused,
                "weights_created": weights.metrics_created,
                "weights_reused": weights.metrics_reused,
                "events_created": events.events if events.created else 0,
                "event_candidates": events.candidates,
                "non_evaluable": non_evaluable,
                "traceability_verified": True,
            }
        )

    def _reconcile_outbox(
        self, *, location: StorageLocationRequest, outbox_path: Path
    ) -> CazatiburonesNotificationReconciliationSummary | None:
        """Reconcile the existing local outbox once the writer connection is closed."""
        try:
            return CazatiburonesNotificationsApplication(self._runtime).reconcile(
                location=location, outbox_state=outbox_path
            )
        except Exception:
            return None

    def _resolve_paths(
        self,
        *,
        location: StorageLocationRequest,
        state_root: Path | None,
        outbox_state: Path | None,
    ) -> tuple[SecInstitutionalHistoryStateStore, Path]:
        if state_root is not None:
            resolved_state_root = state_root
        elif location.legacy_root is not None:
            resolved_state_root = location.legacy_root / "state"
        else:
            resolved_state_root = self._runtime.workspace_service.resolve(
                location.workspace
            ).state_root
        store = self._state_store or SecInstitutionalHistoryStateStore(
            resolved_state_root / SEC_INSTITUTIONAL_HISTORY_STATE_FILE_NAME
        )
        resolved_outbox = (
            outbox_state
            if outbox_state is not None
            else resolved_state_root / CAZATIBURONES_NOTIFICATION_OUTBOX_STATE_FILE_NAME
        )
        return store, resolved_outbox

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise SecInstitutionalHistoryError("history clock must be timezone-aware")
        return value.astimezone(UTC)

    def _persist_failure(
        self,
        *,
        state_store: SecInstitutionalHistoryStateStore,
        state: SecInstitutionalHistoryState,
        reason_code: str,
        effective_known_at: datetime,
    ) -> None:
        """Record a bounded failure without touching previously persisted evidence."""
        state_store.write(self._mark_state(state, "failed", reason_code, effective_known_at))

    def _mark_state(
        self,
        state: SecInstitutionalHistoryState,
        status: str,
        error_code: str | None,
        effective_known_at: datetime,
    ) -> SecInstitutionalHistoryState:
        return state.model_copy(
            update={
                "last_status": status,
                "last_error_code": error_code,
                "updated_at": max(effective_known_at, self._now()),
            }
        )

    def _reconcile_declared_window(
        self,
        state: SecInstitutionalHistoryState,
        *,
        older_link: Sec13FDataSetLink,
        newer_link: Sec13FDataSetLink,
    ) -> SecInstitutionalHistoryState:
        """Discard a declared side that no longer matches the live catalog before resolving."""
        older = state.older
        newer = state.newer
        if older is not None and (
            older.period_start,
            older.period_end,
            older.dataset_url,
        ) != (older_link.period_start, older_link.period_end, older_link.url):
            older = None
        if newer is not None and (
            newer.period_start,
            newer.period_end,
            newer.dataset_url,
        ) != (newer_link.period_start, newer_link.period_end, newer_link.url):
            newer = None
        if older == state.older and newer == state.newer:
            return state
        return state.model_copy(
            update={
                "older": older,
                "newer": newer,
                "target_cursor": 0,
                "total_targets": None,
                "phase": "preparing",
            }
        )

    def _state_with_side(
        self,
        state: SecInstitutionalHistoryState,
        *,
        role: str,
        snapshot: Sec13FManagerUniverseSnapshot,
        link: Sec13FDataSetLink,
        effective_known_at: datetime,
    ) -> SecInstitutionalHistoryState:
        """Persist one resolved side, resetting progress only when the evidence actually changed."""
        current = state.older if role == "older" else state.newer
        resolved = _dataset_state(snapshot, link)
        if current == resolved:
            return state
        return state.model_copy(
            update={
                "older": resolved if role == "older" else state.older,
                "newer": resolved if role == "newer" else state.newer,
                "phase": "preparing",
                "target_cursor": 0,
                "total_targets": None,
                "updated_at": effective_known_at,
            }
        )

    def _resolve_side(
        self,
        repository: SecInstitutionalUniverseRepository,
        *,
        declared: SecInstitutionalHistoryDatasetState | None,
        link: Sec13FDataSetLink,
        known_at: datetime,
    ) -> tuple[Sec13FDataSetRevision, Sec13FManagerUniverseSnapshot] | None:
        """Reuse a verifiable persisted revision and snapshot for one exact catalog period."""
        if declared is not None and (
            declared.period_start,
            declared.period_end,
            declared.dataset_url,
        ) == (link.period_start, link.period_end, link.url):
            snapshot = repository.get_snapshot(declared.snapshot_id)
            if snapshot is not None:
                revision = repository.get_dataset_revision(snapshot.dataset_revision_id)
                if revision is not None and self._verified_side(
                    repository,
                    declared=declared,
                    revision=revision,
                    snapshot=snapshot,
                    link=link,
                    known_at=known_at,
                ):
                    return revision, snapshot
        try:
            found = repository.find_snapshot_for_period(
                period_start=link.period_start,
                period_end=link.period_end,
                dataset_url=link.url,
                known_at=known_at,
            )
        except StorageError:
            return None
        if found is None:
            return None
        revision, snapshot = found
        if snapshot.available_at > known_at:
            return None
        return revision, snapshot

    def _verified_side(
        self,
        repository: SecInstitutionalUniverseRepository,
        *,
        declared: SecInstitutionalHistoryDatasetState,
        revision: Sec13FDataSetRevision,
        snapshot: Sec13FManagerUniverseSnapshot,
        link: Sec13FDataSetLink,
        known_at: datetime,
    ) -> bool:
        if (revision.period_start, revision.period_end) != (link.period_start, link.period_end):
            return False
        if revision.dataset_url != link.url or snapshot.snapshot_id != declared.snapshot_id:
            return False
        if revision.content_sha256 != declared.dataset_sha256:
            return False
        if snapshot.dataset_sha256 != declared.dataset_sha256:
            return False
        if snapshot.available_at > known_at:
            return False
        try:
            repository.verify_blob(snapshot.dataset_sha256, size_bytes=revision.size_bytes)
        except StorageError:
            return False
        return True

    def _refreshed_side(
        self,
        repository: SecInstitutionalUniverseRepository,
        *,
        link: Sec13FDataSetLink,
        snapshot: Sec13FManagerUniverseSnapshot,
    ) -> tuple[Sec13FDataSetRevision, Sec13FManagerUniverseSnapshot]:
        revision = repository.get_dataset_revision(snapshot.dataset_revision_id)
        if revision is None:
            raise SecInstitutionalHistoryError("refreshed dataset revision is absent from storage")
        if (revision.period_start, revision.period_end) != (link.period_start, link.period_end):
            raise SecInstitutionalHistoryError(
                "refreshed dataset period conflicts with the catalog"
            )
        if revision.dataset_url != link.url:
            raise SecInstitutionalHistoryError("refreshed dataset URL conflicts with the catalog")
        if snapshot.dataset_sha256 != revision.content_sha256:
            raise SecInstitutionalHistoryError("refreshed snapshot lineage does not verify")
        return revision, snapshot

    def _period_summaries(
        self,
        *,
        older_link: Sec13FDataSetLink,
        newer_link: Sec13FDataSetLink,
        older_snapshot: Sec13FManagerUniverseSnapshot,
        newer_snapshot: Sec13FManagerUniverseSnapshot,
        reused: bool,
        downloaded_role: str | None,
    ) -> tuple[SecInstitutionalHistoryPeriodSummary, ...]:
        return (
            SecInstitutionalHistoryPeriodSummary(
                role="older",
                period_start=older_link.period_start,
                period_end=older_link.period_end,
                dataset_url=older_link.url,
                dataset_sha256=older_snapshot.dataset_sha256,
                snapshot_id=older_snapshot.snapshot_id,
                zip_downloaded=downloaded_role == "older",
                snapshot_reused=reused and downloaded_role != "older",
            ),
            SecInstitutionalHistoryPeriodSummary(
                role="newer",
                period_start=newer_link.period_start,
                period_end=newer_link.period_end,
                dataset_url=newer_link.url,
                dataset_sha256=newer_snapshot.dataset_sha256,
                snapshot_id=newer_snapshot.snapshot_id,
                zip_downloaded=downloaded_role == "newer",
                snapshot_reused=reused and downloaded_role != "newer",
            ),
        )

    def _preparing_summary(
        self,
        *,
        state: SecInstitutionalHistoryState,
        catalog_calls: int,
        zip_calls: int,
        downloaded_role: str,
        downloaded_link: Sec13FDataSetLink,
        downloaded_snapshot: Sec13FManagerUniverseSnapshot,
    ) -> SecInstitutionalHistorySummary:
        """Report one preparation-only attempt that consumed no manager."""
        pending = state.older if downloaded_role == "newer" else state.newer
        periods = (
            (
                _period_from_state("older", pending, reused=True),
                _period_from_link("newer", downloaded_link, downloaded_snapshot, downloaded=True),
            )
            if downloaded_role == "newer"
            else (
                _period_from_link("older", downloaded_link, downloaded_snapshot, downloaded=True),
                _period_from_state("newer", pending, reused=True),
            )
        )
        return SecInstitutionalHistorySummary(
            effective_known_at=state.updated_at,
            status="preparing",
            phase=state.phase,
            catalog_calls=catalog_calls,
            zip_calls=zip_calls,
            periods=tuple(item for item in periods if item is not None),
            target_cursor_before=0,
            target_cursor_after=0,
            source_ids=(SEC_INSTITUTIONAL_HISTORY_CATALOG_SOURCE_ID,),
        )

    def _window_summary(
        self,
        *,
        state: SecInstitutionalHistoryState,
        status: str,
        catalog_calls: int,
        zip_calls: int,
        periods: tuple[SecInstitutionalHistoryPeriodSummary, ...],
        plan: HistoryWindowPlan,
        total_targets: int,
        cursor_before: int,
        cursor_after: int,
        source_ids: tuple[str, ...],
        target_summary: SecInstitutionalHistoryTargetSummary | None = None,
        notifications: CazatiburonesNotificationReconciliationSummary | None = None,
        reason_code: str | None = None,
        manager_cik: str | None = None,
        manager_name: str | None = None,
    ) -> SecInstitutionalHistorySummary:
        return SecInstitutionalHistorySummary(
            effective_known_at=state.updated_at,
            status=status,
            phase=state.phase,
            reason_code=reason_code,
            catalog_calls=catalog_calls,
            zip_calls=zip_calls,
            submissions_calls=0 if target_summary is None else target_summary.submissions_calls,
            archives_calls=0 if target_summary is None else target_summary.archives_calls,
            periods=periods,
            common_manager_count=total_targets,
            excluded_manager_count=plan.excluded_manager_count,
            non_comparable_manager_count=plan.non_comparable_manager_count,
            total_targets=total_targets,
            target_cursor_before=cursor_before,
            target_cursor_after=cursor_after,
            coverage_complete=cursor_after >= total_targets,
            manager_cik=(
                manager_cik or (None if target_summary is None else target_summary.manager_cik)
            ),
            manager_name=(
                manager_name or (None if target_summary is None else target_summary.manager_name)
            ),
            target=target_summary,
            notifications_created=0 if notifications is None else notifications.created_items,
            notifications_reused=0 if notifications is None else notifications.reused_items,
            source_ids=source_ids,
        )

    def _failed_window_summary(
        self,
        *,
        state_store: SecInstitutionalHistoryStateStore,
        state: SecInstitutionalHistoryState,
        effective_known_at: datetime,
        reason_code: str,
        catalog_calls: int,
        zip_calls: int,
        periods: tuple[SecInstitutionalHistoryPeriodSummary, ...],
        plan: HistoryWindowPlan,
        total_targets: int,
        cursor_before: int,
        target: HistoryTarget,
        target_summary: SecInstitutionalHistoryTargetSummary,
        source_ids: tuple[str, ...],
    ) -> SecInstitutionalHistorySummary:
        """Preserve append-only progress and leave the target resumable without advancing it."""
        failed_target = target_summary.model_copy(
            update={"state": "failed", "reason_code": reason_code}
        )
        state = self._mark_state(state, "failed", reason_code, effective_known_at)
        state_store.write(state)
        return self._window_summary(
            state=state,
            status="failed",
            catalog_calls=catalog_calls,
            zip_calls=zip_calls,
            periods=periods,
            plan=plan,
            total_targets=total_targets,
            cursor_before=cursor_before,
            cursor_after=cursor_before,
            source_ids=source_ids,
            target_summary=failed_target,
            reason_code=reason_code,
            manager_cik=target.manager_cik,
            manager_name=target.manager_name,
        )

    def _empty_target_summary(
        self, *, target: HistoryTarget, state: str, reason_code: str | None
    ) -> SecInstitutionalHistoryTargetSummary:
        return SecInstitutionalHistoryTargetSummary(
            asset_id=target.asset_id,
            manager_cik=target.manager_cik,
            manager_name=target.manager_name,
            older_report_period=target.older_report_period,
            newer_report_period=target.newer_report_period,
            older_candidate_ids=tuple(item.candidate_id for item in target.older_candidates),
            newer_candidate_ids=tuple(item.candidate_id for item in target.newer_candidates),
            state=state,
            reason_code=reason_code,
        )


def _semantic_non_evaluable(
    refresh_summary: SecInstitutionalHoldingsDirectedPeriodsRefreshSummary,
) -> dict[str, int]:
    """Expose the explicit non-evaluable states observed during the shared acquisition."""
    observed = {
        "semantics_not_visible": refresh_summary.semantics_not_visible,
        "semantics_rejected": refresh_summary.semantics_rejected,
    }
    return {reason: count for reason, count in sorted(observed.items()) if count}


def _refresh_reason(
    refresh_summary: SecInstitutionalHoldingsDirectedPeriodsRefreshSummary,
) -> str:
    if refresh_summary.reason_code is not None:
        return refresh_summary.reason_code
    for period in refresh_summary.periods:
        if period.failure_codes:
            return period.failure_codes[0]
    return "period_import_failed"


__all__ = [
    "CAZATIBURONES_NOTIFICATION_OUTBOX_STATE_FILE_NAME",
    "SEC_INSTITUTIONAL_HISTORY_CATALOG_SOURCE_ID",
    "HistoryTarget",
    "HistoryWindowPlan",
    "SecInstitutionalHistoryApplication",
    "SecInstitutionalHistoryError",
    "plan_history_window",
]
