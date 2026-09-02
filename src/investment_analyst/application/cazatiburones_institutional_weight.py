"""Read-write application boundary for declared institutional weights."""

from investment_analyst.analytics.cazatiburones.institutional_weight_models import (
    InstitutionalWeightRunSummary,
)
from investment_analyst.analytics.cazatiburones.institutional_weight_pipeline import (
    InstitutionalWeightPipeline,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models.base import UTCDateTime
from investment_analyst.workspace.models import WorkspaceAccessMode


class CazatiburonesInstitutionalWeightApplication:
    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "CazatiburonesInstitutionalWeightApplication":
        return cls(ApplicationRuntime.create_default())

    def compute(
        self,
        *,
        asset_id: str,
        manager_cik: str,
        known_at: UTCDateTime,
        location: StorageLocationRequest,
    ) -> InstitutionalWeightRunSummary:
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            return InstitutionalWeightPipeline(storage).compute(
                asset_id=asset_id, manager_cik=manager_cik, known_at=known_at
            )
