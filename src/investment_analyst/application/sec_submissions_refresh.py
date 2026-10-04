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
from investment_analyst.providers.failure_reasons import (
    ProviderFailureReason,
    is_known_provider_failure_reason,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarDocument
from investment_analyst.providers.fundamentals.sec_raw_records import (
    create_sec_asset,
    create_sec_submissions_source,
    sec_document_to_raw_record,
)
from investment_analyst.storage import LocalStorage, StorageError
from investment_analyst.storage.errors import RecordNotFoundError


class SecSubmissionsRefreshError(RuntimeError):
    """A fresh SEC Submissions snapshot cannot be safely persisted or verified."""

    def __init__(self, message: str, *, reason_code: str | None = None) -> None:
        self.reason_code = reason_code
        super().__init__(message)


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
        try:
            submissions_document = self._issuer_client.fetch_submissions()
        except Exception as error:  # noqa: BLE001 - preserve the typed cause for classification
            reason_code = (
                error.reason_code
                if isinstance(error, SecSubmissionsRefreshError)
                and is_known_provider_failure_reason(error.reason_code)
                else ProviderFailureReason.SEC_SUBMISSIONS_FETCH_FAILED
            )
            raise SecSubmissionsRefreshError(
                "fresh SEC Submissions snapshot could not be fetched",
                reason_code=reason_code,
            ) from error
        try:
            candidate = sec_document_to_raw_record(submissions_document, self._configuration)
        except (TypeError, ValueError) as error:
            raise SecSubmissionsRefreshError(
                "fresh SEC Submissions snapshot failed validation",
                reason_code=ProviderFailureReason.SEC_SUBMISSIONS_SNAPSHOT_INVALID,
            ) from error
        try:
            existing_asset = self._existing_asset()
            self._storage.assets.upsert(create_sec_asset(self._configuration, existing_asset))
            self._storage.sources.upsert(create_sec_submissions_source(self._configuration))
        except StorageError as error:
            raise SecSubmissionsRefreshError(
                "SEC Submissions snapshot persistence could not be prepared",
                reason_code=ProviderFailureReason.SEC_SUBMISSIONS_SNAPSHOT_PERSIST_FAILED,
            ) from error
        try:
            record = self._storage.raw_records.get(candidate.record_id)
            reused = 1
            created = 0
        except RecordNotFoundError:
            try:
                self._storage.raw_records.save(candidate)
                record = self._storage.raw_records.get(candidate.record_id)
            except StorageError as error:
                raise SecSubmissionsRefreshError(
                    "SEC Submissions snapshot could not be persisted and verified",
                    reason_code=ProviderFailureReason.SEC_SUBMISSIONS_SNAPSHOT_PERSIST_FAILED,
                ) from error
            created = 1
            reused = 0
        except StorageError as error:
            raise SecSubmissionsRefreshError(
                "SEC Submissions snapshot could not be read",
                reason_code=ProviderFailureReason.SEC_SUBMISSIONS_SNAPSHOT_READ_FAILED,
            ) from error
        if (
            record.record_id != candidate.record_id
            or record.asset_id != candidate.asset_id
            or record.source.source_id != candidate.source.source_id
            or record.payload != candidate.payload
            or record.schema_version != candidate.schema_version
        ):
            raise SecSubmissionsRefreshError(
                "stored Submissions snapshot conflicts",
                reason_code=ProviderFailureReason.SEC_SUBMISSIONS_SNAPSHOT_CONFLICT,
            )
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
