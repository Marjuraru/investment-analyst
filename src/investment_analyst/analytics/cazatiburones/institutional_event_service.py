"""Service for materializing and querying persisted institutional 13F events."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from investment_analyst.analytics.cazatiburones.institutional_event_definitions import (
    POLICY_VERSION,
)
from investment_analyst.analytics.cazatiburones.institutional_event_engine import (
    project_institutional_events,
)
from investment_analyst.analytics.cazatiburones.institutional_event_identity import snapshot_id
from investment_analyst.analytics.cazatiburones.institutional_event_models import (
    InstitutionalEventMaterializationSummary,
    InstitutionalEventSnapshot,
)
from investment_analyst.analytics.cazatiburones.institutional_event_repository import (
    InstitutionalEventRepository,
)
from investment_analyst.core.models.metric import MetricResult
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.storage import LocalStorage, StorageError


class InstitutionalEventService:
    def __init__(
        self,
        storage: LocalStorage,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._clock = clock
        self._repository = InstitutionalEventRepository(
            self._storage.paths.processed_dir,
            read_only=self._storage.read_only,
        )

    def materialize(
        self,
        *,
        asset_id: str,
        manager_cik: str,
        known_at: datetime,
    ) -> InstitutionalEventMaterializationSummary:
        if self._storage.read_only:
            raise StorageError("institutional event materialization requires writable storage")

        normalized_mgr = normalize_cik(manager_cik)
        recorded_at = self._clock()

        # Load metrics for asset_id and filter by algorithm_version, manager_cik,
        # and PIT (available_at <= known_at)
        all_metrics: list[MetricResult] = self._storage.metric_results.list(asset_id=asset_id)
        filtered_metrics: list[MetricResult] = []
        for item in all_metrics:
            if item.algorithm_version != "cazatiburones-institutional-metrics-v1":
                continue
            item_mgr = item.parameters.get("manager_cik")
            if item_mgr is None:
                continue
            try:
                if normalize_cik(str(item_mgr)) != normalized_mgr:
                    continue
            except ValueError:
                if str(item_mgr) != normalized_mgr:
                    continue
            if item.available_at <= known_at:
                filtered_metrics.append(item)

        evaluations, events, candidates = project_institutional_events(filtered_metrics)

        # Snapshot identity is strictly deterministic
        snap_id = snapshot_id(
            {
                "asset_id": asset_id,
                "event_ids": [str(e.event_id) for e in events],
                "known_at": known_at,
                "manager_cik": normalized_mgr,
                "metric_result_ids": [str(m.result_id) for m in filtered_metrics],
                "policy_version": POLICY_VERSION,
            }
        )

        snapshot = InstitutionalEventSnapshot(
            snapshot_id=snap_id,
            asset_id=asset_id,
            manager_cik=normalized_mgr,
            known_at=known_at,
            recorded_at=recorded_at,
            policy_version=POLICY_VERSION,
            evaluations=evaluations,
            events=events,
            candidates=candidates,
            omissions=(),
        )

        created = self._repository.save(snapshot)

        return InstitutionalEventMaterializationSummary(
            asset_id=asset_id,
            manager_cik=normalized_mgr,
            known_at=known_at,
            snapshot_id=snap_id,
            created=created,
            events=len(events),
            candidates=len(candidates),
        )

    def query(
        self,
        *,
        asset_id: str,
        manager_cik: str,
        known_at: datetime,
        snapshot_id_value: UUID,
    ) -> InstitutionalEventSnapshot | None:
        normalized_mgr = normalize_cik(manager_cik)
        return self._repository.get(
            asset_id=asset_id,
            manager_cik=normalized_mgr,
            known_at=known_at.isoformat(),
            snapshot_id=snapshot_id_value,
        )
