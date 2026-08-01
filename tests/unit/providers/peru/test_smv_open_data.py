"""Offline contract tests for the official SMV Open Data portal client."""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.peru.smv_open_data import (
    MAX_SMV_PORTAL_BYTES,
    SMV_COMPANIES_URL,
    SMV_SECURITIES_URL,
    SmvOpenDataClient,
    SmvOpenDataDataset,
    SmvOpenDataError,
    SmvOpenDataNotFoundError,
    SmvRegisteredSecurity,
    reported_code_matches_isin,
    validate_isin,
)

RETRIEVED_AT = datetime(2026, 7, 29, 5, 30, tzinfo=UTC)
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


class FakeFormTransport:
    """Serve one initial form and one submitted result while recording both calls."""

    def __init__(
        self,
        result: bytes,
        *,
        final_url: str,
        content_type: str = "text/html; charset=utf-8",
        truncated: bool = False,
    ) -> None:
        self.result = result
        self.final_url = final_url
        self.content_type = content_type
        self.truncated = truncated
        self.get_calls: list[tuple[str, int | None]] = []
        self.post_calls: list[tuple[str, dict[str, str], int | None]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        del headers, timeout_seconds
        self.get_calls.append((url, max_response_bytes))
        return HttpResponse(
            200,
            _initial_form(),
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
        del headers, timeout_seconds
        self.post_calls.append((url, dict(fields), max_response_bytes))
        return HttpResponse(
            200,
            self.result,
            {"Content-Type": self.content_type},
            self.final_url,
            body_truncated=self.truncated,
        )


def _initial_form() -> bytes:
    return b"""
    <html><body><form>
      <input type="hidden" name="__VIEWSTATE" value="view/state+" />
      <input type="hidden" name="__VIEWSTATEGENERATOR" value="generator" />
      <input type="hidden" name="__EVENTVALIDATION" value="event/validation=" />
      <input id="body_txtRazonSocial" />
    </form></body></html>
    """


def _result_page(headers: tuple[str, ...], *rows: tuple[str, ...]) -> bytes:
    header_html = "".join(f"<th>{header}</th>" for header in headers)
    rows_html = "".join(
        "<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>" for row in rows
    )
    return (
        "<html><body>"
        f'<input id="body_txtRazonSocial" value="{LEGAL_NAME}" />'
        '<span id="body_lblEstado"></span>'
        f'<table id="body_GridView1"><tr>{header_html}</tr>{rows_html}</table>'
        "</body></html>"
    ).encode()


def _company_page() -> bytes:
    return _result_page(
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
    )


def _security_page(*, code: str = "64650100", currency: str = "DOLARES") -> bytes:
    return _result_page(
        SECURITY_HEADERS,
        (
            code,
            "69.40",
            LEGAL_NAME,
            "10/11/2000",
            "09/07/2026",
            currency,
            "990658513.96",
            "CVERDEC1",
            LEGAL_NAME,
            "Gerencia Mercado y Emisores 053-2000-EF/94.50",
            "ACCIONES DE CAPITAL",
        ),
    )


def _client(transport: FakeFormTransport) -> SmvOpenDataClient:
    return SmvOpenDataClient(transport, clock=lambda: RETRIEVED_AT)


def test_fetches_company_through_exact_https_form_contract() -> None:
    transport = FakeFormTransport(_company_page(), final_url=SMV_COMPANIES_URL)

    fetch = _client(transport).fetch_registered_company(LEGAL_NAME)

    assert fetch.snapshot.dataset is SmvOpenDataDataset.REGISTERED_COMPANIES
    assert fetch.snapshot.query_legal_name == LEGAL_NAME
    assert fetch.retrieved_at == RETRIEVED_AT
    assert len(fetch.body_sha256) == 64
    company = fetch.snapshot.companies[0]
    assert company.registration_date == date(2000, 11, 10)
    assert company.sector == "MINERAS"
    assert company.website == "https://www.cerroverde.pe/"
    assert transport.get_calls == [(SMV_COMPANIES_URL, MAX_SMV_PORTAL_BYTES)]
    assert len(transport.post_calls) == 1
    _, fields, limit = transport.post_calls[0]
    assert fields["ctl00$body$txtRazonSocial"] == LEGAL_NAME
    assert fields["ctl00$body$TipoConsulta"] == "rbRazSocial"
    assert fields["__VIEWSTATE"] == "view/state+"
    assert limit == MAX_SMV_PORTAL_BYTES


def test_fetches_security_with_exact_decimal_and_abbreviated_code() -> None:
    transport = FakeFormTransport(_security_page(), final_url=SMV_SECURITIES_URL)

    fetch = _client(transport).fetch_registered_securities(LEGAL_NAME)

    security = fetch.snapshot.securities[0]
    assert fetch.snapshot.dataset is SmvOpenDataDataset.REGISTERED_SECURITIES
    assert security.mnemonic == "CVERDEC1"
    assert security.reported_security_code == "64650100"
    assert security.currency == "USD"
    assert security.last_quote == Decimal("69.40")
    assert security.registered_amount == Decimal("990658513.96")
    assert security.last_quote_date == date(2026, 7, 9)


@pytest.mark.parametrize(
    "isin",
    [
        "PEP646501002",
        "PEP622005002",
        "PEP648014202",
        "PEP779301006",
        "US2044481040",
        "US84265V1052",
    ],
)
def test_validates_corroborated_initial_isins(isin: str) -> None:
    assert validate_isin(isin) == isin


def test_reported_code_is_only_checked_against_a_corroborated_isin() -> None:
    assert reported_code_matches_isin("64650100", "PEP646501002") is True
    assert reported_code_matches_isin("4265V105", "US84265V1052") is True
    assert reported_code_matches_isin("77930100", "PEP646501002") is False
    with pytest.raises(SmvOpenDataError, match="check digit"):
        validate_isin("PEP646501003")


def test_rejects_float_values_at_the_typed_boundary() -> None:
    with pytest.raises(ValidationError, match="must not use float"):
        SmvRegisteredSecurity(
            legal_name=LEGAL_NAME,
            security_name=LEGAL_NAME,
            mnemonic="CVERDEC1",
            reported_security_code="64650100",
            security_type="ACCIONES DE CAPITAL",
            registration_date=date(2000, 11, 10),
            registration_resolution="resolution",
            currency_raw="DOLARES",
            currency="USD",
            registered_amount=990658513.96,
            last_quote=Decimal("69.40"),
            last_quote_date=date(2026, 7, 9),
        )


def test_missing_result_is_explicit_and_does_not_create_empty_snapshot() -> None:
    result = (
        "<html><body>"
        f'<input id="body_txtRazonSocial" value="{LEGAL_NAME}" />'
        '<span id="body_lblEstado">Empresa no registrada</span>'
        "</body></html>"
    ).encode()
    transport = FakeFormTransport(result, final_url=SMV_SECURITIES_URL)

    with pytest.raises(SmvOpenDataNotFoundError, match="Empresa no registrada"):
        _client(transport).fetch_registered_securities(LEGAL_NAME)


@pytest.mark.parametrize(
    ("result", "final_url", "content_type", "truncated", "message"),
    [
        (
            _result_page(("Changed",), ("value",)),
            SMV_SECURITIES_URL,
            "text/html",
            False,
            "headers changed",
        ),
        (
            _security_page(),
            "https://example.test/redirect",
            "text/html",
            False,
            "exact official HTTPS",
        ),
        (
            _security_page(),
            SMV_SECURITIES_URL,
            "application/json",
            False,
            "content type",
        ),
        (
            _security_page(),
            SMV_SECURITIES_URL,
            "text/html",
            True,
            "safety limit",
        ),
    ],
)
def test_rejects_contract_drift_redirects_content_type_and_truncation(
    result: bytes,
    final_url: str,
    content_type: str,
    truncated: bool,
    message: str,
) -> None:
    transport = FakeFormTransport(
        result,
        final_url=final_url,
        content_type=content_type,
        truncated=truncated,
    )

    with pytest.raises(SmvOpenDataError, match=message):
        _client(transport).fetch_registered_securities(LEGAL_NAME)
