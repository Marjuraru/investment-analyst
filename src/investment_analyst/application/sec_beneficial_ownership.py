"""Application edge for isolated SEC Schedule 13D/13G evidence."""

from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.catalog.provider_configuration import resolve_sec_configuration
from investment_analyst.evidence.sec_beneficial_ownership.service import BeneficialOwnershipService
from investment_analyst.providers.beneficial_ownership.sec_beneficial_ownership_pipeline import (
    SecBeneficialOwnershipPipeline,
)
from investment_analyst.providers.fundamentals.sec_document_client import SecDocumentClient
from investment_analyst.providers.http import UrlLibHttpTransport
from investment_analyst.workspace.models import WorkspaceAccessMode


class SecBeneficialOwnershipApplication:
    def __init__(self, runtime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "SecBeneficialOwnershipApplication":
        return cls(ApplicationRuntime.create_default())

    def import_beneficial_ownership(self, *, asset_id, request, location, sec_identity):
        configuration = resolve_sec_configuration(
            self._runtime.provider_resolver, asset_id=asset_id
        )
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            return SecBeneficialOwnershipPipeline(
                storage,
                SecDocumentClient(UrlLibHttpTransport(), sec_identity),
                configuration=configuration,
            ).run(request)

    def query_beneficial_ownership(self, *, query, location):
        configuration = resolve_sec_configuration(
            self._runtime.provider_resolver, asset_id=query.asset_id
        )
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            return BeneficialOwnershipService(storage, configuration=configuration).query(query)
