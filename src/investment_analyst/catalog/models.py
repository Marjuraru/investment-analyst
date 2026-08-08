"""Strict, immutable models for the versioned asset catalog."""

import re
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict, StrictBool, StrictInt, field_validator, model_validator

from investment_analyst.core.models import Asset, AssetClass
from investment_analyst.core.models.base import ContractModel, NonEmptyStr

_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")


class CatalogCryptoProfile(StrEnum):
    """Economic crypto taxonomy declared independently from a provider symbol."""

    BITCOIN = "bitcoin"
    ETHEREUM = "ethereum"
    ALTCOIN = "altcoin"
    STABLECOIN = "stablecoin"
    WRAPPED = "wrapped"


class ProviderBinding(ContractModel):
    """One exact external identifier and the capabilities available through it."""

    model_config = ConfigDict(frozen=True)

    provider: NonEmptyStr
    namespace: NonEmptyStr
    identifier: NonEmptyStr
    is_unique: StrictBool = True
    capabilities: tuple[NonEmptyStr, ...]

    @field_validator("provider", "namespace", mode="before")
    @classmethod
    def normalize_slug(cls, value: object) -> object:
        """Normalize provider namespaces without altering external identifiers."""
        if isinstance(value, str):
            return value.strip().casefold()
        return value

    @field_validator("provider", "namespace")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        """Require stable lower-case slugs."""
        if not _SLUG_PATTERN.fullmatch(value):
            raise ValueError("provider and namespace must be lower-case slugs")
        return value

    @field_validator("identifier", mode="before")
    @classmethod
    def preserve_identifier(cls, value: object) -> object:
        """Reject destructive whitespace normalization of provider identifiers."""
        if isinstance(value, str) and value != value.strip():
            raise ValueError("identifier must not contain surrounding whitespace")
        return value

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Require a sorted, unique, namespaced capability tuple."""
        if not values:
            raise ValueError("capabilities must not be empty")
        if any(not _CAPABILITY_PATTERN.fullmatch(value) for value in values):
            raise ValueError("capabilities must use lower-case namespaced names")
        if len(set(values)) != len(values):
            raise ValueError("capabilities must not contain duplicates")
        if values != tuple(sorted(values)):
            raise ValueError("capabilities must be sorted")
        return values

    @property
    def identity(self) -> tuple[str, str, str]:
        """Return the exact external binding identity."""
        return self.provider, self.namespace, self.identifier


class CatalogAsset(Asset):
    """Canonical core asset enriched with aliases and provider bindings."""

    model_config = ConfigDict(frozen=True)

    aliases: tuple[NonEmptyStr, ...]
    provider_bindings: tuple[ProviderBinding, ...]
    crypto_profile: CatalogCryptoProfile | None = None
    security_unit_factor: Decimal | None = None
    security_unit_basis: Literal["reported_common_share", "depositary_receipt"] | None = None
    security_unit_basis_version: NonEmptyStr | None = None
    security_unit_market_adjustment: Literal["all"] | None = None

    @field_validator("security_unit_factor", mode="before")
    @classmethod
    def reject_unsafe_security_unit_factor(cls, value: object) -> object:
        """Keep market-price to reported-share conversion explicit and exact."""
        if isinstance(value, (bool, float)):
            raise ValueError("security_unit_factor must use Decimal-compatible text, not float")
        return value

    @field_validator("security_unit_factor")
    @classmethod
    def validate_security_unit_factor(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("security_unit_factor must be a finite positive Decimal")
        return value

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Require non-empty aliases that are unique case-insensitively."""
        if not values:
            raise ValueError("aliases must not be empty")
        folded = tuple(value.casefold() for value in values)
        if len(set(folded)) != len(folded):
            raise ValueError("aliases must be unique ignoring case")
        return values

    @field_validator("provider_bindings")
    @classmethod
    def validate_bindings(
        cls,
        values: tuple[ProviderBinding, ...],
    ) -> tuple[ProviderBinding, ...]:
        """Require at least one binding and deterministic binding order."""
        if not values:
            raise ValueError("provider_bindings must not be empty")
        identities = tuple(binding.identity for binding in values)
        if len(set(identities)) != len(identities):
            raise ValueError("provider_bindings must not contain duplicate identities")
        if identities != tuple(sorted(identities)):
            raise ValueError("provider_bindings must be sorted by identity")
        return values

    @model_validator(mode="after")
    def validate_crypto_profile(self) -> "CatalogAsset":
        """Keep crypto taxonomy explicit and absent from non-crypto assets."""
        if self.asset_class is AssetClass.CRYPTO and self.crypto_profile is None:
            raise ValueError("crypto assets require an explicit crypto_profile")
        if self.asset_class is not AssetClass.CRYPTO and self.crypto_profile is not None:
            raise ValueError("crypto_profile is only valid for crypto assets")
        unit_contract = (
            self.security_unit_factor,
            self.security_unit_basis,
            self.security_unit_basis_version,
            self.security_unit_market_adjustment,
        )
        if any(value is not None for value in unit_contract) and not all(
            value is not None for value in unit_contract
        ):
            raise ValueError("security unit contract fields must be declared together")
        if self.asset_class is not AssetClass.EQUITY and any(
            value is not None for value in unit_contract
        ):
            raise ValueError("security unit contracts are only valid for equities")
        return self


class AssetCatalogDocument(ContractModel):
    """One validated version of the static asset catalog."""

    model_config = ConfigDict(frozen=True)

    catalog_version: StrictInt
    assets: tuple[CatalogAsset, ...]

    @model_validator(mode="after")
    def validate_document(self) -> "AssetCatalogDocument":
        """Reject empty catalogs, duplicate asset IDs, and duplicate bindings."""
        if self.catalog_version < 1:
            raise ValueError("catalog_version must be positive")
        if not self.assets:
            raise ValueError("assets must not be empty")
        asset_ids = tuple(asset.asset_id for asset in self.assets)
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("asset IDs must be unique")
        if asset_ids != tuple(sorted(asset_ids)):
            raise ValueError("assets must be sorted by asset_id")
        binding_owners: dict[tuple[str, str, str], tuple[str, bool]] = {}
        for asset in self.assets:
            for binding in asset.provider_bindings:
                existing = binding_owners.get(binding.identity)
                if (
                    existing is not None
                    and existing[0] != asset.asset_id
                    and (existing[1] or binding.is_unique)
                ):
                    raise ValueError("unique external provider bindings must be globally unique")
                binding_owners[binding.identity] = (asset.asset_id, binding.is_unique)
        return self

    def list_by_type(self, asset_type: AssetClass) -> tuple[CatalogAsset, ...]:
        """Return assets matching one core asset class."""
        return tuple(asset for asset in self.assets if asset.asset_class is asset_type)
