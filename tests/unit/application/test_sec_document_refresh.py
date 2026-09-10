"""Focused contracts for incremental SEC primary-document refreshes."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from investment_analyst.application.sec_document_refresh import SecPrimaryDocumentRefreshService
from investment_analyst.application.sec_document_refresh_models import (
    SecPrimaryDocumentRefreshRequest,
)
from investment_analyst.core.models import AssetClass
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_document_client import SecPrimaryDocumentResponse
from investment_analyst.providers.fundamentals.sec_document_pipeline import SecDocumentPipeline
from investment_analyst.providers.fundamentals.sec_edgar import (
    APPLE_CIK,
    APPLE_TICKER,
    SecDocumentType,
    SecEdgarDocument,
    SecIssuerFetchResult,
)
from investment_analyst.storage import LocalStorage, StoragePaths


def _configuration() -> SecAssetConfiguration:
    return SecAssetConfiguration(
        asset_id="equity:us:aapl",
        cik=APPLE_CIK,
        ticker=APPLE_TICKER,
        submissions_source_id="sec-edgar:aapl:submissions",
        companyfacts_source_id="sec-edgar:aapl:companyfacts",
        name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


def _issuer_result(*, retrieved_at: datetime, annual_accession: str) -> SecIssuerFetchResult:
    recent = {
        "accessionNumber": [annual_accession, "0000320193-25-000002"],
        "filingDate": ["2025-02-01", "2025-01-31"],
        "reportDate": ["2024-12-31", "2024-09-30"],
        "acceptanceDateTime": ["2025-02-01T18:00:00.000Z", "2025-01-31T18:00:00.000Z"],
        "form": ["10-K", "10-Q"],
        "primaryDocument": ["annual.htm", "quarterly.htm"],
    }
    submissions_body = {
        "cik": APPLE_CIK,
        "name": "Apple Inc.",
        "tickers": [APPLE_TICKER],
        "exchanges": ["NASDAQ"],
        "filings": {"recent": recent},
    }
    company_facts_body = {"cik": APPLE_CIK, "entityName": "Apple Inc.", "facts": {}}
    documents = (
        SecEdgarDocument(
            document_type=SecDocumentType.SUBMISSIONS,
            cik=APPLE_CIK,
            entity_name="Apple Inc.",
            retrieved_at=retrieved_at,
            request_url=f"https://data.sec.gov/submissions/CIK{APPLE_CIK}.json",
            body=submissions_body,
            body_sha256=hashlib.sha256(repr(submissions_body).encode()).hexdigest(),
            content_length=1,
        ),
        SecEdgarDocument(
            document_type=SecDocumentType.COMPANY_FACTS,
            cik=APPLE_CIK,
            entity_name="Apple Inc.",
            retrieved_at=retrieved_at,
            request_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{APPLE_CIK}.json",
            body=company_facts_body,
            body_sha256=hashlib.sha256(repr(company_facts_body).encode()).hexdigest(),
            content_length=1,
        ),
    )
    return SecIssuerFetchResult(
        cik=APPLE_CIK,
        ticker=APPLE_TICKER,
        entity_name="Apple Inc.",
        retrieved_at=retrieved_at,
        documents=documents,
    )


class _IssuerClient:
    def __init__(self, result: SecIssuerFetchResult) -> None:
        self.result = result

    def fetch_issuer_documents(self) -> SecIssuerFetchResult:
        return self.result


class _DocumentClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, document):
        self.calls.append(document.filing.accession)
        body = document.filing.accession.encode()
        return SecPrimaryDocumentResponse(
            content=body,
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            url="https://www.sec.gov/Archives/edgar/data/320193/document.htm",
            retrieved_at=datetime(2025, 2, 4, tzinfo=UTC),
        )


def _service(storage: LocalStorage, issuer: _IssuerClient, documents: _DocumentClient):
    configuration = _configuration()
    return SecPrimaryDocumentRefreshService(
        storage,
        configuration=configuration,
        issuer_client=issuer,
        document_pipeline=SecDocumentPipeline(storage, documents, configuration=configuration),
    )


def test_refresh_reuses_verified_accessions_and_fetches_only_new_delta(tmp_path: Path) -> None:
    first_at = datetime(2025, 2, 2, tzinfo=UTC)
    issuer = _IssuerClient(
        _issuer_result(retrieved_at=first_at, annual_accession="0000320193-25-000001")
    )
    documents = _DocumentClient()
    request = SecPrimaryDocumentRefreshRequest(asset_id="equity:us:aapl")
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        service = _service(storage, issuer, documents)
        first = service.run(request)
        second = service.run(request)
        issuer.result = _issuer_result(
            retrieved_at=datetime(2025, 2, 3, tzinfo=UTC),
            annual_accession="0000320193-25-000003",
        )
        delta = service.run(request)

    assert first.document_fetch_calls == 2
    assert second.document_fetch_calls == 0
    assert second.accessions_reused == first.accessions_selected
    assert delta.accessions_fetched == ("0000320193-25-000003",)
    assert delta.accessions_reused == ("0000320193-25-000002",)
    assert documents.calls == [
        "0000320193-25-000002",
        "0000320193-25-000001",
        "0000320193-25-000003",
    ]
