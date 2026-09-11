"""Application service for running the scheduled Form 13F cycle.

Coordinates the Form 13F lifecycle across universe discovery, directed acquisition, and observation
materialization under a single writer connection, preserving deterministic progress via an atomic
state file and respecting official network budgets.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.application.sec_institutional_cycle_models import (
    SecInstitutionalCycleRequest,
    SecInstitutionalCycleSummary,
)
from investment_analyst.application.sec_institutional_cycle_state import (
    SEC_INSTITUTIONAL_CYCLE_STATE_FILE_NAME,
    SecInstitutionalCycleState,
    SecInstitutionalCycleStateStore,
)
from investment_analyst.application.sec_institutional_holdings_refresh import (
    SecInstitutionalHoldingsDirectedRefreshApplication,
    plan_directed_manager_page,
)
from investment_analyst.application.sec_institutional_holdings_refresh_models import (
    SecInstitutionalHoldingsDirectedRefreshRequest,
)
from investment_analyst.application.sec_institutional_observation_materialization import (
    SecInstitutionalObservationMaterializationApplication,
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
from investment_analyst.evidence.sec_institutional_universe.repository import (
    SecInstitutionalUniverseRepository,
)
from investment_analyst.evidence.sec_institutional_universe.service import (
    SecInstitutionalUniverseService,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpTransport, UrlLibHttpTransport
from investment_analyst.providers.institutional_holdings.sec_13f_data_sets import (
    Sec13FDataSetsClient,
)
from investment_analyst.workspace.models import WorkspaceAccessMode

_VALIDATION_CACHE_TTL_SECONDS = 7 * 86400  # 7 days


class SecInstitutionalCycleError(RuntimeError):
    """Failure executing scheduled Form 13F cycle operations."""


class SecInstitutionalCycleApplication:
    """Orchestrate one step of the Form 13F cycle with bounded network and atomic progress."""

    def __init__(
        self,
        runtime: ApplicationRuntime,
        *,
        transport_factory: Callable[[], HttpTransport] = UrlLibHttpTransport,
        submissions_client_factory: Callable[..., object] | None = None,
        document_client_factory: Callable[..., object] | None = None,
        state_store: SecInstitutionalCycleStateStore | None = None,
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
    def create_default(cls) -> SecInstitutionalCycleApplication:
        return cls(ApplicationRuntime.create_default())

    def _resolve_state_store(
        self,
        *,
        location: StorageLocationRequest | None,
        state_root: Path | None,
    ) -> SecInstitutionalCycleStateStore:
        if self._state_store is not None:
            return self._state_store
        if state_root is not None:
            return SecInstitutionalCycleStateStore(
                state_root / SEC_INSTITUTIONAL_CYCLE_STATE_FILE_NAME
            )
        storage_req = location or StorageLocationRequest()
        paths = self._runtime.workspace_service.resolve(storage_req.workspace)
        return SecInstitutionalCycleStateStore(
            paths.state_root / SEC_INSTITUTIONAL_CYCLE_STATE_FILE_NAME
        )

    def run_cycle(
        self,
        request: SecInstitutionalCycleRequest,
        *,
        sec_identity: SecEdgarIdentity,
        location: StorageLocationRequest | None = None,
        state_root: Path | None = None,
    ) -> SecInstitutionalCycleSummary:
        """Run one bounded step of the Form 13F cycle under a single storage connection."""
        state_store = self._resolve_state_store(location=location, state_root=state_root)
        state = state_store.load()
        storage_req = location or StorageLocationRequest()

        with self._runtime.open_storage(
            storage_req, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            return self._execute_cycle_step(
                storage=storage,
                state_store=state_store,
                state=state,
                request=request,
                sec_identity=sec_identity,
            )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise SecInstitutionalCycleError("cycle clock must be timezone-aware")
        return value.astimezone(UTC)

    def _execute_cycle_step(
        self,
        *,
        storage,
        state_store: SecInstitutionalCycleStateStore,
        state: SecInstitutionalCycleState,
        request: SecInstitutionalCycleRequest,
        sec_identity: SecEdgarIdentity,
    ) -> SecInstitutionalCycleSummary:
        catalog_calls = 0
        zip_calls = 0
        effective_known_at = max(request.known_at, self._now())

        # Stage 1: Universe catalog probe and conditional dataset refresh
        client = Sec13FDataSetsClient(
            identity=sec_identity,
            transport=self._transport_factory(),
            clock=self._clock,
        )
        try:
            catalog_html = client.fetch_catalog_page()
            catalog_calls += 1
            links = client.discover_catalog_links(catalog_html)
            latest_link = links[0]
        except Exception as error:
            error_code = type(error).__name__
            state_store.write(
                state.model_copy(
                    update={
                        "last_status": "failed",
                        "last_error_code": error_code,
                        "updated_at": effective_known_at,
                    }
                )
            )
            raise SecInstitutionalCycleError(f"Catalog probe failed: {error}") from error

        repo = SecInstitutionalUniverseRepository(storage.raw_records, storage.documents)
        existing_snapshot = (
            repo.get_snapshot(state.snapshot_id) if state.snapshot_id is not None else None
        )

        need_zip_download = False
        if (
            existing_snapshot is None
            or state.snapshot_id is None
            or (
                latest_link.url != state.dataset_url
                or latest_link.period_start != state.dataset_period_start
                or latest_link.period_end != state.dataset_period_end
            )
            or state.dataset_last_validated_at is None
            or (
                (effective_known_at - state.dataset_last_validated_at).total_seconds()
                >= _VALIDATION_CACHE_TTL_SECONDS
            )
            or request.force_dataset_refresh
        ):
            need_zip_download = True

        if need_zip_download:
            try:
                download = client.fetch_dataset_archive(latest_link)
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
                # Acceptance 2: download failure must not alter snapshot/cursor nor validation ts
                error_code = type(error).__name__
                state_store.write(
                    state.model_copy(
                        update={
                            "last_status": "failed",
                            "last_error_code": error_code,
                            "updated_at": effective_known_at,
                        }
                    )
                )
                raise SecInstitutionalCycleError(
                    f"Dataset archive refresh failed: {error}"
                ) from error

            snapshot = repo.get_snapshot(universe_result.snapshot_id)
            if snapshot is None:
                raise SecInstitutionalCycleError("Refreshed snapshot not found in storage")
            revision = repo.get_dataset_revision(universe_result.revision_id)
            if revision is None:
                raise SecInstitutionalCycleError("Refreshed revision not found in storage")

            effective_known_at = max(effective_known_at, self._now())
            new_snapshot_id = snapshot.snapshot_id
            cursor = 0 if new_snapshot_id != state.snapshot_id else state.manager_cursor

            state = state.model_copy(
                update={
                    "snapshot_id": new_snapshot_id,
                    "dataset_period_start": snapshot.period_start,
                    "dataset_period_end": snapshot.period_end,
                    "dataset_url": universe_result.dataset_url,
                    "dataset_sha256": snapshot.dataset_sha256,
                    "dataset_last_validated_at": effective_known_at,
                    "manager_cursor": cursor,
                    "updated_at": effective_known_at,
                }
            )
        else:
            assert existing_snapshot is not None
            snapshot = existing_snapshot
            revision = repo.get_dataset_revision(snapshot.dataset_revision_id)
            if revision is None:
                raise SecInstitutionalCycleError("Dataset revision not found for snapshot")

        all_targets = plan_directed_manager_page(snapshot, offset=0, limit=100_000)
        total_managers = len(all_targets)
        cursor_before = state.manager_cursor

        source_ids = tuple(
            sorted(
                {
                    "sec-edgar:form-13f-data-sets",
                    INSTITUTIONAL_HOLDINGS_SOURCE_ID,
                    OBSERVATIONS_SOURCE_ID,
                }
            )
        )

        if cursor_before >= total_managers:
            state = state.model_copy(
                update={
                    "total_managers": total_managers,
                    "last_status": "completed",
                    "last_error_code": None,
                    "updated_at": effective_known_at,
                }
            )
            state_store.write(state)
            return SecInstitutionalCycleSummary(
                effective_known_at=effective_known_at,
                status="completed",
                catalog_calls=catalog_calls,
                zip_calls=zip_calls,
                submissions_calls=0,
                archives_calls=0,
                dataset_period_start=snapshot.period_start,
                dataset_period_end=snapshot.period_end,
                dataset_url=state.dataset_url,
                dataset_sha256=snapshot.dataset_sha256,
                snapshot_id=snapshot.snapshot_id,
                manager_cursor_before=cursor_before,
                manager_cursor_after=cursor_before,
                total_managers=total_managers,
                coverage_complete=True,
                created_accessions=(),
                reused_accessions=(),
                rejected_accessions=(),
                failed_accessions=(),
                backlog_after=0,
                observations_created=0,
                observations_reused=0,
                traceability_verified=True,
                source_ids=source_ids,
            )

        target = all_targets[cursor_before]

        # Stage 2: Directed Holdings Refresh for one manager
        refresh_app = SecInstitutionalHoldingsDirectedRefreshApplication(
            self._runtime,
            transport_factory=self._transport_factory,
            submissions_client_factory=self._submissions_client_factory,
            document_client_factory=self._document_client_factory,
            clock=self._clock,
        )
        effective_known_at = max(effective_known_at, self._now())
        refresh_request = SecInstitutionalHoldingsDirectedRefreshRequest(
            known_at=effective_known_at,
            manager_offset=cursor_before,
            manager_limit=1,
            accessions_per_manager=request.accessions_per_manager,
        )
        try:
            refresh_summary = refresh_app.refresh_with_storage(
                storage,
                refresh_request,
                sec_identity=sec_identity,
                snapshot=snapshot,
                revision=revision,
            )
        except Exception as error:
            error_code = type(error).__name__
            state = state.model_copy(
                update={
                    "last_status": "failed",
                    "last_error_code": error_code,
                    "total_managers": total_managers,
                    "updated_at": effective_known_at,
                }
            )
            state_store.write(state)
            raise SecInstitutionalCycleError(f"Directed refresh failed: {error}") from error

        manager_summary = refresh_summary.managers[0]
        if manager_summary.state == "failed" or len(manager_summary.failed_accessions) > 0:
            error_code = manager_summary.reason_code or (
                manager_summary.failure_codes[0]
                if manager_summary.failure_codes
                else "accession_fetch_failed"
            )
            state = state.model_copy(
                update={
                    "last_status": "failed",
                    "last_error_code": error_code,
                    "total_managers": total_managers,
                    "updated_at": effective_known_at,
                }
            )
            state_store.write(state)
            return SecInstitutionalCycleSummary(
                effective_known_at=effective_known_at,
                status="failed",
                reason_code=error_code,
                catalog_calls=catalog_calls,
                zip_calls=zip_calls,
                submissions_calls=refresh_summary.submissions_calls,
                archives_calls=refresh_summary.archives_calls,
                dataset_period_start=snapshot.period_start,
                dataset_period_end=snapshot.period_end,
                dataset_url=state.dataset_url,
                dataset_sha256=snapshot.dataset_sha256,
                snapshot_id=snapshot.snapshot_id,
                manager_cursor_before=cursor_before,
                manager_cursor_after=cursor_before,
                total_managers=total_managers,
                coverage_complete=False,
                manager_cik=target.manager_cik,
                manager_name=target.manager_name,
                report_period=target.report_period,
                created_accessions=manager_summary.created_accessions,
                reused_accessions=manager_summary.reused_accessions,
                rejected_accessions=manager_summary.rejected_accessions,
                failed_accessions=manager_summary.failed_accessions,
                backlog_after=manager_summary.backlog_after,
                observations_created=0,
                observations_reused=0,
                traceability_verified=False,
                source_ids=source_ids,
            )

        # Stage 3: Observation Materialization for that manager
        materialize_app = SecInstitutionalObservationMaterializationApplication(
            self._runtime,
            clock=self._clock,
        )
        effective_known_at = max(effective_known_at, self._now())
        materialize_request = SecInstitutionalObservationMaterializationRequest(
            known_at=effective_known_at,
            manager_offset=cursor_before,
            manager_limit=1,
        )
        try:
            mat_summary = materialize_app.materialize_with_storage(
                storage,
                materialize_request,
                snapshot=snapshot,
                revision=revision,
            )
        except Exception as error:
            error_code = type(error).__name__
            state = state.model_copy(
                update={
                    "last_status": "failed",
                    "last_error_code": error_code,
                    "total_managers": total_managers,
                    "updated_at": effective_known_at,
                }
            )
            state_store.write(state)
            raise SecInstitutionalCycleError(
                f"Observation materialization failed: {error}"
            ) from error

        if (
            mat_summary.failed_candidates > 0
            or mat_summary.failed_runs > 0
            or not mat_summary.traceability_verified
        ):
            if mat_summary.failed_candidates > 0:
                first_failed = next(c for c in mat_summary.candidates if c.state == "failed")
                reason_code = f"candidate_failed:{first_failed.reason_code}"
            elif mat_summary.failed_runs > 0:
                first_failed_run = next(r for r in mat_summary.runs if r.state == "failed")
                reason_code = f"run_failed:{first_failed_run.reason_code}"
            else:
                reason_code = "materialization_unverified"
            state = state.model_copy(
                update={
                    "last_status": "failed",
                    "last_error_code": reason_code,
                    "total_managers": total_managers,
                    "updated_at": effective_known_at,
                }
            )
            state_store.write(state)
            return SecInstitutionalCycleSummary(
                effective_known_at=effective_known_at,
                status="failed",
                reason_code=reason_code,
                catalog_calls=catalog_calls,
                zip_calls=zip_calls,
                submissions_calls=refresh_summary.submissions_calls,
                archives_calls=refresh_summary.archives_calls,
                dataset_period_start=snapshot.period_start,
                dataset_period_end=snapshot.period_end,
                dataset_url=state.dataset_url,
                dataset_sha256=snapshot.dataset_sha256,
                snapshot_id=snapshot.snapshot_id,
                manager_cursor_before=cursor_before,
                manager_cursor_after=cursor_before,
                total_managers=total_managers,
                coverage_complete=False,
                manager_cik=target.manager_cik,
                manager_name=target.manager_name,
                report_period=target.report_period,
                created_accessions=manager_summary.created_accessions,
                reused_accessions=manager_summary.reused_accessions,
                rejected_accessions=manager_summary.rejected_accessions,
                failed_accessions=manager_summary.failed_accessions,
                backlog_after=manager_summary.backlog_after,
                observations_created=mat_summary.observations_created,
                observations_reused=mat_summary.observations_reused,
                traceability_verified=False,
                source_ids=source_ids,
            )

        # Stage 4: Advance cursor and atomically persist state
        cursor_after = cursor_before + 1
        is_coverage_complete = cursor_after >= total_managers
        effective_known_at = max(effective_known_at, self._now())

        state = state.model_copy(
            update={
                "manager_cursor": cursor_after,
                "total_managers": total_managers,
                "cycle_count": state.cycle_count + 1,
                "last_processed_cik": target.manager_cik,
                "last_status": "success",
                "last_error_code": None,
                "updated_at": effective_known_at,
            }
        )
        state_store.write(state)

        return SecInstitutionalCycleSummary(
            effective_known_at=effective_known_at,
            status="processed",
            catalog_calls=catalog_calls,
            zip_calls=zip_calls,
            submissions_calls=refresh_summary.submissions_calls,
            archives_calls=refresh_summary.archives_calls,
            dataset_period_start=snapshot.period_start,
            dataset_period_end=snapshot.period_end,
            dataset_url=state.dataset_url,
            dataset_sha256=snapshot.dataset_sha256,
            snapshot_id=snapshot.snapshot_id,
            manager_cursor_before=cursor_before,
            manager_cursor_after=cursor_after,
            total_managers=total_managers,
            coverage_complete=is_coverage_complete,
            manager_cik=target.manager_cik,
            manager_name=target.manager_name,
            report_period=target.report_period,
            created_accessions=manager_summary.created_accessions,
            reused_accessions=manager_summary.reused_accessions,
            rejected_accessions=manager_summary.rejected_accessions,
            failed_accessions=manager_summary.failed_accessions,
            backlog_after=manager_summary.backlog_after,
            observations_created=mat_summary.observations_created,
            observations_reused=mat_summary.observations_reused,
            traceability_verified=True,
            source_ids=source_ids,
        )
