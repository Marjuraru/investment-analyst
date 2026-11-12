"""Real-stack HTTP tests for the ephemeral investor overview endpoint."""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest

from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.frontend.local_web import (
    AaplLocalController,
    AaplLocalHttpServer,
    AaplLocalWebApplication,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.service import WorkspaceService

KNOWN_AT = "2026-08-29T12:00:00Z"
AAPL = "equity:us:aapl"
AMD = "equity:us:amd"


def _workspace(tmp_path: Path) -> tuple[Path, StoragePaths]:
    initialized = WorkspaceService().initialize(tmp_path / "workspace", format_version=2)
    paths = StoragePaths.from_workspace_root(
        initialized.paths.root,
        format_version=2,
        workspace_id=initialized.manifest.workspace_id,
    )
    return initialized.paths.root, paths


def _web(workspace: Path) -> AaplLocalWebApplication:
    from investment_analyst.application.aapl_daily_runner import AaplDailyRunner

    runtime = ApplicationRuntime.create_default()
    application = InvestmentAnalystApplication(runtime)
    controller = AaplLocalController(
        AaplDailyRunner(application, runtime.workspace_service),
        application,
        workspace=workspace,
        alpaca_credentials=AlpacaCredentials(api_key="test-key", secret_key="test-secret"),
        sec_identity=SecEdgarIdentity("Investment Analyst tests@example.com"),
    )
    return AaplLocalWebApplication(controller, None)


@contextmanager
def _server(web: AaplLocalWebApplication) -> Iterator[str]:
    server = AaplLocalHttpServer(("127.0.0.1", 0), web)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(root: str, parameters: list[tuple[str, str]]) -> tuple[int, dict[str, object]]:
    request = Request(f"{root}/api/v1/investor-overview?{urlencode(parameters)}")
    try:
        response = urlopen(request, timeout=20)
    except HTTPError as error:
        return error.code, json.loads(error.read())
    with response:
        return response.status, json.loads(response.read())


def _query(*asset_ids: str, known_at: str = KNOWN_AT) -> list[tuple[str, str]]:
    return [("known_at", known_at), *(("asset_id", item) for item in asset_ids)]


def test_endpoint_returns_independent_domains_in_stable_asset_order(tmp_path: Path) -> None:
    workspace, _ = _workspace(tmp_path)
    with _server(_web(workspace)) as root:
        status, payload = _get(root, _query("etf:us:spy", AAPL, "crypto:btc-usd"))

    assert status == 200
    assert payload["schema_version"] == "investor-overview-v1"
    assets = cast(list[dict[str, object]], payload["assets"])
    assert [item["asset_id"] for item in assets] == ["crypto:btc-usd", AAPL, "etf:us:spy"]
    windows = cast(dict[str, object], payload["windows"])
    assert windows["market_end"] == "2026-08-28"
    assert windows["fundamental_frequency"] == "annual"
    for item in assets:
        assert set(item) >= {"market", "fundamental", "valuation", "limitations"}
        serialized = json.dumps(item)
        assert "ranking" not in serialized and "recommend" not in serialized
    spy = assets[2]
    assert cast(dict[str, object], spy["fundamental"])["score"]["state"] == "not_applicable"  # type: ignore[index]


@pytest.mark.parametrize(
    "parameters",
    [
        [("known_at", KNOWN_AT)],
        [("known_at", KNOWN_AT), ("asset_id", AAPL), ("asset_id", AAPL)],
        [("known_at", KNOWN_AT), ("asset_id", AAPL), ("unsupported", "x")],
        [("known_at", "2026-08-29T12:00:00"), ("asset_id", AAPL)],
        [("known_at", "not-a-date"), ("asset_id", AAPL)],
        [("known_at", KNOWN_AT), ("asset_id", "equity:us:not-in-catalog")],
        [("known_at", KNOWN_AT), ("asset_id", "")],
        [("asset_id", AAPL)],
        [("known_at", KNOWN_AT), *(("asset_id", f"a{index:02d}") for index in range(11))],
    ],
)
def test_invalid_requests_are_rejected_without_truncation(
    tmp_path: Path, parameters: list[tuple[str, str]]
) -> None:
    workspace, _ = _workspace(tmp_path)
    with _server(_web(workspace)) as root:
        status, payload = _get(root, parameters)
    assert status == 400
    assert cast(dict[str, object], payload["error"])["code"] == "invalid_request"


def test_only_get_is_routed_and_the_endpoint_never_writes(tmp_path: Path) -> None:
    workspace, paths = _workspace(tmp_path)
    before = paths.database_path.read_bytes()
    with _server(_web(workspace)) as root:
        post = Request(
            f"{root}/api/v1/investor-overview",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as rejected:
            urlopen(post, timeout=10)
        assert rejected.value.code == 404
        for _ in range(2):
            status, _payload = _get(root, _query(AAPL, AMD))
            assert status == 200
    assert paths.database_path.read_bytes() == before


def test_repeated_equivalent_reads_reuse_the_signed_cache(tmp_path: Path) -> None:
    workspace, _ = _workspace(tmp_path)
    web = _web(workspace)
    with _server(web) as root:
        first = _get(root, _query(AAPL, AMD))
        second = _get(root, _query(AMD, AAPL))
    assert first == second
    summary = web.read_performance()
    families = cast(dict[str, dict[str, object]], summary["families"])
    assert cast(int, families["coverage"]["hits"]) >= 1


def _foreign_observation(index: int) -> NormalizedObservation:
    raw_id = uuid4()
    when = datetime(2026, 8, 1, tzinfo=UTC)
    return NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=raw_id,
        asset_id="equity:us:msft",
        field_name="close",
        value=Decimal(index + 1),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=when,
        available_at=when,
        normalized_at=when,
        source=SourceReference(
            source_id="alpaca-market-data:iex:msft:daily-bars:adjustment-all",
            record_key=f"foreign:{index}",
            retrieved_at=when,
        ),
        quality=DataQuality.VALID,
        transformation_version="foreign-corpus-v1",
    )


def test_foreign_corpus_growth_does_not_change_the_selected_projection(tmp_path: Path) -> None:
    workspace, paths = _workspace(tmp_path)
    with _server(_web(workspace)) as root:
        baseline = _get(root, _query(AAPL))
    assert baseline[0] == 200
    with LocalStorage(paths) as storage:
        foreign = [_foreign_observation(index) for index in range(40)]
        storage.raw_records.save_many(
            [
                RawRecord(
                    record_id=row.raw_record_id,
                    asset_id=row.asset_id,
                    source=row.source,
                    event_time=row.observed_at,
                    available_at=row.available_at,
                    received_at=row.source.retrieved_at,
                    payload={"fixture": row.field_name},
                    schema_version="foreign-corpus-v1",
                )
                for row in foreign
            ]
        )
        storage.observations.save_many(foreign)
    with _server(_web(workspace)) as root:
        grown = _get(root, _query(AAPL))
    assert grown == baseline
    assert date(2026, 8, 28).isoformat() in json.dumps(grown[1]["windows"])
