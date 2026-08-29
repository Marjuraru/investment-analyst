import json
from datetime import UTC, datetime

import pytest

from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.institutional_holdings.sec_manager_submissions import (
    MANAGER_SUBMISSIONS_SCHEMA_VERSION,
    SecManagerSubmissionsClient,
    SecManagerSubmissionsError,
)


class _Transport:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode()

    def get(self, url, **kwargs):
        del kwargs
        return HttpResponse(status_code=200, body=self._body, headers={}, url=url)


def _payload() -> dict[str, object]:
    return {
        "cik": 1067983,
        "name": "Manager LLC",
        "filings": {"recent": {"accessionNumber": []}},
    }


def test_fetch_persists_no_asset_and_deterministic_source() -> None:
    client = SecManagerSubmissionsClient(
        _Transport(_payload()),
        SecEdgarIdentity("Analyst analyst@example.com"),
        clock=lambda: datetime(2025, 2, 15, tzinfo=UTC),
    )

    record = client.fetch("1067983")

    assert record.asset_id is None
    assert record.source.source_id == "sec-edgar:manager:0001067983:submissions"
    assert record.schema_version == MANAGER_SUBMISSIONS_SCHEMA_VERSION
    assert record.available_at == record.received_at


def test_fetch_rejects_conflicting_cik_before_persistence() -> None:
    client = SecManagerSubmissionsClient(
        _Transport(_payload() | {"cik": 320193}),
        SecEdgarIdentity("Analyst analyst@example.com"),
    )

    with pytest.raises(SecManagerSubmissionsError, match="identity"):
        client.fetch("1067983")
