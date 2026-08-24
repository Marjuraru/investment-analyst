"""Application boundary for the bounded SEC primary-document corpus."""

from __future__ import annotations

from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.catalog.provider_configuration import resolve_sec_configuration
from investment_analyst.evidence.sec_documents.models import SecDocumentQuery, SecDocumentReplay
from investment_analyst.evidence.sec_documents.service import SecDocumentCorpusService
from investment_analyst.providers.fundamentals.sec_document_client import SecDocumentClient
from investment_analyst.providers.fundamentals.sec_document_pipeline import (
    SecDocumentImportRequest,
    SecDocumentImportSummary,
    SecDocumentPipeline,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import UrlLibHttpTransport
from investment_analyst.workspace.models import WorkspaceAccessMode


class SecDocumentCorpusApplication:
    """Compose catalog, workspace, and one official SEC document provider at the edge."""

    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> SecDocumentCorpusApplication:
        return cls(ApplicationRuntime.create_default())

    def import_documents(
        self,
        *,
        asset_id: str,
        request: SecDocumentImportRequest,
        location: StorageLocationRequest,
        sec_identity: SecEdgarIdentity,
    ) -> SecDocumentImportSummary:
        configuration = resolve_sec_configuration(
            self._runtime.provider_resolver, asset_id=asset_id
        )
        client = SecDocumentClient(UrlLibHttpTransport(), sec_identity)
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            return SecDocumentPipeline(storage, client, configuration=configuration).run(request)

    def replay(
        self,
        *,
        query: SecDocumentQuery,
        location: StorageLocationRequest,
    ) -> SecDocumentReplay:
        configuration = resolve_sec_configuration(
            self._runtime.provider_resolver, asset_id=query.asset_id
        )
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            return SecDocumentCorpusService(storage, configuration=configuration).replay(query)
