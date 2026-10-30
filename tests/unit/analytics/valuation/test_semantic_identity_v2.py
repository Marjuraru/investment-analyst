"""Cut-independent identities for persisted v2 analytical results."""

from datetime import UTC, date, datetime, timedelta
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
from investment_analyst.analytics.valuation.filing_models import (
    CorporateValuationSelectedFilingRequest,
)
from investment_analyst.analytics.valuation.identity import (
    filing_valuation_result_id_v2,
    filing_valuation_result_parameters_v2,
    valuation_result_id_v2,
    valuation_result_parameters_v2,
)
from investment_analyst.analytics.valuation.models import CorporateValuationRequest
from investment_analyst.catalog.models import SecurityUnitBasisRevision
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
    assert "security_unit_basis_evidence" not in parameters


def _adr_evidence(*, factor: str = "0.2", available_days: int = 0) -> SecurityUnitBasisRevision:
    return SecurityUnitBasisRevision(
        basis="depositary_receipt",
        factor=factor,
        adjustment="all",
        valid_from=date(2026, 4, 17),
        available_at=datetime(2026, 4, 17, tzinfo=UTC) + timedelta(days=available_days),
        reported_share_unit="common_share",
        cik="0001046179",
        accession_number="0001628280-26-025362",
        source_url="https://www.sec.gov/Archives/edgar/data/1046179/example.htm",
        source_sha256="9b4a4e6cd176e5f55976dbe067d006b395aab2570115d943e6c089560805a141",
    )


def test_adr_basis_evidence_binds_identity_without_binding_the_cut() -> None:
    evidence = _adr_evidence()
    observation_ids = (uuid4(),)
    request = CorporateValuationRequest(
        asset_id="equity:us:tsm",
        known_at=_CUT_1,
        valuation_date=_AS_OF.date(),
    )
    coordinates = {
        "metric_key": "valuation.corporate.market_cap",
        "valuation_as_of": _AS_OF,
        "available_at": _AVAILABLE,
        "unit": "USD",
        "annual_period_start": None,
        "annual_period_end": _AS_OF,
        "security_basis_version": f"security-unit-basis-v2:{evidence.revision}",
        "input_observation_ids": observation_ids,
        "algorithm_version": "corporate-valuation-latest-annual-v1-decimal34",
        "formula": "close_price * shares_outstanding * market_units_per_reported_share",
        "market_units_per_reported_share": Decimal("0.2"),
        "basis_evidence": evidence,
    }

    baseline = valuation_result_id_v2(request=request, **coordinates)
    later_cut = valuation_result_id_v2(
        request=request.model_copy(update={"known_at": _CUT_2}), **coordinates
    )
    assert baseline == later_cut

    changed_factor = _adr_evidence(factor="0.25")
    assert (
        valuation_result_id_v2(
            request=request,
            **{
                **coordinates,
                "security_basis_version": f"security-unit-basis-v2:{changed_factor.revision}",
                "market_units_per_reported_share": Decimal("0.25"),
                "basis_evidence": changed_factor,
            },
        )
        != baseline
    )

    changed_availability = _adr_evidence(available_days=1)
    assert (
        valuation_result_id_v2(
            request=request,
            **{
                **coordinates,
                "available_at": _AVAILABLE + timedelta(days=1),
                "security_basis_version": (
                    f"security-unit-basis-v2:{changed_availability.revision}"
                ),
                "basis_evidence": changed_availability,
            },
        )
        != baseline
    )

    parameters = valuation_result_parameters_v2(
        request=request,
        annual_period_start=None,
        annual_period_end=_AS_OF,
        security_basis_version=coordinates["security_basis_version"],
        formula=coordinates["formula"],
        market_units_per_reported_share=Decimal("0.2"),
        basis_evidence=evidence,
    )
    assert parameters["security_unit_basis_evidence"]["revision"] == evidence.revision


def test_filing_valuation_identity_binds_selector_and_excludes_the_cut() -> None:
    request = CorporateValuationSelectedFilingRequest(
        asset_id="equity:us:tsm",
        known_at=_CUT_1,
        valuation_date=date(2026, 4, 17),
        annual_period_end=date(2024, 12, 31),
        filing_accession_number="0001193125-25-000001",
        filing_accepted_at=datetime(2025, 2, 15, 16, tzinfo=UTC),
        filing_form="20-F",
        fiscal_year="2024",
        fiscal_period="FY",
    )
    observation_ids = (uuid4(),)
    coordinates = {
        "metric_key": "valuation.corporate.market_cap",
        "valuation_as_of": _AS_OF,
        "available_at": _AVAILABLE,
        "unit": "USD",
        "annual_period_start": None,
        "annual_period_end": _AS_OF,
        "security_basis_version": "security-unit-basis-v2:fixture",
        "input_observation_ids": observation_ids,
        "algorithm_version": "corporate-valuation-selected-annual-filing-v1-decimal34",
        "formula": "close_price * shares_outstanding * market_units_per_reported_share",
        "market_units_per_reported_share": Decimal("1"),
    }

    baseline = filing_valuation_result_id_v2(request=request, **coordinates)
    later_cut = filing_valuation_result_id_v2(
        request=request.model_copy(update={"known_at": _CUT_2}),
        **coordinates,
    )
    changed_accession = filing_valuation_result_id_v2(
        request=request.model_copy(update={"filing_accession_number": "0001193125-25-000002"}),
        **coordinates,
    )
    changed_availability = filing_valuation_result_id_v2(
        request=request.model_copy(
            update={
                "filing_accepted_at": datetime(2025, 2, 15, 20, tzinfo=UTC),
            }
        ),
        **coordinates,
    )
    changed_inputs = filing_valuation_result_id_v2(
        request=request,
        **{**coordinates, "input_observation_ids": (uuid4(),)},
    )
    parameters = filing_valuation_result_parameters_v2(
        request=request,
        selector=request.selector(),
        annual_period_start=None,
        annual_period_end=_AS_OF,
        security_basis_version="security-unit-basis-v2:fixture",
        formula=coordinates["formula"],
        market_units_per_reported_share=Decimal("1"),
    )

    assert baseline.version == 8
    assert baseline == later_cut
    assert baseline != changed_accession
    assert baseline != changed_availability
    assert baseline != changed_inputs
    assert "known_at" not in parameters
    assert parameters["basis"] == "selected_annual_filing"
    assert parameters["filing_accession_number"] == "0001193125-25-000001"
    assert parameters["filing_accepted_at"] == "2025-02-15T16:00:00+00:00"
    assert parameters["fiscal_year"] == "2024"
    assert parameters["fiscal_period"] == "FY"


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
