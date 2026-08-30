"""Read-only application boundary for the declared activity-rule catalog."""

from investment_analyst.analytics.cazatiburones.activity_rule_models import (
    ActivityRuleEvaluationResult,
)
from investment_analyst.analytics.cazatiburones.activity_rule_service import ActivityRuleService
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models.base import UTCDateTime
from investment_analyst.workspace.models import WorkspaceAccessMode


class CazatiburonesActivityRulesApplication:
    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "CazatiburonesActivityRulesApplication":
        return cls(ApplicationRuntime.create_default())

    def query_declared_activity(
        self,
        *,
        asset_id: str,
        known_at: UTCDateTime,
        location: StorageLocationRequest,
    ) -> ActivityRuleEvaluationResult:
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            return ActivityRuleService(storage).query_declared_activity(
                asset_id=asset_id, known_at=known_at
            )

    def query_institutional(
        self,
        *,
        manager_cik: str,
        known_at: UTCDateTime,
        location: StorageLocationRequest,
    ) -> ActivityRuleEvaluationResult:
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            return ActivityRuleService(storage).query_institutional(
                manager_cik=manager_cik, known_at=known_at
            )
