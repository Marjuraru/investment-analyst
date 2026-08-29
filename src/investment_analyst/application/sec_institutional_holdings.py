"""Application edge for SEC Form 13F evidence without asset linkage."""

from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.evidence.sec_institutional_holdings.service import (
    InstitutionalHoldingsService,
)
from investment_analyst.providers.fundamentals.sec_document_client import SecDocumentClient
from investment_analyst.providers.http import UrlLibHttpTransport
from investment_analyst.providers.institutional_holdings import (
    sec_institutional_holdings_pipeline,
)
from investment_analyst.providers.institutional_holdings.sec_manager_submissions import (
    SecManagerSubmissionsClient,
)
from investment_analyst.workspace.models import WorkspaceAccessMode


class SecInstitutionalHoldingsApplication:
    def __init__(self, runtime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "SecInstitutionalHoldingsApplication":
        return cls(ApplicationRuntime.create_default())

    def import_institutional_holdings(self, *, request, location, sec_identity):
        transport = UrlLibHttpTransport()
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            return sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
                storage,
                SecManagerSubmissionsClient(transport, sec_identity),
                SecDocumentClient(transport, sec_identity),
            ).run(request)

    def query_institutional_holdings(self, *, query, location):
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            return InstitutionalHoldingsService(storage).query(query)
