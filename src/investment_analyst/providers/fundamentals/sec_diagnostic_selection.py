"""Point-in-time selection of persisted Apple fundamental metric revisions."""

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from investment_analyst.core.models import DataFrequency, DataQuality, MetricResult
from investment_analyst.providers.fundamentals.sec_diagnostic_models import (
    SecFundamentalDiagnosticInput,
    SecFundamentalDiagnosticMetric,
    SecFundamentalDiagnosticRequest,
    SecFundamentalDiagnosticSelection,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SOURCE_ID,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SEC_FUNDAMENTAL_METRIC_DEFINITIONS,
    SecFundamentalMetricCandidate,
    SecFundamentalMetricInput,
    get_sec_fundamental_metric_definition,
)
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    sec_fundamental_metric_result_id,
)
from investment_analyst.storage import LocalStorage

_ALLOWED_METRIC_NAMES = tuple(
    definition.metric_name for definition in SEC_FUNDAMENTAL_METRIC_DEFINITIONS
)
_ALLOWED_METRIC_SET = frozenset(_ALLOWED_METRIC_NAMES)
_EXPECTED_ROLES = {
    "fundamental.net_margin": ("current_net_income", "current_revenue"),
    "fundamental.liabilities_to_assets": ("current_assets", "current_liabilities"),
    "fundamental.liabilities_to_equity": ("current_equity", "current_liabilities"),
    "fundamental.revenue_yoy_growth": ("current_revenue", "previous_revenue"),
    "fundamental.net_income_yoy_change_rate": (
        "current_net_income",
        "previous_net_income",
    ),
}


class SecFundamentalDiagnosticSelectionError(RuntimeError):
    """Base error for point-in-time fundamental metric selection."""


class MalformedFundamentalMetricError(SecFundamentalDiagnosticSelectionError):
    """Raised when an in-scope persisted metric violates its published contract."""


class AmbiguousFundamentalMetricRevisionError(SecFundamentalDiagnosticSelectionError):
    """Raised when tied revisions differ semantically."""


class MissingFundamentalDiagnosticPeriodError(SecFundamentalDiagnosticSelectionError):
    """Raised when an explicitly requested reporting period has no eligible metrics."""


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MalformedFundamentalMetricError(f"{name} must include timezone information")
    return value.astimezone(UTC)


def _text_parameter(result: MetricResult, name: str) -> str:
    value = result.parameters.get(name)
    if not isinstance(value, str) or not value.strip():
        raise MalformedFundamentalMetricError(
            f"metric result {result.result_id} has an invalid {name} parameter"
        )
    return value.strip()


def _optional_text_parameter(result: MetricResult, name: str) -> str | None:
    value = result.parameters.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MalformedFundamentalMetricError(
            f"metric result {result.result_id} has an invalid {name} parameter"
        )
    return value.strip()


def _datetime_parameter(result: MetricResult, name: str) -> datetime:
    value = _text_parameter(result, name)
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise MalformedFundamentalMetricError(
            f"metric result {result.result_id} has an invalid {name} parameter"
        ) from error
    return _utc(parsed, f"metric parameter {name}")


def _input_roles(result: MetricResult) -> tuple[SecFundamentalDiagnosticInput, ...]:
    raw_roles = result.parameters.get("input_roles")
    if not isinstance(raw_roles, list):
        raise MalformedFundamentalMetricError(
            f"metric result {result.result_id} has invalid input_roles"
        )
    roles: list[SecFundamentalDiagnosticInput] = []
    for item in raw_roles:
        if not isinstance(item, dict):
            raise MalformedFundamentalMetricError(
                f"metric result {result.result_id} has malformed input role entries"
            )
        if set(item) != {"role", "observation_id"}:
            raise MalformedFundamentalMetricError(
                f"metric result {result.result_id} has unexpected input role fields"
            )
        role = item.get("role")
        identifier = item.get("observation_id")
        if not isinstance(role, str) or not role.strip() or not isinstance(identifier, str):
            raise MalformedFundamentalMetricError(
                f"metric result {result.result_id} has malformed input role values"
            )
        try:
            observation_id = UUID(identifier)
        except ValueError as error:
            raise MalformedFundamentalMetricError(
                f"metric result {result.result_id} has an invalid observation ID"
            ) from error
        roles.append(
            SecFundamentalDiagnosticInput(
                role=role.strip(),
                observation_id=observation_id,
            )
        )
    return tuple(roles)


def _metric_frequency(result: MetricResult) -> DataFrequency:
    value = _text_parameter(result, "frequency")
    try:
        frequency = DataFrequency(value)
    except ValueError as error:
        raise MalformedFundamentalMetricError(
            f"metric result {result.result_id} has an unsupported frequency"
        ) from error
    if frequency not in {DataFrequency.ANNUAL, DataFrequency.QUARTERLY}:
        raise MalformedFundamentalMetricError(
            f"metric result {result.result_id} is not annual or quarterly"
        )
    return frequency


def _semantic_identity(metric: SecFundamentalDiagnosticMetric) -> tuple[object, ...]:
    return (
        metric.metric_name,
        metric.value,
        metric.unit,
        metric.frequency,
        metric.period_start,
        metric.period_end,
        metric.available_at,
        metric.formula,
        metric.algorithm_version,
        metric.input_observation_ids,
        tuple((item.role, item.observation_id) for item in metric.input_roles),
        metric.quality,
    )


def _validate_metric(result: MetricResult) -> SecFundamentalDiagnosticMetric:
    definition = get_sec_fundamental_metric_definition(result.metric_key)
    if result.asset_id != ASSET_ID:
        raise MalformedFundamentalMetricError("fundamental metric belongs to another asset")
    if result.unit != definition.unit or result.unit != "ratio":
        raise MalformedFundamentalMetricError("fundamental metric unit is not ratio")
    if result.algorithm_version != definition.algorithm_version:
        raise MalformedFundamentalMetricError("fundamental metric algorithm version is invalid")
    if result.quality is not DataQuality.VALID:
        raise MalformedFundamentalMetricError("fundamental metric quality must be VALID")
    if not isinstance(result.value, Decimal) or not result.value.is_finite():
        raise MalformedFundamentalMetricError("fundamental metric value must be finite Decimal")
    if result.as_of.tzinfo is not UTC:
        raise MalformedFundamentalMetricError("fundamental metric as_of must be UTC")
    if result.available_at.tzinfo is not UTC or result.computed_at.tzinfo is not UTC:
        raise MalformedFundamentalMetricError("fundamental metric timestamps must be UTC")

    source_id = _text_parameter(result, "source_id")
    if source_id != COMPANYFACTS_SOURCE_ID:
        raise MalformedFundamentalMetricError("fundamental metric source is not Company Facts")
    frequency = _metric_frequency(result)
    period_end = _datetime_parameter(result, "period_end")
    if period_end != result.as_of:
        raise MalformedFundamentalMetricError("metric period_end does not match as_of")
    formula = _text_parameter(result, "formula")
    if formula != definition.formula:
        raise MalformedFundamentalMetricError("fundamental metric formula is invalid")
    comparison = _text_parameter(result, "comparison")
    if comparison != definition.comparison.value:
        raise MalformedFundamentalMetricError("fundamental metric comparison is invalid")

    roles = _input_roles(result)
    expected_roles = _EXPECTED_ROLES[result.metric_key]
    if tuple(item.role for item in roles) != expected_roles:
        raise MalformedFundamentalMetricError("fundamental metric input roles are invalid")
    role_ids = tuple(item.observation_id for item in roles)
    if role_ids != tuple(result.input_observation_ids):
        raise MalformedFundamentalMetricError(
            "fundamental metric input IDs do not match input role order"
        )
    if len(role_ids) != 2 or len(set(role_ids)) != 2:
        raise MalformedFundamentalMetricError("fundamental metric requires two distinct inputs")

    period_start_value = result.parameters.get("period_start")
    period_start = None
    if period_start_value is not None:
        if not isinstance(period_start_value, str):
            raise MalformedFundamentalMetricError("metric period_start parameter is invalid")
        normalized = (
            f"{period_start_value[:-1]}+00:00"
            if period_start_value.endswith(("Z", "z"))
            else period_start_value
        )
        try:
            period_start = _utc(datetime.fromisoformat(normalized), "metric period_start")
        except ValueError as error:
            raise MalformedFundamentalMetricError(
                "metric period_start parameter is invalid"
            ) from error

    diagnostic_metric = SecFundamentalDiagnosticMetric(
        result_id=result.result_id,
        metric_name=result.metric_key,
        value=result.value,
        unit=result.unit,
        frequency=frequency,
        period_start=period_start,
        period_end=period_end,
        available_at=result.available_at,
        computed_at=result.computed_at,
        formula=formula,
        algorithm_version=result.algorithm_version,
        input_observation_ids=tuple(result.input_observation_ids),
        input_roles=roles,
        quality=result.quality,
    )

    candidate = SecFundamentalMetricCandidate(
        asset_id=result.asset_id,
        metric_name=result.metric_key,
        value=result.value,
        unit=result.unit,
        frequency=frequency,
        period_end=period_end,
        available_at=result.available_at,
        input_roles=tuple(
            SecFundamentalMetricInput(
                role=item.role,
                observation_id=item.observation_id,
            )
            for item in roles
        ),
        formula=formula,
        algorithm_version=result.algorithm_version,
        comparison=definition.comparison,
        fiscal_year=_optional_text_parameter(result, "fiscal_year"),
        fiscal_period=_optional_text_parameter(result, "fiscal_period"),
        quality=result.quality,
    )
    if sec_fundamental_metric_result_id(candidate) != result.result_id:
        raise MalformedFundamentalMetricError(
            "fundamental metric result ID does not match its deterministic inputs"
        )
    return diagnostic_metric


class SecFundamentalDiagnosticSelector:
    """Select one unambiguous point-in-time fundamental metric period."""

    def __init__(self, storage: LocalStorage) -> None:
        storage.require_open()
        self._storage = storage

    def select(
        self,
        request: SecFundamentalDiagnosticRequest,
    ) -> SecFundamentalDiagnosticSelection:
        """Read persisted metrics once and select the requested current revisions."""
        self._storage.require_open()
        results = tuple(self._storage.metric_results.list(asset_id=request.asset_id))
        return self.select_from_results(request, results)

    @staticmethod
    def select_from_results(
        request: SecFundamentalDiagnosticRequest,
        results: Iterable[MetricResult],
    ) -> SecFundamentalDiagnosticSelection:
        """Select from an explicit collection without additional storage access."""
        result_tuple = tuple(results)
        eligible: list[SecFundamentalDiagnosticMetric] = []
        for result in result_tuple:
            if result.asset_id != request.asset_id:
                continue
            if result.metric_key not in _ALLOWED_METRIC_SET:
                continue
            if result.available_at > request.known_at:
                continue
            metric = _validate_metric(result)
            if metric.frequency is not request.frequency:
                continue
            eligible.append(metric)

        grouped: dict[
            tuple[str, DataFrequency, datetime],
            list[SecFundamentalDiagnosticMetric],
        ] = defaultdict(list)
        for metric in eligible:
            grouped[(metric.metric_name, metric.frequency, metric.period_end)].append(metric)

        selected_by_period: dict[datetime, list[SecFundamentalDiagnosticMetric]] = defaultdict(list)
        superseded = 0
        for group_key in sorted(grouped, key=lambda item: (item[2], item[0])):
            revisions = grouped[group_key]
            latest_available = max(item.available_at for item in revisions)
            latest = [item for item in revisions if item.available_at == latest_available]
            identities = {_semantic_identity(item) for item in latest}
            if len(identities) != 1:
                metric_name, _, period_end = group_key
                raise AmbiguousFundamentalMetricRevisionError(
                    "ambiguous revisions for "
                    f"{metric_name} at {period_end.isoformat()} and "
                    f"available_at {latest_available.isoformat()}"
                )
            chosen = latest[0]
            selected_by_period[chosen.period_end].append(chosen)
            superseded += len(revisions) - 1

        if request.as_of_period_end is not None:
            target_candidates = [
                period for period in selected_by_period if period.date() == request.as_of_period_end
            ]
            if not target_candidates:
                raise MissingFundamentalDiagnosticPeriodError(
                    "no eligible fundamental metrics exist for the requested period"
                )
            target_period = target_candidates[0]
        else:
            target_period = max(selected_by_period, default=None)

        selected = tuple(
            sorted(selected_by_period.get(target_period, []), key=lambda item: item.metric_name)
        )
        selected_names = {item.metric_name for item in selected}
        missing = tuple(name for name in _ALLOWED_METRIC_NAMES if name not in selected_names)
        return SecFundamentalDiagnosticSelection(
            request=request,
            target_period_end=target_period,
            selected_metrics=selected,
            missing_metric_names=missing,
            metrics_examined=len(result_tuple),
            metrics_eligible=len(eligible),
            revisions_superseded=superseded,
            traceability_verified=True,
        )
