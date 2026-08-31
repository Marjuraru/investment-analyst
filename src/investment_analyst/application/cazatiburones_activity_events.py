"""Application boundary for isolated declared-activity event snapshots."""

from uuid import UUID

from investment_analyst.analytics.cazatiburones.activity_event_models import (
    ActivityEventMaterializationSummary,
    ActivityEventSnapshot,
)
from investment_analyst.analytics.cazatiburones.activity_event_service import ActivityEventService
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models.base import UTCDateTime
from investment_analyst.workspace.models import WorkspaceAccessMode


class CazatiburonesActivityEventsApplication:
    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "CazatiburonesActivityEventsApplication":
        return cls(ApplicationRuntime.create_default())

    def materialize(
        self, *, asset_id: str, known_at: UTCDateTime, location: StorageLocationRequest
    ) -> ActivityEventMaterializationSummary:
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            return ActivityEventService(storage).materialize(asset_id=asset_id, known_at=known_at)

    def query(
        self,
        *,
        asset_id: str,
        known_at: UTCDateTime,
        snapshot_id: UUID,
        location: StorageLocationRequest,
    ) -> ActivityEventSnapshot | None:
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            return ActivityEventService(storage).query(
                asset_id=asset_id, known_at=known_at, snapshot_id_value=snapshot_id
            )
