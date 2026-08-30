"""Write snapshots from already persisted activity metrics without recomputation."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from investment_analyst.analytics.cazatiburones.activity_event_definitions import POLICY_VERSION
from investment_analyst.analytics.cazatiburones.activity_event_engine import project_activity_events
from investment_analyst.analytics.cazatiburones.activity_event_identity import snapshot_id
from investment_analyst.analytics.cazatiburones.activity_event_models import (
    ActivityEventMaterializationSummary,
    ActivityEventSnapshot,
)
from investment_analyst.analytics.cazatiburones.activity_event_repository import (
    ActivityEventRepository,
)
from investment_analyst.core.models.base import UTCDateTime
from investment_analyst.storage import LocalStorage, StorageError


class ActivityEventService:
    def __init__(
        self, storage: LocalStorage, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._storage = storage
        self._clock = clock

    def materialize(
        self, *, asset_id: str, known_at: UTCDateTime
    ) -> ActivityEventMaterializationSummary:
        self._storage.require_open()
        if self._storage.read_only:
            raise StorageError("activity event materialization requires writable storage")
        metrics = tuple(
            item
            for item in self._storage.metric_results.list(asset_id=asset_id)
            if item.available_at <= known_at
            and item.parameters.get("known_at") == known_at.isoformat()
        )
        evaluations, events, candidates = project_activity_events(metrics)
        identifier = snapshot_id(
            {
                "asset_id": asset_id,
                "known_at": known_at,
                "policy_version": POLICY_VERSION,
                "metric_result_ids": [str(item.result_id) for item in metrics],
                "event_ids": [str(item.event_id) for item in events],
            }
        )
        recorded_at = self._clock().astimezone(UTC)
        snapshot = ActivityEventSnapshot(
            snapshot_id=identifier,
            asset_id=asset_id,
            known_at=known_at,
            recorded_at=recorded_at,
            policy_version=POLICY_VERSION,
            evaluations=evaluations,
            events=events,
            candidates=candidates,
            omissions=("missing_persisted_metric",) if not metrics else (),
        )
        created = ActivityEventRepository(self._storage.paths.processed_dir, read_only=False).save(
            snapshot
        )
        return ActivityEventMaterializationSummary(
            asset_id=asset_id,
            known_at=known_at,
            snapshot_id=identifier,
            created=created,
            events=len(events),
            candidates=len(candidates),
        )

    def query(
        self, *, asset_id: str, known_at: UTCDateTime, snapshot_id_value: UUID
    ) -> ActivityEventSnapshot | None:
        repository = ActivityEventRepository(self._storage.paths.processed_dir, read_only=True)
        return repository.get(
            asset_id=asset_id, known_at=known_at.isoformat(), snapshot_id=snapshot_id_value
        )
