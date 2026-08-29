"""Bounded fetch and immutable RawRecord conversion for manager Submissions."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpTransport
from investment_analyst.storage import StorageError

MANAGER_SUBMISSIONS_SCHEMA_VERSION = "sec-manager-submissions-snapshot-v1"
_BASE_URL = "https://data.sec.gov"
_MAX_RESPONSE_BYTES = 50 * 1024 * 1024
_RAW_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-manager-submissions:v1")


class SecManagerSubmissionsError(StorageError):
    """A manager Submissions response violates the bounded provider contract."""


def manager_submissions_source_id(cik: str) -> str:
    return f"sec-edgar:manager:{normalize_cik(cik)}:submissions"


class SecManagerSubmissionsClient:
    def __init__(
        self,
        transport: HttpTransport,
        identity: SecEdgarIdentity,
        *,
        clock: callable = lambda: datetime.now(UTC),
        timeout_seconds: float = 30.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise SecManagerSubmissionsError("SEC timeout must be greater than zero")
        self._transport = transport
        self._identity = identity
        self._clock = clock
        self._timeout_seconds = timeout_seconds

    def fetch(self, filer_cik: str) -> RawRecord:
        cik = normalize_cik(filer_cik)
        path = f"/submissions/CIK{cik}.json"
        url = f"{_BASE_URL}{path}"
        response = self._transport.get(
            url,
            headers={"Accept": "application/json", "User-Agent": self._identity.user_agent},
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        if (
            response.status_code != 200
            or response.url != url
            or response.body_truncated
            or not response.body
        ):
            raise SecManagerSubmissionsError("SEC manager Submissions response is invalid")
        try:
            document = json.loads(
                response.body,
                parse_int=str,
                parse_float=str,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise SecManagerSubmissionsError(
                "SEC manager Submissions payload is malformed"
            ) from error
        if not isinstance(document, dict):
            raise SecManagerSubmissionsError("SEC manager Submissions payload is malformed")
        try:
            returned_cik = normalize_cik(str(document["cik"]))
            manager_name = str(document["name"]).strip()
            recent = document["filings"]["recent"]
        except (KeyError, TypeError, ValueError) as error:
            raise SecManagerSubmissionsError(
                "SEC manager Submissions payload is malformed"
            ) from error
        if returned_cik != cik or not manager_name or not isinstance(recent, dict):
            raise SecManagerSubmissionsError("SEC manager Submissions identity is invalid")
        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise SecManagerSubmissionsError("SEC manager clock must be timezone-aware")
        retrieved_at = retrieved_at.astimezone(UTC)
        digest = hashlib.sha256(response.body).hexdigest()
        source_id = manager_submissions_source_id(cik)
        record_id = uuid5(
            _RAW_NAMESPACE, f"{source_id}|{digest}|{MANAGER_SUBMISSIONS_SCHEMA_VERSION}"
        )
        return RawRecord(
            record_id=record_id,
            asset_id=None,
            source=SourceReference(
                source_id=source_id,
                record_key=f"CIK{cik}:submissions:{digest[:16]}",
                retrieved_at=retrieved_at,
                raw_uri=url,
                checksum_sha256=digest,
            ),
            event_time=retrieved_at,
            available_at=retrieved_at,
            received_at=retrieved_at,
            payload={
                "document_type": "submissions",
                "cik": cik,
                "entity_name": manager_name,
                "body_sha256": digest,
                "content_length": len(response.body),
                "document": document,
            },
            schema_version=MANAGER_SUBMISSIONS_SCHEMA_VERSION,
        )


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")
