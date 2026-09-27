"""Tests for point-in-time selection of Apple fundamental metric revisions."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_analyst.core.models import DataFrequency, DataQuality, MetricResult
from investment_analyst.providers.fundamentals.sec_diagnostic_models import (
    SecFundamentalDiagnosticRequest,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_selection import (
    AmbiguousFundamentalMetricRevisionError,
    MalformedFundamentalMetricError,
    MissingFundamentalDiagnosticPeriodError,
    SecFundamentalDiagnosticSelector,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SecFundamentalMetricCandidate,
    SecFundamentalMetricInput,
    get_sec_fundamental_metric_definition,
)
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    sec_fundamental_metric_result_id,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_ROLES = {
    "fundamental.net_margin": ("current_net_income", "current_revenue"),
    "fundamental.liabilities_to_assets": ("current_assets", "current_liabilities"),
    "fundamental.liabilities_to_equity": ("current_equity", "current_liabilities"),
    "fundamental.revenue_yoy_growth": ("current_revenue", "previous_revenue"),
    "fundamental.net_income_yoy_change_rate": (
        "current_net_income",
        "previous_net_income",
    ),
}


def _result(
    metric_name: str,
    value: str,
    *,
    period_end: datetime | None = None,
    available_at: datetime | None = None,
    frequency: DataFrequency = DataFrequency.ANNUAL,
    input_ids: tuple | None = None,
) -> MetricResult:
    definition = get_sec_fundamental_metric_definition(metric_name)
    period_end = period_end or datetime(2025, 9, 27, tzinfo=UTC)
    available_at = available_at or datetime(2025, 10, 31, tzinfo=UTC)
    identifiers = input_ids or (uuid4(), uuid4())
    inputs = tuple(
        SecFundamentalMetricInput(role=role, observation_id=identifier)
        for role, identifier in zip(_ROLES[metric_name], identifiers, strict=True)
    )
    candidate = SecFundamentalMetricCandidate(
        asset_id="equity:us:aapl",
        metric_name=metric_name,
        value=Decimal(value),
        unit="ratio",
        frequency=frequency,
        period_end=period_end,
        available_at=available_at,
        input_roles=inputs,
        formula=definition.formula,
        algorithm_version=definition.algorithm_version,
        comparison=definition.comparison,
        fiscal_year="2025",
        fiscal_period="FY" if frequency is DataFrequency.ANNUAL else "Q1",
        quality=DataQuality.VALID,
    )
    return MetricResult(
        result_id=sec_fundamental_metric_result_id(candidate),
        asset_id=candidate.asset_id,
        metric_key=metric_name,
        value=candidate.value,
        unit="ratio",
        as_of=period_end,
        available_at=available_at,
        computed_at=max(datetime(2026, 1, 2, tzinfo=UTC), available_at),
        parameters={
            "source_id": "sec-edgar:aapl:companyfacts",
            "frequency": frequency.value,
            "period_end": period_end.isoformat(),
            "comparison": definition.comparison.value,
            "formula": definition.formula,
            "input_roles": [
                {"role": item.role, "observation_id": str(item.observation_id)} for item in inputs
            ],
            "fiscal_year": "2025",
            "fiscal_period": "FY" if frequency is DataFrequency.ANNUAL else "Q1",
        },
        input_observation_ids=list(candidate.input_observation_ids()),
        algorithm_version=definition.algorithm_version,
        quality=DataQuality.VALID,
    )


def _request(**overrides: object) -> SecFundamentalDiagnosticRequest:
    values: dict[str, object] = {
        "known_at": datetime(2026, 1, 1, tzinfo=UTC),
        "frequency": DataFrequency.ANNUAL,
    }
    values.update(overrides)
    return SecFundamentalDiagnosticRequest.model_validate(values)


def test_selector_reads_metric_results_once(tmp_path, monkeypatch) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.metric_results.save(_result("fundamental.net_margin", "0.20"))
        calls = 0
        seen_kwargs: list[dict] = []
        original = storage.metric_results.list

        def counting_list(**kwargs):
            nonlocal calls
            calls += 1
            seen_kwargs.append(dict(kwargs))
            return original(**kwargs)

        monkeypatch.setattr(storage.metric_results, "list", counting_list)
        selection = SecFundamentalDiagnosticSelector(storage).select(_request())

        assert calls == 1
        assert selection.target_period_end == datetime(2025, 9, 27, tzinfo=UTC)
        assert seen_kwargs[0].get("asset_id") == "equity:us:aapl"


def test_selector_reads_only_sec_metric_keys_with_pit_equivalence(tmp_path) -> None:
    from investment_analyst.providers.fundamentals.sec_diagnostic_selection import (
        _ALLOWED_METRIC_NAMES,
    )
    from investment_analyst.providers.fundamentals.sec_metric_models import (
        SEC_FUNDAMENTAL_METRIC_DEFINITIONS,
    )

    expected_keys = tuple(
        definition.metric_name for definition in SEC_FUNDAMENTAL_METRIC_DEFINITIONS
    )
    assert tuple(_ALLOWED_METRIC_NAMES) == expected_keys

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        sec_results = tuple(
            _result(name, "0.20")
            for name in ("fundamental.net_margin", "fundamental.liabilities_to_assets")
        )
        for result in sec_results:
            storage.metric_results.save(result)
        storage.metric_results.save(
            _result("fundamental.net_margin", "0.20").model_copy(
                update={
                    "metric_key": "market.history.simple_return_1d",
                    "parameters": {},
                    "algorithm_version": "other",
                }
            )
        )
        calls: list[dict] = []
        original = storage.metric_results.list

        def recording_list(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(dict(kwargs))
            return original(**kwargs)

        storage.metric_results.list = recording_list  # type: ignore[method-assign]
        selector = SecFundamentalDiagnosticSelector(storage)
        selection = selector.select(_request())
        reference = SecFundamentalDiagnosticSelector.select_from_results(
            _request(),
            tuple(original(asset_id="equity:us:aapl")),
        )

        assert len(calls) == 1
        assert calls[0].get("asset_id") == "equity:us:aapl"
        assert tuple(calls[0].get("metric_keys", ())) == expected_keys
        assert selection.selected_metrics == reference.selected_metrics
        assert selection.target_period_end == reference.target_period_end
        assert selection.missing_metric_names == reference.missing_metric_names
        assert selection.metrics_examined == len(sec_results)
        assert selection.metrics_eligible == len(sec_results)


def test_latest_period_and_exact_period_selection() -> None:
    older = _result(
        "fundamental.net_margin",
        "0.18",
        period_end=datetime(2024, 9, 28, tzinfo=UTC),
        available_at=datetime(2024, 11, 1, tzinfo=UTC),
    )
    latest = _result("fundamental.net_margin", "0.20")

    automatic = SecFundamentalDiagnosticSelector.select_from_results(
        _request(),
        (older, latest),
    )
    exact = SecFundamentalDiagnosticSelector.select_from_results(
        _request(as_of_period_end=datetime(2024, 9, 28, tzinfo=UTC).date()),
        (older, latest),
    )

    assert automatic.selected_metrics[0].result_id == latest.result_id
    assert exact.selected_metrics[0].result_id == older.result_id


def test_missing_exact_period_is_not_silently_replaced() -> None:
    with pytest.raises(MissingFundamentalDiagnosticPeriodError):
        SecFundamentalDiagnosticSelector.select_from_results(
            _request(as_of_period_end=datetime(2023, 9, 30, tzinfo=UTC).date()),
            (_result("fundamental.net_margin", "0.20"),),
        )


def test_future_revision_is_excluded_and_current_revision_selected() -> None:
    current = _result("fundamental.net_margin", "0.20")
    future = _result(
        "fundamental.net_margin",
        "0.22",
        available_at=datetime(2026, 2, 1, tzinfo=UTC),
    )

    selection = SecFundamentalDiagnosticSelector.select_from_results(
        _request(),
        (current, future),
    )

    assert selection.selected_metrics[0].result_id == current.result_id


def test_later_revision_supersedes_earlier_revision() -> None:
    earlier = _result(
        "fundamental.net_margin",
        "0.18",
        available_at=datetime(2025, 10, 1, tzinfo=UTC),
    )
    later = _result(
        "fundamental.net_margin",
        "0.20",
        available_at=datetime(2025, 10, 31, tzinfo=UTC),
    )

    selection = SecFundamentalDiagnosticSelector.select_from_results(
        _request(),
        (earlier, later),
    )

    assert selection.selected_metrics[0].result_id == later.result_id
    assert selection.revisions_superseded == 1


def test_identical_duplicate_collapses_but_conflicting_tie_is_rejected() -> None:
    metric = _result("fundamental.net_margin", "0.20")
    collapsed = SecFundamentalDiagnosticSelector.select_from_results(
        _request(),
        (metric, metric),
    )
    conflicting = _result("fundamental.net_margin", "0.21")

    assert len(collapsed.selected_metrics) == 1
    with pytest.raises(AmbiguousFundamentalMetricRevisionError):
        SecFundamentalDiagnosticSelector.select_from_results(
            _request(),
            (metric, conflicting),
        )


def test_market_metrics_are_ignored_before_fundamental_validation() -> None:
    market = MetricResult(
        asset_id="equity:us:aapl",
        metric_key="market.history.simple_return_1d",
        value=Decimal("0.01"),
        unit="ratio",
        as_of=datetime(2025, 9, 27, tzinfo=UTC),
        available_at=datetime(2025, 10, 31, tzinfo=UTC),
        computed_at=datetime(2026, 1, 2, tzinfo=UTC),
        parameters={},
        input_observation_ids=[uuid4()],
        algorithm_version="other",
        quality=DataQuality.VALID,
    )

    selection = SecFundamentalDiagnosticSelector.select_from_results(_request(), (market,))

    assert selection.selected_metrics == ()
    assert selection.target_period_end is None


def test_malformed_in_scope_metric_is_rejected() -> None:
    result = _result("fundamental.net_margin", "0.20").model_copy(update={"unit": "USD"})

    with pytest.raises(MalformedFundamentalMetricError):
        SecFundamentalDiagnosticSelector.select_from_results(_request(), (result,))
