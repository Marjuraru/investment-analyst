"""Strict contracts for one unified fundamental-analysis workspace."""

from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.analytics.fundamentals.research_history_models import (
    AaplFundamentalResearchHistoryResult,
)
from investment_analyst.analytics.fundamentals.research_models import (
    FUNDAMENTAL_RESEARCH_METRIC_COUNT,
    FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS,
    AaplFundamentalResearchRequest,
    get_fundamental_research_metric_definition,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SOURCE_ID,
)

FundamentalAnalysisSectionKey = Literal[
    "growth_and_per_share",
    "profitability",
    "returns_and_efficiency",
    "earnings_quality",
    "liquidity_and_balance",
    "debt_and_solvency",
    "cash_and_reinvestment",
    "capital_allocation",
]
CompanyCategoryKey = Literal[
    "slow_grower",
    "stalwart",
    "fast_grower",
    "cyclical",
    "turnaround",
    "asset_play",
]


class FundamentalAnalysisMetricReference(ContractModel):
    """Assign one published metric to exactly one analytical section."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_key: NonEmptyStr
    relevance_es: NonEmptyStr

    @model_validator(mode="after")
    def validate_metric_key(self) -> "FundamentalAnalysisMetricReference":
        """Reject references to unpublished calculations."""
        get_fundamental_research_metric_definition(self.metric_key)
        return self


class FundamentalAnalysisSectionDefinition(ContractModel):
    """Versioned analytical grouping without investor names or duplicated metrics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    section_key: FundamentalAnalysisSectionKey
    display_name_es: NonEmptyStr
    scope_es: NonEmptyStr
    metric_references: tuple[FundamentalAnalysisMetricReference, ...]
    algorithm_version: NonEmptyStr

    @model_validator(mode="after")
    def validate_definition(self) -> "FundamentalAnalysisSectionDefinition":
        """Require useful and unique metric references inside one section."""
        keys = tuple(item.metric_key for item in self.metric_references)
        if not keys:
            raise ValueError("fundamental analysis sections require metric references")
        if len(keys) != len(set(keys)):
            raise ValueError("fundamental analysis section metrics must be unique")
        return self


def _reference(metric_key: str, relevance_es: str) -> FundamentalAnalysisMetricReference:
    return FundamentalAnalysisMetricReference(
        metric_key=metric_key,
        relevance_es=relevance_es,
    )


FUNDAMENTAL_ANALYSIS_SECTION_DEFINITIONS = (
    FundamentalAnalysisSectionDefinition(
        section_key="growth_and_per_share",
        display_name_es="Crecimiento y por acción",
        scope_es=(
            "Beneficio, ingresos, caja y base accionaria medidos por acción cuando es posible."
        ),
        metric_references=(
            _reference(
                "fundamental.research.diluted_eps",
                "Muestra el beneficio diluido declarado por cada acción promedio.",
            ),
            _reference(
                "fundamental.research.revenue_per_diluted_share",
                "Distingue el crecimiento de ingresos total del crecimiento por acción.",
            ),
            _reference(
                "fundamental.research.free_cash_flow_per_diluted_share",
                "Relaciona la caja libre contable con la base accionaria diluida.",
            ),
            _reference(
                "fundamental.research.diluted_shares",
                "Permite seguir la dilución promedio que afecta al beneficio por acción.",
            ),
            _reference(
                "fundamental.research.shares_outstanding",
                "Hace visible la evolución de acciones efectivamente en circulación al cierre.",
            ),
        ),
        algorithm_version="fundamental-analysis-growth-per-share-v1",
    ),
    FundamentalAnalysisSectionDefinition(
        section_key="profitability",
        display_name_es="Rentabilidad",
        scope_es="Márgenes contables y conversión de ingresos en caja operativa y libre.",
        metric_references=(
            _reference(
                "fundamental.research.gross_margin",
                "Rentabilidad después del coste directo de ventas.",
            ),
            _reference(
                "fundamental.research.operating_margin",
                "Resultado operativo retenido por cada unidad de ingreso.",
            ),
            _reference(
                "fundamental.research.net_margin",
                "Resultado final retenido por cada unidad de ingreso.",
            ),
            _reference(
                "fundamental.research.operating_cash_flow_margin",
                "Caja operativa generada en relación con los ingresos.",
            ),
            _reference(
                "fundamental.research.free_cash_flow_margin",
                "Caja operativa residual después del capex total reportado.",
            ),
        ),
        algorithm_version="fundamental-analysis-profitability-v1",
    ),
    FundamentalAnalysisSectionDefinition(
        section_key="returns_and_efficiency",
        display_name_es="Retornos y eficiencia",
        scope_es=("Uso de activos y capital, con fórmulas explícitas basadas en saldos al cierre."),
        metric_references=(
            _reference(
                "fundamental.research.return_on_invested_capital_ending_balance",
                "Aproxima el retorno operativo después de impuestos sobre capital invertido.",
            ),
            _reference(
                "fundamental.research.return_on_equity_ending_balance",
                "Relaciona el resultado neto con el patrimonio del cierre.",
            ),
            _reference(
                "fundamental.research.return_on_assets_ending_balance",
                "Relaciona el resultado neto con los activos del cierre.",
            ),
            _reference(
                "fundamental.research.asset_turnover",
                "Mide cuántos ingresos produce cada unidad de activos al cierre.",
            ),
            _reference(
                "fundamental.research.fixed_asset_turnover",
                "Expone la intensidad de uso del PP&E neto declarado.",
            ),
            _reference(
                "fundamental.research.effective_tax_rate",
                "Hace visible la carga fiscal efectiva usada también en el NOPAT aproximado.",
            ),
        ),
        algorithm_version="fundamental-analysis-returns-efficiency-v1",
    ),
    FundamentalAnalysisSectionDefinition(
        section_key="earnings_quality",
        display_name_es="Calidad del beneficio",
        scope_es="Relación entre resultado contable y generación de caja declarada.",
        metric_references=(
            _reference(
                "fundamental.research.operating_cash_flow_to_net_income",
                "Contrasta el beneficio neto con la caja operativa.",
            ),
            _reference(
                "fundamental.research.free_cash_flow_to_net_income",
                "Añade capex total al contraste entre caja y beneficio.",
            ),
        ),
        algorithm_version="fundamental-analysis-earnings-quality-v1",
    ),
    FundamentalAnalysisSectionDefinition(
        section_key="liquidity_and_balance",
        display_name_es="Liquidez y balance",
        scope_es="Cobertura corriente, capital de trabajo y liquidez neta reportada.",
        metric_references=(
            _reference(
                "fundamental.research.current_ratio",
                "Relaciona activos y pasivos corrientes del mismo cierre.",
            ),
            _reference(
                "fundamental.research.cash_ratio",
                "Aísla la cobertura inmediata basada en efectivo.",
            ),
            _reference(
                "fundamental.research.working_capital",
                "Expone el excedente o déficit corriente en valor absoluto.",
            ),
            _reference(
                "fundamental.research.net_liquid_assets",
                "Contrasta liquidez ampliada con deuda a largo plazo reportada.",
            ),
        ),
        algorithm_version="fundamental-analysis-liquidity-balance-v1",
    ),
    FundamentalAnalysisSectionDefinition(
        section_key="debt_and_solvency",
        display_name_es="Deuda y solvencia",
        scope_es=(
            "Deuda financiera, vencimientos corrientes, cobertura y arrendamientos declarados."
        ),
        metric_references=(
            _reference(
                "fundamental.research.financial_debt",
                "Suma commercial paper y deuda a largo plazo corriente y no corriente.",
            ),
            _reference(
                "fundamental.research.net_debt",
                "Resta efectivo y valores negociables a la deuda financiera.",
            ),
            _reference(
                "fundamental.research.current_financial_debt",
                "Expone obligaciones financieras clasificadas como corrientes.",
            ),
            _reference(
                "fundamental.research.current_financial_debt_share",
                "Muestra qué parte de la deuda financiera tiene vencimiento corriente.",
            ),
            _reference(
                "fundamental.research.financial_debt_to_assets",
                "Dimensiona deuda financiera frente a activos totales.",
            ),
            _reference(
                "fundamental.research.financial_debt_to_equity",
                "Contrasta deuda financiera con patrimonio contable.",
            ),
            _reference(
                "fundamental.research.financial_debt_to_free_cash_flow",
                "Expresa deuda financiera en años equivalentes de FCF contable.",
            ),
            _reference(
                "fundamental.research.net_debt_to_free_cash_flow",
                "Ajusta la deuda por liquidez antes de compararla con el FCF.",
            ),
            _reference(
                "fundamental.research.interest_coverage",
                "Contrasta resultado operativo con gasto por intereses cuando está declarado.",
            ),
            _reference(
                "fundamental.research.lease_liabilities",
                "Mantiene visibles las obligaciones de arrendamiento fuera de la deuda financiera.",
            ),
            _reference(
                "fundamental.research.total_financial_obligations",
                "Suma deuda financiera y arrendamientos sin incluir pasivos operativos.",
            ),
        ),
        algorithm_version="fundamental-analysis-debt-solvency-v1",
    ),
    FundamentalAnalysisSectionDefinition(
        section_key="cash_and_reinvestment",
        display_name_es="Caja y reinversión",
        scope_es="Caja libre contable, intensidad de capex y gastos para sostener el negocio.",
        metric_references=(
            _reference(
                "fundamental.research.free_cash_flow",
                "Aproxima caja residual sin confundirla con owner earnings.",
            ),
            _reference(
                "fundamental.research.capex_to_operating_cash_flow",
                "Dimensiona el capex total frente a la caja operativa.",
            ),
            _reference(
                "fundamental.research.research_and_development_to_revenue",
                "Expone la intensidad contable de investigación y desarrollo.",
            ),
            _reference(
                "fundamental.research.selling_general_and_administrative_to_revenue",
                "Permite seguir el peso relativo de SG&A.",
            ),
            _reference(
                "fundamental.research.share_based_compensation_to_revenue",
                "Hace visible la compensación en acciones frente a ingresos.",
            ),
        ),
        algorithm_version="fundamental-analysis-cash-reinvestment-v1",
    ),
    FundamentalAnalysisSectionDefinition(
        section_key="capital_allocation",
        display_name_es="Asignación de capital",
        scope_es="Dividendos y recompras frente a la caja libre contable disponible.",
        metric_references=(
            _reference(
                "fundamental.research.shareholder_distributions",
                "Cuantifica salidas declaradas por dividendos y recompras.",
            ),
            _reference(
                "fundamental.research.shareholder_distributions_to_free_cash_flow",
                "Relaciona distribuciones con la caja libre contable del período.",
            ),
        ),
        algorithm_version="fundamental-analysis-capital-allocation-v1",
    ),
)


class CompanyCategoryDefinition(ContractModel):
    """One visible company-profile category and its analytical meaning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category_key: CompanyCategoryKey
    display_name_es: NonEmptyStr
    description_es: NonEmptyStr


COMPANY_CATEGORY_DEFINITIONS = (
    CompanyCategoryDefinition(
        category_key="slow_grower",
        display_name_es="Crecimiento lento",
        description_es="Negocio maduro cuyo crecimiento suele acompañar a la economía.",
    ),
    CompanyCategoryDefinition(
        category_key="stalwart",
        display_name_es="Empresa estable",
        description_es="Empresa grande con crecimiento moderado y operación consolidada.",
    ),
    CompanyCategoryDefinition(
        category_key="fast_grower",
        display_name_es="Crecimiento rápido",
        description_es=(
            "Empresa con expansión elevada que debe financiar y sostener su crecimiento."
        ),
    ),
    CompanyCategoryDefinition(
        category_key="cyclical",
        display_name_es="Cíclica",
        description_es="Resultados condicionados de forma material por el ciclo económico.",
    ),
    CompanyCategoryDefinition(
        category_key="turnaround",
        display_name_es="Recuperación",
        description_es="Negocio en deterioro o reestructuración cuya continuidad debe evaluarse.",
    ),
    CompanyCategoryDefinition(
        category_key="asset_play",
        display_name_es="Activo oculto",
        description_es=(
            "La tesis depende de activos cuyo valor no aparece claramente en resultados."
        ),
    ),
)

COMPANY_CLASSIFICATION_MISSING_REQUIREMENTS = (
    "Crecimiento comparable de ingresos y beneficio por acción.",
    "Sensibilidad histórica de ingresos y márgenes al ciclo económico.",
    "Evidencia de reestructuración, riesgo de continuidad y obligaciones relevantes.",
    "Valoración identificable de activos no reflejados por los resultados.",
)


class CompanyClassificationView(ContractModel):
    """Visible classification state that refuses an unsupported automatic label."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["classified", "insufficient_evidence"] = "insufficient_evidence"
    selected_category: CompanyCategoryKey | None = None
    categories: tuple[CompanyCategoryDefinition, ...] = COMPANY_CATEGORY_DEFINITIONS
    missing_requirements: tuple[NonEmptyStr, ...] = COMPANY_CLASSIFICATION_MISSING_REQUIREMENTS
    explanation_es: NonEmptyStr = (
        "La categoría se mostrará aquí cuando exista una regla versionada y evidencia suficiente."
    )

    @model_validator(mode="after")
    def validate_contract(self) -> "CompanyClassificationView":
        """Preserve the complete visible category framework and honest state."""
        if self.categories != COMPANY_CATEGORY_DEFINITIONS:
            raise ValueError("company categories must preserve the versioned catalog")
        if self.missing_requirements != COMPANY_CLASSIFICATION_MISSING_REQUIREMENTS:
            raise ValueError("classification requirements must preserve the versioned contract")
        if self.status == "classified" and self.selected_category is None:
            raise ValueError("classified company profiles require a selected category")
        if self.status == "insufficient_evidence" and self.selected_category is not None:
            raise ValueError("insufficient evidence cannot select a company category")
        return self


class FundamentalAnalysisCoverage(ContractModel):
    """Evidence availability counts, never an investment score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_metrics: int = Field(ge=1, le=FUNDAMENTAL_RESEARCH_METRIC_COUNT)
    latest_period_metrics: int = Field(ge=0, le=FUNDAMENTAL_RESEARCH_METRIC_COUNT)
    historical_series: int = Field(ge=0, le=FUNDAMENTAL_RESEARCH_METRIC_COUNT)
    series_with_previous_comparison: int = Field(ge=0, le=FUNDAMENTAL_RESEARCH_METRIC_COUNT)

    @model_validator(mode="after")
    def validate_counts(self) -> "FundamentalAnalysisCoverage":
        """Keep evidence counts bounded by the published metric catalog."""
        if self.latest_period_metrics > self.expected_metrics:
            raise ValueError("latest analysis metrics exceed expected metrics")
        if self.historical_series > self.expected_metrics:
            raise ValueError("analysis historical series exceed expected metrics")
        if self.series_with_previous_comparison > self.historical_series:
            raise ValueError("analysis comparisons exceed historical series")
        return self


class FundamentalAnalysisSectionView(ContractModel):
    """Point-in-time evidence availability for one non-overlapping section."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    definition: FundamentalAnalysisSectionDefinition
    latest_period_end: UTCDateTime | None = None
    available_metric_keys: tuple[NonEmptyStr, ...]
    missing_metric_keys: tuple[NonEmptyStr, ...]
    historical_metric_keys: tuple[NonEmptyStr, ...]
    coverage: FundamentalAnalysisCoverage

    @model_validator(mode="after")
    def validate_view(self) -> "FundamentalAnalysisSectionView":
        """Tie availability exactly to one section definition."""
        expected = tuple(item.metric_key for item in self.definition.metric_references)
        available = set(self.available_metric_keys)
        missing = set(self.missing_metric_keys)
        historical = set(self.historical_metric_keys)
        if available & missing or available | missing != set(expected):
            raise ValueError("analysis availability must partition expected metrics")
        if self.available_metric_keys != tuple(key for key in expected if key in available):
            raise ValueError("available analysis metrics must preserve definition order")
        if self.missing_metric_keys != tuple(key for key in expected if key in missing):
            raise ValueError("missing analysis metrics must preserve definition order")
        if not historical <= set(expected):
            raise ValueError("analysis history references unexpected metrics")
        if self.historical_metric_keys != tuple(key for key in expected if key in historical):
            raise ValueError("historical analysis metrics must preserve definition order")
        if self.coverage.expected_metrics != len(expected):
            raise ValueError("analysis expected count does not match its definition")
        if self.coverage.latest_period_metrics != len(self.available_metric_keys):
            raise ValueError("analysis latest count does not match available metrics")
        if self.coverage.historical_series != len(self.historical_metric_keys):
            raise ValueError("analysis history count does not match historical metrics")
        if self.available_metric_keys and self.latest_period_end is None:
            raise ValueError("available analysis metrics require a latest period")
        return self


FUNDAMENTAL_ANALYSIS_LIMITATIONS = (
    "Las secciones organizan evidencia SEC existente sin añadir cifras ni umbrales implícitos.",
    "Cada métrica aparece en una sola sección para evitar duplicados.",
    "La cobertura indica disponibilidad de evidencia y no es una puntuación de inversión.",
    "La clasificación empresarial no se asigna sin una regla versionada y evidencia suficiente.",
    "El análisis no emite recomendación, ranking, valor intrínseco ni señal de compra o venta.",
)


class AaplFundamentalAnalysisResult(ContractModel):
    """Versioned unified analysis over exact fundamental research history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["aapl-fundamental-analysis-v1"] = "aapl-fundamental-analysis-v1"
    asset_id: Literal["equity:us:aapl"] = ASSET_ID
    source_id: Literal["sec-edgar:aapl:companyfacts"] = COMPANYFACTS_SOURCE_ID
    request: AaplFundamentalResearchRequest
    history: AaplFundamentalResearchHistoryResult
    classification: CompanyClassificationView = CompanyClassificationView()
    sections: tuple[FundamentalAnalysisSectionView, ...]
    coverage: FundamentalAnalysisCoverage
    traceability_verified: Literal[True] = True
    limitations: tuple[NonEmptyStr, ...] = FUNDAMENTAL_ANALYSIS_LIMITATIONS

    @model_validator(mode="after")
    def validate_result(self) -> "AaplFundamentalAnalysisResult":
        """Preserve exact history and one non-overlapping complete metric catalog."""
        if self.request != self.history.request:
            raise ValueError("fundamental analysis request must match embedded history")
        if self.limitations != FUNDAMENTAL_ANALYSIS_LIMITATIONS:
            raise ValueError("analysis limitations must preserve the versioned contract")
        definitions = tuple(item.definition for item in self.sections)
        if definitions != FUNDAMENTAL_ANALYSIS_SECTION_DEFINITIONS:
            raise ValueError("fundamental analysis must preserve the section catalog")
        section_metric_keys = tuple(
            reference.metric_key
            for definition in definitions
            for reference in definition.metric_references
        )
        research_metric_keys = tuple(
            definition.metric_key for definition in FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS
        )
        if len(section_metric_keys) != len(set(section_metric_keys)):
            raise ValueError("fundamental analysis sections must not duplicate metrics")
        if set(section_metric_keys) != set(research_metric_keys):
            raise ValueError("fundamental analysis sections must cover every research metric")
        latest_period_end = (
            self.history.research.periods[-1].period_end if self.history.research.periods else None
        )
        if any(item.latest_period_end != latest_period_end for item in self.sections):
            raise ValueError("fundamental analysis sections must share the latest period")
        expected_metrics = sum(item.coverage.expected_metrics for item in self.sections)
        latest_metrics = sum(item.coverage.latest_period_metrics for item in self.sections)
        historical_series = sum(item.coverage.historical_series for item in self.sections)
        comparisons = sum(item.coverage.series_with_previous_comparison for item in self.sections)
        if self.coverage != FundamentalAnalysisCoverage(
            expected_metrics=expected_metrics,
            latest_period_metrics=latest_metrics,
            historical_series=historical_series,
            series_with_previous_comparison=comparisons,
        ):
            raise ValueError("fundamental analysis coverage does not match its sections")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return exact embedded evidence and compact analytical sections."""
        return {
            "schema_version": self.schema_version,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "request": self.request.model_dump(mode="json"),
            "history": self.history.to_json_dict(),
            "classification": self.classification.model_dump(mode="json"),
            "sections": [item.model_dump(mode="json") for item in self.sections],
            "coverage": self.coverage.model_dump(mode="json"),
            "traceability_verified": self.traceability_verified,
            "limitations": list(self.limitations),
        }


__all__ = [
    "AaplFundamentalAnalysisResult",
    "COMPANY_CATEGORY_DEFINITIONS",
    "COMPANY_CLASSIFICATION_MISSING_REQUIREMENTS",
    "FUNDAMENTAL_ANALYSIS_LIMITATIONS",
    "FUNDAMENTAL_ANALYSIS_SECTION_DEFINITIONS",
    "CompanyCategoryDefinition",
    "CompanyCategoryKey",
    "CompanyClassificationView",
    "FundamentalAnalysisCoverage",
    "FundamentalAnalysisMetricReference",
    "FundamentalAnalysisSectionDefinition",
    "FundamentalAnalysisSectionKey",
    "FundamentalAnalysisSectionView",
]
