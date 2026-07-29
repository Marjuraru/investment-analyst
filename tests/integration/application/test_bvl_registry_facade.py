"""Application-facade coverage for the catalog-driven SMV/BVL registry batch."""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.peru_registry import (
    BvlRegistryRefreshRequest,
    BvlRegistryRefreshService,
    BvlRegistryStatus,
    BvlRegistryUniverseRequest,
)
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.catalog.models import (
    AssetCatalogDocument,
    CatalogAsset,
    ProviderBinding,
)
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.peru.smv_open_data import (
    SMV_COMPANIES_URL,
    SmvOpenDataClient,
)
from investment_analyst.storage import LocalStorage, StoragePaths

ASSET_ID = "equity:pe:bvl:cverdec1"
LEGAL_NAME = "SOCIEDAD MINERA CERRO VERDE S.A.A."
RETRIEVED_AT = datetime(2026, 7, 29, 12, tzinfo=UTC)


class FixtureFormTransport:
    """Serve both official registry pages and count network operations."""

    def __init__(self) -> None:
        self.get_calls = 0
        self.post_calls = 0

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        del headers, timeout_seconds, max_response_bytes
        self.get_calls += 1
        return HttpResponse(
            200,
            b"""
            <html><body>
              <input type="hidden" name="__VIEWSTATE" value="view" />
              <input type="hidden" name="__VIEWSTATEGENERATOR" value="generator" />
              <input type="hidden" name="__EVENTVALIDATION" value="validation" />
              <input id="body_txtRazonSocial" />
            </body></html>
            """,
            {"Content-Type": "text/html"},
            url,
        )

    def post_form(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        fields: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        del headers, timeout_seconds, max_response_bytes
        assert fields["ctl00$body$txtRazonSocial"] == LEGAL_NAME
        self.post_calls += 1
        body = _company_page() if url == SMV_COMPANIES_URL else _security_page()
        return HttpResponse(200, body, {"Content-Type": "text/html"}, url)


def _page(headers: tuple[str, ...], row: tuple[str, ...]) -> bytes:
    return (
        "<html><body>"
        f'<input id="body_txtRazonSocial" value="{LEGAL_NAME}" />'
        '<span id="body_lblEstado"></span>'
        '<table id="body_GridView1"><tr>'
        + "".join(f"<th>{header}</th>" for header in headers)
        + "</tr><tr>"
        + "".join(f"<td>{value}</td>" for value in row)
        + "</tr></table></body></html>"
    ).encode()


def _company_page() -> bytes:
    return _page(
        (
            "Domicilio",
            "FechaInscripcion",
            "GerenteGeneral",
            "PaginaWeb",
            "PresidenteDirectorio",
            "RazonSocial",
            "ResolucionInscripcion",
            "SeccionRegistro",
            "TipoSector",
        ),
        (
            "Arequipa, Perú",
            "10/11/2000",
            "GERENTE",
            "https://www.cerroverde.pe/",
            "PRESIDENTE",
            LEGAL_NAME,
            "Resolución 053-2000",
            "EMPRESAS EMISORAS",
            "MINERAS",
        ),
    )


def _security_page() -> bytes:
    return _page(
        (
            "CodigoISIN",
            "Cotizacion",
            "DenominacionValor",
            "FechaInscripcion",
            "FechaUltCot",
            "Moneda",
            "MontoInscrito",
            "NemonicoValor",
            "RazonSocial",
            "ResolucionInscripcion",
            "TipoValor",
        ),
        (
            "64650100",
            "69.40",
            LEGAL_NAME,
            "10/11/2000",
            "09/07/2026",
            "DOLARES",
            "990658513.96",
            "CVERDEC1",
            LEGAL_NAME,
            "Resolución 053-2000",
            "ACCIONES DE CAPITAL",
        ),
    )


def test_facade_refreshes_one_catalog_asset_and_queries_locally(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    with LocalStorage(StoragePaths.from_root(storage_root)):
        pass
    transport = FixtureFormTransport()
    application = InvestmentAnalystApplication(
        ApplicationRuntime.create_default(),
        form_transport_factory=lambda: transport,
    )
    location = StorageLocationRequest(legacy_root=storage_root)

    refreshed = application.refresh_bvl_registry(
        BvlRegistryRefreshRequest(asset_ids=(ASSET_ID,)),
        location=location,
    )
    effective_known_at = max(
        refreshed.assets[0].registry.company.retrieved_at,
        refreshed.assets[0].registry.securities.retrieved_at,
    )
    calls_after_refresh = (transport.get_calls, transport.post_calls)
    queried = application.query_bvl_registry(
        BvlRegistryUniverseRequest(
            known_at=effective_known_at,
            asset_ids=(ASSET_ID,),
        ),
        location=location,
    )

    assert refreshed.requested_asset_ids == (ASSET_ID,)
    assert refreshed.raw_records_created == 2
    assert refreshed.assets[0].status is BvlRegistryStatus.SECURITY_VERIFIED
    assert refreshed.assets[0].isin == "PEP646501002"
    assert len(queried.assets) == 1
    assert queried.assets[0].status is BvlRegistryStatus.SECURITY_VERIFIED
    assert queried.assets[0].matching_securities[0].mnemonic == "CVERDEC1"
    assert queried.assets[0].company_available_at <= effective_known_at
    assert queried.assets[0].securities_available_at <= effective_known_at
    assert (transport.get_calls, transport.post_calls) == calls_after_refresh

    with LocalStorage(StoragePaths.from_root(storage_root), read_only=True) as storage:
        assert len(storage.raw_records.list()) == 2
        assert storage.assets.list_all() == []
        assert storage.observations.list() == []
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []


def test_refresh_fetches_a_shared_issuer_once_for_multiple_listings(
    tmp_path: Path,
) -> None:
    default_catalog = AssetCatalogService.load_default()
    common = default_catalog.get(ASSET_ID)
    synthetic_listing = CatalogAsset(
        asset_id="equity:pe:bvl:cverde-preferred",
        symbol="CVERDEP",
        name="Sociedad Minera Cerro Verde S.A.A. Preferente",
        asset_class=common.asset_class,
        quote_currency=common.quote_currency,
        exchange=common.exchange,
        provider_symbols={
            "bvl_isin": "US0378331005",
            "bvl_mnemonic": "CVERDEP",
        },
        aliases=("Cerro Verde Preferente", "CVERDEP"),
        provider_bindings=(
            ProviderBinding(
                provider="bvl",
                namespace="isin",
                identifier="US0378331005",
                capabilities=("registry.exchange_listing",),
            ),
            ProviderBinding(
                provider="bvl",
                namespace="mnemonic",
                identifier="CVERDEP",
                capabilities=("registry.exchange_listing",),
            ),
            ProviderBinding(
                provider="smv",
                namespace="legal_name",
                identifier=LEGAL_NAME,
                is_unique=False,
                capabilities=("registry.issuer",),
            ),
        ),
    )
    catalog = AssetCatalogService(
        AssetCatalogDocument(
            catalog_version=1,
            assets=tuple(sorted((common, synthetic_listing), key=lambda item: item.asset_id)),
        )
    )
    resolver = ProviderAssetContextResolver(catalog)
    transport = FixtureFormTransport()

    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        summary = BvlRegistryRefreshService(
            storage,
            catalog,
            resolver,
            SmvOpenDataClient(transport, clock=lambda: RETRIEVED_AT),
        ).run(BvlRegistryRefreshRequest())

        assert (transport.get_calls, transport.post_calls) == (2, 2)
        assert summary.raw_records_created == 2
        assert summary.raw_records_reused == 0
        assert tuple(item.status for item in summary.assets) == (
            BvlRegistryStatus.ISSUER_VERIFIED,
            BvlRegistryStatus.SECURITY_VERIFIED,
        )
        assert len(storage.raw_records.list()) == 2
