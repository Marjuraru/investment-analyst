"""Unit tests for institutional effective holdings engine."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.institutional_effective_holdings_engine import (
    compose,
)
from investment_analyst.analytics.cazatiburones.institutional_effective_holdings_models import (
    InstitutionalEffectiveHoldingsQuery,
)
from investment_analyst.evidence.sec_institutional_semantics.models import (
    InstitutionalSemanticsRow,
)

_MANAGER = "0001067983"
_PERIOD = date(2024, 12, 31)
_T0 = datetime(2025, 2, 14, 12, tzinfo=UTC)


def _row(
    row_number: int, cusip: str = "037833100", value: Decimal = Decimal("10.00")
) -> InstitutionalSemanticsRow:
    return InstitutionalSemanticsRow(
        row_id=uuid4(),
        row_number=row_number,
        issuer_name="TEST CORP",
        title_of_class="COM",
        cusip=cusip,
        value_as_reported=value,
        quantity=Decimal("100"),
    )


def _artifact(
    *,
    accession: str,
    available_at: datetime,
    is_amendment: bool = False,
    amendment_number: str | None = None,
    amendment_type: str | None = None,
    rows: tuple[InstitutionalSemanticsRow, ...] = (),
    report_period: date = _PERIOD,
    declared_entry_total: int | None = None,
    declared_value_total: Decimal | None = None,
    confidential_omitted: bool | None = None,
    report_type: str | None = "13F HOLDINGS REPORT",
    other_managers_included: tuple[object, ...] = (),
) -> object:
    entry_tot = len(rows) if declared_entry_total is None else declared_entry_total
    val_tot = (
        sum((r.value_as_reported for r in rows), start=Decimal(0))
        if declared_value_total is None
        else declared_value_total
    )
    return SimpleNamespace(
        artifact_id=uuid4(),
        accession=accession,
        manager_cik=_MANAGER,
        report_period=report_period,
        available_at=available_at,
        is_amendment=is_amendment,
        amendment_number=amendment_number,
        amendment_type=amendment_type,
        report_type=report_type,
        confidential_omitted=confidential_omitted,
        declared_entry_total=entry_tot,
        declared_value_total=val_tot,
        rows=rows,
        other_managers_included=other_managers_included,
    )


def test_restatement_replaces_and_new_holdings_supplements() -> None:
    # Original has 2 rows
    orig = _artifact(
        accession="0000950123-25-000001",
        available_at=_T0,
        rows=(_row(1), _row(2)),
    )
    # Restatement has 3 rows (replaces orig)
    restat = _artifact(
        accession="0000950123-25-000002",
        available_at=_T0 + timedelta(days=1),
        is_amendment=True,
        amendment_number="1",
        amendment_type="RESTATEMENT",
        rows=(_row(1), _row(2), _row(3)),
    )
    # New Holdings has 1 row (supplements restat)
    suppl = _artifact(
        accession="0000950123-25-000003",
        available_at=_T0 + timedelta(days=2),
        is_amendment=True,
        amendment_number="2",
        amendment_type="NEW HOLDINGS",
        rows=(_row(1),),
    )

    query = InstitutionalEffectiveHoldingsQuery(
        manager_cik=_MANAGER,
        report_period=_PERIOD,
        known_at=_T0 + timedelta(days=3),
    )
    result = compose(query=query, artifacts=(orig, restat, suppl))

    assert result.status == "effective"
    assert result.total_rows == 4  # 3 from restatement + 1 from new holdings
    assert len(result.contributors) == 2
    assert result.contributors[0].accession == restat.accession
    assert result.contributors[1].accession == suppl.accession


def test_interleaved_amendment_chain_composes_deterministically() -> None:
    # Sequence: Original -> New Holdings 1 -> Restatement -> New Holdings 2
    orig = _artifact(
        accession="0000950123-25-000001",
        available_at=_T0,
        rows=(_row(1),),
    )
    nh1 = _artifact(
        accession="0000950123-25-000002",
        available_at=_T0 + timedelta(days=1),
        is_amendment=True,
        amendment_number="1",
        amendment_type="NEW HOLDINGS",
        rows=(_row(1),),
    )
    restat = _artifact(
        accession="0000950123-25-000003",
        available_at=_T0 + timedelta(days=2),
        is_amendment=True,
        amendment_number="2",
        amendment_type="RESTATEMENT",
        rows=(_row(1), _row(2)),
    )
    nh2 = _artifact(
        accession="0000950123-25-000004",
        available_at=_T0 + timedelta(days=3),
        is_amendment=True,
        amendment_number="3",
        amendment_type="NEW HOLDINGS",
        rows=(_row(1),),
    )

    query = InstitutionalEffectiveHoldingsQuery(
        manager_cik=_MANAGER,
        report_period=_PERIOD,
        known_at=_T0 + timedelta(days=4),
    )
    # The restatement replaces orig and nh1; only restat and nh2 survive
    result = compose(query=query, artifacts=(orig, nh1, restat, nh2))
    assert result.status == "effective"
    assert result.total_rows == 3  # 2 from restat + 1 from nh2
    assert tuple(c.accession for c in result.contributors) == (restat.accession, nh2.accession)


def test_berkshire_q2_2025_chain_produces_114_rows() -> None:
    # Original 110 rows
    orig_rows = tuple(_row(i) for i in range(1, 111))
    orig = _artifact(
        accession="0000950123-25-005701",
        available_at=_T0,
        rows=orig_rows,
    )
    # Amendment 4 rows (NEW HOLDINGS)
    amend_rows = tuple(_row(i) for i in range(1, 5))
    amend = _artifact(
        accession="0000950123-25-008361",
        available_at=_T0 + timedelta(days=1),
        is_amendment=True,
        amendment_number="1",
        amendment_type="NEW HOLDINGS",
        rows=amend_rows,
    )

    # Before amendment: exactly 110 rows
    query_before = InstitutionalEffectiveHoldingsQuery(
        manager_cik=_MANAGER,
        report_period=_PERIOD,
        known_at=_T0 + timedelta(hours=1),
    )
    res_before = compose(query=query_before, artifacts=(orig, amend))
    assert res_before.status == "effective"
    assert res_before.total_rows == 110
    assert len(res_before.contributors) == 1

    # After amendment: exactly 114 rows
    query_after = InstitutionalEffectiveHoldingsQuery(
        manager_cik=_MANAGER,
        report_period=_PERIOD,
        known_at=_T0 + timedelta(days=2),
    )
    res_after = compose(query=query_after, artifacts=(orig, amend))
    assert res_after.status == "effective"
    assert res_after.total_rows == 114
    assert len(res_after.contributors) == 2


def test_known_at_excludes_future_amendments() -> None:
    orig = _artifact(accession="0000950123-25-000001", available_at=_T0, rows=(_row(1),))
    future = _artifact(
        accession="0000950123-25-000002",
        available_at=_T0 + timedelta(days=1),
        is_amendment=True,
        amendment_number="1",
        amendment_type="NEW HOLDINGS",
        rows=(_row(1),),
    )
    query = InstitutionalEffectiveHoldingsQuery(
        manager_cik=_MANAGER,
        report_period=_PERIOD,
        known_at=_T0 + timedelta(hours=1),
    )
    result = compose(query=query, artifacts=(orig, future))
    assert result.status == "effective"
    assert result.total_rows == 1
    assert result.contributors[0].accession == orig.accession


def test_decimal_and_source_row_lineage_remain_exact() -> None:
    value = Decimal("12345.67")
    r1 = _row(1, value=value)
    orig = _artifact(accession="0000950123-25-000001", available_at=_T0, rows=(r1,))
    query = InstitutionalEffectiveHoldingsQuery(
        manager_cik=_MANAGER, report_period=_PERIOD, known_at=_T0 + timedelta(days=1)
    )
    result = compose(query=query, artifacts=(orig,))
    assert result.rows[0].row.value_as_reported == value
    assert result.rows[0].source_row_id == r1.row_id
    assert result.rows[0].source_row_number == 1
    assert result.rows[0].source_accession == orig.accession


def test_multiple_original_gap_tie_and_unknown_type_fail_closed() -> None:
    # 1. Multiple originals
    o1 = _artifact(accession="0000950123-25-000001", available_at=_T0, rows=(_row(1),))
    o2 = _artifact(
        accession="0000950123-25-000002", available_at=_T0 + timedelta(hours=1), rows=(_row(1),)
    )
    q = InstitutionalEffectiveHoldingsQuery(
        manager_cik=_MANAGER, report_period=_PERIOD, known_at=_T0 + timedelta(days=1)
    )
    assert compose(query=q, artifacts=(o1, o2)).status == "ambiguous"

    # 2. Gap in amendment chain
    a2 = _artifact(
        accession="0000950123-25-000003",
        available_at=_T0 + timedelta(days=1),
        is_amendment=True,
        amendment_number="2",
        amendment_type="NEW HOLDINGS",
        rows=(_row(1),),
    )
    assert compose(query=q, artifacts=(o1, a2)).status == "insufficient"

    # 3. Available at tie
    a1_tie = _artifact(
        accession="0000950123-25-000004",
        available_at=_T0,
        is_amendment=True,
        amendment_number="1",
        amendment_type="NEW HOLDINGS",
        rows=(_row(1),),
    )
    assert compose(query=q, artifacts=(o1, a1_tie)).status == "ambiguous"

    # 4. Unknown type (including legacy NEW HOLDINGS ENTRIES)
    a1_unknown = _artifact(
        accession="0000950123-25-000005",
        available_at=_T0 + timedelta(days=1),
        is_amendment=True,
        amendment_number="1",
        amendment_type="NEW HOLDINGS ENTRIES",
        rows=(_row(1),),
    )
    assert compose(query=q, artifacts=(o1, a1_unknown)).status == "ambiguous"


def test_missing_or_mismatched_effective_contributor_is_not_evaluable() -> None:
    # Declared total mismatch
    orig = _artifact(
        accession="0000950123-25-000001",
        available_at=_T0,
        rows=(_row(1),),
        declared_entry_total=2,  # Mismatch! 1 row observed, 2 declared
    )
    q = InstitutionalEffectiveHoldingsQuery(
        manager_cik=_MANAGER, report_period=_PERIOD, known_at=_T0 + timedelta(days=1)
    )
    result = compose(query=q, artifacts=(orig,))
    assert result.status == "not_evaluable"
    assert result.reason in {"composition_not_evaluable", "contributor_incomplete"}


def test_restatement_is_never_additive() -> None:
    # Restatement replaces, does NOT add to original
    orig = _artifact(accession="0000950123-25-000001", available_at=_T0, rows=(_row(1), _row(2)))
    restat = _artifact(
        accession="0000950123-25-000002",
        available_at=_T0 + timedelta(days=1),
        is_amendment=True,
        amendment_number="1",
        amendment_type="RESTATEMENT",
        rows=(_row(1),),
    )
    q = InstitutionalEffectiveHoldingsQuery(
        manager_cik=_MANAGER, report_period=_PERIOD, known_at=_T0 + timedelta(days=2)
    )
    result = compose(query=q, artifacts=(orig, restat))
    assert result.status == "effective"
    assert result.total_rows == 1  # Exactly 1 row from restatement, NOT 3


def test_new_holdings_is_never_replacement() -> None:
    # New holdings supplements, does NOT replace original
    orig = _artifact(accession="0000950123-25-000001", available_at=_T0, rows=(_row(1), _row(2)))
    suppl = _artifact(
        accession="0000950123-25-000002",
        available_at=_T0 + timedelta(days=1),
        is_amendment=True,
        amendment_number="1",
        amendment_type="NEW HOLDINGS",
        rows=(_row(1),),
    )
    q = InstitutionalEffectiveHoldingsQuery(
        manager_cik=_MANAGER, report_period=_PERIOD, known_at=_T0 + timedelta(days=2)
    )
    result = compose(query=q, artifacts=(orig, suppl))
    assert result.status == "effective"
    assert result.total_rows == 3  # 2 + 1, original is retained


def test_rows_are_never_deduplicated_merged_or_aggregated() -> None:
    # Same CUSIP across original and new holdings
    r1 = _row(1, cusip="037833100")
    r2 = _row(1, cusip="037833100")
    orig = _artifact(accession="0000950123-25-000001", available_at=_T0, rows=(r1,))
    suppl = _artifact(
        accession="0000950123-25-000002",
        available_at=_T0 + timedelta(days=1),
        is_amendment=True,
        amendment_number="1",
        amendment_type="NEW HOLDINGS",
        rows=(r2,),
    )
    q = InstitutionalEffectiveHoldingsQuery(
        manager_cik=_MANAGER, report_period=_PERIOD, known_at=_T0 + timedelta(days=2)
    )
    result = compose(query=q, artifacts=(orig, suppl))
    assert result.total_rows == 2
    assert len(result.rows) == 2
    # Rows retain their distinct contributor indices
    assert result.rows[0].contributor_index == 0
    assert result.rows[1].contributor_index == 1


def test_pagination_never_loses_contributor_lineage() -> None:
    r1 = _row(1)
    r2 = _row(2)
    orig = _artifact(accession="0000950123-25-000001", available_at=_T0, rows=(r1, r2))
    q = InstitutionalEffectiveHoldingsQuery(
        manager_cik=_MANAGER,
        report_period=_PERIOD,
        known_at=_T0 + timedelta(days=1),
        offset=1,
        limit=1,
    )
    result = compose(query=q, artifacts=(orig,))
    assert result.total_rows == 2
    assert len(result.rows) == 1
    assert result.rows[0].source_row_number == 2
    assert result.rows[0].source_accession == orig.accession
    assert result.truncated is False


def test_no_asset_mapping_cross_manager_aggregation_or_persistence() -> None:
    # Result only exposes cusip/raw semantics, never asset_id
    orig = _artifact(accession="0000950123-25-000001", available_at=_T0, rows=(_row(1),))
    q = InstitutionalEffectiveHoldingsQuery(
        manager_cik=_MANAGER, report_period=_PERIOD, known_at=_T0 + timedelta(days=1)
    )
    result = compose(query=q, artifacts=(orig,))
    assert not hasattr(result.rows[0], "asset_id")


def test_no_score_signal_recommendation_or_existing_consumer_change() -> None:
    # Result contains no score, signal or recommendation
    orig = _artifact(accession="0000950123-25-000001", available_at=_T0, rows=(_row(1),))
    q = InstitutionalEffectiveHoldingsQuery(
        manager_cik=_MANAGER, report_period=_PERIOD, known_at=_T0 + timedelta(days=1)
    )
    result = compose(query=q, artifacts=(orig,))
    assert not hasattr(result, "score")
    assert not hasattr(result, "signal")
    assert not hasattr(result, "recommendation")
