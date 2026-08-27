"""Bounded official SEC Archives client for exact primary document bytes."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from investment_analyst.evidence.sec_documents.models import SecLogicalDocument
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarError, SecEdgarIdentity
from investment_analyst.providers.http import HttpTransport

_ARCHIVES_BASE = "https://www.sec.gov"
_MAX_RESPONSE_BYTES = 50 * 1024 * 1024
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_SAFE_MANIFEST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")


class SecDocumentClientError(SecEdgarError):
    """An Archives request or response violates the primary-document contract."""


@dataclass(frozen=True, slots=True)
class SecPrimaryDocumentResponse:
    content: bytes
    sha256: str
    size_bytes: int
    url: str
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class SecAccessionManifest:
    """Exact, validated directory manifest returned by SEC Archives."""

    entries: tuple[str, ...]
    sha256: str
    size_bytes: int
    url: str
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class SecResolvedOwnershipDocument:
    """The declared locator and the one raw ownership XML from its accession."""

    manifest: SecAccessionManifest
    locator: SecPrimaryDocumentResponse
    semantic: SecPrimaryDocumentResponse


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
        response = self._get(url, _MAX_RESPONSE_BYTES)
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

    def fetch_manifest(self, document: SecLogicalDocument) -> SecAccessionManifest:
        """Fetch the official `index.json` for exactly one accession, fail-closed."""
        cik = str(int(document.filing.filer_cik))
        accession = document.filing.accession.replace("-", "")
        path = f"/Archives/edgar/data/{cik}/{accession}/index.json"
        url = f"{_ARCHIVES_BASE}{path}"
        response = self._get(url, _MAX_MANIFEST_BYTES)
        if response.status_code != 200 or response.body_truncated or not response.body:
            raise SecDocumentClientError("SEC manifest is empty or exceeds the size limit")
        if response.url != url or not _is_official_archives_url(response.url, path):
            raise SecDocumentClientError("SEC Archives redirected away from the requested manifest")
        try:
            payload = json.loads(response.body)
            items = payload["directory"]["item"]
            names = tuple(item["name"] for item in items)
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise SecDocumentClientError("SEC manifest is malformed") from error
        if (
            not names
            or len(set(names)) != len(names)
            or any(not _safe_manifest_name(name) for name in names)
        ):
            raise SecDocumentClientError("SEC manifest contains unsafe or ambiguous entries")
        retrieved_at = self._retrieved_at()
        return SecAccessionManifest(
            entries=tuple(sorted(names)),
            sha256=hashlib.sha256(response.body).hexdigest(),
            size_bytes=len(response.body),
            url=url,
            retrieved_at=retrieved_at,
        )

    def resolve_ownership_document(
        self, document: SecLogicalDocument
    ) -> SecResolvedOwnershipDocument:
        """Keep the declared locator distinct from the single raw XML manifest entry."""
        manifest = self.fetch_manifest(document)
        candidates = tuple(
            name for name in manifest.entries if "/" not in name and name.lower().endswith(".xml")
        )
        if len(candidates) != 1:
            raise SecDocumentClientError("official SEC manifest has no unique raw ownership XML")
        semantic_name = candidates[0]
        if (
            "/" not in document.name
            and document.name.lower().endswith(".xml")
            and document.name != semantic_name
        ):
            raise SecDocumentClientError(
                "declared SEC XML locator contradicts the official manifest"
            )
        semantic_document = SecLogicalDocument(
            document_id=SecLogicalDocument.expected_id(document.filing.filing_id, semantic_name),
            filing=document.filing,
            name=semantic_name,
        )
        return SecResolvedOwnershipDocument(
            manifest=manifest,
            locator=self.fetch(document),
            semantic=self.fetch(semantic_document),
        )

    def _get(self, url: str, max_response_bytes: int):
        return self._transport.get(
            url,
            headers={
                "Accept": "application/json,application/xml,text/xml,text/html;q=0.8",
                "User-Agent": self._identity.user_agent,
            },
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=max_response_bytes,
        )

    def _retrieved_at(self) -> datetime:
        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise SecDocumentClientError("SEC document clock must be timezone-aware")
        return retrieved_at.astimezone(UTC)


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


def _safe_manifest_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and _SAFE_MANIFEST_NAME.fullmatch(value) is not None
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )
