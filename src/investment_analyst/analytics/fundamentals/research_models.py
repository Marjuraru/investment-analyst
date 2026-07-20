"""Strict contracts for point-in-time fundamental research metrics."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BeforeValidator, ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models import DataFrequency
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SOURCE_ID,
)

_ALLOWED_FREQUENCIES = frozenset({DataFrequency.ANNUAL, DataFrequency.QUARTERLY})


def _reject_financial_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("financial values must use Decimal, not float or bool")
    return value


FinancialDecimal = Annotated[Decimal, BeforeValidator(_reject_financial_float)]


class FundamentalResearchMetricField(ContractModel):
    """One named observation role required by a research metric."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: NonEmptyStr
    field_name: NonEmptyStr


class FundamentalResearchMetricDefinition(ContractModel):
    """Published formula and exact inputs for one descriptive metric."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_key: NonEmptyStr
    display_name_es: NonEmptyStr
    formula: NonEmptyStr
    unit: Literal["ratio", "USD"]
    input_fields: tuple[FundamentalResearchMetricField, ...]
    algorithm_version: NonEmptyStr
    limitations: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_definition(self) -> "FundamentalResearchMetricDefinition":
        """Require deterministic, unique analytical roles and source fields."""
        roles = tuple(item.role for item in self.input_fields)
        fields = tuple(item.field_name for item in self.input_fields)
        if not roles:
            raise ValueError("research metrics require at least one input field")
        if roles != tuple(sorted(roles)):
            raise ValueError("research metric roles must be ordered")
        if len(set(roles)) != len(roles):
            raise ValueError("research metric roles must be unique")
        if len(set(fields)) != len(fields):
            raise ValueError("research metric fields must be unique")
        return self


def _input(role: str, field_name: str) -> FundamentalResearchMetricField:
    return FundamentalResearchMetricField(role=role, field_name=field_name)


FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS = (
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.capex_to_operating_cash_flow",
        display_name_es="Capex / flujo operativo",
        formula="capital_expenditures / operating_cash_flow",
        unit="ratio",
        input_fields=(
            _input("capital_expenditures", "fundamental.capital_expenditures"),
            _input("operating_cash_flow", "fundamental.operating_cash_flow"),
        ),
        algorithm_version="fundamental-research-capex-to-ocf-v1-decimal34",
        limitations=("Solo se calcula cuando el flujo operativo es positivo.",),
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.cash_ratio",
        display_name_es="Cash ratio",
        formula="cash_and_cash_equivalents / current_liabilities",
        unit="ratio",
        input_fields=(
            _input("cash_and_cash_equivalents", "fundamental.cash_and_cash_equivalents"),
            _input("current_liabilities", "fundamental.current_liabilities"),
        ),
        algorithm_version="fundamental-research-cash-ratio-v1-decimal34",
        limitations=("No incluye valores negociables ni otras fuentes de liquidez.",),
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.current_ratio",
        display_name_es="Current ratio",
        formula="current_assets / current_liabilities",
        unit="ratio",
        input_fields=(
            _input("current_assets", "fundamental.current_assets"),
            _input("current_liabilities", "fundamental.current_liabilities"),
        ),
        algorithm_version="fundamental-research-current-ratio-v1-decimal34",
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.free_cash_flow",
        display_name_es="Flujo de caja libre",
        formula="operating_cash_flow - capital_expenditures",
        unit="USD",
        input_fields=(
            _input("capital_expenditures", "fundamental.capital_expenditures"),
            _input("operating_cash_flow", "fundamental.operating_cash_flow"),
        ),
        algorithm_version="fundamental-research-free-cash-flow-v1-decimal34",
        limitations=(
            "Es una aproximación contable; no equivale a owner earnings ni a capex "
            "de mantenimiento.",
        ),
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.free_cash_flow_margin",
        display_name_es="Margen de flujo de caja libre",
        formula="(operating_cash_flow - capital_expenditures) / revenue",
        unit="ratio",
        input_fields=(
            _input("capital_expenditures", "fundamental.capital_expenditures"),
            _input("operating_cash_flow", "fundamental.operating_cash_flow"),
            _input("revenue", "fundamental.revenue"),
        ),
        algorithm_version="fundamental-research-free-cash-flow-margin-v1-decimal34",
        limitations=(
            "Es una aproximación contable; no equivale a owner earnings ni a capex "
            "de mantenimiento.",
        ),
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.free_cash_flow_to_net_income",
        display_name_es="Flujo de caja libre / resultado neto",
        formula="(operating_cash_flow - capital_expenditures) / net_income",
        unit="ratio",
        input_fields=(
            _input("capital_expenditures", "fundamental.capital_expenditures"),
            _input("net_income", "fundamental.net_income"),
            _input("operating_cash_flow", "fundamental.operating_cash_flow"),
        ),
        algorithm_version="fundamental-research-fcf-to-net-income-v1-decimal34",
        limitations=(
            "Solo se calcula cuando el resultado neto es positivo.",
            "El flujo de caja libre es una aproximación contable y la conversión puede variar "
            "por capital de trabajo, timing y capex.",
        ),
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.gross_margin",
        display_name_es="Margen bruto",
        formula="gross_profit / revenue",
        unit="ratio",
        input_fields=(
            _input("gross_profit", "fundamental.gross_profit"),
            _input("revenue", "fundamental.revenue"),
        ),
        algorithm_version="fundamental-research-gross-margin-v1-decimal34",
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.net_liquid_assets",
        display_name_es="Activos líquidos netos de deuda a largo plazo",
        formula=(
            "cash_and_cash_equivalents + marketable_securities_current + "
            "marketable_securities_noncurrent - long_term_debt_current - "
            "long_term_debt_noncurrent"
        ),
        unit="USD",
        input_fields=(
            _input("cash_and_cash_equivalents", "fundamental.cash_and_cash_equivalents"),
            _input("long_term_debt_current", "fundamental.long_term_debt_current"),
            _input("long_term_debt_noncurrent", "fundamental.long_term_debt_noncurrent"),
            _input(
                "marketable_securities_current",
                "fundamental.marketable_securities_current",
            ),
            _input(
                "marketable_securities_noncurrent",
                "fundamental.marketable_securities_noncurrent",
            ),
        ),
        algorithm_version="fundamental-research-net-liquid-assets-v1-decimal34",
        limitations=(
            "No es deuda neta total: excluye otros préstamos, pasivos y efectos fiscales.",
        ),
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.net_margin",
        display_name_es="Margen neto",
        formula="net_income / revenue",
        unit="ratio",
        input_fields=(
            _input("net_income", "fundamental.net_income"),
            _input("revenue", "fundamental.revenue"),
        ),
        algorithm_version="fundamental-research-net-margin-v1-decimal34",
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.operating_cash_flow_margin",
        display_name_es="Margen de flujo operativo",
        formula="operating_cash_flow / revenue",
        unit="ratio",
        input_fields=(
            _input("operating_cash_flow", "fundamental.operating_cash_flow"),
            _input("revenue", "fundamental.revenue"),
        ),
        algorithm_version="fundamental-research-ocf-margin-v1-decimal34",
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.operating_cash_flow_to_net_income",
        display_name_es="Flujo operativo / resultado neto",
        formula="operating_cash_flow / net_income",
        unit="ratio",
        input_fields=(
            _input("net_income", "fundamental.net_income"),
            _input("operating_cash_flow", "fundamental.operating_cash_flow"),
        ),
        algorithm_version="fundamental-research-ocf-to-net-income-v1-decimal34",
        limitations=(
            "Solo se calcula cuando el resultado neto es positivo.",
            "Describe conversión contable y no constituye una puntuación de calidad de beneficios.",
        ),
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.operating_margin",
        display_name_es="Margen operativo",
        formula="operating_income / revenue",
        unit="ratio",
        input_fields=(
            _input("operating_income", "fundamental.operating_income"),
            _input("revenue", "fundamental.revenue"),
        ),
        algorithm_version="fundamental-research-operating-margin-v1-decimal34",
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.research_and_development_to_revenue",
        display_name_es="R&D / ingresos",
        formula="research_and_development / revenue",
        unit="ratio",
        input_fields=(
            _input(
                "research_and_development",
                "fundamental.research_and_development",
            ),
            _input("revenue", "fundamental.revenue"),
        ),
        algorithm_version="fundamental-research-r-and-d-to-revenue-v1-decimal34",
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.selling_general_and_administrative_to_revenue",
        display_name_es="SG&A / ingresos",
        formula="selling_general_and_administrative / revenue",
        unit="ratio",
        input_fields=(
            _input("revenue", "fundamental.revenue"),
            _input(
                "selling_general_and_administrative",
                "fundamental.selling_general_and_administrative",
            ),
        ),
        algorithm_version="fundamental-research-sg-and-a-to-revenue-v1-decimal34",
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.share_based_compensation_to_revenue",
        display_name_es="Stock-based compensation / ingresos",
        formula="share_based_compensation / revenue",
        unit="ratio",
        input_fields=(
            _input("revenue", "fundamental.revenue"),
            _input("share_based_compensation", "fundamental.share_based_compensation"),
        ),
        algorithm_version="fundamental-research-sbc-to-revenue-v1-decimal34",
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.shareholder_distributions",
        display_name_es="Distribuciones al accionista",
        formula="dividends_paid + share_repurchases",
        unit="USD",
        input_fields=(
            _input("dividends_paid", "fundamental.dividends_paid"),
            _input("share_repurchases", "fundamental.share_repurchases"),
        ),
        algorithm_version="fundamental-research-shareholder-distributions-v1-decimal34",
        limitations=(
            "Suma salidas de caja declaradas y no mide por sí sola creación de valor por acción.",
        ),
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.shareholder_distributions_to_free_cash_flow",
        display_name_es="Distribuciones / flujo de caja libre",
        formula=(
            "(dividends_paid + share_repurchases) / (operating_cash_flow - capital_expenditures)"
        ),
        unit="ratio",
        input_fields=(
            _input("capital_expenditures", "fundamental.capital_expenditures"),
            _input("dividends_paid", "fundamental.dividends_paid"),
            _input("operating_cash_flow", "fundamental.operating_cash_flow"),
            _input("share_repurchases", "fundamental.share_repurchases"),
        ),
        algorithm_version="fundamental-research-distributions-to-fcf-v1-decimal34",
        limitations=("Solo se calcula cuando el flujo de caja libre es positivo.",),
    ),
    FundamentalResearchMetricDefinition(
        metric_key="fundamental.research.working_capital",
        display_name_es="Capital de trabajo",
        formula="current_assets - current_liabilities",
        unit="USD",
        input_fields=(
            _input("current_assets", "fundamental.current_assets"),
            _input("current_liabilities", "fundamental.current_liabilities"),
        ),
        algorithm_version="fundamental-research-working-capital-v1-decimal34",
    ),
)

FUNDAMENTAL_RESEARCH_METRIC_COUNT = len(FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS)

_DEFINITION_BY_KEY = {
    definition.metric_key: definition for definition in FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS
}

FUNDAMENTAL_RESEARCH_LIMITATIONS = (
    "Los hechos provienen de SEC EDGAR Company Facts y se seleccionan point-in-time.",
    "Los flujos trimestrales son periodos discretos; no se calculan TTM ni trimestres "
    "desde acumulados.",
    "Las métricas son descriptivas y no producen recomendación, ranking ni puntuación agregada.",
    "No se incluyen valoración, estimaciones, comparables ni ajustes sectoriales en esta versión.",
)


class AaplFundamentalResearchRequest(ContractModel):
    """Bounded point-in-time request for Apple research metrics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    known_at: UTCDateTime
    frequency: DataFrequency
    start_period_end: date | None = None
    end_period_end: date | None = None
    limit: int | None = Field(default=None, ge=1, le=100)

    @field_validator("limit", mode="before")
    @classmethod
    def reject_boolean_limit(cls, value: object) -> object:
        """Reject booleans masquerading as integers."""
        if isinstance(value, bool):
            raise ValueError("limit must be an integer between 1 and 100")
        return value

    @model_validator(mode="after")
    def validate_request(self) -> "AaplFundamentalResearchRequest":
        """Keep frequency and inclusive reporting-period bounds explicit."""
        if self.frequency not in _ALLOWED_FREQUENCIES:
            raise ValueError("frequency must be annual or quarterly")
        if (
            self.start_period_end is not None
            and self.end_period_end is not None
            and self.start_period_end > self.end_period_end
        ):
            raise ValueError("start_period_end must not be later than end_period_end")
        return self


class FundamentalResearchMetricInput(ContractModel):
    """One exact SEC observation assigned to a formula role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: NonEmptyStr
    field_name: NonEmptyStr
    observation_id: UUID
    value: FinancialDecimal
    unit: Literal["USD"] = "USD"
    available_at: UTCDateTime

    @model_validator(mode="after")
    def validate_value(self) -> "FundamentalResearchMetricInput":
        """Reject non-finite financial evidence."""
        if not self.value.is_finite():
            raise ValueError("research metric input value must be finite")
        return self


class FundamentalResearchMetricValue(ContractModel):
    """One deterministic calculated value with complete input evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_key: NonEmptyStr
    display_name_es: NonEmptyStr
    value: FinancialDecimal
    unit: Literal["ratio", "USD"]
    frequency: DataFrequency
    period_end: UTCDateTime
    available_at: UTCDateTime
    formula: NonEmptyStr
    algorithm_version: NonEmptyStr
    inputs: tuple[FundamentalResearchMetricInput, ...]
    limitations: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_metric(self) -> "FundamentalResearchMetricValue":
        """Match the published definition and deterministic evidence order."""
        definition = _DEFINITION_BY_KEY.get(self.metric_key)
        if definition is None:
            raise ValueError("metric_key is not a fundamental research metric")
        if (
            self.display_name_es != definition.display_name_es
            or self.unit != definition.unit
            or self.formula != definition.formula
            or self.algorithm_version != definition.algorithm_version
            or self.limitations != definition.limitations
        ):
            raise ValueError("research metric does not match its published definition")
        if self.frequency not in _ALLOWED_FREQUENCIES:
            raise ValueError("research metric frequency must be annual or quarterly")
        if not self.value.is_finite():
            raise ValueError("research metric value must be finite")
        roles = tuple(item.role for item in self.inputs)
        fields = tuple(item.field_name for item in self.inputs)
        identifiers = tuple(item.observation_id for item in self.inputs)
        if roles != tuple(sorted(roles)):
            raise ValueError("research metric inputs must be ordered by role")
        if len(set(roles)) != len(roles) or len(set(identifiers)) != len(identifiers):
            raise ValueError("research metric inputs must be unique")
        if roles != tuple(item.role for item in definition.input_fields):
            raise ValueError("research metric input roles do not match its definition")
        if fields != tuple(item.field_name for item in definition.input_fields):
            raise ValueError("research metric input fields do not match its definition")
        if self.available_at != max(item.available_at for item in self.inputs):
            raise ValueError("research metric availability must match its latest input")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return JSON-compatible exact Decimal strings."""
        return {
            "metric_key": self.metric_key,
            "display_name_es": self.display_name_es,
            "value": str(self.value),
            "unit": self.unit,
            "frequency": self.frequency.value,
            "period_end": self.period_end.isoformat(),
            "available_at": self.available_at.isoformat(),
            "formula": self.formula,
            "algorithm_version": self.algorithm_version,
            "inputs": [
                {
                    **item.model_dump(mode="json"),
                    "value": str(item.value),
                }
                for item in self.inputs
            ],
            "limitations": list(self.limitations),
        }


class AaplFundamentalResearchPeriod(ContractModel):
    """Calculated research metrics for one reporting period."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_end: UTCDateTime
    frequency: DataFrequency
    metrics: tuple[FundamentalResearchMetricValue, ...]

    @model_validator(mode="after")
    def validate_period(self) -> "AaplFundamentalResearchPeriod":
        """Require useful, ordered, same-period metric output."""
        if not self.metrics:
            raise ValueError("research periods must contain at least one metric")
        keys = tuple(item.metric_key for item in self.metrics)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("research period metrics must be ordered and unique")
        if any(item.frequency is not self.frequency for item in self.metrics):
            raise ValueError("research metric frequency does not match its period")
        if any(item.period_end != self.period_end for item in self.metrics):
            raise ValueError("research metric period_end does not match its period")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return one exact period representation."""
        return {
            "period_end": self.period_end.isoformat(),
            "frequency": self.frequency.value,
            "metrics": [item.to_json_dict() for item in self.metrics],
        }


class AaplFundamentalResearchCoverage(ContractModel):
    """Selection and computation counts for one research query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observations_examined: int = Field(ge=0)
    observations_eligible: int = Field(ge=0)
    observations_selected: int = Field(ge=0)
    observations_superseded: int = Field(ge=0)
    source_periods: int = Field(ge=0)
    output_periods: int = Field(ge=0)
    metrics_returned: int = Field(ge=0)
    metric_counts: dict[NonEmptyStr, int]
    skipped_counts: dict[NonEmptyStr, int]
    earliest_period_end: UTCDateTime | None = None
    latest_period_end: UTCDateTime | None = None

    @model_validator(mode="after")
    def validate_coverage(self) -> "AaplFundamentalResearchCoverage":
        """Keep counts and optional period bounds internally consistent."""
        if sum(self.metric_counts.values()) != self.metrics_returned:
            raise ValueError("metric_counts must equal metrics_returned")
        if any(value < 0 for value in self.skipped_counts.values()):
            raise ValueError("skipped counts must be non-negative")
        if self.output_periods == 0:
            if self.earliest_period_end is not None or self.latest_period_end is not None:
                raise ValueError("empty research output must not define period bounds")
        elif self.earliest_period_end is None or self.latest_period_end is None:
            raise ValueError("research output requires both period bounds")
        elif self.earliest_period_end > self.latest_period_end:
            raise ValueError("research period bounds are reversed")
        return self


class AaplFundamentalResearchResult(ContractModel):
    """Versioned point-in-time analytical result for Apple fundamentals."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["aapl-fundamental-research-v1"] = "aapl-fundamental-research-v1"
    asset_id: Literal["equity:us:aapl"] = ASSET_ID
    source_id: Literal["sec-edgar:aapl:companyfacts"] = COMPANYFACTS_SOURCE_ID
    request: AaplFundamentalResearchRequest
    definitions: tuple[FundamentalResearchMetricDefinition, ...] = (
        FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS
    )
    periods: tuple[AaplFundamentalResearchPeriod, ...]
    coverage: AaplFundamentalResearchCoverage
    traceability_verified: Literal[True] = True
    limitations: tuple[NonEmptyStr, ...] = FUNDAMENTAL_RESEARCH_LIMITATIONS

    @model_validator(mode="after")
    def validate_result(self) -> "AaplFundamentalResearchResult":
        """Validate ordering, point-in-time safety, and versioned contracts."""
        if self.definitions != FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS:
            raise ValueError("research definitions must preserve the versioned contract")
        if self.limitations != FUNDAMENTAL_RESEARCH_LIMITATIONS:
            raise ValueError("research limitations must preserve the versioned contract")
        if len(self.periods) != self.coverage.output_periods:
            raise ValueError("research period count must match coverage")
        ends = tuple(item.period_end for item in self.periods)
        if ends != tuple(sorted(ends)) or len(ends) != len(set(ends)):
            raise ValueError("research periods must be ordered and unique")
        metrics = sum(len(item.metrics) for item in self.periods)
        if metrics != self.coverage.metrics_returned:
            raise ValueError("research metric count must match coverage")
        for period in self.periods:
            if period.frequency is not self.request.frequency:
                raise ValueError("research period frequency does not match request")
            for metric in period.metrics:
                if metric.available_at > self.request.known_at:
                    raise ValueError("research metric uses evidence unavailable at known_at")
        if ends:
            if self.coverage.earliest_period_end != ends[0]:
                raise ValueError("research earliest period does not match coverage")
            if self.coverage.latest_period_end != ends[-1]:
                raise ValueError("research latest period does not match coverage")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return a compact JSON-compatible analytical contract."""
        return {
            "schema_version": self.schema_version,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "request": self.request.model_dump(mode="json"),
            "definitions": [item.model_dump(mode="json") for item in self.definitions],
            "periods": [item.to_json_dict() for item in self.periods],
            "coverage": self.coverage.model_dump(mode="json"),
            "traceability_verified": self.traceability_verified,
            "limitations": list(self.limitations),
        }


def get_fundamental_research_metric_definition(
    metric_key: str,
) -> FundamentalResearchMetricDefinition:
    """Return one exact research definition or reject unknown metrics."""
    try:
        return _DEFINITION_BY_KEY[metric_key]
    except KeyError as error:
        raise ValueError(f"unsupported fundamental research metric: {metric_key}") from error


__all__ = [
    "AaplFundamentalResearchCoverage",
    "AaplFundamentalResearchPeriod",
    "AaplFundamentalResearchRequest",
    "AaplFundamentalResearchResult",
    "FUNDAMENTAL_RESEARCH_LIMITATIONS",
    "FUNDAMENTAL_RESEARCH_METRIC_COUNT",
    "FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS",
    "FinancialDecimal",
    "FundamentalResearchMetricDefinition",
    "FundamentalResearchMetricField",
    "FundamentalResearchMetricInput",
    "FundamentalResearchMetricValue",
    "get_fundamental_research_metric_definition",
]
