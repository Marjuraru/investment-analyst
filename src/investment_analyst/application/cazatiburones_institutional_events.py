"""Application boundary facade for cazatiburones institutional 13F events."""

from datetime import datetime
from uuid import UUID

from investment_analyst.analytics.cazatiburones.institutional_event_models import (
    InstitutionalEventMaterializationSummary,
    InstitutionalEventSnapshot,
)
from investment_analyst.analytics.cazatiburones.institutional_event_service import (
    InstitutionalEventService,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.workspace.models import WorkspaceAccessMode


class CazatiburonesInstitutionalEventsApplication:
    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "CazatiburonesInstitutionalEventsApplication":
        return cls(ApplicationRuntime.create_default())

    def materialize(
        self,
        *,
        asset_id: str,
        manager_cik: str,
        known_at: datetime,
        location: StorageLocationRequest,
    ) -> InstitutionalEventMaterializationSummary:
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            service = InstitutionalEventService(storage)
            return service.materialize(
                asset_id=asset_id,
                manager_cik=manager_cik,
                known_at=known_at,
            )

    def query(
        self,
        *,
        asset_id: str,
        manager_cik: str,
        known_at: datetime,
        snapshot_id: UUID,
        location: StorageLocationRequest,
    ) -> InstitutionalEventSnapshot | None:
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            service = InstitutionalEventService(storage)
            return service.query(
                asset_id=asset_id,
                manager_cik=manager_cik,
                known_at=known_at,
                snapshot_id_value=snapshot_id,
            )
