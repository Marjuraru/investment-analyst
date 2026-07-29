"""Catalog-driven local universe for BVL listings and SMV registry evidence."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.catalog.provider_configuration import (
    resolve_smv_bvl_configuration,
)
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.peru.asset_config import SmvBvlAssetConfiguration
from investment_analyst.providers.peru.smv_open_data import (
    SmvRegisteredCompany,
    SmvRegisteredSecurity,
    validate_isin,
)
from investment_analyst.providers.peru.smv_pipeline import (
    SmvRegistryClient,
    SmvRegistryImportSummary,
    SmvRegistryPipeline,
)
from investment_analyst.providers.peru.smv_point_in_time import (
    SmvIssuerRegistryView,
    SmvPointInTimeQuery,
    SmvPointInTimeService,
)
from investment_analyst.storage import LocalStorage, StorageError

_REGISTRY_CAPABILITY = "registry.exchange_listing"


class BvlRegistryStatus(StrEnum):
    """How far official local evidence corroborates one configured BVL listing."""

    NOT_IMPORTED = "not_imported"
    PARTIAL = "partial"
    ISSUER_VERIFIED = "issuer_verified"
    SECURITY_VERIFIED = "security_verified"
    SECURITY_MISMATCH = "security_mismatch"


class BvlRegistryUniverseRequest(ContractModel):
    """Select configured BVL assets at one point-in-time cut."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    known_at: UTCDateTime
    asset_ids: tuple[NonEmptyStr, ...] = ()

    @field_validator("asset_ids")
    @classmethod
    def validate_asset_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Require deterministic optional asset filtering."""
        if values != tuple(sorted(set(values))):
            raise ValueError("BVL asset_ids must be unique and sorted")
        return values


class BvlRegistryRefreshRequest(ContractModel):
    """Refresh all configured BVL identities or one deterministic subset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_ids: tuple[NonEmptyStr, ...] = ()

    @field_validator("asset_ids")
    @classmethod
    def validate_asset_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Require unique sorted IDs so a batch is reproducible."""
        if values != tuple(sorted(set(values))):
            raise ValueError("BVL refresh asset_ids must be unique and sorted")
        return values


class BvlRegistryAsset(ContractModel):
    """One BVL catalog identity with independently selected SMV evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    symbol: NonEmptyStr
    name: NonEmptyStr
    exchange: NonEmptyStr
    quote_currency: NonEmptyStr
    mnemonic: NonEmptyStr
    isin: NonEmptyStr
    legal_name: NonEmptyStr
    reported_security_code: NonEmptyStr | None = None
    status: BvlRegistryStatus
    companies: tuple[SmvRegisteredCompany, ...]
    matching_securities: tuple[SmvRegisteredSecurity, ...]
    company_available_at: UTCDateTime | None = None
    securities_available_at: UTCDateTime | None = None
    company_raw_record_ids: tuple[UUID, ...]
    securities_raw_record_ids: tuple[UUID, ...]
    limitations: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_identity_and_status(self) -> "BvlRegistryAsset":
        """Keep catalog identity, evidence, and explicit status consistent."""
        validate_isin(self.isin)
        if self.exchange != "BVL":
            raise ValueError("BVL registry asset must use the BVL exchange")
        if self.status is BvlRegistryStatus.NOT_IMPORTED and (
            self.companies
            or self.matching_securities
            or self.company_raw_record_ids
            or self.securities_raw_record_ids
        ):
            raise ValueError("not-imported BVL asset cannot expose registry evidence")
        if self.status is BvlRegistryStatus.SECURITY_VERIFIED and (
            self.reported_security_code is None or not self.matching_securities
        ):
            raise ValueError("security-verified BVL asset requires matching SMV evidence")
        if any(item.mnemonic != self.mnemonic for item in self.matching_securities):
            raise ValueError("BVL asset contains a different SMV mnemonic")
        if self.reported_security_code is not None and any(
            item.reported_security_code != self.reported_security_code
            for item in self.matching_securities
        ):
            raise ValueError("BVL asset contains a different SMV security code")
        if any(item.currency != self.quote_currency for item in self.matching_securities):
            raise ValueError("BVL asset and matching SMV security currency disagree")
        return self


class BvlRegistryUniverse(ContractModel):
    """Complete configured BVL universe with point-in-time registry evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: NonEmptyStr = "bvl-registry-universe-v1"
    catalog_version: int = Field(ge=1)
    request: BvlRegistryUniverseRequest
    assets: tuple[BvlRegistryAsset, ...]
    raw_records_examined: int = Field(ge=0)
    raw_records_eligible: int = Field(ge=0)
    revisions_superseded: int = Field(ge=0)
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_universe(self) -> "BvlRegistryUniverse":
        """Require ordered configured assets and verified local traceability."""
        asset_ids = tuple(item.asset_id for item in self.assets)
        if asset_ids != tuple(sorted(set(asset_ids))):
            raise ValueError("BVL registry assets must be unique and sorted")
        if self.request.asset_ids and any(
            asset_id not in self.request.asset_ids for asset_id in asset_ids
        ):
            raise ValueError("BVL registry universe contains an unrequested asset")
        if self.raw_records_eligible > self.raw_records_examined:
            raise ValueError("eligible BVL registry records exceed examined records")
        if not self.traceability_verified:
            raise ValueError("BVL registry universe must have verified traceability")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return deterministic JSON primitives for CLI and local web use."""
        return self.model_dump(mode="json")


class BvlRegistryUniverseService:
    """Join static listing identity to local SMV evidence without creating prices."""

    def __init__(
        self,
        storage: LocalStorage,
        catalog: AssetCatalogService,
        resolver: ProviderAssetContextResolver,
    ) -> None:
        self._storage = storage
        self._catalog = catalog
        self._resolver = resolver

    def query(self, request: BvlRegistryUniverseRequest) -> BvlRegistryUniverse:
        """Return every configured or explicitly selected BVL listing."""
        configurations = tuple(
            resolve_smv_bvl_configuration(self._resolver, asset_id=asset.asset_id)
            for asset in self._catalog.list_assets(capability=_REGISTRY_CAPABILITY)
            if asset.exchange == "BVL"
            and (not request.asset_ids or asset.asset_id in request.asset_ids)
        )
        if request.asset_ids:
            configured_ids = {item.asset_id for item in configurations}
            missing = tuple(
                asset_id for asset_id in request.asset_ids if asset_id not in configured_ids
            )
            if missing:
                raise ValueError(f"BVL registry asset is not configured: {missing[0]}")
        legal_names = tuple(sorted({item.legal_name for item in configurations}))
        registry = SmvPointInTimeService(self._storage).query(
            SmvPointInTimeQuery(
                known_at=request.known_at,
                legal_names=legal_names,
            )
        )
        issuer_by_name = {issuer.legal_name: issuer for issuer in registry.issuers}
        assets = tuple(
            sorted(
                (
                    _asset_view(configuration, issuer_by_name.get(configuration.legal_name))
                    for configuration in configurations
                ),
                key=lambda item: item.asset_id,
            )
        )
        return BvlRegistryUniverse(
            catalog_version=self._catalog.catalog_version,
            request=request,
            assets=assets,
            raw_records_examined=registry.raw_records_examined,
            raw_records_eligible=registry.raw_records_eligible,
            revisions_superseded=registry.revisions_superseded,
            traceability_verified=registry.traceability_verified,
        )


@dataclass(frozen=True, slots=True)
class BvlRegistryAssetRefresh:
    """One successfully persisted and locally revalidated BVL registry asset."""

    asset_id: str
    mnemonic: str
    isin: str
    status: BvlRegistryStatus
    registry: SmvRegistryImportSummary

    def to_json_dict(self) -> dict[str, object]:
        """Return deterministic JSON primitives."""
        return {
            "asset_id": self.asset_id,
            "mnemonic": self.mnemonic,
            "isin": self.isin,
            "status": self.status.value,
            "registry": self.registry.to_json_dict(),
        }


@dataclass(frozen=True, slots=True)
class BvlRegistryRefreshSummary:
    """Auditable outcome of one resumable catalog-driven BVL registry batch."""

    requested_asset_ids: tuple[str, ...]
    assets: tuple[BvlRegistryAssetRefresh, ...]
    raw_records_created: int
    raw_records_reused: int
    traceability_verified: bool

    def to_json_dict(self) -> dict[str, object]:
        """Return deterministic JSON primitives."""
        return {
            "requested_asset_ids": list(self.requested_asset_ids),
            "assets": [item.to_json_dict() for item in self.assets],
            "raw_records_created": self.raw_records_created,
            "raw_records_reused": self.raw_records_reused,
            "traceability_verified": self.traceability_verified,
        }


class BvlRegistryRefreshService:
    """Refresh configured SMV issuers sequentially and validate each local result."""

    def __init__(
        self,
        storage: LocalStorage,
        catalog: AssetCatalogService,
        resolver: ProviderAssetContextResolver,
        client: SmvRegistryClient,
    ) -> None:
        self._storage = storage
        self._catalog = catalog
        self._resolver = resolver
        self._client = client

    def run(self, request: BvlRegistryRefreshRequest) -> BvlRegistryRefreshSummary:
        """Persist an ordered batch, retaining earlier assets if a later one fails."""
        configurations = tuple(
            resolve_smv_bvl_configuration(self._resolver, asset_id=asset.asset_id)
            for asset in self._catalog.list_assets(capability=_REGISTRY_CAPABILITY)
            if asset.exchange == "BVL"
            and (not request.asset_ids or asset.asset_id in request.asset_ids)
        )
        configured_ids = tuple(item.asset_id for item in configurations)
        if request.asset_ids:
            missing = tuple(
                asset_id for asset_id in request.asset_ids if asset_id not in configured_ids
            )
            if missing:
                raise ValueError(f"BVL registry asset is not configured: {missing[0]}")
        completed: list[BvlRegistryAssetRefresh] = []
        imports_by_legal_name: dict[str, SmvRegistryImportSummary] = {}
        for configuration in configurations:
            registry = imports_by_legal_name.get(configuration.legal_name)
            if registry is None:
                registry = SmvRegistryPipeline(self._storage, self._client).run(
                    configuration.legal_name
                )
                imports_by_legal_name[configuration.legal_name] = registry
            known_at = max(
                registry.company.retrieved_at,
                registry.securities.retrieved_at,
            )
            universe = BvlRegistryUniverseService(
                self._storage,
                self._catalog,
                self._resolver,
            ).query(
                BvlRegistryUniverseRequest(
                    known_at=known_at,
                    asset_ids=(configuration.asset_id,),
                )
            )
            if len(universe.assets) != 1:
                raise StorageError("BVL registry refresh could not reselect its catalog asset")
            selected = universe.assets[0]
            allowed = {
                BvlRegistryStatus.ISSUER_VERIFIED,
                BvlRegistryStatus.SECURITY_VERIFIED,
            }
            if selected.status not in allowed:
                raise StorageError(
                    f"BVL registry identity validation failed for {configuration.asset_id}: "
                    f"{selected.status.value}"
                )
            completed.append(
                BvlRegistryAssetRefresh(
                    asset_id=configuration.asset_id,
                    mnemonic=configuration.mnemonic,
                    isin=configuration.isin,
                    status=selected.status,
                    registry=registry,
                )
            )
        return BvlRegistryRefreshSummary(
            requested_asset_ids=configured_ids,
            assets=tuple(completed),
            raw_records_created=sum(
                item.raw_records_created for item in imports_by_legal_name.values()
            ),
            raw_records_reused=sum(
                item.raw_records_reused for item in imports_by_legal_name.values()
            ),
            traceability_verified=True,
        )


def _asset_view(
    configuration: SmvBvlAssetConfiguration,
    issuer: SmvIssuerRegistryView | None,
) -> BvlRegistryAsset:
    if issuer is None:
        return BvlRegistryAsset(
            asset_id=configuration.asset_id,
            symbol=configuration.mnemonic,
            name=configuration.name,
            exchange=configuration.exchange,
            quote_currency=configuration.quote_currency,
            mnemonic=configuration.mnemonic,
            isin=configuration.isin,
            legal_name=configuration.legal_name,
            reported_security_code=configuration.reported_security_code,
            status=BvlRegistryStatus.NOT_IMPORTED,
            companies=(),
            matching_securities=(),
            company_raw_record_ids=(),
            securities_raw_record_ids=(),
            limitations=_limitations(configuration.reported_security_code),
        )
    mnemonic_matches = tuple(
        item for item in issuer.securities if item.mnemonic == configuration.mnemonic
    )
    matching = (
        tuple(
            item
            for item in mnemonic_matches
            if item.reported_security_code == configuration.reported_security_code
        )
        if configuration.reported_security_code is not None
        else mnemonic_matches
    )
    if not issuer.companies or not issuer.securities:
        status = BvlRegistryStatus.PARTIAL
    elif configuration.reported_security_code is None:
        status = BvlRegistryStatus.ISSUER_VERIFIED
    elif matching:
        status = BvlRegistryStatus.SECURITY_VERIFIED
    else:
        status = BvlRegistryStatus.SECURITY_MISMATCH
    return BvlRegistryAsset(
        asset_id=configuration.asset_id,
        symbol=configuration.mnemonic,
        name=configuration.name,
        exchange=configuration.exchange,
        quote_currency=configuration.quote_currency,
        mnemonic=configuration.mnemonic,
        isin=configuration.isin,
        legal_name=configuration.legal_name,
        reported_security_code=configuration.reported_security_code,
        status=status,
        companies=issuer.companies,
        matching_securities=matching,
        company_available_at=issuer.company_available_at,
        securities_available_at=issuer.securities_available_at,
        company_raw_record_ids=issuer.company_raw_record_ids,
        securities_raw_record_ids=issuer.securities_raw_record_ids,
        limitations=_limitations(configuration.reported_security_code),
    )


def _limitations(reported_security_code: str | None) -> tuple[str, ...]:
    limitations = [
        (
            "SMV Open Data has no historical availability timestamp; a revision becomes usable "
            "at its first verified local retrieval."
        ),
        (
            "The SMV field labelled CodigoISIN is an abbreviated provider code and is not treated "
            "as a complete ISIN."
        ),
    ]
    if reported_security_code is None:
        limitations.append(
            "The exact BVL listing is not exposed by the issuer's SMV security query; only issuer "
            "identity is corroborated by SMV."
        )
    return tuple(limitations)


__all__ = [
    "BvlRegistryAsset",
    "BvlRegistryAssetRefresh",
    "BvlRegistryRefreshRequest",
    "BvlRegistryRefreshService",
    "BvlRegistryRefreshSummary",
    "BvlRegistryStatus",
    "BvlRegistryUniverse",
    "BvlRegistryUniverseRequest",
    "BvlRegistryUniverseService",
]
