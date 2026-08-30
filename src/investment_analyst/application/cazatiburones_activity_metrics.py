"""Read-write application boundary for cazatiburones activity metric computation."""

from investment_analyst.analytics.cazatiburones.activity_metric_models import (
    ActivityMetricRunSummary,
)
from investment_analyst.analytics.cazatiburones.activity_metric_pipeline import (
    ActivityMetricPipeline,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models.base import UTCDateTime
from investment_analyst.workspace.models import WorkspaceAccessMode


class CazatiburonesActivityMetricsApplication:
    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "CazatiburonesActivityMetricsApplication":
        return cls(ApplicationRuntime.create_default())

    def compute(
        self,
        *,
        asset_id: str,
        known_at: UTCDateTime,
        location: StorageLocationRequest,
    ) -> ActivityMetricRunSummary:
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            return ActivityMetricPipeline(storage).compute(asset_id=asset_id, known_at=known_at)
