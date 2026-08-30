"""Read-only composition of the declared activity-rule engine over integrated verticals.

Two entries are exposed, by issuer and by manager, and never cross: neither joins a
Form 13F position to an issuer, and neither persists, deduplicates, or notifies.
"""

from investment_analyst.analytics.cazatiburones.activity_rule_engine import (
    evaluate_declared_activity_rules,
    evaluate_institutional_rules,
)
from investment_analyst.analytics.cazatiburones.activity_rule_models import (
    ActivityRuleEvaluationResult,
)
from investment_analyst.analytics.cazatiburones.declared_activity_service import (
    DeclaredActivityService,
)
from investment_analyst.analytics.cazatiburones.institutional_change_service import (
    InstitutionalChangeService,
)
from investment_analyst.core.models.base import UTCDateTime
from investment_analyst.storage import StorageError
from investment_analyst.storage.local import LocalStorage


class ActivityRuleService:
    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def query_declared_activity(
        self, *, asset_id: str, known_at: UTCDateTime
    ) -> ActivityRuleEvaluationResult:
        if not self._storage.read_only:
            raise StorageError("activity rule query requires read-only storage")
        result = DeclaredActivityService(self._storage).query(asset_id=asset_id, known_at=known_at)
        return evaluate_declared_activity_rules(result)

    def query_institutional(
        self, *, manager_cik: str, known_at: UTCDateTime
    ) -> ActivityRuleEvaluationResult:
        if not self._storage.read_only:
            raise StorageError("activity rule query requires read-only storage")
        results = InstitutionalChangeService(self._storage).query(
            manager_cik=manager_cik, known_at=known_at
        )
        return evaluate_institutional_rules(results)
