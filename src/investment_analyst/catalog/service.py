"""Load and query the static versioned asset catalog without I/O side effects."""

import json
from importlib import resources
from pathlib import Path
from types import MappingProxyType

from pydantic import ValidationError

from investment_analyst.catalog.models import (
    AssetCatalogDocument,
    CatalogAsset,
    ProviderBinding,
)
from investment_analyst.core.models import AssetClass

_CURRENT_CATALOG_VERSION = 1
_DEFAULT_RESOURCE = "default_assets.v1.json"


class AssetCatalogError(RuntimeError):
    """Base error for catalog loading and lookup operations."""


class AssetCatalogFormatError(AssetCatalogError):
    """Raised when the catalog JSON or model structure is malformed."""


class AssetCatalogVersionError(AssetCatalogError):
    """Raised when a catalog version is unsupported."""


class AssetNotFoundError(AssetCatalogError):
    """Raised when a canonical asset ID does not exist."""


class AssetAliasNotFoundError(AssetCatalogError):
    """Raised when an alias does not identify any asset."""


class AmbiguousAssetAliasError(AssetCatalogError):
    """Raised when an alias identifies more than one asset."""


class ProviderBindingNotFoundError(AssetCatalogError):
    """Raised when an asset does not expose the requested provider binding."""


class AmbiguousProviderBindingError(AssetCatalogError):
    """Raised when a provider lookup has multiple possible matches."""


class DuplicateCatalogIdentityError(AssetCatalogFormatError):
    """Raised when canonical or external identities are duplicated."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _normalized_slug(value: str, *, field_name: str) -> str:
    normalized = value.strip().casefold()
    if not normalized:
        raise AssetCatalogFormatError(f"{field_name} must not be empty")
    return normalized


def _identifier(value: str) -> str:
    if not value or value != value.strip():
        raise AssetCatalogFormatError("identifier must be non-empty without surrounding whitespace")
    return value


class AssetCatalogService:
    """Immutable indexed view over one validated asset catalog document."""

    def __init__(
        self,
        document: AssetCatalogDocument | None = None,
        *,
        path: Path | None = None,
    ) -> None:
        if (document is None) == (path is None):
            raise AssetCatalogFormatError("provide exactly one catalog document or path")
        loaded = document if document is not None else self._load_path(Path(path))
        self._validate_version(loaded)
        self._document = loaded
        self._assets_by_id = MappingProxyType({asset.asset_id: asset for asset in loaded.assets})
        aliases: dict[str, list[CatalogAsset]] = {}
        external: dict[tuple[str, str, str], list[CatalogAsset]] = {}
        for asset in loaded.assets:
            for alias in asset.aliases:
                aliases.setdefault(alias.casefold(), []).append(asset)
            for binding in asset.provider_bindings:
                external.setdefault(binding.identity, []).append(asset)
        self._aliases = MappingProxyType(
            {
                key: tuple(sorted(values, key=lambda item: item.asset_id))
                for key, values in aliases.items()
            }
        )
        self._external = MappingProxyType(
            {
                key: tuple(sorted(values, key=lambda item: item.asset_id))
                for key, values in external.items()
            }
        )

    @classmethod
    def load_default(cls) -> "AssetCatalogService":
        """Load the packaged default catalog exactly once for this service instance."""
        try:
            resource = resources.files("investment_analyst.catalog").joinpath(_DEFAULT_RESOURCE)
            text = resource.read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError, OSError) as error:
            raise AssetCatalogFormatError(
                "default asset catalog resource is unavailable"
            ) from error
        return cls(cls._parse_document(text))

    @property
    def catalog_version(self) -> int:
        """Return the loaded static catalog version."""
        return self._document.catalog_version

    @property
    def document(self) -> AssetCatalogDocument:
        """Return the immutable validated catalog document."""
        return self._document

    def get(self, asset_id: str) -> CatalogAsset:
        """Return one asset by its canonical identifier."""
        try:
            return self._assets_by_id[asset_id.strip()]
        except KeyError as error:
            raise AssetNotFoundError(f"asset not found: {asset_id}") from error

    def list_assets(
        self,
        *,
        asset_type: AssetClass | None = None,
        capability: str | None = None,
    ) -> tuple[CatalogAsset, ...]:
        """List assets deterministically, optionally filtered by class and capability."""
        normalized_capability = capability.strip() if capability is not None else None
        assets = (
            asset
            for asset in self._document.assets
            if asset_type is None or asset.asset_class is asset_type
        )
        if normalized_capability is not None:
            assets = (
                asset
                for asset in assets
                if any(
                    normalized_capability in binding.capabilities
                    for binding in asset.provider_bindings
                )
            )
        return tuple(sorted(assets, key=lambda asset: asset.asset_id))

    def resolve_alias(
        self,
        alias: str,
        *,
        asset_type: AssetClass | None = None,
    ) -> CatalogAsset:
        """Resolve a case-insensitive alias without arbitrary tie-breaking."""
        key = alias.strip().casefold()
        candidates = tuple(
            asset
            for asset in self._aliases.get(key, ())
            if asset_type is None or asset.asset_class is asset_type
        )
        if not candidates:
            raise AssetAliasNotFoundError(f"asset alias not found: {alias}")
        if len(candidates) > 1:
            raise AmbiguousAssetAliasError(f"asset alias is ambiguous: {alias}")
        return candidates[0]

    def get_binding(
        self,
        asset_id: str,
        *,
        provider: str,
        namespace: str,
    ) -> ProviderBinding:
        """Return one provider binding for a canonical asset."""
        provider_slug = _normalized_slug(provider, field_name="provider")
        namespace_slug = _normalized_slug(namespace, field_name="namespace")
        candidates = tuple(
            binding
            for binding in self.get(asset_id).provider_bindings
            if binding.provider == provider_slug and binding.namespace == namespace_slug
        )
        if not candidates:
            raise ProviderBindingNotFoundError(
                f"provider binding not found for {asset_id}: {provider_slug}/{namespace_slug}"
            )
        if len(candidates) > 1:
            raise AmbiguousProviderBindingError(
                f"provider binding is ambiguous for {asset_id}: {provider_slug}/{namespace_slug}"
            )
        return candidates[0]

    def resolve_external(
        self,
        *,
        provider: str,
        namespace: str,
        identifier: str,
    ) -> CatalogAsset:
        """Reverse-resolve an exact provider identifier to its canonical asset."""
        key = (
            _normalized_slug(provider, field_name="provider"),
            _normalized_slug(namespace, field_name="namespace"),
            _identifier(identifier),
        )
        candidates = self._external.get(key, ())
        if not candidates:
            raise ProviderBindingNotFoundError(
                f"external provider binding not found: {key[0]}/{key[1]}/{identifier}"
            )
        if len(candidates) > 1:
            raise AmbiguousProviderBindingError(
                f"external provider binding is ambiguous: {key[0]}/{key[1]}/{identifier}"
            )
        return candidates[0]

    def supports(self, asset_id: str, capability: str) -> bool:
        """Return whether any binding for an asset exposes a capability."""
        requested = capability.strip()
        if not requested:
            raise AssetCatalogFormatError("capability must not be empty")
        return any(
            requested in binding.capabilities for binding in self.get(asset_id).provider_bindings
        )

    @classmethod
    def _load_path(cls, path: Path) -> AssetCatalogDocument:
        try:
            text = path.expanduser().resolve().read_text(encoding="utf-8")
        except OSError as error:
            raise AssetCatalogFormatError(f"asset catalog could not be read: {path}") from error
        return cls._parse_document(text)

    @staticmethod
    def _parse_document(text: str) -> AssetCatalogDocument:
        try:
            payload = json.loads(text, parse_constant=_reject_json_constant)
            return AssetCatalogDocument.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError) as error:
            message = str(error)
            if "globally unique" in message or "asset IDs must be unique" in message:
                raise DuplicateCatalogIdentityError(
                    "asset catalog contains duplicate identities"
                ) from error
            raise AssetCatalogFormatError("asset catalog is malformed") from error

    @staticmethod
    def _validate_version(document: AssetCatalogDocument) -> None:
        if document.catalog_version != _CURRENT_CATALOG_VERSION:
            raise AssetCatalogVersionError(
                f"unsupported asset catalog version: {document.catalog_version}"
            )
