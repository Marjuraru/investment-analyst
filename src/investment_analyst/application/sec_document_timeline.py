"""Application boundary for point-in-time SEC document timeline queries."""

from __future__ import annotations

from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.catalog.provider_configuration import resolve_sec_configuration
from investment_analyst.evidence.sec_documents.timeline_models import (
    SecDocumentTimelineQuery,
    SecDocumentTimelineResult,
)
from investment_analyst.evidence.sec_documents.timeline_service import SecDocumentTimelineService
from investment_analyst.workspace.models import WorkspaceAccessMode


class SecDocumentTimelineApplicationError(RuntimeError):
    """Raised when timeline execution or catalog validation fails."""


class SecDocumentTimelineApplication:
    """Compose catalog validation, workspace lifecycle, and timeline search."""

    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> SecDocumentTimelineApplication:
        return cls(ApplicationRuntime.create_default())

    def query(
        self,
        *,
        query: SecDocumentTimelineQuery,
        location: StorageLocationRequest,
    ) -> SecDocumentTimelineResult:
        """Validate catalog assets and execute a point-in-time document timeline query."""
        for asset_id in query.asset_ids:
            try:
                resolve_sec_configuration(self._runtime.provider_resolver, asset_id=asset_id)
            except Exception as error:
                raise SecDocumentTimelineApplicationError(
                    f"asset {asset_id!r} has no SEC configuration in catalog"
                ) from error

        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            return SecDocumentTimelineService(storage).query(query)
