"""Unit tests for Form 13F tabular parser and deterministic selection policy."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal

from investment_analyst.evidence.sec_institutional_universe.models import Sec13FDataSetRevision
from investment_analyst.evidence.sec_institutional_universe.service import (
    SecInstitutionalUniverseService,
)


def _build_test_zip() -> bytes:
    """Build a synthetic valid Form 13F dataset zip for selection policy testing."""
    submissions_tsv = (
        "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n"
        # Manager 1: Older period (should be ignored for AAPL)
        "0001067983-26-000001\t10-JAN-2026\t13F-HR\t0001067983\t31-DEC-2025\n"
        # Manager 1: Latest period initial filing
        "0001067983-26-000010\t15-APR-2026\t13F-HR\t0001067983\t31-MAR-2026\n"
        # Manager 1: Latest period amendment filing
        "0001067983-26-000020\t15-MAY-2026\t13F-HR/A\t0001067983\t31-MAR-2026\n"
        # Manager 2: Same value as Manager 1 amendment (tiebreaker: CIK 2 < 1067983)
        "0000000002-26-000001\t10-MAY-2026\t13F-HR\t0000000002\t31-MAR-2026\n"
        # Manager 3: Lower value
        "0000000003-26-000001\t10-MAY-2026\t13F-HR\t0000000003\t31-MAR-2026\n"
        # Manager 4: 13F-NT (Notice report, must be excluded)
        "0000000004-26-000001\t10-MAY-2026\t13F-NT\t0000000004\t31-MAR-2026\n"
    )

    coverpage_tsv = (
        "ACCESSION_NUMBER\tFILINGMANAGER_NAME\tISAMENDMENT\n"
        "0001067983-26-000001\tBERKSHIRE HATHAWAY INC\tN\n"
        "0001067983-26-000010\tBERKSHIRE HATHAWAY INC\tN\n"
        "0001067983-26-000020\tBERKSHIRE HATHAWAY INC\tY\n"
        "0000000002-26-000001\tALPHA ASSET MANAGEMENT\tN\n"
        "0000000003-26-000001\tBETA PARTNERS LP\tN\n"
        "0000000004-26-000001\tGAMMA NOTICE ONLY\tN\n"
    )

    infotable_tsv = (
        "ACCESSION_NUMBER\tCUSIP\tVALUE\n"
        # Manager 1 older period (AAPL)
        "0001067983-26-000001\t037833100\t50000\n"
        # Manager 1 latest period initial: two rows summing to 50000 + 25000 = 75000
        "0001067983-26-000010\t037833100\t50000\n"
        "0001067983-26-000010\t037833100\t25000\n"
        # Manager 1 latest period amendment: one row of 80000
        "0001067983-26-000020\t037833100\t80000\n"
        # Manager 2 latest period: one row of 80000 (tie with Manager 1)
        "0000000002-26-000001\t037833100\t80000\n"
        # Manager 3 latest period: one row of 10000
        "0000000003-26-000001\t037833100\t10000\n"
        # Manager 4 13F-NT row (must be skipped because 13F-NT is excluded)
        "0000000004-26-000001\t037833100\t99999\n"
        # Unrelated non-catalog CUSIP (must be streamed past and ignored)
        "0000000002-26-000001\t999999999\t100000\n"
    )

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as zf:
        zf.writestr("SUBMISSION.tsv", submissions_tsv)
        zf.writestr("COVERPAGE.tsv", coverpage_tsv)
        zf.writestr("INFOTABLE.tsv", infotable_tsv)
    return stream.getvalue()


def test_service_exact_cusip_latest_period_decimal_cap_and_ties() -> None:
    zip_bytes = _build_test_zip()
    service = SecInstitutionalUniverseService()

    revision = Sec13FDataSetRevision.create(
        dataset_url="https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip",
        period_start=date(2026, 3, 1),
        period_end=date(2026, 5, 31),
        content_sha256="f" * 64,
        size_bytes=len(zip_bytes),
        retrieved_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    # Catalog contains 2 assets: AAPL (matched) and OTHER (unmatched)
    catalog_cusips = {
        "037833100": "equity:us:aapl",
        "111111111": "equity:us:unmatched",
    }

    # Set cap = 2 to test selection cap (3 candidate managers, top 2 selected)
    snapshot = service.build_universe_snapshot(
        zip_bytes,
        dataset_revision=revision,
        catalog_cusips=catalog_cusips,
        catalog_version=1,
        max_managers_per_asset=2,
    )

    assert snapshot.eligible_asset_count == 2
    assert snapshot.matched_asset_count == 1
    # 3 candidates (13F-NT excluded)
    assert snapshot.candidate_manager_count == 3
    assert snapshot.selected_manager_count == 2
    assert snapshot.unselected_manager_count == 1
    assert snapshot.covered_cusips == ("037833100",)
    assert snapshot.missing_cusips == ("111111111",)

    # Verify candidates
    candidates = snapshot.candidates
    assert len(candidates) == 3

    # Tiebreaker test:
    # Manager 2 and Manager 1 both have value 80000.
    # Manager 2 CIK is "0000000002" < Manager 1 CIK "0001067983".
    # Therefore Manager 2 is Rank 1, Manager 1 is Rank 2!
    m2 = candidates[0]
    assert m2.manager_cik == "0000000002"
    assert m2.manager_name == "ALPHA ASSET MANAGEMENT"
    assert m2.value_as_filed == Decimal("80000")
    assert m2.selection_rank == 1
    assert m2.is_selected is True

    m1 = candidates[1]
    assert m1.manager_cik == "0001067983"
    assert m1.manager_name == "BERKSHIRE HATHAWAY INC"
    assert m1.value_as_filed == Decimal("80000")
    assert m1.accession == "0001067983-26-000020"  # Latest amendment accession chosen
    assert m1.form == "13F-HR/A"
    assert m1.is_amendment is True
    assert set(m1.accession_lineage) == {"0001067983-26-000010", "0001067983-26-000020"}
    assert m1.selection_rank == 2
    assert m1.is_selected is True

    # Manager 3 has value 10000, unselected due to cap = 2
    m3 = candidates[2]
    assert m3.manager_cik == "0000000003"
    assert m3.value_as_filed == Decimal("10000")
    assert m3.selection_rank is None
    assert m3.is_selected is False
