"""Integration coverage for append-only SMV registry persistence and reconstruction."""

from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from investment_analyst.application.peru_registry import (
    BvlRegistryStatus,
    BvlRegistryUniverseRequest,
    BvlRegistryUniverseService,
)
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.providers.peru.smv_open_data import (
    SMV_COMPANIES_URL,
    SMV_SECURITIES_URL,
    SmvOpenDataDataset,
    SmvOpenDataError,
    SmvOpenDataFetch,
    parse_smv_portal_snapshot,
)
from investment_analyst.providers.peru.smv_pipeline import SmvRegistryPipeline
from investment_analyst.providers.peru.smv_point_in_time import (
    AmbiguousSmvRevisionError,
    SmvPointInTimeQuery,
    SmvPointInTimeService,
)
from investment_analyst.providers.peru.smv_raw_records import (
    SMV_COMPANIES_SOURCE_ID,
    SMV_SECURITIES_SOURCE_ID,
)
from investment_analyst.storage import LocalStorage, StoragePaths

LEGAL_NAME = "SOCIEDAD MINERA CERRO VERDE S.A.A."
COMPANY_HEADERS = (
    "Domicilio",
    "FechaInscripcion",
    "GerenteGeneral",
    "PaginaWeb",
    "PresidenteDirectorio",
    "RazonSocial",
    "ResolucionInscripcion",
    "SeccionRegistro",
    "TipoSector",
)
SECURITY_HEADERS = (
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
)


def _page(
    headers: tuple[str, ...],
    row: tuple[str, ...],
    *,
    legal_name: str = LEGAL_NAME,
    nonce: str = "one",
) -> bytes:
    return (
        "<html><body>"
        f'<input type="hidden" name="__VIEWSTATE" value="{nonce}" />'
        f'<input id="body_txtRazonSocial" value="{legal_name}" />'
        '<span id="body_lblEstado"></span>'
        '<table id="body_GridView1"><tr>'
        + "".join(f"<th>{header}</th>" for header in headers)
        + "</tr><tr>"
        + "".join(f"<td>{value}</td>" for value in row)
        + "</tr></table></body></html>"
    ).encode()


def _company_body(*, nonce: str = "one") -> bytes:
    return _page(
        COMPANY_HEADERS,
        (
            "Calle Jacinto Ibañez No. 315, Arequipa",
            "10/11/2000",
            "GONZALES PAIHUA, TOMAS",
            "https://www.cerroverde.pe/",
            "STEVENS, ANTONIONI CORNELIUS",
            LEGAL_NAME,
            "Gerencia Mercado y Emisores 053-2000-EF/94.50",
            "EMPRESAS EMISORAS",
            "MINERAS",
        ),
        nonce=nonce,
    )


def _security_body(*, quote: str, nonce: str = "one") -> bytes:
    return _page(
        SECURITY_HEADERS,
        (
            "64650100",
            quote,
            LEGAL_NAME,
            "10/11/2000",
            "09/07/2026",
            "DOLARES",
            "990658513.96",
            "CVERDEC1",
            LEGAL_NAME,
            "Gerencia Mercado y Emisores 053-2000-EF/94.50",
            "ACCIONES DE CAPITAL",
        ),
        nonce=nonce,
    )


def _fetch(
    dataset: SmvOpenDataDataset,
    body: bytes,
    retrieved_at: datetime,
) -> SmvOpenDataFetch:
    url = (
        SMV_COMPANIES_URL
        if dataset is SmvOpenDataDataset.REGISTERED_COMPANIES
        else SMV_SECURITIES_URL
    )
    snapshot = parse_smv_portal_snapshot(
        body.decode(),
        dataset=dataset,
        query_legal_name=LEGAL_NAME,
    )
    return SmvOpenDataFetch(
        snapshot=snapshot,
        requested_url=url,
        final_url=url,
        retrieved_at=retrieved_at,
        response_body=body,
        body_sha256=sha256(body).hexdigest(),
        content_type="text/html",
    )


class FixtureClient:
    """Return a complete exact-name issuer snapshot."""

    def __init__(
        self,
        *,
        retrieved_at: datetime,
        quote: str,
        nonce: str = "one",
        fail_securities: bool = False,
    ) -> None:
        self.retrieved_at = retrieved_at
        self.quote = quote
        self.nonce = nonce
        self.fail_securities = fail_securities

    def fetch_registered_company(self, legal_name: str) -> SmvOpenDataFetch:
        assert legal_name == LEGAL_NAME
        return _fetch(
            SmvOpenDataDataset.REGISTERED_COMPANIES,
            _company_body(nonce=self.nonce),
            self.retrieved_at,
        )

    def fetch_registered_securities(self, legal_name: str) -> SmvOpenDataFetch:
        assert legal_name == LEGAL_NAME
        if self.fail_securities:
            raise SmvOpenDataError("fixture security failure")
        return _fetch(
            SmvOpenDataDataset.REGISTERED_SECURITIES,
            _security_body(quote=self.quote, nonce=self.nonce),
            self.retrieved_at,
        )


def _query(storage: LocalStorage, known_at: datetime):
    return SmvPointInTimeService(storage).query(SmvPointInTimeQuery(known_at=known_at))


def test_pipeline_is_idempotent_revisioned_isolated_and_point_in_time(
    tmp_path: Path,
) -> None:
    first_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    second_at = datetime(2026, 7, 29, 13, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        first = SmvRegistryPipeline(
            storage,
            FixtureClient(retrieved_at=first_at, quote="69.40"),
        ).run(LEGAL_NAME)
        equivalent = SmvRegistryPipeline(
            storage,
            FixtureClient(
                retrieved_at=second_at,
                quote="69.40",
                nonce="different-form-state",
            ),
        ).run(LEGAL_NAME)
        revised = SmvRegistryPipeline(
            storage,
            FixtureClient(retrieved_at=second_at, quote="70.15"),
        ).run(LEGAL_NAME)

        assert first.raw_records_created == 2
        assert first.raw_records_reused == 0
        assert equivalent.raw_records_created == 0
        assert equivalent.raw_records_reused == 2
        assert revised.raw_records_created == 1
        assert revised.raw_records_reused == 1
        assert len(storage.raw_records.list(source_id=SMV_COMPANIES_SOURCE_ID)) == 1
        assert len(storage.raw_records.list(source_id=SMV_SECURITIES_SOURCE_ID)) == 2
        assert storage.assets.list_all() == []
        assert storage.observations.list() == []
        assert storage.metric_definitions.list_all() == []
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []

        before = _query(storage, datetime(2026, 7, 29, 11, 59, tzinfo=UTC))
        first_view = _query(storage, datetime(2026, 7, 29, 12, 30, tzinfo=UTC))
        revised_view = _query(storage, second_at)

        assert before.issuers == ()
        assert first_view.issuers[0].securities[0].last_quote == Decimal("69.40")
        assert revised_view.issuers[0].securities[0].last_quote == Decimal("70.15")
        assert revised_view.revisions_superseded == 1
        assert revised_view.traceability_verified is True

        runtime = ApplicationRuntime.create_default()
        universe = BvlRegistryUniverseService(
            storage,
            runtime.catalog,
            runtime.provider_resolver,
        ).query(BvlRegistryUniverseRequest(known_at=second_at))
        by_asset = {item.asset_id: item for item in universe.assets}
        assert len(universe.assets) == 6
        assert by_asset["equity:pe:bvl:cverdec1"].status is BvlRegistryStatus.SECURITY_VERIFIED
        assert by_asset["equity:pe:bvl:cverdec1"].isin == "PEP646501002"
        assert by_asset["equity:pe:bvl:bvn"].status is BvlRegistryStatus.NOT_IMPORTED


def test_later_stage_failure_preserves_successful_company_snapshot(tmp_path: Path) -> None:
    retrieved_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        with pytest.raises(SmvOpenDataError, match="fixture security failure"):
            SmvRegistryPipeline(
                storage,
                FixtureClient(
                    retrieved_at=retrieved_at,
                    quote="69.40",
                    fail_securities=True,
                ),
            ).run(LEGAL_NAME)

        assert len(storage.raw_records.list(source_id=SMV_COMPANIES_SOURCE_ID)) == 1
        assert storage.raw_records.list(source_id=SMV_SECURITIES_SOURCE_ID) == []
        partial = _query(storage, retrieved_at)
        assert len(partial.issuers) == 1
        assert len(partial.issuers[0].companies) == 1
        assert partial.issuers[0].securities == ()


def test_conflicting_same_availability_revisions_fail_explicitly(tmp_path: Path) -> None:
    retrieved_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        SmvRegistryPipeline(
            storage,
            FixtureClient(retrieved_at=retrieved_at, quote="69.40"),
        ).run(LEGAL_NAME)
        SmvRegistryPipeline(
            storage,
            FixtureClient(retrieved_at=retrieved_at, quote="70.15"),
        ).run(LEGAL_NAME)

        with pytest.raises(AmbiguousSmvRevisionError, match="conflicting registered_securities"):
            _query(storage, retrieved_at)
