#!/usr/bin/env python3
"""Exercise the SEC v2 PIT contract using only temporary local storage and a fake client."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.core.models import AssetClass, RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import (
    SEC_DOCUMENT_SCHEMA_VERSION,
    SEC_DOCUMENT_SOURCE_ID,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_ownership.models import (
    OWNERSHIP_SCHEMA_VERSION,
    OWNERSHIP_SOURCE_ID,
)
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_document_client import (
    SecAccessionManifest,
    SecPrimaryDocumentResponse,
    SecResolvedOwnershipDocument,
)
from investment_analyst.providers.ownership.sec_ownership_pipeline import (
    SecOwnershipImportRequest,
    SecOwnershipPipeline,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_ASSET_ID = "equity:us:aapl"
_ACCEPTED_AT = datetime(2025, 1, 31, 18, tzinfo=UTC)
_KNOWN_AT = datetime(2025, 2, 1, tzinfo=UTC)
_ROOT = Path(__file__).resolve().parents[1]


def _configuration() -> SecAssetConfiguration:
    return SecAssetConfiguration(
        asset_id=_ASSET_ID,
        cik="0000320193",
        ticker="AAPL",
        submissions_source_id="sec-edgar:aapl:submissions",
        companyfacts_source_id="sec-edgar:aapl:companyfacts",
        name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


def _submissions() -> RawRecord:
    retrieved_at = datetime(2025, 2, 1, tzinfo=UTC)
    return RawRecord(
        record_id=uuid4(),
        asset_id=_ASSET_ID,
        source=SourceReference(source_id="sec-edgar:aapl:submissions", retrieved_at=retrieved_at),
        event_time=retrieved_at,
        available_at=retrieved_at,
        received_at=retrieved_at,
        payload={
            "document": {
                "cik": "0000320193",
                "filings": {
                    "recent": {
                        "accessionNumber": ["0000320193-25-000001"],
                        "filingDate": ["2025-01-31"],
                        "reportDate": ["2025-01-30"],
                        "acceptanceDateTime": ["2025-01-31T18:00:00.000Z"],
                        "form": ["4"],
                        "primaryDocument": ["xslF345X06/form4.xml"],
                    }
                },
            }
        },
        schema_version="sec-edgar-submissions-snapshot-v1",
    )


class _Client:
    def __init__(self, retrieved_at: datetime) -> None:
        self._retrieved_at = retrieved_at

    def resolve_ownership_document(
        self, document: SecLogicalDocument
    ) -> SecResolvedOwnershipDocument:
        base_url = "https://www.sec.gov/Archives/edgar/data/320193/000032019325000001/"
        locator = b"<!DOCTYPE html><html/>"
        semantic = b"""<ownershipDocument>
<documentType>4</documentType><periodOfReport>2025-01-30</periodOfReport>
<issuer><issuerCik>0000320193</issuerCik><issuerName>Apple Inc.</issuerName></issuer>
<reportingOwner><reportingOwnerId><rptOwnerCik>0001234567</rptOwnerCik><rptOwnerName>Owner</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><isTenPercentOwner>0</isTenPercentOwner><isOther>0</isOther></reportingOwnerRelationship></reportingOwner>
</ownershipDocument>"""
        return SecResolvedOwnershipDocument(
            manifest=SecAccessionManifest(
                entries=("form4.xml", document.name),
                sha256="a" * 64,
                size_bytes=10,
                url=f"{base_url}index.json",
                retrieved_at=self._retrieved_at,
            ),
            locator=SecPrimaryDocumentResponse(
                locator,
                hashlib.sha256(locator).hexdigest(),
                len(locator),
                f"{base_url}{document.name}",
                self._retrieved_at,
            ),
            semantic=SecPrimaryDocumentResponse(
                semantic,
                hashlib.sha256(semantic).hexdigest(),
                len(semantic),
                f"{base_url}form4.xml",
                self._retrieved_at,
            ),
        )


def _legacy_record(*, source_id: str, schema_version: str, retrieved_at: datetime) -> RawRecord:
    return RawRecord(
        record_id=uuid4(),
        asset_id=_ASSET_ID,
        source=SourceReference(source_id=source_id, retrieved_at=retrieved_at),
        event_time=retrieved_at,
        available_at=retrieved_at,
        received_at=retrieved_at,
        payload={"legacy": True},
        schema_version=schema_version,
    )


def _populate(root: Path, retrieved_at: datetime) -> None:
    with LocalStorage(StoragePaths.from_root(root)) as storage:
        storage.raw_records.save(_submissions())
        SecOwnershipPipeline(storage, _Client(retrieved_at), configuration=_configuration()).run(
            SecOwnershipImportRequest(forms=("4",))
        )
        storage.raw_records.save(
            _legacy_record(
                source_id=SEC_DOCUMENT_SOURCE_ID,
                schema_version=SEC_DOCUMENT_SCHEMA_VERSION,
                retrieved_at=retrieved_at,
            )
        )
        storage.raw_records.save(
            _legacy_record(
                source_id=OWNERSHIP_SOURCE_ID,
                schema_version=OWNERSHIP_SCHEMA_VERSION,
                retrieved_at=retrieved_at,
            )
        )


def _run_cli(script_name: str, root: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts" / script_name),
            "--root",
            str(root),
            "--asset-id",
            _ASSET_ID,
            "--known-at",
            _KNOWN_AT.isoformat(),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{script_name} failed: {completed.stderr.strip()}")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"{script_name} did not return a JSON object")
    return payload


def _head_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_ROOT, text=True).strip()


def _assert_output(document: dict[str, object], ownership: dict[str, object]) -> None:
    if document.get("state") != "found" or document.get("legacy_records_excluded") != 1:
        raise RuntimeError("document replay did not expose v2 evidence and excluded legacy count")
    revision = document.get("revision")
    if not isinstance(revision, dict) or revision.get("available_at") != _ACCEPTED_AT.isoformat():
        raise RuntimeError("document replay did not select the filing acceptance timestamp")
    if ownership.get("state") != "found" or ownership.get("legacy_records_excluded") != 1:
        raise RuntimeError("ownership query did not expose v2 evidence and excluded legacy count")
    statements = ownership.get("statements")
    if not isinstance(statements, list) or len(statements) != 1:
        raise RuntimeError("ownership query did not return exactly one statement")
    statement = statements[0]
    if not isinstance(statement, dict) or statement.get("available_at") != _ACCEPTED_AT.isoformat():
        raise RuntimeError("ownership query did not preserve filing acceptance availability")


def main() -> int:
    started_at = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="investment-analyst-sec-pit-") as temporary_root:
        root = Path(temporary_root)
        first_root = root / "first"
        second_root = root / "second"
        _populate(first_root, datetime(2025, 2, 2, tzinfo=UTC))
        _populate(second_root, datetime(2026, 3, 10, tzinfo=UTC))
        first_document = _run_cli("query_sec_document_corpus.py", first_root)
        first_ownership = _run_cli("query_sec_ownership.py", first_root)
        second_document = _run_cli("query_sec_document_corpus.py", second_root)
        second_ownership = _run_cli("query_sec_ownership.py", second_root)
        _assert_output(first_document, first_ownership)
        _assert_output(second_document, second_ownership)
        if first_document != second_document or first_ownership != second_ownership:
            raise RuntimeError("replay changed across distinct local retrieval instants")
    print(
        json.dumps(
            {
                "command": "scripts/smoke_sec_pit_semantics.py",
                "completed_at": datetime.now(UTC).isoformat(),
                "head_sha": _head_sha(),
                "network": "disabled by hermetic fake provider client",
                "started_at": started_at.isoformat(),
                "status": "PASS",
                "temporary_storage": "created and cleaned",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
