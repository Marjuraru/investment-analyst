"""Asset data contract."""

from pydantic import Field

from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.core.models.enums import AssetClass


class Asset(ContractModel):
    """Stable identity and provider mappings for an investable asset."""

    asset_id: NonEmptyStr
    symbol: NonEmptyStr
    name: NonEmptyStr
    asset_class: AssetClass
    quote_currency: NonEmptyStr
    exchange: NonEmptyStr | None = None
    provider_symbols: dict[NonEmptyStr, NonEmptyStr] = Field(default_factory=dict)
    is_active: bool = True
