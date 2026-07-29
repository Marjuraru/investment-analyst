"""Catalog-backed identities for one BVL listing and its SMV issuer."""

import re

from pydantic import ConfigDict, model_validator

from investment_analyst.core.models import AssetClass
from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.providers.peru.smv_open_data import (
    reported_code_matches_isin,
    validate_isin,
    validate_legal_name,
)

_MNEMONIC_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,31}$")
_REPORTED_CODE_PATTERN = re.compile(r"^[A-Z0-9]{8}$")


class SmvBvlAssetConfiguration(ContractModel):
    """Exact BVL listing identity linked to an independently queried SMV issuer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    name: NonEmptyStr
    asset_class: AssetClass
    exchange: NonEmptyStr
    quote_currency: NonEmptyStr
    mnemonic: NonEmptyStr
    isin: NonEmptyStr
    legal_name: NonEmptyStr
    reported_security_code: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> "SmvBvlAssetConfiguration":
        """Reject cross-market, incomplete, or heuristically synthesized identity."""
        if self.asset_class is not AssetClass.EQUITY or self.exchange != "BVL":
            raise ValueError("SMV/BVL configuration requires one BVL equity listing")
        if not _MNEMONIC_PATTERN.fullmatch(self.mnemonic):
            raise ValueError("BVL mnemonic is invalid")
        validate_isin(self.isin)
        validate_legal_name(self.legal_name)
        if self.quote_currency not in {"PEN", "USD"}:
            raise ValueError("initial BVL listing currency must be PEN or USD")
        if self.reported_security_code is not None:
            if not _REPORTED_CODE_PATTERN.fullmatch(self.reported_security_code):
                raise ValueError("SMV reported security code is invalid")
            if not reported_code_matches_isin(self.reported_security_code, self.isin):
                raise ValueError("SMV reported security code conflicts with corroborated BVL ISIN")
        return self


__all__ = ["SmvBvlAssetConfiguration"]
