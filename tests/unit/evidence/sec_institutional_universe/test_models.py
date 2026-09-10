"""Unit tests for SEC Form 13F manager universe domain models and deterministic identities."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from investment_analyst.evidence.sec_institutional_universe.identity import (
    candidate_id,
    dataset_raw_record_id,
    dataset_revision_id,
    snapshot_id,
    snapshot_raw_record_id,
)
from investment_analyst.evidence.sec_institutional_universe.models import (
    Sec13FDataSetRevision,
    Sec13FManagerCandidate,
    Sec13FManagerUniverseSnapshot,
)


def test_dataset_revision_deterministic_identity_and_invariants() -> None:
    period_start = date(2026, 3, 1)
    period_end = date(2026, 5, 31)
    content_sha256 = "a" * 64
    retrieved_at = datetime(2026, 6, 1, 14, 30, tzinfo=UTC)

    rev = Sec13FDataSetRevision.create(
        dataset_url="https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip",
        period_start=period_start,
        period_end=period_end,
        content_sha256=content_sha256,
        size_bytes=10_000_000,
        retrieved_at=retrieved_at,
    )

    expected_rev_id = dataset_revision_id(period_start, period_end, content_sha256)
    expected_raw_id = dataset_raw_record_id(expected_rev_id)

    assert rev.revision_id == expected_rev_id
    assert rev.raw_record_id == expected_raw_id
    assert rev.available_at == retrieved_at

    # Negative: available_at != retrieved_at fails
    with pytest.raises(ValueError, match="available_at must equal retrieved_at"):
        Sec13FDataSetRevision(
            revision_id=expected_rev_id,
            raw_record_id=expected_raw_id,
            dataset_url="https://www.sec.gov/test.zip",
            period_start=period_start,
            period_end=period_end,
            content_sha256=content_sha256,
            size_bytes=1000,
            retrieved_at=retrieved_at,
            available_at=datetime(2026, 6, 2, 0, 0, tzinfo=UTC),
        )

    # Negative: period_start > period_end fails
    with pytest.raises(ValueError, match="period_start"):
        Sec13FDataSetRevision.create(
            dataset_url="https://www.sec.gov/test.zip",
            period_start=date(2026, 6, 1),
            period_end=date(2026, 5, 1),
            content_sha256=content_sha256,
            size_bytes=1000,
            retrieved_at=retrieved_at,
        )


def test_candidate_manager_normalization_and_selection_rules() -> None:
    rev_id = dataset_revision_id(date(2026, 3, 1), date(2026, 5, 31), "b" * 64)
    cand_id = candidate_id(
        dataset_sha256="b" * 64,
        asset_id="equity:us:aapl",
        cusip="037833100",
        manager_cik="1067983",  # unpadded
        accession="0001067983-26-000010",
        form="13F-HR",
        report_period=date(2026, 3, 31),
    )

    candidate = Sec13FManagerCandidate(
        candidate_id=cand_id,
        dataset_revision_id=rev_id,
        asset_id="equity:us:aapl",
        cusip="037833100",
        manager_cik="1067983",
        manager_name="BERKSHIRE HATHAWAY INC",
        accession="0001067983-26-000010",
        form="13F-HR",
        filing_date=date(2026, 5, 15),
        report_period=date(2026, 3, 31),
        value_as_filed=Decimal("150000000"),
        is_amendment=False,
        is_selected=True,
        selection_rank=1,
    )

    # CIK must be normalized to 10 digits
    assert candidate.manager_cik == "0001067983"
    assert candidate.value_as_filed == Decimal("150000000")
    assert candidate.value_unit == "usd_thousands_as_filed"

    # Negative: is_selected=True without selection_rank fails
    with pytest.raises(ValueError, match="selection_rank is required"):
        Sec13FManagerCandidate(
            candidate_id=cand_id,
            dataset_revision_id=rev_id,
            asset_id="equity:us:aapl",
            cusip="037833100",
            manager_cik="0001067983",
            manager_name="BERKSHIRE",
            accession="0001067983-26-000010",
            form="13F-HR",
            filing_date=date(2026, 5, 15),
            report_period=date(2026, 3, 31),
            value_as_filed=Decimal("100"),
            is_selected=True,
            selection_rank=None,
        )

    # Negative: is_selected=False with rank fails
    with pytest.raises(ValueError, match="selection_rank must be None"):
        Sec13FManagerCandidate(
            candidate_id=cand_id,
            dataset_revision_id=rev_id,
            asset_id="equity:us:aapl",
            cusip="037833100",
            manager_cik="0001067983",
            manager_name="BERKSHIRE",
            accession="0001067983-26-000010",
            form="13F-HR",
            filing_date=date(2026, 5, 15),
            report_period=date(2026, 3, 31),
            value_as_filed=Decimal("100"),
            is_selected=False,
            selection_rank=2,
        )


def test_snapshot_invariants_and_deterministic_identity() -> None:
    rev_id = dataset_revision_id(date(2026, 3, 1), date(2026, 5, 31), "c" * 64)
    retrieved_at = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    event_time = datetime(2026, 5, 31, 0, 0, tzinfo=UTC)

    snapshot = Sec13FManagerUniverseSnapshot.create(
        dataset_revision_id=rev_id,
        dataset_sha256="c" * 64,
        catalog_version=1,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 5, 31),
        retrieved_at=retrieved_at,
        event_time=event_time,
        eligible_asset_count=1,
        matched_asset_count=1,
        candidate_manager_count=0,
        selected_manager_count=0,
        unselected_manager_count=0,
        covered_cusips=("037833100",),
    )

    expected_snap_id = snapshot_id(
        rev_id, "sec-13f-manager-universe-selection-v1", 1, ("037833100",)
    )
    assert snapshot.snapshot_id == expected_snap_id
    assert snapshot.raw_record_id == snapshot_raw_record_id(expected_snap_id)

    # Incompatible manager counts
    with pytest.raises(ValueError, match="selected \\+ unselected"):
        Sec13FManagerUniverseSnapshot.create(
            dataset_revision_id=rev_id,
            dataset_sha256="c" * 64,
            catalog_version=1,
            period_start=date(2026, 3, 1),
            period_end=date(2026, 5, 31),
            retrieved_at=retrieved_at,
            event_time=event_time,
            eligible_asset_count=1,
            matched_asset_count=1,
            candidate_manager_count=5,
            selected_manager_count=3,
            unselected_manager_count=1,  # 3 + 1 != 5
        )
