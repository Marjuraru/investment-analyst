# ruff: noqa: E501
"""Read-only application boundary for declared ownership activity."""

from investment_analyst.analytics.cazatiburones.declared_activity_models import (
    DeclaredActivityQueryResult,
)
from investment_analyst.analytics.cazatiburones.declared_activity_service import (
    DeclaredActivityService,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models.base import UTCDateTime
from investment_analyst.workspace.models import WorkspaceAccessMode


class CazatiburonesDeclaredActivityApplication:
    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "CazatiburonesDeclaredActivityApplication":
        return cls(ApplicationRuntime.create_default())

    def query(
        self,
        *,
        asset_id: str,
        known_at: UTCDateTime,
        location: StorageLocationRequest,
    ) -> DeclaredActivityQueryResult:
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            return DeclaredActivityService(storage).query(asset_id=asset_id, known_at=known_at)
