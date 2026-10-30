"""Strict read-only contracts for explicit annual filing selection."""

from datetime import date
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.analytics.valuation.models import (
    ValuationCoverage,
    ValuationInput,
    ValuationMetricDefinition,
    ValuationMetricValue,
    ValuationSecurityBasis,
    ValuationSnapshotStatus,
    ValuationStatus,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


def _require_calendar_date(value: object) -> object:
    if isinstance(value, str):
        return date.fromisoformat(value)
    return value


class CorporateValuationFilingSelector(ContractModel):
    """The literal tuple that identifies one persisted annual filing context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    annual_period_end: date
    filing_accession_number: NonEmptyStr
    filing_accepted_at: UTCDateTime
    filing_form: NonEmptyStr
    fiscal_year: NonEmptyStr
    fiscal_period: NonEmptyStr

    @field_validator("annual_period_end", mode="before")
    @classmethod
    def require_calendar_date(cls, value: object) -> object:
        return _require_calendar_date(value)


class CorporateValuationFilingQuery(ContractModel):
    """Bounded request for available annual filing contexts of one asset."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["corporate-valuation-filing-query-v1"] = (
        "corporate-valuation-filing-query-v1"
    )
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    valuation_date: date
    start_date: date
    end_date: date
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("valuation_date", "start_date", "end_date", mode="before")
    @classmethod
    def require_calendar_date(cls, value: object) -> object:
        return _require_calendar_date(value)

    @model_validator(mode="after")
    def validate_range(self) -> "CorporateValuationFilingQuery":
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class CorporateValuationFilingOption(ContractModel):
    """One selectable annual context without ranking or metric calculation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selector: CorporateValuationFilingSelector
    available_field_roles: tuple[NonEmptyStr, ...]
    taxonomies: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_declared_fields(self) -> "CorporateValuationFilingOption":
        roles = self.available_field_roles
        if roles != tuple(sorted(roles)) or len(roles) != len(set(roles)):
            raise ValueError("available_field_roles must be sorted and unique")
        taxonomies = self.taxonomies
        if taxonomies != tuple(sorted(taxonomies)) or len(taxonomies) != len(set(taxonomies)):
            raise ValueError("taxonomies must be sorted and unique")
        return self


class CorporateValuationFilingOptions(ContractModel):
    """One bounded page of explicit annual filing contexts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["corporate-valuation-filing-options-v1"] = (
        "corporate-valuation-filing-options-v1"
    )
    request: CorporateValuationFilingQuery
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    returned: int = Field(ge=0)
    options: tuple[CorporateValuationFilingOption, ...]
    limitations: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_pagination(self) -> "CorporateValuationFilingOptions":
        if self.offset != self.request.offset or self.limit != self.request.limit:
            raise ValueError("filing options pagination must preserve the request")
        if self.returned != len(self.options):
            raise ValueError("returned must match the number of options")
        if self.offset + self.returned > self.total and self.returned != 0:
            raise ValueError("filing options page exceeds the declared total")
        return self


class CorporateValuationSelectedFilingRequest(ContractModel):
    """One explicit annual filing selection with its point-in-time cut."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["corporate-valuation-selected-filing-request-v1"] = (
        "corporate-valuation-selected-filing-request-v1"
    )
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    valuation_date: date
    basis: Literal["selected_annual_filing"] = "selected_annual_filing"
    annual_period_end: date
    filing_accession_number: NonEmptyStr
    filing_accepted_at: UTCDateTime
    filing_form: NonEmptyStr
    fiscal_year: NonEmptyStr
    fiscal_period: NonEmptyStr

    @field_validator("valuation_date", "annual_period_end", mode="before")
    @classmethod
    def require_calendar_date(cls, value: object) -> object:
        return _require_calendar_date(value)

    @model_validator(mode="after")
    def validate_selection(self) -> "CorporateValuationSelectedFilingRequest":
        if self.annual_period_end > self.valuation_date:
            raise ValueError("annual_period_end must not be after valuation_date")
        if self.filing_accepted_at > self.known_at:
            raise ValueError("filing_accepted_at must not be after known_at")
        return self

    def selector(self) -> CorporateValuationFilingSelector:
        """Return the literal grouping tuple selected by this request."""
        return CorporateValuationFilingSelector(
            annual_period_end=self.annual_period_end,
            filing_accession_number=self.filing_accession_number,
            filing_accepted_at=self.filing_accepted_at,
            filing_form=self.filing_form,
            fiscal_year=self.fiscal_year,
            fiscal_period=self.fiscal_period,
        )


class CorporateValuationSelectedFilingSnapshot(ContractModel):
    """Ephemeral selected-filing valuation; never a persisted latest-annual result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["corporate-valuation-selected-filing-snapshot-v1"] = (
        "corporate-valuation-selected-filing-snapshot-v1"
    )
    asset_id: NonEmptyStr
    request: CorporateValuationSelectedFilingRequest
    status: ValuationSnapshotStatus
    valuation_as_of: UTCDateTime | None = None
    known_at: UTCDateTime
    computed_at: UTCDateTime
    available_at: UTCDateTime | None = None
    price_age_days: int | None = Field(default=None, ge=0)
    annual_period_start: UTCDateTime | None = None
    annual_period_end: UTCDateTime | None = None
    filing_date: date | None = None
    filing_accepted_at: UTCDateTime | None = None
    filing_accession_number: NonEmptyStr | None = None
    filing_form: NonEmptyStr | None = None
    fiscal_year: NonEmptyStr | None = None
    fiscal_period: NonEmptyStr | None = None
    price_currency: NonEmptyStr | None = None
    report_currency: NonEmptyStr | None = None
    price_source_id: NonEmptyStr | None = None
    fundamental_source_id: NonEmptyStr | None = None
    security_basis: ValuationSecurityBasis | None = None
    inputs: tuple[ValuationInput, ...] = ()
    definitions: tuple[ValuationMetricDefinition, ...]
    metrics: tuple[ValuationMetricValue, ...]
    coverage: ValuationCoverage
    limitations: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_snapshot(self) -> "CorporateValuationSelectedFilingSnapshot":
        if self.request.asset_id != self.asset_id or self.request.known_at != self.known_at:
            raise ValueError("selected filing snapshot must preserve the request identity and cut")
        metric_keys = tuple(item.metric_key for item in self.metrics)
        if metric_keys != tuple(sorted(metric_keys)) or len(metric_keys) != len(set(metric_keys)):
            raise ValueError("valuation metrics must be ordered and unique")
        definition_keys = tuple(item.metric_key for item in self.definitions)
        if definition_keys != metric_keys:
            raise ValueError("valuation definitions must match metric values")
        if self.coverage.total != len(self.metrics):
            raise ValueError("valuation coverage total must match metrics")
        if self.available_at is not None and self.available_at > self.computed_at:
            raise ValueError("available_at must not be after computed_at")
        if self.status is not _snapshot_status(self.metrics):
            raise ValueError("valuation snapshot status must match metric coverage")
        if (
            self.valuation_as_of is not None
            and self.valuation_as_of.date() > self.request.valuation_date
        ):
            raise ValueError("valuation price must not be after valuation_date")
        if self.filing_accepted_at is not None and self.filing_accepted_at > self.known_at:
            raise ValueError("filing acceptance must not be after known_at")
        input_ids = {item.observation_id for item in self.inputs}
        if any(not set(metric.input_observation_ids) <= input_ids for metric in self.metrics):
            raise ValueError("metric input IDs must belong to snapshot inputs")
        self._validate_selected_context()
        return self

    def _validate_selected_context(self) -> None:
        evidence = (
            self.annual_period_end,
            self.filing_accepted_at,
            self.filing_accession_number,
            self.filing_form,
            self.fiscal_year,
            self.fiscal_period,
        )
        if all(value is None for value in evidence):
            return
        selector = self.request.selector()
        if self.annual_period_end is None or self.annual_period_end.date() != (
            selector.annual_period_end
        ):
            raise ValueError("selected filing period must match the request")
        if (
            self.filing_accepted_at != selector.filing_accepted_at
            or self.filing_accession_number != selector.filing_accession_number
            or self.filing_form != selector.filing_form
            or self.fiscal_year != selector.fiscal_year
            or self.fiscal_period != selector.fiscal_period
        ):
            raise ValueError("selected filing context must match the request")

    def to_json_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-safe projection preserving Decimal as text."""
        return self.model_dump(mode="json")


def _snapshot_status(
    metrics: tuple[ValuationMetricValue, ...],
) -> ValuationSnapshotStatus:
    statuses = {item.status for item in metrics}
    if statuses == {ValuationStatus.EVALUATED}:
        return ValuationSnapshotStatus.EVALUATED
    if ValuationStatus.EVALUATED in statuses:
        return ValuationSnapshotStatus.PARTIAL
    if statuses == {ValuationStatus.NOT_APPLICABLE}:
        return ValuationSnapshotStatus.NOT_APPLICABLE
    return ValuationSnapshotStatus.NOT_EVALUABLE


__all__ = [
    "CorporateValuationFilingOption",
    "CorporateValuationFilingOptions",
    "CorporateValuationFilingQuery",
    "CorporateValuationFilingSelector",
    "CorporateValuationSelectedFilingRequest",
    "CorporateValuationSelectedFilingSnapshot",
]
