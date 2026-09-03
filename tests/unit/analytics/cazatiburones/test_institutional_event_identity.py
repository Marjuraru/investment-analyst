"""Unit tests for deterministic institutional event identities."""

from datetime import UTC, datetime
from uuid import UUID

from investment_analyst.analytics.cazatiburones.institutional_event_identity import (
    candidate_id,
    event_id,
    snapshot_id,
)


def test_distinct_clocks_produce_identical_identities() -> None:
    metric_available = datetime(2024, 11, 14, 16, 0, 0, tzinfo=UTC)
    known_at = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)

    payload = {
        "algorithm_version": "cazatiburones-institutional-metrics-v1",
        "asset_id": "equity:us:aapl",
        "available_at": metric_available,
        "cusip": "037833100",
        "input_observation_ids": [
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        ],
        "manager_cik": "0001350694",
        "metric_key": "cazatiburones.institutional.delta_reported_shares",
        "metric_result_id": "33333333-3333-3333-3333-333333333333",
        "parameters": {
            "cusip": "037833100",
            "manager_cik": "0001350694",
            "prior_report_period": "2024-06-30",
            "put_call": None,
            "report_period": "2024-09-30",
            "title_of_class": "COM",
        },
        "prior_report_period": "2024-06-30",
        "put_call": None,
        "report_period": "2024-09-30",
        "rule_id": "institutional-reported-shares-increased",
        "title_of_class": "COM",
        "unit": "shares",
    }

    # Generation under clock one
    id_event_one = event_id(payload)
    id_cand_one = candidate_id(id_event_one, "cazatiburones-persisted-institutional-events-v1")
    id_snap_one = snapshot_id(
        {
            "asset_id": "equity:us:aapl",
            "event_ids": [str(id_event_one)],
            "known_at": known_at,
            "manager_cik": "0001350694",
            "metric_result_ids": ["33333333-3333-3333-3333-333333333333"],
            "policy_version": "cazatiburones-persisted-institutional-events-v1",
        }
    )

    # Generation under clock two (clock is not in payload)
    id_event_two = event_id(payload)
    id_cand_two = candidate_id(id_event_two, "cazatiburones-persisted-institutional-events-v1")
    id_snap_two = snapshot_id(
        {
            "asset_id": "equity:us:aapl",
            "event_ids": [str(id_event_two)],
            "known_at": known_at,
            "manager_cik": "0001350694",
            "metric_result_ids": ["33333333-3333-3333-3333-333333333333"],
            "policy_version": "cazatiburones-persisted-institutional-events-v1",
        }
    )

    assert id_event_one == id_event_two
    assert id_cand_one == id_cand_two
    assert id_snap_one == id_snap_two
    assert isinstance(id_event_one, UUID)
    assert isinstance(id_cand_one, UUID)
    assert isinstance(id_snap_one, UUID)


def test_divergent_payloads_produce_distinct_identities() -> None:
    base = {
        "algorithm_version": "cazatiburones-institutional-metrics-v1",
        "asset_id": "equity:us:aapl",
        "available_at": datetime(2024, 11, 14, 16, 0, 0, tzinfo=UTC),
        "cusip": "037833100",
        "input_observation_ids": ["11111111-1111-1111-1111-111111111111"],
        "manager_cik": "0001350694",
        "metric_key": "cazatiburones.institutional.delta_reported_shares",
        "metric_result_id": "33333333-3333-3333-3333-333333333333",
        "parameters": {},
        "prior_report_period": "2024-06-30",
        "put_call": None,
        "report_period": "2024-09-30",
        "rule_id": "institutional-reported-shares-increased",
        "title_of_class": "COM",
        "unit": "shares",
    }
    variant = dict(base)
    variant["rule_id"] = "institutional-reported-shares-reduced"

    assert event_id(base) != event_id(variant)
