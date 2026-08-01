"""Resolve runtime operation routes from catalog-declared capabilities."""

from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict, model_validator

from investment_analyst.application.analysis_capabilities import (
    FundamentalAnalysisMode,
    MarketAnalysisMode,
)
from investment_analyst.application.market_universe import MarketAssetUniverse
from investment_analyst.core.models.base import ContractModel, NonEmptyStr


class RuntimeOperationRoute(StrEnum):
    """Compatible application facade selected by provider/domain/frequency."""

    LISTED_MARKET_DAILY = "listed_market_daily"
    CRYPTO_SPOT_DAILY = "crypto_spot_daily"
    CRYPTO_SPOT_INTRADAY = "crypto_spot_intraday"
    CORPORATE_FUNDAMENTALS = "corporate_fundamentals"


class RuntimeCapabilityBinding(ContractModel):
    """One immutable dispatch key and its public compatible route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["runtime-capability-binding-v1"] = "runtime-capability-binding-v1"
    asset_id: NonEmptyStr
    provider: NonEmptyStr
    domain: NonEmptyStr
    frequency: NonEmptyStr
    source_ids: tuple[NonEmptyStr, ...]
    route: RuntimeOperationRoute

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.asset_id, self.provider, self.domain, self.frequency

    @model_validator(mode="after")
    def validate_sources(self) -> "RuntimeCapabilityBinding":
        if not self.source_ids or self.source_ids != tuple(sorted(set(self.source_ids))):
            raise ValueError("runtime source IDs must be non-empty, unique, and sorted")
        return self


class CapabilityDrivenRuntimePlan(ContractModel):
    """Deterministic dispatch inventory derived only from the current catalog universe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["capability-driven-runtime-plan-v1"] = (
        "capability-driven-runtime-plan-v1"
    )
    catalog_version: int
    bindings: tuple[RuntimeCapabilityBinding, ...]

    @model_validator(mode="after")
    def validate_bindings(self) -> "CapabilityDrivenRuntimePlan":
        keys = tuple(item.key for item in self.bindings)
        if not keys or keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("runtime capability bindings must be non-empty, unique, and sorted")
        return self

    def resolve(
        self,
        *,
        asset_id: str,
        provider: str,
        domain: str,
        frequency: str,
    ) -> RuntimeCapabilityBinding:
        """Resolve exactly one route or fail before provider/storage work."""
        key = (asset_id, provider, domain, frequency)
        matches = tuple(item for item in self.bindings if item.key == key)
        if len(matches) != 1:
            raise ValueError("runtime capability is not configured")
        return matches[0]

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def build_capability_runtime_plan(universe: MarketAssetUniverse) -> CapabilityDrivenRuntimePlan:
    """Build market and fundamental dispatch routes without symbol-specific decisions."""
    bindings: list[RuntimeCapabilityBinding] = []
    for asset in universe.assets:
        daily_route = (
            RuntimeOperationRoute.CRYPTO_SPOT_DAILY
            if asset.analysis.market_mode is MarketAnalysisMode.CRYPTO_SPOT
            else RuntimeOperationRoute.LISTED_MARKET_DAILY
        )
        bindings.append(
            RuntimeCapabilityBinding(
                asset_id=asset.asset_id,
                provider=asset.provider,
                domain="market",
                frequency="day_1",
                source_ids=(asset.source_id,),
                route=daily_route,
            )
        )
        if asset.supports_intraday and asset.intraday_source_id is not None:
            bindings.append(
                RuntimeCapabilityBinding(
                    asset_id=asset.asset_id,
                    provider=asset.provider,
                    domain="market",
                    frequency="minute_1",
                    source_ids=(asset.intraday_source_id,),
                    route=RuntimeOperationRoute.CRYPTO_SPOT_INTRADAY,
                )
            )
        if (
            asset.has_fundamentals
            and asset.analysis.fundamental_mode is FundamentalAnalysisMode.CORPORATE
        ):
            for frequency in asset.fundamental_frequencies:
                bindings.append(
                    RuntimeCapabilityBinding(
                        asset_id=asset.asset_id,
                        provider="sec-edgar",
                        domain="fundamentals",
                        frequency=frequency.value,
                        source_ids=asset.fundamental_source_ids,
                        route=RuntimeOperationRoute.CORPORATE_FUNDAMENTALS,
                    )
                )
    return CapabilityDrivenRuntimePlan(
        catalog_version=universe.catalog_version,
        bindings=tuple(sorted(bindings, key=lambda item: item.key)),
    )


__all__ = [
    "CapabilityDrivenRuntimePlan",
    "RuntimeCapabilityBinding",
    "RuntimeOperationRoute",
    "build_capability_runtime_plan",
]
