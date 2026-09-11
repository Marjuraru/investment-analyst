"""Unit tests for the directed, resumable Form 13F refresh over the manager universe."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid5

import pytest
from pydantic import ValidationError

from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.application.sec_institutional_holdings_refresh import (
    MISSING_UNIVERSE_INSTRUCTION,
    SecInstitutionalHoldingsDirectedRefreshApplication,
    SecInstitutionalHoldingsDirectedRefreshError,
    plan_directed_manager_page,
)
from investment_analyst.application.sec_institutional_holdings_refresh_models import (
    SEC_INSTITUTIONAL_HOLDINGS_DIRECTED_REFRESH_POLICY,
    SEC_INSTITUTIONAL_HOLDINGS_DIRECTED_REFRESH_SCHEMA_VERSION,
    SecInstitutionalHoldingsDirectedRefreshRequest,
)
from investment_analyst.application.sec_institutional_universe import (
    SecInstitutionalUniverseApplication,
)
from investment_analyst.application.sec_institutional_universe_models import (
    SecInstitutionalUniverseRefreshRequest,
    SecInstitutionalUniverseRefreshResult,
)
from investment_analyst.evidence.sec_institutional_universe.identity import (
    candidate_id,
    dataset_revision_id,
)
from investment_analyst.evidence.sec_institutional_universe.models import (
    Sec13FManagerCandidate,
    Sec13FManagerUniverseSnapshot,
)
from investment_analyst.evidence.sec_institutional_universe.repository import (
    SecInstitutionalUniverseRepository,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse, HttpTransport
from investment_analyst.providers.institutional_holdings.sec_13f_data_sets import (
    SEC_13F_DATA_SETS_CATALOG_URL,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_PERIOD = date(2026, 3, 31)
_OLDER_PERIOD = date(2025, 12, 31)
_SHA = "a" * 64
_REVISION = dataset_revision_id(date(2026, 3, 1), date(2026, 5, 31), _SHA)
_ZIP_URL = (
    "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
    "01mar2026-31may2026_form13f.zip"
)
_IDENTITY = SecEdgarIdentity("Analyst user@example.com")


def _build_universe_zip() -> bytes:
    submissions_tsv = (
        "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n"
        "0001067983-26-000010\t15-APR-2026\t13F-HR\t0001067983\t31-MAR-2026\n"
        "0000000002-26-000001\t10-MAY-2026\t13F-HR\t0000000002\t31-MAR-2026\n"
    )
    coverpage_tsv = (
        "ACCESSION_NUMBER\tFILINGMANAGER_NAME\tISAMENDMENT\n"
        "0001067983-26-000010\tBERKSHIRE HATHAWAY INC\tN\n"
        "0000000002-26-000001\tALPHA ASSET MANAGEMENT\tN\n"
    )
    infotable_tsv = (
        "ACCESSION_NUMBER\tCUSIP\tVALUE\n"
        "0001067983-26-000010\t037833100\t80000\n"
        "0000000002-26-000001\t037833100\t50000\n"
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("SUBMISSION.tsv", submissions_tsv)
        archive.writestr("COVERPAGE.tsv", coverpage_tsv)
        archive.writestr("INFOTABLE.tsv", infotable_tsv)
    return stream.getvalue()


class _UniverseTransport(HttpTransport):
    def __init__(self, zip_bytes: bytes) -> None:
        self.zip_bytes = zip_bytes
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers,
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        self.calls.append(url)
        if url == SEC_13F_DATA_SETS_CATALOG_URL:
            body = f'<html><a href="{_ZIP_URL}">2026 Q1</a></html>'.encode()
        elif url == _ZIP_URL:
            body = self.zip_bytes
        else:
            raise RuntimeError(f"Unexpected URL request: {url}")
        return HttpResponse(status_code=200, body=body, headers={}, url=url)


def _materialize_universe(
    location: StorageLocationRequest,
) -> SecInstitutionalUniverseRefreshResult:
    transport = _UniverseTransport(_build_universe_zip())
    return SecInstitutionalUniverseApplication(
        ApplicationRuntime.create_default(), transport_factory=lambda: transport
    ).refresh(
        SecInstitutionalUniverseRefreshRequest(),
        sec_identity=_IDENTITY,
        location=location,
    )


def _forbidden_application() -> SecInstitutionalHoldingsDirectedRefreshApplication:
    def _forbidden(*args, **kwargs):
        raise AssertionError("no provider client may be created without a verified snapshot")

    return SecInstitutionalHoldingsDirectedRefreshApplication(
        ApplicationRuntime.create_default(),
        submissions_client_factory=_forbidden,
        document_client_factory=_forbidden,
    )


def _candidate(
    *,
    cik: str,
    rank: int | None,
    period: date = _PERIOD,
    accession: str,
    name: str = "MANAGER LLC",
    value: str = "100",
) -> Sec13FManagerCandidate:
    return Sec13FManagerCandidate(
        candidate_id=candidate_id(
            dataset_sha256=_SHA,
            asset_id="equity:us:aapl",
            cusip="037833100",
            manager_cik=cik,
            accession=accession,
            form="13F-HR",
            report_period=period,
        ),
        dataset_revision_id=_REVISION,
        asset_id="equity:us:aapl",
        cusip="037833100",
        manager_cik=cik,
        manager_name=name,
        accession=accession,
        form="13F-HR",
        filing_date=date(2026, 4, 15),
        report_period=period,
        value_as_filed=Decimal(value),
        is_selected=rank is not None,
        selection_rank=rank,
        accession_lineage=(accession,),
    )


def _snapshot(candidates: tuple[Sec13FManagerCandidate, ...]) -> Sec13FManagerUniverseSnapshot:
    selected = sum(1 for item in candidates if item.is_selected)
    return Sec13FManagerUniverseSnapshot.create(
        dataset_revision_id=_REVISION,
        dataset_sha256=_SHA,
        catalog_version=1,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 5, 31),
        retrieved_at=datetime(2026, 6, 1, tzinfo=UTC),
        event_time=datetime(2026, 5, 31, tzinfo=UTC),
        eligible_asset_count=1,
        matched_asset_count=1,
        candidate_manager_count=len(candidates),
        selected_manager_count=selected,
        unselected_manager_count=len(candidates) - selected,
        coverage_complete=True,
        covered_cusips=("037833100",),
        candidates=candidates,
    )


def test_request_admits_only_operational_bounds() -> None:
    request = SecInstitutionalHoldingsDirectedRefreshRequest(
        known_at=datetime(2026, 6, 2, tzinfo=UTC),
        manager_offset=1,
        manager_limit=5,
        accessions_per_manager=4,
    )
    assert request.manager_limit == 5
    assert request.accessions_per_manager == 4
    assert SecInstitutionalHoldingsDirectedRefreshRequest(
        known_at=datetime(2026, 6, 2, tzinfo=UTC)
    ).model_dump(mode="json") == {
        "known_at": "2026-06-02T00:00:00Z",
        "manager_offset": 0,
        "manager_limit": 1,
        "accessions_per_manager": 1,
    }
    for overrides in (
        {"manager_limit": 0},
        {"manager_limit": 26},
        {"manager_offset": -1},
        {"accessions_per_manager": 0},
        {"accessions_per_manager": 11},
        {"filer_cik": "0001067983"},
        {"asset_id": "equity:us:aapl"},
        {"cusip": "037833100"},
        {"accession": "0001067983-26-000010"},
        {"form": "13F-HR"},
        {"url": _ZIP_URL},
        {"report_period": _PERIOD},
    ):
        with pytest.raises(ValidationError):
            SecInstitutionalHoldingsDirectedRefreshRequest(
                known_at=datetime(2026, 6, 2, tzinfo=UTC), **overrides
            )
    assert SEC_INSTITUTIONAL_HOLDINGS_DIRECTED_REFRESH_SCHEMA_VERSION == (
        "sec-institutional-holdings-directed-refresh-v1"
    )
    assert SEC_INSTITUTIONAL_HOLDINGS_DIRECTED_REFRESH_POLICY == (
        "sec-institutional-holdings-directed-page-v1"
    )


def test_page_selection_is_ranked_deduplicated_and_paginated() -> None:
    snapshot = _snapshot(
        (
            _candidate(cik="0000000003", rank=3, accession="0000000003-26-000001", value="30"),
            _candidate(cik="0000000009", rank=None, accession="0000000009-26-000001", value="10"),
            _candidate(cik="0000000001", rank=1, accession="0000000001-26-000001", value="90"),
            _candidate(cik="0000000002", rank=2, accession="0000000002-26-000002", value="50"),
            _candidate(
                cik="0000000001",
                rank=1,
                accession="0000000001-26-000002",
                value="90",
                name="OTHER NAME LLC",
            ),
            _candidate(
                cik="0000000002",
                rank=4,
                accession="0000000002-26-000001",
                value="20",
                period=_OLDER_PERIOD,
            ),
        )
    )

    page = plan_directed_manager_page(snapshot, offset=0, limit=10)
    assert [(item.manager_cik, item.report_period) for item in page] == [
        ("0000000001", _PERIOD),
        ("0000000002", _PERIOD),
        ("0000000003", _PERIOD),
        ("0000000002", _OLDER_PERIOD),
    ]

    assert [
        item.manager_cik
        for item in plan_directed_manager_page(snapshot, offset=0, limit=2)
        + plan_directed_manager_page(snapshot, offset=2, limit=2)
    ] == ["0000000001", "0000000002", "0000000003", "0000000002"]
    assert plan_directed_manager_page(snapshot, offset=4, limit=2) == ()

    deduplicated = page[0]
    assert len(deduplicated.candidate_ids) == 2
    assert len(set(deduplicated.candidate_ids)) == 2
    assert deduplicated.dataset_accession_hints == (
        "0000000001-26-000001",
        "0000000001-26-000002",
    )
    assert set(deduplicated.dataset_accession_hints).issubset(set(deduplicated.accession_lineage))
    assert deduplicated.asset_ids == ("equity:us:aapl",)
    assert deduplicated.cusips == ("037833100",)
    assert deduplicated.manager_name == "MANAGER LLC"

    unselected_only = _snapshot(
        (_candidate(cik="0000000009", rank=None, accession="0000000009-26-000001"),)
    )
    assert plan_directed_manager_page(unselected_only, offset=0, limit=5) == ()
    with pytest.raises(SecInstitutionalHoldingsDirectedRefreshError):
        plan_directed_manager_page(snapshot, offset=-1, limit=1)
    with pytest.raises(SecInstitutionalHoldingsDirectedRefreshError):
        plan_directed_manager_page(snapshot, offset=0, limit=0)


def test_refresh_fails_closed_without_a_snapshot_and_never_creates_a_client(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        SecInstitutionalHoldingsDirectedRefreshError, match="manager universe snapshot"
    ):
        _forbidden_application().refresh(
            SecInstitutionalHoldingsDirectedRefreshRequest(
                known_at=datetime(2026, 6, 2, tzinfo=UTC)
            ),
            sec_identity=_IDENTITY,
            location=StorageLocationRequest(legacy_root=tmp_path),
        )
    assert MISSING_UNIVERSE_INSTRUCTION.endswith(
        "run scripts/refresh_sec_institutional_manager_universe.py first"
    )


def test_refresh_fails_closed_before_the_snapshot_cut(tmp_path: Path) -> None:
    location = StorageLocationRequest(legacy_root=tmp_path)
    universe = _materialize_universe(location)
    with pytest.raises(SecInstitutionalHoldingsDirectedRefreshError):
        _forbidden_application().refresh(
            SecInstitutionalHoldingsDirectedRefreshRequest(
                known_at=universe.available_at - timedelta(seconds=1)
            ),
            sec_identity=_IDENTITY,
            location=location,
        )


def test_refresh_rejects_a_snapshot_whose_dataset_revision_is_absent(tmp_path: Path) -> None:
    location = StorageLocationRequest(legacy_root=tmp_path)
    universe = _materialize_universe(location)
    orphan = Sec13FManagerUniverseSnapshot.create(
        dataset_revision_id=uuid5(_REVISION, "orphan-revision"),
        dataset_sha256=universe.dataset_sha256,
        catalog_version=1,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 5, 31),
        retrieved_at=universe.available_at + timedelta(days=1),
        event_time=datetime(2026, 5, 31, tzinfo=UTC),
        eligible_asset_count=1,
        matched_asset_count=1,
        candidate_manager_count=0,
        selected_manager_count=0,
        unselected_manager_count=0,
        coverage_complete=True,
        candidates=(),
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        SecInstitutionalUniverseRepository(storage.raw_records, storage.documents).save_snapshot(
            orphan
        )

    with pytest.raises(SecInstitutionalHoldingsDirectedRefreshError, match="lineage"):
        _forbidden_application().refresh(
            SecInstitutionalHoldingsDirectedRefreshRequest(
                known_at=universe.available_at + timedelta(days=2)
            ),
            sec_identity=_IDENTITY,
            location=location,
        )
