"""Persist one fresh official SEC Submissions snapshot for incremental refreshes.

Every incremental SEC capability starts from the same primitive: exactly one GET to
``data.sec.gov/submissions``, one append-only ``RawRecord`` for the issuer asset and the
Submissions source, and a verified read-back. This collaborator is the single shared owner of
that primitive; the semantics are byte-for-byte the ones already integrated by SEC-CORPUS-25.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from investment_analyst.core.models import Asset, RawRecord
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarDocument
from investment_analyst.providers.fundamentals.sec_raw_records import (
    create_sec_asset,
    create_sec_submissions_source,
    sec_document_to_raw_record,
)
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError


class SecSubmissionsRefreshError(RuntimeError):
    """A fresh SEC Submissions snapshot cannot be safely persisted or verified."""


class SecSubmissionsIssuerClient(Protocol):
    def fetch_submissions(self) -> SecEdgarDocument:
        """Fetch exactly one validated SEC Submissions snapshot."""
        ...


@dataclass(frozen=True, slots=True)
class SecSubmissionsSnapshot:
    """One verified Submissions snapshot plus its persistence counters."""

    record: RawRecord
    checked_at: datetime
    created: int
    reused: int


class SecSubmissionsRefreshService:
    """Acquire exactly one Submissions snapshot and persist it without rewriting history."""

    def __init__(
        self,
        storage: LocalStorage,
        *,
        configuration: SecAssetConfiguration,
        issuer_client: SecSubmissionsIssuerClient,
    ) -> None:
        storage.require_open()
        self._storage = storage
        self._configuration = configuration
        self._issuer_client = issuer_client

    def persist_fresh_snapshot(self) -> SecSubmissionsSnapshot:
        """Persist the current Submissions snapshot and return its verified identity."""
        self._storage.require_open()
        submissions_document = self._issuer_client.fetch_submissions()
        candidate = sec_document_to_raw_record(submissions_document, self._configuration)
        existing_asset = self._existing_asset()
        self._storage.assets.upsert(create_sec_asset(self._configuration, existing_asset))
        self._storage.sources.upsert(create_sec_submissions_source(self._configuration))
        try:
            record = self._storage.raw_records.get(candidate.record_id)
            reused = 1
            created = 0
        except RecordNotFoundError:
            self._storage.raw_records.save(candidate)
            record = self._storage.raw_records.get(candidate.record_id)
            created = 1
            reused = 0
        if (
            record.record_id != candidate.record_id
            or record.asset_id != candidate.asset_id
            or record.source.source_id != candidate.source.source_id
            or record.payload != candidate.payload
            or record.schema_version != candidate.schema_version
        ):
            raise SecSubmissionsRefreshError("stored Submissions snapshot conflicts")
        return SecSubmissionsSnapshot(
            record=record,
            checked_at=candidate.received_at,
            created=created,
            reused=reused,
        )

    def _existing_asset(self) -> Asset | None:
        try:
            return self._storage.assets.get(self._configuration.asset_id)
        except RecordNotFoundError:
            return None


__all__ = [
    "SecSubmissionsRefreshError",
    "SecSubmissionsRefreshService",
    "SecSubmissionsSnapshot",
]
