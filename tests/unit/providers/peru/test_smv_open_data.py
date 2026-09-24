"""Offline contract tests for the official SMV Open Data portal client."""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from unittest.mock import patch

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
        result: bytes = b"",
        *,
        final_url: str = SMV_SECURITIES_URL,
        content_type: str = "text/html; charset=utf-8",
        truncated: bool = False,
        status_code: int = 200,
        initial_body: bytes | None = None,
        initial_status_code: int = 200,
        initial_url: str | None = None,
        initial_content_type: str = "text/html",
        initial_truncated: bool = False,
    ) -> None:
        self.result = result
        self.final_url = final_url
        self.content_type = content_type
        self.truncated = truncated
        self.status_code = status_code
        self.initial_body = initial_body if initial_body is not None else _initial_form()
        self.initial_status_code = initial_status_code
        self.initial_url = initial_url
        self.initial_content_type = initial_content_type
        self.initial_truncated = initial_truncated
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
            self.initial_status_code,
            self.initial_body,
            {"Content-Type": self.initial_content_type},
            self.initial_url or url,
            body_truncated=self.initial_truncated,
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
            self.status_code,
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


def test_smv_fetch_contract_reason_codes_are_static_and_stage_aware() -> None:
    # 1. GET response checks
    t = FakeFormTransport(initial_status_code=500)
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_get_http_status"
    assert exc_info.value.status_code == 500

    t = FakeFormTransport(initial_truncated=True)
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_get_truncated"

    t = FakeFormTransport(initial_url="https://other.domain.gov.pe/other")
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_get_redirect"

    t = FakeFormTransport(initial_content_type="application/json")
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_get_content_type"

    t = FakeFormTransport(initial_body=b"\xff\xfe\x00")
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_get_utf8"

    # 2. Form state checks
    with patch(
        "investment_analyst.providers.peru.smv_open_data.HTMLParser.feed",
        side_effect=Exception("parse error"),
    ):
        t = FakeFormTransport()
        with pytest.raises(SmvOpenDataError) as exc_info:
            _client(t).fetch_registered_company(LEGAL_NAME)
        assert exc_info.value.reason_code == "smv_form_html_invalid"

    t = FakeFormTransport(
        initial_body=b"<html><body><form><input id='body_txtRazonSocial' /></form></body></html>"
    )
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_form_state_missing"

    # 3. POST response checks
    t = FakeFormTransport(status_code=500, final_url=SMV_COMPANIES_URL)
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_post_http_status"
    assert exc_info.value.status_code == 500

    t = FakeFormTransport(truncated=True, final_url=SMV_COMPANIES_URL)
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_post_truncated"

    t = FakeFormTransport(final_url="https://other.domain.gov.pe/other")
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_post_redirect"

    t = FakeFormTransport(content_type="application/json", final_url=SMV_COMPANIES_URL)
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_post_content_type"

    t = FakeFormTransport(result=b"\xff\xfe\x00", final_url=SMV_COMPANIES_URL)
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_post_utf8"

    # 4. Result page checks
    t = FakeFormTransport(result=b"<html>ok</html>", final_url=SMV_COMPANIES_URL)
    feed_calls = [0]
    orig_feed = HTMLParser.feed

    def _feed_side_effect(self, data):  # type: ignore[no-untyped-def]
        feed_calls[0] += 1
        if feed_calls[0] > 1:
            raise Exception("result parse error")
        return orig_feed(self, data)

    with patch(
        "investment_analyst.providers.peru.smv_open_data.HTMLParser.feed",
        new=_feed_side_effect,
    ):
        with pytest.raises(SmvOpenDataError) as exc_info:
            _client(t).fetch_registered_company(LEGAL_NAME)
        assert exc_info.value.reason_code == "smv_result_html_invalid"

    page = (
        b"<html><body>"
        b'<input id="body_txtRazonSocial" value="OTHER CORP" />'
        b'<span id="body_lblEstado"></span>'
        b'<table id="body_GridView1"><tr><th>Domicilio</th></tr></table>'
        b"</body></html>"
    )
    t = FakeFormTransport(result=page, final_url=SMV_COMPANIES_URL)
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_query_echo_mismatch"

    page = (
        "<html><body>"
        f'<input id="body_txtRazonSocial" value="{LEGAL_NAME}" />'
        '<span id="body_lblEstado">No se encontraron registros</span>'
        "</body></html>"
    ).encode()
    t = FakeFormTransport(result=page, final_url=SMV_COMPANIES_URL)
    with pytest.raises(SmvOpenDataNotFoundError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_exact_name_not_found"

    page = (
        "<html><body>"
        f'<input id="body_txtRazonSocial" value="{LEGAL_NAME}" />'
        '<span id="body_lblEstado"></span>'
        "</body></html>"
    ).encode()
    t = FakeFormTransport(result=page, final_url=SMV_COMPANIES_URL)
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_result_table_missing"

    page = (
        "<html><body>"
        f'<input id="body_txtRazonSocial" value="{LEGAL_NAME}" />'
        '<table id="body_GridView1"><tr><th>Col</th></tr></table>'
        '<table id="body_GridView1"><tr><th>Col</th></tr></table>'
        "</body></html>"
    ).encode()
    t = FakeFormTransport(result=page, final_url=SMV_COMPANIES_URL)
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_result_table_ambiguous"

    page = (
        "<html><body>"
        f'<input id="body_txtRazonSocial" value="{LEGAL_NAME}" />'
        '<table id="body_GridView1"><tr><td>Data without TH</td></tr></table>'
        "</body></html>"
    ).encode()
    t = FakeFormTransport(result=page, final_url=SMV_COMPANIES_URL)
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_result_header_missing"

    t = FakeFormTransport(
        result=_result_page(("WrongHeader",), ("Val",)),
        final_url=SMV_COMPANIES_URL,
    )
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_company_headers_changed"

    t = FakeFormTransport(
        result=_result_page(("WrongHeader",), ("Val",)),
        final_url=SMV_SECURITIES_URL,
    )
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_securities(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_security_headers_changed"

    # 5. Row and field checks
    t = FakeFormTransport(
        result=_result_page(COMPANY_HEADERS, ("col1", "col2")),
        final_url=SMV_COMPANIES_URL,
    )
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_company_row_width"

    t = FakeFormTransport(
        result=_result_page(SECURITY_HEADERS, ("col1", "col2")),
        final_url=SMV_SECURITIES_URL,
    )
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_securities(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_security_row_width"

    comp_row = [
        "Calle Jacinto Ibañez No. 315, Arequipa",
        "10/11/2000",
        "GONZALES PAIHUA, TOMAS",
        "https://www.cerroverde.pe/",
        "STEVENS, ANTONIONI CORNELIUS",
        "WRONG NAME S.A.A.",
        "Gerencia Mercado y Emisores 053-2000-EF/94.50",
        "EMPRESAS EMISORAS",
        "MINERAS",
    ]
    t = FakeFormTransport(
        result=_result_page(COMPANY_HEADERS, tuple(comp_row)),
        final_url=SMV_COMPANIES_URL,
    )
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_company_name_mismatch"

    sec_row = [
        "64650100",
        "69.40",
        LEGAL_NAME,
        "10/11/2000",
        "09/07/2026",
        "DOLARES",
        "990658513.96",
        "CVERDEC1",
        "WRONG NAME S.A.A.",
        "Gerencia Mercado y Emisores 053-2000-EF/94.50",
        "ACCIONES DE CAPITAL",
    ]
    t = FakeFormTransport(
        result=_result_page(SECURITY_HEADERS, tuple(sec_row)),
        final_url=SMV_SECURITIES_URL,
    )
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_securities(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_security_name_mismatch"

    sec_row[8] = LEGAL_NAME
    sec_row[5] = "BITCOINS"
    t = FakeFormTransport(
        result=_result_page(SECURITY_HEADERS, tuple(sec_row)),
        final_url=SMV_SECURITIES_URL,
    )
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_securities(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_security_currency_unsupported"

    sec_row[5] = "DOLARES"
    sec_row[4] = ""
    t = FakeFormTransport(
        result=_result_page(SECURITY_HEADERS, tuple(sec_row)),
        final_url=SMV_SECURITIES_URL,
    )
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_securities(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_security_quote_incomplete"

    comp_row[5] = LEGAL_NAME
    comp_row[0] = ""
    t = FakeFormTransport(
        result=_result_page(COMPANY_HEADERS, tuple(comp_row)),
        final_url=SMV_COMPANIES_URL,
    )
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_required_field_empty"

    comp_row[0] = "Calle 123"
    comp_row[1] = ""
    t = FakeFormTransport(
        result=_result_page(COMPANY_HEADERS, tuple(comp_row)),
        final_url=SMV_COMPANIES_URL,
    )
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_date_field_empty"

    comp_row[1] = "not-a-date"
    t = FakeFormTransport(
        result=_result_page(COMPANY_HEADERS, tuple(comp_row)),
        final_url=SMV_COMPANIES_URL,
    )
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_company(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_date_field_invalid"

    sec_row[4] = "09/07/2026"
    sec_row[6] = ""
    t = FakeFormTransport(
        result=_result_page(SECURITY_HEADERS, tuple(sec_row)),
        final_url=SMV_SECURITIES_URL,
    )
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_securities(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_decimal_field_empty"

    sec_row[6] = "invalid-decimal"
    t = FakeFormTransport(
        result=_result_page(SECURITY_HEADERS, tuple(sec_row)),
        final_url=SMV_SECURITIES_URL,
    )
    with pytest.raises(SmvOpenDataError) as exc_info:
        _client(t).fetch_registered_securities(LEGAL_NAME)
    assert exc_info.value.reason_code == "smv_decimal_field_invalid"
