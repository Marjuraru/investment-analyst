"""Resolve immutable provider-specific asset context from the static catalog."""

from pydantic import ConfigDict, field_validator, model_validator

from investment_analyst.catalog.models import CatalogAsset, ProviderBinding
from investment_analyst.catalog.service import (
    AssetCatalogService,
    AssetNotFoundError,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr


class ProviderAssetContextError(RuntimeError):
    """Base error for provider-context resolution."""


class ProviderAssetNotConfiguredError(ProviderAssetContextError):
    """Raised when an asset has no binding for the requested provider."""


class ProviderNamespaceMissingError(ProviderAssetContextError):
    """Raised when a required provider namespace is unavailable."""


class ProviderCapabilityMissingError(ProviderAssetContextError):
    """Raised when a required provider capability is unavailable."""


class ProviderAssetContext(ContractModel):
    """Validated provider bindings for one canonical catalog asset."""

    model_config = ConfigDict(frozen=True)

    asset: CatalogAsset
    provider: NonEmptyStr
    bindings: tuple[ProviderBinding, ...]
    capabilities: tuple[NonEmptyStr, ...]

    @field_validator("provider", mode="before")
    @classmethod
    def normalize_provider(cls, value: object) -> object:
        """Normalize the provider slug without altering identifiers."""
        if isinstance(value, str):
            return value.strip().casefold()
        return value

    @model_validator(mode="after")
    def validate_context(self) -> "ProviderAssetContext":
        """Require one provider, unique namespaces, and exact capability coverage."""
        if not self.bindings:
            raise ValueError("provider context requires at least one binding")
        if any(binding.provider != self.provider for binding in self.bindings):
            raise ValueError("all bindings must belong to the context provider")
        namespaces = tuple(binding.namespace for binding in self.bindings)
        if len(set(namespaces)) != len(namespaces):
            raise ValueError("provider context namespaces must be unique")
        expected_bindings = tuple(
            sorted(self.bindings, key=lambda item: (item.namespace, item.identifier))
        )
        if self.bindings != expected_bindings:
            raise ValueError("provider context bindings must be deterministically ordered")
        expected_capabilities = tuple(
            sorted({capability for binding in self.bindings for capability in binding.capabilities})
        )
        if self.capabilities != expected_capabilities:
            raise ValueError("provider context capabilities must match its bindings")
        return self

    def require_identifier(self, namespace: str) -> str:
        """Return one exact provider identifier or fail with a typed error."""
        normalized = namespace.strip().casefold()
        matches = tuple(
            binding.identifier for binding in self.bindings if binding.namespace == normalized
        )
        if not matches:
            raise ProviderNamespaceMissingError(
                f"provider namespace is not configured: {self.provider}/{normalized}"
            )
        if len(matches) != 1:
            raise ProviderNamespaceMissingError(
                f"provider namespace is ambiguous: {self.provider}/{normalized}"
            )
        return matches[0]

    def supports(self, capability: str) -> bool:
        """Return whether this provider context exposes one exact capability."""
        return capability.strip() in self.capabilities


class ProviderAssetContextResolver:
    """Resolve provider context once from one injected immutable catalog service."""

    def __init__(self, catalog: AssetCatalogService) -> None:
        self._catalog = catalog

    def resolve(
        self,
        asset_id: str,
        *,
        provider: str,
        required_namespaces: tuple[str, ...] = (),
        required_capabilities: tuple[str, ...] = (),
    ) -> ProviderAssetContext:
        """Resolve and validate all bindings for one asset/provider pair."""
        normalized_provider = provider.strip().casefold()
        try:
            asset = self._catalog.get(asset_id)
        except AssetNotFoundError:
            raise
        bindings = tuple(
            sorted(
                (
                    binding
                    for binding in asset.provider_bindings
                    if binding.provider == normalized_provider
                ),
                key=lambda item: (item.namespace, item.identifier),
            )
        )
        if not bindings:
            raise ProviderAssetNotConfiguredError(
                f"asset {asset.asset_id} is not configured for provider {normalized_provider}"
            )
        capabilities = tuple(
            sorted({capability for binding in bindings for capability in binding.capabilities})
        )
        context = ProviderAssetContext(
            asset=asset,
            provider=normalized_provider,
            bindings=bindings,
            capabilities=capabilities,
        )
        for namespace in required_namespaces:
            context.require_identifier(namespace)
        for capability in required_capabilities:
            if not context.supports(capability):
                raise ProviderCapabilityMissingError(
                    f"asset {asset.asset_id} lacks {normalized_provider} capability {capability}"
                )
        return context
