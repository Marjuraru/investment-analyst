"""Read-write application boundary for declared-activity observation normalization."""

from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models.base import UTCDateTime
from investment_analyst.evidence.sec_declared_activity_observations.models import (
    DeclaredActivityObservationRunSummary,
)
from investment_analyst.evidence.sec_declared_activity_observations.service import (
    DeclaredActivityObservationService,
)
from investment_analyst.workspace.models import WorkspaceAccessMode


class CazatiburonesActivityObservationsApplication:
    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "CazatiburonesActivityObservationsApplication":
        return cls(ApplicationRuntime.create_default())

    def normalize(
        self,
        *,
        asset_id: str,
        known_at: UTCDateTime,
        location: StorageLocationRequest,
    ) -> DeclaredActivityObservationRunSummary:
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            return DeclaredActivityObservationService(storage).normalize(
                asset_id=asset_id, known_at=known_at
            )
