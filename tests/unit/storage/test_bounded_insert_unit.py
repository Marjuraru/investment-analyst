"""Tests for the fixed-schema bounded SQL transport."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import duckdb
import pytest

from investment_analyst.storage.bounded_insert import (
    BoundedInsertTable,
    insert_bounded,
    write_transaction,
)


def test_bounded_insert_preserves_exact_rows_and_order() -> None:
    connection = duckdb.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE metric_results_v2 (
            result_id VARCHAR, asset_id VARCHAR, metric_key VARCHAR,
            value_text VARCHAR, unit VARCHAR, as_of VARCHAR,
            available_at VARCHAR, computed_at VARCHAR, parameters_json VARCHAR,
            evidence_set_id VARCHAR, algorithm_version VARCHAR, quality VARCHAR
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE diagnostic_v2_components (
            diagnostic_id VARCHAR, position INTEGER, component_key VARCHAR,
            score_text VARCHAR, weight_text VARCHAR,
            weighted_contribution_text VARCHAR, explanation VARCHAR
        )
        """
    )

    result_id = uuid4()
    evidence_set_id = uuid4()
    exact_instant = datetime(2026, 5, 8, 9, 10, 11, 123456, tzinfo=timezone(timedelta(hours=-5)))
    parameters = '{"label":"cierre, δ y \\"quotes\\"","n":2}'
    assert (
        insert_bounded(
            connection,
            BoundedInsertTable.METRICS_V2,
            [
                [
                    result_id,
                    "equity:us:prueba",
                    "precio δ",
                    Decimal("12345.6700"),
                    "USD",
                    exact_instant,
                    exact_instant,
                    exact_instant,
                    parameters,
                    evidence_set_id,
                    "v1",
                    "PARTIAL",
                ]
            ],
        )
        == 1
    )
    stored = connection.execute("SELECT * FROM metric_results_v2").fetchone()
    assert stored == (
        str(result_id),
        "equity:us:prueba",
        "precio δ",
        "12345.6700",
        "USD",
        exact_instant.astimezone(UTC).isoformat(),
        exact_instant.astimezone(UTC).isoformat(),
        exact_instant.astimezone(UTC).isoformat(),
        parameters,
        str(evidence_set_id),
        "v1",
        "PARTIAL",
    )

    diagnostic_id = uuid4()
    ordered_rows = [
        [
            diagnostic_id,
            2,
            "tercero",
            Decimal("0.30"),
            Decimal("0.3"),
            Decimal("0.09"),
            'texto δ "tres"',
        ],
        [
            diagnostic_id,
            0,
            "primero",
            Decimal("0.10"),
            Decimal("0.1"),
            Decimal("0.01"),
            "texto primero",
        ],
        [
            diagnostic_id,
            1,
            "segundo",
            Decimal("0.20"),
            Decimal("0.2"),
            Decimal("0.04"),
            "texto segundo",
        ],
    ]
    assert (
        insert_bounded(connection, BoundedInsertTable.DIAGNOSTIC_COMPONENTS_V2, ordered_rows) == 1
    )
    ordered = connection.execute(
        "SELECT position, component_key, score_text, explanation "
        "FROM diagnostic_v2_components ORDER BY position"
    ).fetchall()
    assert ordered == [
        (0, "primero", "0.10", "texto primero"),
        (1, "segundo", "0.20", "texto segundo"),
        (2, "tercero", "0.30", 'texto δ "tres"'),
    ]
    connection.close()


def test_lot_failure_rolls_back_owned_unit_without_taking_over_caller_transaction() -> None:
    connection = duckdb.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE diagnostic_v2_components (
            diagnostic_id VARCHAR, position INTEGER, component_key VARCHAR,
            score_text VARCHAR, weight_text VARCHAR,
            weighted_contribution_text VARCHAR, explanation VARCHAR,
            PRIMARY KEY (diagnostic_id, position)
        )
        """
    )
    diagnostic_id = str(uuid4())
    rows = [
        [diagnostic_id, index, f"component-{index}", "1", "1", "1", "atomic"]
        for index in range(256)
    ]
    rows.append([diagnostic_id, 0, "duplicate", "1", "1", "1", "must roll back"])
    with pytest.raises(duckdb.ConstraintException), write_transaction(connection):
        insert_bounded(connection, BoundedInsertTable.DIAGNOSTIC_COMPONENTS_V2, rows)
    assert connection.execute("SELECT count(*) FROM diagnostic_v2_components").fetchone() == (0,)

    connection.execute("BEGIN TRANSACTION")
    with write_transaction(connection):
        insert_bounded(
            connection,
            BoundedInsertTable.DIAGNOSTIC_COMPONENTS_V2,
            [[diagnostic_id, 0, "caller-owned", "1", "1", "1", "visible before caller commit"]],
        )
    assert connection.execute("SELECT count(*) FROM diagnostic_v2_components").fetchone() == (1,)
    connection.execute("ROLLBACK")
    assert connection.execute("SELECT count(*) FROM diagnostic_v2_components").fetchone() == (0,)
    connection.close()


def test_transport_is_bounded_and_rejects_unsafe_structure() -> None:
    connection = duckdb.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE diagnostic_v2_components (
            diagnostic_id VARCHAR, position INTEGER, component_key VARCHAR,
            score_text VARCHAR, weight_text VARCHAR,
            weighted_contribution_text VARCHAR, explanation VARCHAR
        )
        """
    )
    diagnostic_id = str(uuid4())
    expected_total = 0
    for size in (0, 1, 256, 257, 513):
        rows = [
            [
                diagnostic_id,
                expected_total + index,
                f"component-{expected_total + index}",
                "1",
                "1",
                "1",
                "bounded",
            ]
            for index in range(size)
        ]
        expected_executions = (size + 255) // 256
        assert (
            insert_bounded(connection, BoundedInsertTable.DIAGNOSTIC_COMPONENTS_V2, rows)
            == expected_executions
        )
        expected_total += size
        assert connection.execute("SELECT count(*) FROM diagnostic_v2_components").fetchone() == (
            expected_total,
        )
    assert connection.execute(
        "SELECT min(position), max(position) FROM diagnostic_v2_components"
    ).fetchone() == (0, expected_total - 1)

    with pytest.raises(TypeError, match="registered table"):
        insert_bounded(connection, "diagnostic_v2_components; DROP TABLE metric_results_v2", rows)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="expected 7"):
        insert_bounded(
            connection,
            BoundedInsertTable.DIAGNOSTIC_COMPONENTS_V2,
            [[diagnostic_id, 0]],
        )
    with pytest.raises(TypeError, match="float"):
        insert_bounded(
            connection,
            BoundedInsertTable.DIAGNOSTIC_COMPONENTS_V2,
            [[diagnostic_id, 0, "k", 1.0, "1", "1", "text"]],
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        insert_bounded(
            connection,
            BoundedInsertTable.RAW_RECORD_INDEX,
            [
                [
                    "id",
                    "asset",
                    "source",
                    None,
                    datetime(2026, 1, 1),
                    datetime.now(UTC),
                    "p",
                    "h",
                    "v1",
                    "{}",
                ]
            ],
        )
    assert connection.execute("SELECT count(*) FROM diagnostic_v2_components").fetchone() == (
        expected_total,
    )
    connection.close()
