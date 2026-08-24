"""Bounded official SEC Archives client for exact primary document bytes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from investment_analyst.evidence.sec_documents.models import SecLogicalDocument
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarError, SecEdgarIdentity
from investment_analyst.providers.http import HttpTransport

_ARCHIVES_BASE = "https://www.sec.gov"
_MAX_RESPONSE_BYTES = 50 * 1024 * 1024


class SecDocumentClientError(SecEdgarError):
    """An Archives request or response violates the primary-document contract."""


@dataclass(frozen=True, slots=True)
class SecPrimaryDocumentResponse:
    content: bytes
    sha256: str
    size_bytes: int
    url: str
    retrieved_at: datetime


class SecDocumentClient:
    """Fetch one primary document from the single official SEC Archives origin."""

    def __init__(
        self,
        transport: HttpTransport,
        identity: SecEdgarIdentity,
        *,
        clock: callable = lambda: datetime.now(UTC),
        timeout_seconds: float = 30.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise SecDocumentClientError("SEC document timeout must be greater than zero")
        self._transport = transport
        self._identity = identity
        self._clock = clock
        self._timeout_seconds = timeout_seconds

    def fetch(self, document: SecLogicalDocument) -> SecPrimaryDocumentResponse:
        """Fetch exact primary-document bytes once, rejecting redirects and truncation."""
        cik = str(int(document.filing.filer_cik))
        accession = document.filing.accession.replace("-", "")
        path = f"/Archives/edgar/data/{cik}/{accession}/{document.name}"
        url = f"{_ARCHIVES_BASE}{path}"
        response = self._transport.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8",
                "User-Agent": self._identity.user_agent,
            },
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        if response.status_code != 200:
            raise SecDocumentClientError("SEC Archives returned an unsuccessful response")
        if response.body_truncated or not response.body:
            raise SecDocumentClientError("SEC Archives response is empty or exceeds the size limit")
        if response.url != url or not _is_official_archives_url(response.url, path):
            raise SecDocumentClientError("SEC Archives redirected away from the requested document")
        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise SecDocumentClientError("SEC document clock must be timezone-aware")
        content = response.body
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=url,
            retrieved_at=retrieved_at.astimezone(UTC),
        )


def _is_official_archives_url(url: str, expected_path: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "www.sec.gov"
        and parsed.port in (None, 443)
        and parsed.path == expected_path
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )
