from datetime import UTC, date, datetime

from investment_analyst.analytics.cazatiburones.institutional_metric_engine import calculate
from investment_analyst.analytics.cazatiburones.institutional_metric_models import (
    InstitutionalMetricClose,
)


def test_unresolved_close_is_never_bridged() -> None:
    closes = (
        InstitutionalMetricClose(
            report_period=date(2024, 3, 31), artifact_id=None, status="insufficient"
        ),
        InstitutionalMetricClose(
            report_period=date(2024, 6, 30), artifact_id=None, status="ambiguous"
        ),
    )
    result = calculate(
        asset_id="equity:us:aapl",
        manager_cik="1067983",
        known_at=datetime(2025, 1, 1, tzinfo=UTC),
        closes=closes,
    )
    assert {item.reason for item in result.skipped} == {"unresolved_close"}


def test_positions_with_and_without_put_call_are_ordered_deterministically() -> None:
    import json
    from decimal import Decimal
    from uuid import uuid4

    from investment_analyst.core.models import NormalizedObservation, SourceReference
    from investment_analyst.core.models.enums import DataFrequency, DataQuality

    prior_date = date(2024, 3, 31)
    current_date = date(2024, 6, 30)
    prior_dt = datetime(2024, 3, 31, 18, tzinfo=UTC)
    current_dt = datetime(2024, 6, 30, 18, tzinfo=UTC)

    def _make_obs(
        *,
        field_name: str,
        value: Decimal,
        cusip: str,
        title_of_class: str,
        put_call: str | None,
        row_id: int,
        period_end: datetime,
    ) -> NormalizedObservation:
        record_key = json.dumps(
            {
                "cusip": cusip,
                "title_of_class": title_of_class,
                "put_call": put_call,
                "row_id": row_id,
            }
        )
        return NormalizedObservation(
            observation_id=uuid4(),
            raw_record_id=uuid4(),
            asset_id="equity:us:aapl",
            field_name=field_name,
            value=value,
            unit="shares" if "shares" in field_name else "USD",
            frequency=DataFrequency.QUARTERLY,
            period_end=period_end,
            available_at=period_end,
            normalized_at=period_end,
            source=SourceReference(
                source_id="sec-edgar:institutional-holdings-13f",
                record_key=record_key,
                retrieved_at=period_end,
            ),
            quality=DataQuality.VALID,
            transformation_version="test-v1",
        )

    # Manager holds the same CUSIP with and without options (put_call=None, CALL, PUT)
    positions = [
        ("037833100", "COM", None, 1),
        ("037833100", "COM", "CALL", 2),
        ("037833100", "COM", "PUT", 3),
    ]
    fields = [
        ("institutional_reported_shares", Decimal("100"), Decimal("150")),
        ("institutional_reported_fair_value", Decimal("20000"), Decimal("30000")),
    ]

    prior_obs: list[NormalizedObservation] = []
    current_obs: list[NormalizedObservation] = []
    for cusip, title, put_call, row_id in positions:
        for field_name, p_val, c_val in fields:
            prior_obs.append(
                _make_obs(
                    field_name=field_name,
                    value=p_val,
                    cusip=cusip,
                    title_of_class=title,
                    put_call=put_call,
                    row_id=row_id,
                    period_end=prior_dt,
                )
            )
            current_obs.append(
                _make_obs(
                    field_name=field_name,
                    value=c_val,
                    cusip=cusip,
                    title_of_class=title,
                    put_call=put_call,
                    row_id=row_id,
                    period_end=current_dt,
                )
            )

    closes = (
        InstitutionalMetricClose(
            report_period=prior_date,
            artifact_id=None,
            status="original_complete",
            observations=tuple(prior_obs),
        ),
        InstitutionalMetricClose(
            report_period=current_date,
            artifact_id=None,
            status="original_complete",
            observations=tuple(current_obs),
        ),
    )

    result = calculate(
        asset_id="equity:us:aapl",
        manager_cik="0000884546",
        known_at=datetime(2025, 1, 1, tzinfo=UTC),
        closes=closes,
    )

    assert len(result.candidates) > 0
    # Collect the unique put_call values in the order they were processed
    seen_put_calls: list[str | None] = []
    for candidate in result.candidates:
        pc = candidate.parameters["put_call"]
        if pc not in seen_put_calls:
            seen_put_calls.append(pc)

    # Deterministic order: None first, then "CALL", then "PUT"
    assert seen_put_calls == [None, "CALL", "PUT"]
