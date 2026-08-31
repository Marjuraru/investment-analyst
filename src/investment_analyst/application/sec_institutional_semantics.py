"""Application edge for local Form 13F semantic enrichment and PIT queries."""

from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.evidence.sec_institutional_semantics.service import (
    InstitutionalHoldingsSemanticsService,
)
from investment_analyst.workspace.models import WorkspaceAccessMode


class SecInstitutionalSemanticsApplication:
    def __init__(self, runtime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "SecInstitutionalSemanticsApplication":
        return cls(ApplicationRuntime.create_default())

    def enrich(self, *, request, location):
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            return InstitutionalHoldingsSemanticsService(storage).enrich(request)

    def query(self, *, query, location):
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            return InstitutionalHoldingsSemanticsService(storage).query(query)
