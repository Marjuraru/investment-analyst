"""Versioned, provider-specific catalog for bounded FRED/ALFRED automation."""

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.providers.macro.fred_alfred import validate_fred_series_id


class FredMacroDomain(StrEnum):
    """Independent descriptive macro dimensions represented in the catalog."""

    GROWTH = "growth"
    INFLATION = "inflation"
    LABOR = "labor"
    RATES = "rates"
    CREDIT = "credit"
    LIQUIDITY = "liquidity"
    DOLLAR = "dollar"
    COMMODITIES = "commodities"
    CURVE = "curve"


class FredSeriesCatalogEntry(ContractModel):
    """One explicit series scope and safe automatic-refresh budget."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["fred-series-catalog-entry-v1"] = "fred-series-catalog-entry-v1"
    series_id: NonEmptyStr
    name_es: NonEmptyStr
    domain: FredMacroDomain
    observation_start: date
    data_frequency: NonEmptyStr
    automation_enabled: bool
    max_vintages_per_run: int = Field(default=3, ge=1, le=30)
    freshness_threshold_seconds: int = Field(
        default=172_800,
        ge=60,
        le=31_536_000,
    )
    automation_note: NonEmptyStr

    @field_validator("series_id")
    @classmethod
    def require_canonical_series_id(cls, value: str) -> str:
        """Require an exact official identifier rather than a search term."""
        return validate_fred_series_id(value)

    @field_validator(
        "automation_enabled",
        mode="before",
    )
    @classmethod
    def require_boolean(cls, value: object) -> object:
        """Reject ambiguous truthy configuration."""
        if not isinstance(value, bool):
            raise ValueError("automation_enabled must be a bool")
        return value

    @field_validator(
        "max_vintages_per_run",
        "freshness_threshold_seconds",
        mode="before",
    )
    @classmethod
    def reject_boolean_integers(cls, value: object) -> object:
        """Reject booleans accepted as integers by Python."""
        if isinstance(value, bool):
            raise ValueError("FRED catalog budgets must be integers")
        return value


class FredSeriesCatalog(ContractModel):
    """Immutable ordered catalog, including deferred high-volume series."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["fred-series-catalog-v1"] = "fred-series-catalog-v1"
    catalog_version: Literal[1] = 1
    entries: tuple[FredSeriesCatalogEntry, ...]

    @model_validator(mode="after")
    def validate_entries(self) -> "FredSeriesCatalog":
        """Require one deterministically ordered entry per official series."""
        ids = tuple(item.series_id for item in self.entries)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("FRED catalog entries must be unique and sorted")
        return self

    def automated_entries(self) -> tuple[FredSeriesCatalogEntry, ...]:
        """Return only series whose bounded snapshot volume is approved."""
        return tuple(item for item in self.entries if item.automation_enabled)


FRED_SERIES_CATALOG = FredSeriesCatalog(
    entries=tuple(
        sorted(
            (
                FredSeriesCatalogEntry(
                    series_id="CPIAUCSL",
                    name_es="Índice de precios al consumidor de EE. UU.",
                    domain=FredMacroDomain.INFLATION,
                    observation_start=date(1947, 1, 1),
                    data_frequency="monthly",
                    automation_enabled=True,
                    automation_note=(
                        "Serie mensual; se automatiza una instantánea completa por vintage "
                        "nuevo bajo un lote acotado."
                    ),
                ),
                FredSeriesCatalogEntry(
                    series_id="DCOILWTICO",
                    name_es="Petróleo WTI",
                    domain=FredMacroDomain.COMMODITIES,
                    observation_start=date(1986, 1, 2),
                    data_frequency="daily",
                    automation_enabled=False,
                    automation_note=(
                        "Diferida hasta diseñar almacenamiento columnar y backfill por ventanas "
                        "para series diarias."
                    ),
                ),
                FredSeriesCatalogEntry(
                    series_id="DTWEXBGS",
                    name_es="Índice amplio del dólar estadounidense",
                    domain=FredMacroDomain.DOLLAR,
                    observation_start=date(2006, 1, 2),
                    data_frequency="daily",
                    automation_enabled=False,
                    automation_note=(
                        "Diferida hasta validar volumen, ventanas y particionado de historia "
                        "diaria."
                    ),
                ),
                FredSeriesCatalogEntry(
                    series_id="FEDFUNDS",
                    name_es="Tasa efectiva de fondos federales",
                    domain=FredMacroDomain.RATES,
                    observation_start=date(1954, 7, 1),
                    data_frequency="monthly",
                    automation_enabled=True,
                    automation_note=(
                        "Serie mensual; se automatiza con descubrimiento de vintages reanudable."
                    ),
                ),
                FredSeriesCatalogEntry(
                    series_id="GDPC1",
                    name_es="Producto interno bruto real de EE. UU.",
                    domain=FredMacroDomain.GROWTH,
                    observation_start=date(1947, 1, 1),
                    data_frequency="quarterly",
                    automation_enabled=True,
                    automation_note=(
                        "Serie trimestral; el primer refresh toma solo el vintage oficial más "
                        "reciente."
                    ),
                ),
                FredSeriesCatalogEntry(
                    series_id="M2SL",
                    name_es="Oferta monetaria M2 de EE. UU.",
                    domain=FredMacroDomain.LIQUIDITY,
                    observation_start=date(1959, 1, 1),
                    data_frequency="monthly",
                    automation_enabled=True,
                    automation_note=(
                        "Serie mensual; se automatiza con límite explícito de revisiones por día."
                    ),
                ),
                FredSeriesCatalogEntry(
                    series_id="T10Y2Y",
                    name_es="Diferencial del Treasury a 10 y 2 años",
                    domain=FredMacroDomain.CURVE,
                    observation_start=date(1976, 6, 1),
                    data_frequency="daily",
                    automation_enabled=False,
                    automation_note=(
                        "Diferida hasta disponer de persistencia columnar para series diarias."
                    ),
                ),
                FredSeriesCatalogEntry(
                    series_id="TOTALSL",
                    name_es="Crédito al consumidor de EE. UU.",
                    domain=FredMacroDomain.CREDIT,
                    observation_start=date(1943, 1, 1),
                    data_frequency="monthly",
                    automation_enabled=True,
                    automation_note=(
                        "Serie mensual; se automatiza en snapshots completos y lotes pequeños."
                    ),
                ),
                FredSeriesCatalogEntry(
                    series_id="UNRATE",
                    name_es="Tasa de desempleo de EE. UU.",
                    domain=FredMacroDomain.LABOR,
                    observation_start=date(1948, 1, 1),
                    data_frequency="monthly",
                    automation_enabled=True,
                    automation_note=(
                        "Serie mensual; se automatiza con cobertura point-in-time independiente."
                    ),
                ),
            ),
            key=lambda item: item.series_id,
        )
    )
)


def fred_catalog_entry(series_id: str) -> FredSeriesCatalogEntry:
    """Resolve one configured series or fail without provider discovery."""
    canonical = validate_fred_series_id(series_id)
    for entry in FRED_SERIES_CATALOG.entries:
        if entry.series_id == canonical:
            return entry
    raise ValueError(f"FRED series is not configured: {canonical}")


__all__ = [
    "FRED_SERIES_CATALOG",
    "FredMacroDomain",
    "FredSeriesCatalog",
    "FredSeriesCatalogEntry",
    "fred_catalog_entry",
]
