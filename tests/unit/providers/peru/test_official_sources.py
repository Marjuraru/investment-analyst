"""Offline tests for the bounded SMV/BVL phase-zero source probe."""

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.peru.official_sources import (
    MAX_INSPECTED_BYTES,
    OFFICIAL_SOURCE_DEFINITIONS,
    PeruOfficialSource,
    PeruOfficialSourceProbe,
    PeruOfficialSourceProbeError,
    SourceContractStatus,
)

CHECKED_AT = datetime(2026, 7, 25, 18, 0, tzinfo=UTC)


class FakeTransport:
    """Return deterministic prefixes while recording bounded requests."""

    def __init__(
        self,
        *,
        missing_marker_source: PeruOfficialSource | None = None,
        redirect_source: PeruOfficialSource | None = None,
        status_code: int = 200,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.missing_marker_source = missing_marker_source
        self.redirect_source = redirect_source
        self.status_code = status_code
        self.content_type = content_type
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
        definition = next(item for item in OFFICIAL_SOURCE_DEFINITIONS if item.url == url)
        body = b" ".join(definition.required_markers)
        if definition.source == self.missing_marker_source:
            body = b"unexpected page"
        final_url = (
            "https://example.test/redirected" if definition.source == self.redirect_source else url
        )
        return HttpResponse(
            status_code=self.status_code,
            body=body,
            headers={"Content-Type": self.content_type},
            url=final_url,
            body_truncated=definition.source is PeruOfficialSource.BVL_DAILY_EQUITY_BULLETIN,
        )


def test_probe_checks_complete_matrix_with_bounded_reads_and_no_persistence() -> None:
    transport = FakeTransport()

    report = PeruOfficialSourceProbe(transport, clock=lambda: CHECKED_AT).run()

    assert report.schema_version == "peru-official-source-probe-v1"
    assert report.checked_at == CHECKED_AT
    assert report.all_sources_available is True
    assert report.persistence_performed is False
    assert tuple(result.source for result in report.results) == tuple(
        definition.source for definition in OFFICIAL_SOURCE_DEFINITIONS
    )
    assert report.results[0].contract_status is SourceContractStatus.OPEN_DATA_ODBL
    assert (
        report.results[1].contract_status
        is SourceContractStatus.PUBLIC_DOCUMENT_TERMS_REVIEW_REQUIRED
    )
    assert all(limit == MAX_INSPECTED_BYTES for _, limit in transport.requests)
    assert all(len(result.inspected_prefix_sha256) == 64 for result in report.results)


def test_probe_rejects_a_source_that_loses_its_contract_markers() -> None:
    probe = PeruOfficialSourceProbe(
        FakeTransport(missing_marker_source=PeruOfficialSource.BVL_DAILY_BULLETIN_NOTES),
        clock=lambda: CHECKED_AT,
    )

    with pytest.raises(PeruOfficialSourceProbeError, match="contract markers"):
        probe.run()


def test_probe_rejects_redirect_outside_the_official_host() -> None:
    probe = PeruOfficialSourceProbe(
        FakeTransport(redirect_source=PeruOfficialSource.SMV_REGISTERED_SECURITIES),
        clock=lambda: CHECKED_AT,
    )

    with pytest.raises(PeruOfficialSourceProbeError, match="official HTTPS host"):
        probe.run()


def test_probe_rejects_non_success_status_without_persisting_partial_report() -> None:
    probe = PeruOfficialSourceProbe(FakeTransport(status_code=503), clock=lambda: CHECKED_AT)

    with pytest.raises(PeruOfficialSourceProbeError, match="HTTP 503"):
        probe.run()


def test_probe_rejects_unexpected_content_type() -> None:
    probe = PeruOfficialSourceProbe(
        FakeTransport(content_type="application/json"),
        clock=lambda: CHECKED_AT,
    )

    with pytest.raises(PeruOfficialSourceProbeError, match="content type"):
        probe.run()
