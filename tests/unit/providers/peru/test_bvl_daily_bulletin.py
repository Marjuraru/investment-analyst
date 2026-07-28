"""Offline contract tests for the official BVL daily bulletin reader."""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.peru.bvl_daily_bulletin import (
    MAX_BULLETIN_BYTES,
    BvlCurrency,
    BvlDailyBulletinError,
    BvlDailyBulletinReader,
    BvlDailyQuote,
)

RETRIEVED_AT = datetime(2026, 7, 28, 17, 30, tzinfo=UTC)
BULLETIN_URL = "https://documents.bvl.com.pe/pubdif/boldia/stockq.htm"
MISSING = "......"


class FakeTransport:
    """Return a deterministic bulletin while recording the bounded request."""

    def __init__(
        self,
        body: bytes,
        *,
        status_code: int = 200,
        content_type: str = "text/html; charset=utf-8",
        final_url: str = BULLETIN_URL,
        body_truncated: bool = False,
    ) -> None:
        self.body = body
        self.status_code = status_code
        self.content_type = content_type
        self.final_url = final_url
        self.body_truncated = body_truncated
        self.requests: list[tuple[str, int | None]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        del headers, timeout_seconds
        self.requests.append((url, max_response_bytes))
        return HttpResponse(
            status_code=self.status_code,
            body=self.body,
            headers={"Content-Type": self.content_type},
            url=self.final_url,
            body_truncated=self.body_truncated,
        )


def _row(*values: str) -> str:
    assert len(values) == 17
    return "<tr>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>"


def _quote_row(
    mnemonic: str = "CVERDEC1",
    currency: str = "US$",
    *,
    previous_close: str = "68.79",
    previous_date: str = "24/07/26",
) -> str:
    return _row(
        mnemonic,
        currency,
        previous_close,
        previous_date,
        "69.00",
        "69.00",
        "69.00",
        "69.00",
        "0.31%",
        "68.00",
        "69.00",
        "68.99",
        "690",
        "47,600.00",
        "29",
        "100 %",
        "50.23%",
    )


def _missing_quote_row(mnemonic: str = "VOLCABC1 (ver 4)") -> str:
    return _row(
        mnemonic,
        "S/",
        MISSING,
        MISSING,
        MISSING,
        MISSING,
        MISSING,
        MISSING,
        MISSING,
        "0.79",
        "0.81",
        MISSING,
        MISSING,
        MISSING,
        MISSING,
        "30 %",
        "-7.43%",
    )


def _bulletin(*rows: str, publication: str = "27 de julio de 2026") -> bytes:
    header = """
    <thead>
      <tr>
        <td rowspan="3">Valores</td><td rowspan="3">Mo ne da</td>
        <td colspan="2">Anterior</td><td colspan="11">Negociación del Día</td>
        <td rowspan="3">Frec. de Cotiz.</td><td rowspan="3">Rendim. Bursátil 2026</td>
      </tr>
      <tr>
        <td rowspan="2">Cotización Anterior</td><td></td>
        <td colspan="4">Cotizaciones</td><td rowspan="2">Variac. Día %</td>
        <td colspan="2">Propuestas</td><td rowspan="2">Precio promedio</td>
        <td rowspan="2">N° de Accs Negociadas</td>
        <td rowspan="2">Monto Efectivo Negociado</td><td rowspan="2">N° de Oper.</td>
      </tr>
      <tr>
        <td>Fecha</td><td>Apertura</td><td>Cierre</td><td>Máxima</td><td>Mínima</td>
        <td>Compra</td><td>Venta</td>
      </tr>
    </thead>
    """
    document = (
        f"<html><body><b>Actualizado al {publication}</b>"
        f'<table class="Tablas">{header}<tbody>{"".join(rows)}</tbody></table>'
        "</body></html>"
    )
    return document.encode()


def _reader(transport: FakeTransport) -> BvlDailyBulletinReader:
    return BvlDailyBulletinReader(transport, clock=lambda: RETRIEVED_AT)


def test_reader_extracts_exact_decimals_currency_metadata_and_requested_order() -> None:
    transport = FakeTransport(_bulletin(_quote_row(), _missing_quote_row()))

    report = _reader(transport).inspect(("volcabc1", "CVERDEC1", "NOEXISTE"))

    assert report.schema_version == "bvl-daily-equity-bulletin-inspection-v1"
    assert report.bulletin_date == date(2026, 7, 27)
    assert report.retrieved_at == RETRIEVED_AT
    assert report.persistence_performed is False
    assert report.detected_quote_rows == 2
    assert report.requested_mnemonics == ("VOLCABC1", "CVERDEC1", "NOEXISTE")
    assert tuple(quote.mnemonic for quote in report.quotes) == ("VOLCABC1", "CVERDEC1")
    assert report.missing_mnemonics == ("NOEXISTE",)
    assert report.quotes[0].currency is BvlCurrency.PEN
    assert report.quotes[0].bulletin_note_reference == "ver 4"
    assert report.quotes[0].close is None
    assert report.quotes[0].bid == Decimal("0.79")
    assert report.quotes[0].annual_return_percent == Decimal("-7.43")
    assert report.quotes[1].currency is BvlCurrency.USD
    assert report.quotes[1].previous_close == Decimal("68.79")
    assert report.quotes[1].traded_amount == Decimal("47600.00")
    assert report.quotes[1].operation_count == 29
    assert report.quotes[1].quote_frequency_percent == Decimal("100")
    assert len(report.document_sha256) == 64
    assert transport.requests == [(BULLETIN_URL, MAX_BULLETIN_BYTES)]


def test_reader_rejects_duplicate_rows_for_a_requested_mnemonic() -> None:
    reader = _reader(FakeTransport(_bulletin(_quote_row(), _quote_row())))

    with pytest.raises(BvlDailyBulletinError, match="ambiguous duplicate"):
        reader.inspect(("CVERDEC1",))


def test_reader_rejects_an_unsupported_currency_instead_of_reporting_missing() -> None:
    reader = _reader(FakeTransport(_bulletin(_quote_row(currency="EUR"))))

    with pytest.raises(BvlDailyBulletinError, match="unsupported currency"):
        reader.inspect(("CVERDEC1",))


def test_reader_rejects_changed_quotation_headers() -> None:
    body = _bulletin(_quote_row()).replace(b">Cierre<", b">Ultimo<")

    with pytest.raises(BvlDailyBulletinError, match="exactly one recognized"):
        _reader(FakeTransport(body)).inspect(("CVERDEC1",))


def test_reader_rejects_a_truncated_document_before_parsing() -> None:
    transport = FakeTransport(_bulletin(_quote_row()), body_truncated=True)

    with pytest.raises(BvlDailyBulletinError, match="safety limit"):
        _reader(transport).inspect(("CVERDEC1",))


def test_reader_rejects_redirect_to_a_different_official_path() -> None:
    transport = FakeTransport(
        _bulletin(_quote_row()),
        final_url="https://documents.bvl.com.pe/pubdif/boldia/other.htm",
    )

    with pytest.raises(BvlDailyBulletinError, match="exact official HTTPS path"):
        _reader(transport).inspect(("CVERDEC1",))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_bulletin(_quote_row(), publication="31 de febrero de 2026"), "date is invalid"),
        (_bulletin(_quote_row(previous_close="not-a-number")), "invalid previous close"),
        (
            _bulletin(_quote_row(previous_close=MISSING, previous_date="24/07/26")),
            "violates financial consistency",
        ),
        (
            _bulletin(_quote_row(previous_date="28/07/26")),
            "violates report consistency",
        ),
    ],
)
def test_reader_rejects_invalid_dates_numbers_and_incomplete_previous_close(
    body: bytes,
    message: str,
) -> None:
    with pytest.raises(BvlDailyBulletinError, match=message):
        _reader(FakeTransport(body)).inspect(("CVERDEC1",))


def test_reader_validates_requested_mnemonics_before_network_access() -> None:
    transport = FakeTransport(_bulletin(_quote_row()))
    reader = _reader(transport)

    with pytest.raises(BvlDailyBulletinError, match="must be unique"):
        reader.inspect(("BVN", "bvn"))
    with pytest.raises(BvlDailyBulletinError, match="mnemonic is invalid"):
        reader.inspect(("BVN (ver 1)",))

    assert transport.requests == []


def test_quote_model_rejects_float_financial_values() -> None:
    with pytest.raises(ValidationError, match="must use Decimal"):
        BvlDailyQuote(
            mnemonic="CVERDEC1",
            currency=BvlCurrency.USD,
            close=69.0,
        )
