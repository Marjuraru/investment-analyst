"""Regression tests for persisted COMPLETE_REFRESH payloads without explicit identity."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.market.chart_models import (
    BtcMarketChart,
    CryptoSpotDailyMarketChart,
    ListedMarketChart,
)
from investment_analyst.analytics.market.chart_service import (
    BtcMarketChartService,
    CryptoSpotDailyMarketChartService,
    ListedMarketChartService,
)
from investment_analyst.application.aapl_bootstrap_models import (
    AaplRefreshMode,
    AaplWorkspaceBootstrapRequest,
)
from investment_analyst.application.manual_operations import (
    ManualOperationKind,
    ManualOperationQueue,
    ManualOperationRequest,
    ManualOperationResult,
    ManualOperationState,
    ManualOperationStateStore,
    ManualOperationStatus,
)
from investment_analyst.application.operational_models import (
    LegacyCompleteRefreshSnapshotAdapter,
    OperationalRefreshRequestSnapshot,
)
from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID

_LEGACY_PAYLOAD: dict[str, object] = {
    "market_start": "2026-07-01",
    "market_end": "2026-07-02",
    "fundamental_frequency": "quarterly",
    "refresh_mode": "auto",
    "requested_known_at": "2026-07-03T00:00:00Z",
    "require_complete": True,
}
_SUBMITTED = datetime(2026, 7, 3, hour=1, tzinfo=UTC)


def _legacy_request() -> ManualOperationRequest:
    return ManualOperationRequest(
        operation_kind=ManualOperationKind.COMPLETE_REFRESH,
        payload=dict(_LEGACY_PAYLOAD),
    )


def _result() -> ManualOperationResult:
    return ManualOperationResult(
        result_schema_version="aapl-daily-run-state-v1",
        effective_known_at=_SUBMITTED,
        created_count=0,
        reused_count=0,
    )


def _persist_running_legacy_operation(path: Path) -> ManualOperationState:
    request = _legacy_request()
    state = ManualOperationState(
        operation_id=UUID("00000000-0000-0000-0000-0000000000aa"),
        fingerprint=request.fingerprint,
        request=request,
        status=ManualOperationStatus.RUNNING,
        submitted_at=_SUBMITTED,
        started_at=_SUBMITTED + timedelta(seconds=1),
    )
    ManualOperationStateStore(path).write(state)
    return state


def test_persisted_legacy_complete_refresh_loads_adapts_resumes_and_keeps_its_fingerprint_without_rewriting_state(  # noqa: E501
    tmp_path: Path,
) -> None:
    path = tmp_path / "manual_operation_state_v1.json"
    persisted = _persist_running_legacy_operation(path)
    store = ManualOperationStateStore(path)

    before = path.read_bytes()
    document = store.load()

    assert path.read_bytes() == before
    stored = document.operations[0]
    assert stored.request.operation_kind is ManualOperationKind.COMPLETE_REFRESH
    assert stored.request.payload == _LEGACY_PAYLOAD
    assert "asset_id" not in stored.request.payload
    assert stored.fingerprint == _legacy_request().fingerprint
    assert json.loads(before.decode("utf-8"))["operations"][0]["request"]["payload"] == (
        _LEGACY_PAYLOAD
    )

    adapted = LegacyCompleteRefreshSnapshotAdapter.adapt(stored.request.payload)
    assert isinstance(adapted, OperationalRefreshRequestSnapshot)
    assert adapted.asset_id == ASSET_ID
    assert adapted.to_request() == AaplWorkspaceBootstrapRequest(
        asset_id=ASSET_ID,
        market_start=date(2026, 7, 1),
        market_end=date(2026, 7, 2),
        fundamental_frequency=DataFrequency.QUARTERLY,
        refresh_mode=AaplRefreshMode.AUTO,
        requested_known_at=datetime(2026, 7, 3, tzinfo=UTC),
        require_complete=True,
    )

    dispatched: list[ManualOperationRequest] = []

    def dispatch(request: ManualOperationRequest) -> ManualOperationResult:
        dispatched.append(request)
        return _result()

    queue = ManualOperationQueue(store, dispatch)
    recovered = store.load().operations[0]
    completed = queue.run_next()

    assert recovered.status is ManualOperationStatus.QUEUED
    assert recovered.recovery_count == 1
    assert completed is not None and completed.status is ManualOperationStatus.SUCCEEDED
    assert completed.operation_id == persisted.operation_id
    assert dispatched == [_legacy_request()]
    assert dispatched[0].fingerprint == persisted.fingerprint

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["operations"][0]["request"]["payload"] == _LEGACY_PAYLOAD
    assert after["operations"][0]["fingerprint"] == persisted.fingerprint


def test_corrupt_ambiguous_or_identityless_legacy_payload_fails_closed_without_discarding_or_defaulting(  # noqa: E501
    tmp_path: Path,
) -> None:
    for unusable in (None, "", 7, ["equity:us:aapl"], {"asset_id": ASSET_ID}):
        with pytest.raises(ValidationError, match="asset_id"):
            LegacyCompleteRefreshSnapshotAdapter.adapt({**_LEGACY_PAYLOAD, "asset_id": unusable})

    for corrupt in (
        {**_LEGACY_PAYLOAD, "market_start": "2026-13-45"},
        {**_LEGACY_PAYLOAD, "market_end": "2026-07-02T00:00:00Z"},
        {**_LEGACY_PAYLOAD, "require_complete": "yes"},
        {**_LEGACY_PAYLOAD, "refresh_mode": "sometimes"},
        {**_LEGACY_PAYLOAD, "unexpected": True},
    ):
        with pytest.raises(ValidationError):
            LegacyCompleteRefreshSnapshotAdapter.adapt(corrupt)

    path = tmp_path / "manual_operation_state_v1.json"
    _persist_running_legacy_operation(path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["operations"][0]["request"]["payload"]["asset_id"] = None
    path.write_text(json.dumps(document), encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    with pytest.raises(AaplOperationalStateError, match="malformed or unreadable"):
        ManualOperationStateStore(path).load()

    assert path.read_text(encoding="utf-8") == before
    assert json.loads(before)["operations"][0]["request"]["payload"]["asset_id"] is None


def test_no_default_asset_id_substituted_when_adapting_a_legacy_payload() -> None:
    adapted = LegacyCompleteRefreshSnapshotAdapter.adapt(_LEGACY_PAYLOAD)
    assert adapted.asset_id == ASSET_ID

    for unusable in (None, "", 7):
        with pytest.raises(ValidationError, match="asset_id"):
            LegacyCompleteRefreshSnapshotAdapter.adapt({**_LEGACY_PAYLOAD, "asset_id": unusable})

    foreign = LegacyCompleteRefreshSnapshotAdapter.adapt(
        {**_LEGACY_PAYLOAD, "asset_id": "equity:us:amd"}
    )
    assert foreign.asset_id == "equity:us:amd"
    with pytest.raises(ValidationError, match="not enabled for a non-Apple asset"):
        foreign.to_request()


def _dispatch_through_the_bootstrap_seam(
    request: ManualOperationRequest,
) -> ManualOperationResult:
    """Mirror the production dispatch: adapt the stored payload and build the request."""
    LegacyCompleteRefreshSnapshotAdapter.adapt(request.payload).to_request()
    return _result()


def test_complete_refresh_is_not_enabled_for_a_non_apple_asset_and_crypto_legacy_contracts_are_not_collapsed(  # noqa: E501
    tmp_path: Path,
) -> None:
    foreign = ManualOperationRequest(
        operation_kind=ManualOperationKind.COMPLETE_REFRESH,
        payload={**_LEGACY_PAYLOAD, "asset_id": "equity:us:amd"},
    )
    with pytest.raises(ValidationError, match="not enabled for a non-Apple asset"):
        _dispatch_through_the_bootstrap_seam(foreign)

    path = tmp_path / "manual_operation_state_v1.json"
    store = ManualOperationStateStore(path)
    queue = ManualOperationQueue(store, _dispatch_through_the_bootstrap_seam)
    enqueued = queue.enqueue(foreign)
    completed = queue.run_next()

    assert completed is not None and completed.status is ManualOperationStatus.FAILED
    assert completed.operation_id == enqueued.operation_id
    assert completed.request.payload["asset_id"] == "equity:us:amd"
    assert len(store.load().operations) == 1
    assert store.load().operations[0].failure is not None

    assert BtcMarketChart.model_fields["schema_version"].default == "btc-market-chart-v1"
    assert (
        CryptoSpotDailyMarketChart.model_fields["schema_version"].default
        == "crypto-spot-daily-market-chart-v1"
    )
    assert ListedMarketChart.model_fields["schema_version"].default == "listed-market-chart-v1"
    assert not issubclass(BtcMarketChart, ListedMarketChart)
    assert not issubclass(CryptoSpotDailyMarketChart, ListedMarketChart)
    assert not issubclass(ListedMarketChart, BtcMarketChart)
    assert BtcMarketChartService.query is not ListedMarketChartService.query
    assert CryptoSpotDailyMarketChartService.query is not ListedMarketChartService.query
