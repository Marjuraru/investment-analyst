"""Read-only query and replay service for the SEC primary-document corpus."""

from __future__ import annotations

from investment_analyst.evidence.sec_documents.models import SecDocumentQuery, SecDocumentReplay
from investment_analyst.evidence.sec_documents.repository import SecDocumentRepository
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.storage import LocalStorage, StorageError


class SecDocumentServiceError(StorageError):
    """A local replay request conflicts with its configured issuer."""


class SecDocumentCorpusService:
    """Serve local point-in-time replay without a writer or provider client."""

    def __init__(self, storage: LocalStorage, *, configuration: SecAssetConfiguration) -> None:
        self._storage = storage
        self._configuration = configuration
        self._repository = SecDocumentRepository(storage.raw_records, storage.documents)

    def replay(self, query: SecDocumentQuery) -> SecDocumentReplay:
        self._storage.require_open()
        if not self._storage.read_only:
            raise SecDocumentServiceError("document replay requires read-only storage")
        if query.asset_id != self._configuration.asset_id:
            raise SecDocumentServiceError("query asset_id does not match the configured SEC issuer")
        return self._repository.replay(
            asset_id=query.asset_id,
            known_at=query.known_at,
            form=query.form,
            accession=query.accession,
            revision_id=query.revision_id,
            include_content=query.include_content,
        )
