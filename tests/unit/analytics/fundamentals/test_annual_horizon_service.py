"""Tests for deterministic explicit annual horizons over research metrics."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Context, Decimal, localcontext
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.fundamentals.annual_horizon_models import (
    FundamentalAnnualHorizonsRequest,
    FundamentalAnnualHorizonsResult,
)
from investment_analyst.analytics.fundamentals.annual_horizon_service import (
    SecIssuerFundamentalAnnualHorizonService,
)
from investment_analyst.analytics.fundamentals.research_service import (
    AmbiguousFundamentalResearchRevisionError,
)
from investment_analyst.core.models import (
    AssetClass,
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    SourceReference,
)
from investment_analyst.providers.asset_config import (
    SecAssetConfiguration,
    sec_source_ids,
)
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    GENERIC_TRANSFORMATION_VERSION,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    TRANSFORMATION_VERSION as AAPL_TRANSFORMATION_VERSION,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    SecFactPeriodType,
    get_sec_fact_definition,
)
from investment_analyst.providers.fundamentals.sec_raw_records import (
    aapl_sec_configuration,
)

_KNOWN_AT = datetime(2026, 10, 29, 19, 47, tzinfo=UTC)
_FCF = "fundamental.research.free_cash_flow"
_EPS = "fundamental.research.diluted_eps"
_ROA = "fundamental.research.return_on_assets_ending_balance"
_GROSS_MARGIN = "fundamental.research.gross_margin"
_DATES = (
    date(2015, 12, 26),
    date(2016, 12, 31),
    date(2017, 12, 30),
    date(2018, 12, 29),
    date(2019, 12, 28),
    date(2020, 12, 26),
    date(2021, 12, 25),
    date(2022, 12, 31),
    date(2023, 12, 30),
    date(2024, 12, 28),
    date(2025, 12, 27),
)


class _ObservationRepository:
    def __init__(self, observations: list[NormalizedObservation]) -> None:
        self._observations = observations
        self.calls: list[dict[str, object]] = []
        self.hydrated: list[NormalizedObservation] = []

    def list(self, **filters: object) -> list[NormalizedObservation]:
        self.calls.append(filters)
        field_names = filters.get("field_names")
        allowed_fields = set(field_names) if field_names is not None else None
        period_from = filters.get("period_end_from")
        period_to = filters.get("period_end_to")
        rows = [
            item
            for item in self._observations
            if (filters.get("asset_id") is None or item.asset_id == filters["asset_id"])
            and (filters.get("source_id") is None or item.source.source_id == filters["source_id"])
            and (allowed_fields is None or item.field_name in allowed_fields)
            and (filters.get("frequency") is None or item.frequency is filters["frequency"])
            and (filters.get("quality") is None or item.quality is filters["quality"])
            and (
                filters.get("available_to") is None or item.available_at <= filters["available_to"]
            )
            and (
                period_from is None
                or (item.period_end is not None and item.period_end.date() >= period_from)
            )
            and (
                period_to is None
                or (item.period_end is not None and item.period_end.date() <= period_to)
            )
        ]
        self.hydrated.extend(rows)
        return rows


class _Storage:
    def __init__(self, observations: list[NormalizedObservation]) -> None:
        self.observations = _ObservationRepository(observations)
        self.is_open = True

    def require_open(self) -> None:
        assert self.is_open


def _generic_configuration() -> SecAssetConfiguration:
    submissions, companyfacts = sec_source_ids("AMD")
    return SecAssetConfiguration(
        asset_id="equity:us:amd",
        cik="0000002488",
        ticker="AMD",
        submissions_source_id=submissions,
        companyfacts_source_id=companyfacts,
        name="Advanced Micro Devices",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


def _transformation_version(configuration: SecAssetConfiguration) -> str:
    if configuration.asset_id == aapl_sec_configuration().asset_id:
        return AAPL_TRANSFORMATION_VERSION
    return GENERIC_TRANSFORMATION_VERSION


def _observation(
    configuration: SecAssetConfiguration,
    field_name: str,
    value: str,
    period_end: date,
    *,
    available_at: datetime | None = None,
    accession_number: str = "0000320193-25-000001",
    observation_id=None,
    raw_record_id=None,
) -> NormalizedObservation:
    definition = get_sec_fact_definition(field_name)
    available = available_at or datetime.combine(period_end, datetime.min.time(), tzinfo=UTC) + (
        timedelta(days=40)
    )
    raw_id = raw_record_id or uuid4()
    period_at = datetime.combine(period_end, datetime.min.time(), tzinfo=UTC)
    period_start = (
        period_at - timedelta(days=363)
        if definition.period_type is SecFactPeriodType.DURATION
        else None
    )
    key = {
        "accession_number": accession_number,
        "taxonomy": definition.taxonomy,
        "tag": definition.tag,
        "unit": definition.unit,
        "period": period_end.isoformat(),
        "companyfacts_record_id": str(raw_id),
        "submissions_record_id": str(uuid4()),
        "form": "10-K",
        "fiscal_year": str(period_end.year),
        "fiscal_period": "FY",
    }
    return NormalizedObservation(
        observation_id=observation_id or uuid4(),
        raw_record_id=raw_id,
        asset_id=configuration.asset_id,
        field_name=field_name,
        value=Decimal(value),
        unit=definition.unit,
        frequency=DataFrequency.ANNUAL,
        observed_at=period_at,
        period_start=period_start,
        period_end=period_at,
        available_at=available,
        normalized_at=max(available, datetime(2025, 11, 1, tzinfo=UTC)),
        source=SourceReference(
            source_id=configuration.companyfacts_source_id,
            record_key=json.dumps(key, separators=(",", ":"), sort_keys=True),
            retrieved_at=max(available, datetime(2025, 11, 1, tzinfo=UTC)),
        ),
        quality=DataQuality.VALID,
        transformation_version=_transformation_version(configuration),
    )


def _seed_year(
    configuration: SecAssetConfiguration,
    observations: list[NormalizedObservation],
    period_end: date,
    *,
    free_cash_flow: str | None = None,
    diluted_eps: str | None = None,
    roa: str | None = None,
    gross_margin: str | None = None,
    available_at: datetime | None = None,
    accession_number: str | None = None,
) -> None:
    available = available_at or datetime.combine(
        period_end, datetime.min.time(), tzinfo=UTC
    ) + timedelta(days=40)
    accession = accession_number or f"0000320193-{period_end.year}-000001"
    if free_cash_flow is not None:
        cash = Decimal(free_cash_flow)
        observations.append(
            _observation(
                configuration,
                "fundamental.operating_cash_flow",
                str(Decimal("100") + cash),
                period_end,
                available_at=available,
                accession_number=accession,
            )
        )
        observations.append(
            _observation(
                configuration,
                "fundamental.capital_expenditures",
                "100",
                period_end,
                available_at=available,
                accession_number=accession,
            )
        )
    if diluted_eps is not None:
        observations.append(
            _observation(
                configuration,
                "fundamental.diluted_earnings_per_share",
                diluted_eps,
                period_end,
                available_at=available,
                accession_number=accession,
            )
        )
    if roa is not None:
        observations.append(
            _observation(
                configuration,
                "fundamental.net_income",
                str(Decimal(roa) * Decimal("500")),
                period_end,
                available_at=available,
                accession_number=accession,
            )
        )
        observations.append(
            _observation(
                configuration,
                "fundamental.assets",
                "500",
                period_end,
                available_at=available,
                accession_number=accession,
            )
        )
    if gross_margin is not None:
        observations.append(
            _observation(
                configuration,
                "fundamental.gross_profit",
                str(Decimal(gross_margin) * Decimal("1000")),
                period_end,
                available_at=available,
                accession_number=accession,
            )
        )
        observations.append(
            _observation(
                configuration,
                "fundamental.revenue",
                "1000",
                period_end,
                available_at=available,
                accession_number=accession,
            )
        )


def _four_metric_seed(
    configuration: SecAssetConfiguration,
    *,
    dates: tuple[date, ...] = _DATES,
    free_cash_flows: tuple[str, ...] | None = None,
    known_available_at: datetime | None = None,
) -> list[NormalizedObservation]:
    observations: list[NormalizedObservation] = []
    cash_values = free_cash_flows or tuple(str(80 + index * 9) for index in range(len(dates)))
    for index, period_end in enumerate(dates):
        _seed_year(
            configuration,
            observations,
            period_end,
            free_cash_flow=cash_values[index],
            diluted_eps=str(Decimal("2") + Decimal(index) / Decimal("10")),
            roa=str(Decimal("0.10") + Decimal(index) / Decimal("1000")),
            gross_margin=str(Decimal("0.40") + Decimal(index) / Decimal("1000")),
            available_at=known_available_at,
        )
    return observations


def _request(
    anchor: date,
    *,
    asset_id: str = "equity:us:aapl",
    metric_keys: tuple[str, ...] = (_FCF, _EPS, _ROA),
    known_at: datetime = _KNOWN_AT,
    horizons: tuple[int, ...] = (3, 5, 10),
) -> FundamentalAnnualHorizonsRequest:
    return FundamentalAnnualHorizonsRequest(
        asset_id=asset_id,
        known_at=known_at,
        end_period_end=anchor,
        metric_keys=metric_keys,
        horizons=horizons,
    )


def _series(result: FundamentalAnnualHorizonsResult, metric_key: str):
    return next(item for item in result.series if item.definition.metric_key == metric_key)


def _expected_cagr(first: Decimal, last: Decimal, first_date: date, last_date: date) -> Decimal:
    elapsed = (last_date - first_date).days
    with localcontext(Context(prec=34)):
        return (last / first) ** (Decimal("365.2425") / Decimal(elapsed)) - 1


def test_three_five_and_ten_complete_horizons_use_real_closes_and_decimal_cagr() -> None:
    configuration = aapl_sec_configuration()
    observations = _four_metric_seed(configuration)
    service = SecIssuerFundamentalAnnualHorizonService(_Storage(observations), configuration)

    result = service.query(_request(_DATES[-1]))

    assert result.asset_id == "equity:us:aapl"
    assert result.source_id == configuration.companyfacts_source_id
    assert tuple(item.definition.metric_key for item in result.series) == tuple(
        sorted((_FCF, _EPS, _ROA))
    )
    cash = _series(result, _FCF)
    assert len(cash.points) == 11
    assert {row.status for row in cash.horizons} == {"complete"}
    assert tuple(row.required_points for row in cash.horizons) == (4, 6, 11)
    assert tuple(row.observed_points for row in cash.horizons) == (4, 6, 11)
    expected_cash = tuple(str(80 + index * 9) for index in range(11))
    assert tuple(str(point.value) for point in cash.points) == expected_cash
    for row in cash.horizons:
        segment = cash.points[-row.observed_points :]
        assert row.statistics is not None
        assert row.statistics.point_count == row.required_points
        assert row.statistics.first_period_end == segment[0].period_end
        assert row.statistics.latest_period_end == segment[-1].period_end
        assert row.cagr_reason == "calculated"
        assert row.statistics.compound_annual_growth_rate == _expected_cagr(
            segment[0].value,
            segment[-1].value,
            segment[0].period_end.date(),
            segment[-1].period_end.date(),
        )
        assert (
            row.elapsed_days == (segment[-1].period_end.date() - segment[0].period_end.date()).days
        )
        assert all(point.input_observation_ids for point in segment)

    eps = _series(result, _EPS)
    assert {row.status for row in eps.horizons} == {"complete"}
    assert all(row.cagr_reason == "calculated" for row in eps.horizons)

    roa = _series(result, _ROA)
    assert {row.status for row in roa.horizons} == {"complete"}
    assert all(row.cagr_reason == "unit_not_eligible" for row in roa.horizons)
    assert all(row.statistics is not None for row in roa.horizons)
    assert all(row.statistics.compound_annual_growth_rate is None for row in roa.horizons)
    assert all(row.statistics.horizon_change_rate is None for row in roa.horizons)
    assert all(row.statistics.horizon_change is not None for row in roa.horizons)

    assert result.coverage.series_returned == 3
    assert result.coverage.points_returned == 33
    assert result.coverage.horizon_rows_returned == 9
    assert result.coverage.complete_horizons == 9
    assert result.coverage.incomplete_horizons == 0
    payload = result.to_json_dict()
    eps_index = next(
        index
        for index, item in enumerate(payload["series"])
        if item["definition"]["metric_key"] == _EPS
    )
    assert payload["series"][eps_index]["points"][0]["value"] == "2"
    assert FundamentalAnnualHorizonsResult.model_validate(payload) == result


def test_missing_anchor_close_returns_anchor_unavailable_before_other_reasons() -> None:
    configuration = aapl_sec_configuration()
    observations = _four_metric_seed(configuration)
    service = SecIssuerFundamentalAnnualHorizonService(_Storage(observations), configuration)

    result = service.query(_request(date(2026, 1, 2)))

    for series in result.series:
        assert {row.status for row in series.horizons} == {"anchor_unavailable"}
        assert all(row.statistics is None for row in series.horizons)
        assert all(row.cagr_reason is None for row in series.horizons)
        assert all(row.observed_points == 11 for row in series.horizons)
    assert result.coverage.complete_horizons == 0
    assert result.coverage.incomplete_horizons == 9


def test_ten_year_horizon_is_insufficient_when_only_six_closes_exist() -> None:
    configuration = aapl_sec_configuration()
    observations = _four_metric_seed(configuration, dates=_DATES[-6:])
    service = SecIssuerFundamentalAnnualHorizonService(_Storage(observations), configuration)

    result = service.query(_request(_DATES[-1]))

    cash = _series(result, _FCF)
    statuses = {row.horizon_years: row for row in cash.horizons}
    assert statuses[3].status == "complete"
    assert statuses[5].status == "complete"
    assert statuses[10].status == "insufficient_history"
    assert statuses[10].observed_points == 6
    assert statuses[10].required_points == 11
    assert statuses[10].statistics is None
    assert statuses[10].cagr_reason is None
    assert result.coverage.complete_horizons == 6
    assert result.coverage.incomplete_horizons == 3


def test_annual_jump_is_nonconsecutive_not_a_shortened_horizon() -> None:
    configuration = aapl_sec_configuration()
    dates = (
        date(2020, 12, 26),
        date(2021, 12, 25),
        date(2022, 12, 31),
        date(2023, 12, 30),
        date(2025, 12, 27),
    )
    observations = _four_metric_seed(configuration, dates=dates)
    service = SecIssuerFundamentalAnnualHorizonService(_Storage(observations), configuration)

    result = service.query(_request(dates[-1]))

    cash = _series(result, _FCF)
    statuses = {row.horizon_years: row for row in cash.horizons}
    assert statuses[3].status == "nonconsecutive_periods"
    assert statuses[3].observed_points == 4
    assert statuses[3].statistics is None
    assert statuses[5].status == "insufficient_history"
    assert statuses[10].status == "insufficient_history"
    assert statuses[3].period_ends[0] == datetime(2021, 12, 25, tzinfo=UTC)
    assert statuses[3].period_ends[-1] == datetime(2025, 12, 27, tzinfo=UTC)


def test_fifty_two_and_fifty_three_week_gaps_remain_consecutive() -> None:
    configuration = aapl_sec_configuration()
    dates = (
        date(2022, 12, 31),
        date(2023, 12, 30),
        date(2024, 12, 28),
        date(2025, 12, 27),
    )
    observations = _four_metric_seed(configuration, dates=dates)
    service = SecIssuerFundamentalAnnualHorizonService(_Storage(observations), configuration)

    result = service.query(_request(dates[-1], horizons=(3,)))

    cash = _series(result, _FCF)
    row = cash.horizons[0]
    assert row.status == "complete"
    assert row.elapsed_days == (dates[-1] - dates[0]).days
    assert row.statistics is not None
    assert row.statistics.compound_annual_growth_rate == _expected_cagr(
        Decimal("80"),
        Decimal("107"),
        dates[0],
        dates[-1],
    )


def test_ratio_horizon_is_complete_without_relative_rates() -> None:
    configuration = aapl_sec_configuration()
    observations = _four_metric_seed(configuration)
    service = SecIssuerFundamentalAnnualHorizonService(_Storage(observations), configuration)

    result = service.query(_request(_DATES[-1], metric_keys=(_GROSS_MARGIN,)))

    row = result.series[0].horizons[-1]
    assert row.status == "complete"
    assert row.cagr_reason == "unit_not_eligible"
    assert row.statistics is not None
    assert row.statistics.compound_annual_growth_rate is None
    assert row.statistics.horizon_change_rate is None
    assert row.statistics.latest_change_rate_from_previous_available is None
    assert row.statistics.horizon_change is not None


def test_nonpositive_earliest_endpoint_blocks_only_the_affected_cagr() -> None:
    configuration = aapl_sec_configuration()
    cash_values = ("-50",) + tuple(str(90 + index) for index in range(10))
    observations = _four_metric_seed(configuration, free_cash_flows=cash_values)
    service = SecIssuerFundamentalAnnualHorizonService(_Storage(observations), configuration)

    result = service.query(_request(_DATES[-1], metric_keys=(_FCF,)))

    rows = {row.horizon_years: row for row in result.series[0].horizons}
    assert rows[3].status == "complete"
    assert rows[3].cagr_reason == "calculated"
    assert rows[5].status == "complete"
    assert rows[5].cagr_reason == "calculated"
    assert rows[10].status == "complete"
    assert rows[10].cagr_reason == "nonpositive_endpoint"
    assert rows[10].statistics is not None
    assert rows[10].statistics.compound_annual_growth_rate is None


def test_intermediate_negative_value_does_not_block_a_positive_endpoint_cagr() -> None:
    configuration = aapl_sec_configuration()
    cash_values = tuple("100" if index != 5 else "-5" for index in range(11))
    observations = _four_metric_seed(configuration, free_cash_flows=cash_values)
    service = SecIssuerFundamentalAnnualHorizonService(_Storage(observations), configuration)

    result = service.query(_request(_DATES[-1], metric_keys=(_FCF,), horizons=(10,)))

    row = result.series[0].horizons[0]
    assert row.status == "complete"
    assert row.cagr_reason == "calculated"
    assert row.statistics is not None
    assert row.statistics.compound_annual_growth_rate == Decimal("0")


def test_revision_visibility_follows_known_at_and_changes_the_anchor_value() -> None:
    configuration = aapl_sec_configuration()
    original_available_at = datetime(2026, 1, 10, tzinfo=UTC)
    revision_available_at = datetime(2026, 2, 10, tzinfo=UTC)
    observations = _four_metric_seed(
        configuration,
        known_available_at=original_available_at,
    )
    anchor = _DATES[-1]
    original = next(
        item
        for item in observations
        if item.field_name == "fundamental.diluted_earnings_per_share"
        and item.period_end is not None
        and item.period_end.date() == anchor
    )
    revision = _observation(
        configuration,
        "fundamental.diluted_earnings_per_share",
        "9.25",
        anchor,
        available_at=revision_available_at,
        accession_number="0000320193-25-000099",
    )
    observations.append(revision)
    service = SecIssuerFundamentalAnnualHorizonService(_Storage(observations), configuration)

    before = service.query(
        _request(
            anchor,
            metric_keys=(_EPS,),
            horizons=(3,),
            known_at=datetime(2026, 1, 31, tzinfo=UTC),
        )
    )
    after = service.query(
        _request(
            anchor,
            metric_keys=(_EPS,),
            horizons=(3,),
            known_at=datetime(2026, 3, 1, tzinfo=UTC),
        )
    )

    assert before.series[0].points[-1].value == original.value
    assert before.series[0].points[-1].input_observation_ids == (original.observation_id,)
    assert after.series[0].points[-1].value == Decimal("9.25")
    assert after.series[0].points[-1].input_observation_ids == (revision.observation_id,)


def test_equally_available_conflicting_revisions_fail_closed() -> None:
    configuration = aapl_sec_configuration()
    original_available_at = datetime(2026, 1, 10, tzinfo=UTC)
    observations = _four_metric_seed(
        configuration,
        known_available_at=original_available_at,
    )
    anchor = _DATES[-1]
    observations.append(
        _observation(
            configuration,
            "fundamental.diluted_earnings_per_share",
            "9.25",
            anchor,
            available_at=original_available_at,
            accession_number="0000320193-25-000098",
        )
    )
    service = SecIssuerFundamentalAnnualHorizonService(_Storage(observations), configuration)

    with pytest.raises(AmbiguousFundamentalResearchRevisionError):
        service.query(_request(anchor, metric_keys=(_EPS,), horizons=(3,)))


def test_generic_issuer_reuses_the_same_rules_without_aapl_fallback() -> None:
    configuration = _generic_configuration()
    observations = _four_metric_seed(configuration)
    service = SecIssuerFundamentalAnnualHorizonService(_Storage(observations), configuration)

    result = service.query(_request(_DATES[-1], asset_id=configuration.asset_id))

    assert result.asset_id == "equity:us:amd"
    assert result.source_id == configuration.companyfacts_source_id
    assert result.source_id != aapl_sec_configuration().companyfacts_source_id
    cash = _series(result, _FCF)
    assert {row.status for row in cash.horizons} == {"complete"}
    assert tuple(str(point.value) for point in cash.points) == tuple(
        str(80 + index * 9) for index in range(11)
    )


def test_sparse_generic_profile_reports_explicit_absences_without_approximation() -> None:
    configuration = _generic_configuration()
    observations: list[NormalizedObservation] = []
    for index, period_end in enumerate((date(2024, 12, 28), date(2025, 12, 27))):
        _seed_year(
            configuration,
            observations,
            period_end,
            diluted_eps=str(Decimal("1") + Decimal(index) / Decimal("10")),
        )
    service = SecIssuerFundamentalAnnualHorizonService(_Storage(observations), configuration)

    result = service.query(_request(_DATES[-1], asset_id=configuration.asset_id))

    assert tuple(series.definition.metric_key for series in result.series) == tuple(
        sorted((_FCF, _EPS, _ROA))
    )
    eps = _series(result, _EPS)
    assert len(eps.points) == 2
    assert {row.status for row in eps.horizons} == {"insufficient_history"}
    cash = _series(result, _FCF)
    assert cash.points == ()
    assert {row.status for row in cash.horizons} == {"anchor_unavailable"}
    roa = _series(result, _ROA)
    assert roa.points == ()
    assert {row.status for row in roa.horizons} == {"anchor_unavailable"}
    assert result.coverage.complete_horizons == 0
    assert result.coverage.incomplete_horizons == 9
    assert result.coverage.points_returned == 2
    assert "fundamental.research.free_cash_flow:missing_inputs" in (result.coverage.skipped_counts)
    assert "fundamental.research.return_on_assets_ending_balance:missing_inputs" in (
        result.coverage.skipped_counts
    )


def test_only_requested_metrics_and_fields_are_read_and_calculated() -> None:
    configuration = aapl_sec_configuration()
    observations = _four_metric_seed(configuration)
    storage = _Storage(observations)
    service = SecIssuerFundamentalAnnualHorizonService(storage, configuration)

    result = service.query(_request(_DATES[-1], metric_keys=(_FCF, _EPS)))

    requested_fields = {
        "fundamental.operating_cash_flow",
        "fundamental.capital_expenditures",
        "fundamental.diluted_earnings_per_share",
    }
    assert storage.observations.calls
    assert all(call["frequency"] is DataFrequency.ANNUAL for call in storage.observations.calls)
    assert all(call["field_names"] == requested_fields for call in storage.observations.calls)
    assert storage.observations.hydrated
    assert {item.field_name for item in storage.observations.hydrated} == requested_fields
    assert {item.asset_id for item in storage.observations.hydrated} == {"equity:us:aapl"}
    assert {item.source.source_id for item in storage.observations.hydrated} == {
        configuration.companyfacts_source_id
    }
    assert tuple(item.definition.metric_key for item in result.series) == (_EPS, _FCF)


def test_result_contract_rejects_tampered_absence_and_coverage() -> None:
    configuration = aapl_sec_configuration()
    observations = _four_metric_seed(configuration, dates=_DATES[-6:])
    service = SecIssuerFundamentalAnnualHorizonService(_Storage(observations), configuration)
    result = service.query(_request(_DATES[-1], metric_keys=(_FCF,)))
    payload = result.to_json_dict()

    payload["series"][0]["horizons"][2]["statistics"] = payload["series"][0]["horizons"][0][
        "statistics"
    ]
    with pytest.raises(ValidationError, match="incomplete annual horizon cannot publish"):
        FundamentalAnnualHorizonsResult.model_validate(payload)

    payload = result.to_json_dict()
    payload["coverage"]["complete_horizons"] = 1
    payload["coverage"]["incomplete_horizons"] = 2
    with pytest.raises(ValidationError, match="complete count differs from coverage"):
        FundamentalAnnualHorizonsResult.model_validate(payload)
