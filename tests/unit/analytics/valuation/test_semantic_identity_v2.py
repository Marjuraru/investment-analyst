"""Cut-independent identities for persisted v2 analytical results."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.activity_metric_identity import (
    semantic_activity_metric_result_id,
)
from investment_analyst.analytics.cazatiburones.activity_metric_models import (
    ActivityMetricCandidate,
)
from investment_analyst.analytics.cazatiburones.institutional_metric_identity import (
    semantic_institutional_metric_result_id,
)
from investment_analyst.analytics.cazatiburones.institutional_metric_models import (
    InstitutionalMetricCandidate,
)
from investment_analyst.analytics.cazatiburones.institutional_weight_identity import (
    semantic_weight_result_id,
)
from investment_analyst.analytics.valuation.identity import (
    valuation_result_id_v2,
    valuation_result_parameters_v2,
)
from investment_analyst.analytics.valuation.models import CorporateValuationRequest
from investment_analyst.core.models import DataQuality

_AVAILABLE = datetime(2026, 1, 5, tzinfo=UTC)
_AS_OF = datetime(2026, 1, 2, tzinfo=UTC)
_CUT_1 = datetime(2026, 1, 6, tzinfo=UTC)
_CUT_2 = datetime(2026, 1, 7, tzinfo=UTC)


def test_valuation_v2_identity_excludes_request_cut_and_keeps_semantic_parameters() -> None:
    observation_ids = (uuid4(), uuid4())
    first_request = CorporateValuationRequest(
        asset_id="equity:us:aapl",
        known_at=_CUT_1,
        valuation_date=_AS_OF.date(),
    )
    second_request = first_request.model_copy(update={"known_at": _CUT_2})
    coordinates = {
        "metric_key": "valuation.corporate.market_cap",
        "valuation_as_of": _AS_OF,
        "available_at": _AVAILABLE,
        "unit": "USD",
        "annual_period_start": None,
        "annual_period_end": _AS_OF,
        "security_basis_version": "aapl-common-share-v1",
        "input_observation_ids": observation_ids,
        "algorithm_version": "corporate-valuation-latest-annual-v1-decimal34",
        "formula": "close_price * shares_outstanding",
        "market_units_per_reported_share": Decimal("1"),
    }

    first_id = valuation_result_id_v2(request=first_request, **coordinates)
    second_id = valuation_result_id_v2(request=second_request, **coordinates)
    parameters = valuation_result_parameters_v2(
        request=first_request,
        annual_period_start=None,
        annual_period_end=_AS_OF,
        security_basis_version="aapl-common-share-v1",
        formula="close_price * shares_outstanding",
        market_units_per_reported_share=Decimal("1"),
    )

    assert first_id.version == 8
    assert first_id == second_id
    assert "known_at" not in parameters


def test_activity_and_institutional_v2_identities_exclude_only_the_execution_cut() -> None:
    input_ids = (uuid4(), uuid4())
    activity = ActivityMetricCandidate(
        asset_id="equity:us:aapl",
        metric_key="cazatiburones.insider.holding_delta_ratio",
        value=Decimal("0.25"),
        unit="ratio",
        as_of=_AS_OF,
        available_at=_AVAILABLE,
        known_at=_CUT_1,
        parameters={"family": "insider", "participant_cik": "0000000001"},
        input_observation_ids=input_ids,
        algorithm_version="cazatiburones-activity-metrics-v1",
        quality=DataQuality.VALID,
    )
    later_activity = activity.model_copy(update={"known_at": _CUT_2})
    assert semantic_activity_metric_result_id(activity).version == 8
    assert semantic_activity_metric_result_id(activity) == semantic_activity_metric_result_id(
        later_activity
    )

    institutional = InstitutionalMetricCandidate(
        asset_id="equity:us:aapl",
        metric_key="cazatiburones.institutional.position_delta",
        value=Decimal("4"),
        unit="shares",
        as_of=_AS_OF,
        available_at=_AVAILABLE,
        known_at=_CUT_1,
        parameters={
            "manager_cik": "0001350694",
            "cusip": "037833100",
            "report_period": "2025-09-30",
        },
        input_observation_ids=input_ids,
        quality=DataQuality.VALID,
    )
    later_institutional = institutional.model_copy(update={"known_at": _CUT_2})
    assert semantic_institutional_metric_result_id(institutional).version == 8
    assert semantic_institutional_metric_result_id(
        institutional
    ) == semantic_institutional_metric_result_id(later_institutional)


def test_weight_v2_identity_excludes_cut_but_changes_with_input_evidence() -> None:
    observation_id = uuid4()
    coordinates = {
        "asset_id": "equity:us:aapl",
        "metric_key": "cazatiburones.institutional.weight.shares",
        "as_of": _AS_OF,
        "available_at": _AVAILABLE,
        "quality": DataQuality.VALID,
        "algorithm_version": "cazatiburones-institutional-weight-v1",
        "parameters": {"manager_cik": "0001350694", "cusip": "037833100"},
        "input_observation_id": observation_id,
    }
    first = semantic_weight_result_id(known_at=_CUT_1, **coordinates)
    second = semantic_weight_result_id(known_at=_CUT_2, **coordinates)
    changed_evidence = semantic_weight_result_id(
        known_at=_CUT_2,
        **{**coordinates, "input_observation_id": uuid4()},
    )

    assert first.version == 8
    assert first == second
    assert first != changed_evidence
