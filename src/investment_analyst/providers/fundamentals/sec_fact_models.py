"""Strict models for selected SEC corporate fundamental facts."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models import DataFrequency, DataQuality
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.asset_config import SecAccountingStandard

ASSET_ID = "equity:us:aapl"
CIK = "0000320193"
SUBMISSIONS_SOURCE_ID = "sec-edgar:aapl:submissions"
COMPANYFACTS_SOURCE_ID = "sec-edgar:aapl:companyfacts"
SUBMISSIONS_SCHEMA_VERSION = "sec-edgar-submissions-snapshot-v1"
COMPANYFACTS_SCHEMA_VERSION = "sec-edgar-companyfacts-snapshot-v1"
TRANSFORMATION_VERSION = "sec-aapl-companyfacts-normalizer-v1"
_ALLOWED_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"})


class SecFactPeriodType(StrEnum):
    """Accounting period shape of a selected SEC fact."""

    DURATION = "duration"
    INSTANT = "instant"


class SecFactDefinition(ContractModel):
    """Explicit XBRL concept selected for normalization."""

    model_config = ConfigDict(frozen=True)

    field_name: NonEmptyStr
    taxonomy: NonEmptyStr
    tag: NonEmptyStr
    unit: NonEmptyStr
    period_type: SecFactPeriodType


SEC_FACT_DEFINITIONS = (
    SecFactDefinition(
        field_name="fundamental.revenue",
        taxonomy="us-gaap",
        tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.net_income",
        taxonomy="us-gaap",
        tag="NetIncomeLoss",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.assets",
        taxonomy="us-gaap",
        tag="Assets",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.liabilities",
        taxonomy="us-gaap",
        tag="Liabilities",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.stockholders_equity",
        taxonomy="us-gaap",
        tag="StockholdersEquity",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
)

# These additional facts expand the research foundation without changing the
# existing five-field point-in-time query contract. All values preserve the
# positive amounts reported by Apple; cash outflows are not sign-inverted here.
SEC_RESEARCH_FACT_DEFINITIONS = (
    SecFactDefinition(
        field_name="fundamental.diluted_earnings_per_share",
        taxonomy="us-gaap",
        tag="EarningsPerShareDiluted",
        unit="USD/shares",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.weighted_average_diluted_shares",
        taxonomy="us-gaap",
        tag="WeightedAverageNumberOfDilutedSharesOutstanding",
        unit="shares",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.shares_outstanding",
        taxonomy="us-gaap",
        tag="CommonStockSharesOutstanding",
        unit="shares",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.gross_profit",
        taxonomy="us-gaap",
        tag="GrossProfit",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.operating_income",
        taxonomy="us-gaap",
        tag="OperatingIncomeLoss",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.operating_cash_flow",
        taxonomy="us-gaap",
        tag="NetCashProvidedByUsedInOperatingActivities",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.capital_expenditures",
        taxonomy="us-gaap",
        tag="PaymentsToAcquirePropertyPlantAndEquipment",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.share_based_compensation",
        taxonomy="us-gaap",
        tag="ShareBasedCompensation",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.dividends_paid",
        taxonomy="us-gaap",
        tag="PaymentsOfDividends",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.share_repurchases",
        taxonomy="us-gaap",
        tag="PaymentsForRepurchaseOfCommonStock",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.research_and_development",
        taxonomy="us-gaap",
        tag="ResearchAndDevelopmentExpense",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.selling_general_and_administrative",
        taxonomy="us-gaap",
        tag="SellingGeneralAndAdministrativeExpense",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.cash_and_cash_equivalents",
        taxonomy="us-gaap",
        tag="CashAndCashEquivalentsAtCarryingValue",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.current_assets",
        taxonomy="us-gaap",
        tag="AssetsCurrent",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.current_liabilities",
        taxonomy="us-gaap",
        tag="LiabilitiesCurrent",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.inventory",
        taxonomy="us-gaap",
        tag="InventoryNet",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.accounts_receivable",
        taxonomy="us-gaap",
        tag="AccountsReceivableNetCurrent",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.accounts_payable",
        taxonomy="us-gaap",
        tag="AccountsPayableCurrent",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.long_term_debt_current",
        taxonomy="us-gaap",
        tag="LongTermDebtCurrent",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.long_term_debt_noncurrent",
        taxonomy="us-gaap",
        tag="LongTermDebtNoncurrent",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.marketable_securities_current",
        taxonomy="us-gaap",
        tag="MarketableSecuritiesCurrent",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.marketable_securities_noncurrent",
        taxonomy="us-gaap",
        tag="MarketableSecuritiesNoncurrent",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.commercial_paper",
        taxonomy="us-gaap",
        tag="CommercialPaper",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.interest_expense",
        taxonomy="us-gaap",
        tag="InterestExpense",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.income_before_tax",
        taxonomy="us-gaap",
        tag=(
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItems"
            "NoncontrollingInterest"
        ),
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.income_tax_expense",
        taxonomy="us-gaap",
        tag="IncomeTaxExpenseBenefit",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.property_plant_and_equipment_net",
        taxonomy="us-gaap",
        tag="PropertyPlantAndEquipmentNet",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.operating_lease_liability_current",
        taxonomy="us-gaap",
        tag="OperatingLeaseLiabilityCurrent",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.operating_lease_liability_noncurrent",
        taxonomy="us-gaap",
        tag="OperatingLeaseLiabilityNoncurrent",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.finance_lease_liability_current",
        taxonomy="us-gaap",
        tag="FinanceLeaseLiabilityCurrent",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.finance_lease_liability_noncurrent",
        taxonomy="us-gaap",
        tag="FinanceLeaseLiabilityNoncurrent",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
)

SEC_NORMALIZED_FACT_DEFINITIONS = SEC_FACT_DEFINITIONS + SEC_RESEARCH_FACT_DEFINITIONS

# IFRS definitions intentionally include only concepts with direct economic and unit
# compatibility with the provider-independent field. Missing US-GAAP research fields
# remain absent rather than being inferred from broader or issuer-specific concepts.
SEC_IFRS_FACT_DEFINITIONS = (
    SecFactDefinition(
        field_name="fundamental.revenue",
        taxonomy="ifrs-full",
        tag="Revenue",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.net_income",
        taxonomy="ifrs-full",
        tag="ProfitLoss",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.assets",
        taxonomy="ifrs-full",
        tag="Assets",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.liabilities",
        taxonomy="ifrs-full",
        tag="Liabilities",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.stockholders_equity",
        taxonomy="ifrs-full",
        tag="EquityAttributableToOwnersOfParent",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
)

SEC_IFRS_RESEARCH_FACT_DEFINITIONS = (
    SecFactDefinition(
        field_name="fundamental.diluted_earnings_per_share",
        taxonomy="ifrs-full",
        tag="DilutedEarningsLossPerShare",
        unit="USD/shares",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.weighted_average_diluted_shares",
        taxonomy="ifrs-full",
        tag="WeightedAverageShares",
        unit="shares",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.shares_outstanding",
        taxonomy="ifrs-full",
        tag="NumberOfSharesOutstanding",
        unit="shares",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.gross_profit",
        taxonomy="ifrs-full",
        tag="GrossProfit",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.operating_income",
        taxonomy="ifrs-full",
        tag="ProfitLossFromOperatingActivities",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.operating_cash_flow",
        taxonomy="ifrs-full",
        tag="CashFlowsFromUsedInOperatingActivities",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.capital_expenditures",
        taxonomy="ifrs-full",
        tag="PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.dividends_paid",
        taxonomy="ifrs-full",
        tag="DividendsPaidToEquityHoldersOfParentClassifiedAsFinancingActivities",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.share_repurchases",
        taxonomy="ifrs-full",
        tag="PaymentsToAcquireOrRedeemEntitysShares",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.selling_general_and_administrative",
        taxonomy="ifrs-full",
        tag="AdministrativeExpense",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.cash_and_cash_equivalents",
        taxonomy="ifrs-full",
        tag="CashAndCashEquivalents",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.current_assets",
        taxonomy="ifrs-full",
        tag="CurrentAssets",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.current_liabilities",
        taxonomy="ifrs-full",
        tag="CurrentLiabilities",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.inventory",
        taxonomy="ifrs-full",
        tag="Inventories",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.accounts_receivable",
        taxonomy="ifrs-full",
        tag="TradeAndOtherCurrentReceivables",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.accounts_payable",
        taxonomy="ifrs-full",
        tag="TradeAndOtherCurrentPayables",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.interest_expense",
        taxonomy="ifrs-full",
        tag="FinanceCosts",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.income_before_tax",
        taxonomy="ifrs-full",
        tag="ProfitLossBeforeTax",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.income_tax_expense",
        taxonomy="ifrs-full",
        tag="IncomeTaxExpenseContinuingOperations",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.property_plant_and_equipment_net",
        taxonomy="ifrs-full",
        tag="PropertyPlantAndEquipmentIncludingRightofuseAssets",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.operating_lease_liability_current",
        taxonomy="ifrs-full",
        tag="CurrentLeaseLiabilities",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.operating_lease_liability_noncurrent",
        taxonomy="ifrs-full",
        tag="NoncurrentLeaseLiabilities",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
)

SEC_IFRS_NORMALIZED_FACT_DEFINITIONS = (
    SEC_IFRS_FACT_DEFINITIONS + SEC_IFRS_RESEARCH_FACT_DEFINITIONS
)
_DEFINITIONS_BY_TAXONOMY = {
    SecAccountingStandard.US_GAAP.value: SEC_NORMALIZED_FACT_DEFINITIONS,
    SecAccountingStandard.IFRS.value: SEC_IFRS_NORMALIZED_FACT_DEFINITIONS,
}
_DEFINITION_BY_TAXONOMY_FIELD = {
    (item.taxonomy, item.field_name): item
    for definitions in _DEFINITIONS_BY_TAXONOMY.values()
    for item in definitions
}


class SecFilingMetadata(ContractModel):
    """Validated metadata for one supported SEC filing accession."""

    model_config = ConfigDict(frozen=True)

    accession_number: NonEmptyStr
    form: NonEmptyStr
    filing_date: date
    report_date: date
    acceptance_at: UTCDateTime
    primary_document: NonEmptyStr
    is_amendment: bool

    @model_validator(mode="after")
    def validate_filing(self) -> "SecFilingMetadata":
        """Validate supported forms, chronology, and amendment semantics."""
        if self.form not in _ALLOWED_FORMS:
            raise ValueError("form must be a supported SEC annual or quarterly filing")
        if self.report_date > self.filing_date:
            raise ValueError("report_date must not be later than filing_date")
        if self.is_amendment != self.form.endswith("/A"):
            raise ValueError("is_amendment must match the filing form")
        return self


class SecFundamentalFact(ContractModel):
    """Selected point-in-time issuer fact before observation persistence."""

    model_config = ConfigDict(frozen=True)

    asset_id: NonEmptyStr
    companyfacts_record_id: UUID
    submissions_record_id: UUID
    field_name: NonEmptyStr
    taxonomy: NonEmptyStr
    tag: NonEmptyStr
    unit: NonEmptyStr
    value: Decimal
    accession_number: NonEmptyStr
    form: NonEmptyStr
    fiscal_year: int = Field(ge=1900, le=10000)
    fiscal_period: NonEmptyStr
    period_start: date | None = None
    period_end: date
    filed_date: date
    acceptance_at: UTCDateTime
    frequency: DataFrequency
    frame: NonEmptyStr | None = None
    quality: DataQuality

    @field_validator("value", mode="before")
    @classmethod
    def reject_binary_floating_point(cls, value: object) -> object:
        """Reject floats and booleans before Decimal validation."""
        if isinstance(value, (bool, float)):
            raise ValueError("value must be provided without float or bool")
        return value

    @field_validator("fiscal_year", mode="before")
    @classmethod
    def reject_boolean_year(cls, value: object) -> object:
        """Reject booleans masquerading as integers."""
        if isinstance(value, bool):
            raise ValueError("fiscal_year must be an integer")
        return value

    @model_validator(mode="after")
    def validate_fact(self) -> "SecFundamentalFact":
        """Validate the selected concept and accounting period shape."""
        definition = _DEFINITION_BY_TAXONOMY_FIELD.get((self.taxonomy, self.field_name))
        if definition is None:
            raise ValueError("field_name and taxonomy are not a selected SEC concept")
        if self.taxonomy != definition.taxonomy or self.tag != definition.tag:
            raise ValueError("taxonomy and tag must match the selected field definition")
        if self.unit != definition.unit:
            raise ValueError("unit must match the selected field definition")
        if not self.value.is_finite():
            raise ValueError("value must be a finite Decimal")
        if self.form not in _ALLOWED_FORMS:
            raise ValueError("form must be a supported SEC annual or quarterly filing")
        if self.frequency not in {DataFrequency.QUARTERLY, DataFrequency.ANNUAL}:
            raise ValueError("frequency must be quarterly or annual")
        if self.quality is not DataQuality.VALID:
            raise ValueError("SEC fundamental facts must have VALID quality")
        if definition.period_type is SecFactPeriodType.DURATION:
            if self.period_start is None:
                raise ValueError("duration facts require period_start")
            if self.period_start > self.period_end:
                raise ValueError("period_start must not be later than period_end")
        elif self.period_start is not None:
            raise ValueError("instant facts must not define period_start")
        return self

    @property
    def period_type(self) -> SecFactPeriodType:
        """Return the fixed period type for this selected concept."""
        return _DEFINITION_BY_TAXONOMY_FIELD[(self.taxonomy, self.field_name)].period_type

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return {
            "asset_id": self.asset_id,
            "companyfacts_record_id": str(self.companyfacts_record_id),
            "submissions_record_id": str(self.submissions_record_id),
            "field_name": self.field_name,
            "taxonomy": self.taxonomy,
            "tag": self.tag,
            "unit": self.unit,
            "value": str(self.value),
            "accession_number": self.accession_number,
            "form": self.form,
            "fiscal_year": self.fiscal_year,
            "fiscal_period": self.fiscal_period,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat(),
            "filed_date": self.filed_date.isoformat(),
            "acceptance_at": self.acceptance_at.isoformat(),
            "frequency": self.frequency.value,
            "frame": self.frame,
            "quality": self.quality.value,
        }


def sec_fact_definitions(taxonomy: str) -> tuple[SecFactDefinition, ...]:
    """Return the exact deterministic fact profile for one declared taxonomy."""
    try:
        return _DEFINITIONS_BY_TAXONOMY[taxonomy]
    except KeyError as error:
        raise ValueError(f"unsupported SEC accounting taxonomy: {taxonomy}") from error


def get_sec_fact_definition(
    field_name: str,
    *,
    taxonomy: str = SecAccountingStandard.US_GAAP.value,
) -> SecFactDefinition:
    """Return one taxonomy-specific definition or raise for an unsupported field."""
    try:
        return _DEFINITION_BY_TAXONOMY_FIELD[(taxonomy, field_name)]
    except KeyError as error:
        raise ValueError(f"unsupported SEC fact field for {taxonomy}: {field_name}") from error
