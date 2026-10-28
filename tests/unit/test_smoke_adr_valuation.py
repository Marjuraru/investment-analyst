"""Unit contracts for the read-only ADR valuation smoke script."""

import importlib.util
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.workspace.service import WorkspaceService

_ROOT = Path(__file__).parents[2]
_SCRIPT = _ROOT / "scripts" / "smoke_adr_valuation.py"
_FIXTURE = _ROOT / "tests" / "fixtures" / "valuation" / "adr_basis_sources.json"
_SPEC = importlib.util.spec_from_file_location("smoke_adr_valuation", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)


def _fixture() -> dict[str, object]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_fixture_hashes_bounds_and_factors_match_catalog_declarations() -> None:
    catalog = AssetCatalogService.load_default()
    payload = _fixture()
    sources = {item["asset_id"]: item for item in payload["sources"]}

    assert set(sources) == {"equity:us:tsm", "equity:us:bvn"}
    for asset_id, source in sources.items():
        revision = catalog.get(asset_id).security_unit_basis_revisions[0]
        assert str(revision.source_sha256) == source["source_sha256"]
        assert str(revision.factor) == source["units_per_reported_share"]
        assert revision.cik == source["cik"]
        assert revision.accession_number == source["accession_number"]
        assert revision.source_url == source["source_url"]
        assert revision.valid_from == date.fromisoformat(str(source["available_at"])[:10])
        assert revision.available_at == datetime.fromisoformat(
            str(source["available_at"]).replace("Z", "+00:00")
        )
        assert revision.reported_share_unit == source["reported_share_unit"]
        assert source["traded_unit"] == revision.basis
        assert len(str(source["index_sha256"])) == 64
        assert int(str(source["document_bytes"])) > 0
        assert "American Depositary Shares" in str(source["ratio_excerpt"])


def test_fixture_factors_follow_the_documented_ratio() -> None:
    sources = {item["asset_id"]: item for item in _fixture()["sources"]}
    assert sources["equity:us:tsm"]["units_per_reported_share"] == "0.2"
    assert "five (5)" in sources["equity:us:tsm"]["ratio_excerpt"]
    assert sources["equity:us:bvn"]["units_per_reported_share"] == "1"
    assert "one Common Share" in sources["equity:us:bvn"]["ratio_excerpt"]


def test_json_value_serializes_exact_financial_types() -> None:
    assert smoke._json_value(Decimal("0.2")) == "0.2"
    assert smoke._json_value(date(2026, 4, 17)) == "2026-04-17"
    assert smoke._json_value(datetime(2026, 4, 17, tzinfo=UTC)) == ("2026-04-17T00:00:00+00:00")
    assert smoke._json_value("text") == "text"


def test_basis_receipt_exposes_source_hash_interval_and_factor() -> None:
    catalog = AssetCatalogService.load_default()
    asset = catalog.get("equity:us:tsm")
    revision = asset.security_unit_basis_revisions[0]
    from investment_analyst.analytics.valuation.models import ValuationSecurityBasis

    basis = ValuationSecurityBasis(
        basis=revision.basis,
        market_units_per_reported_share=revision.factor,
        market_adjustment=revision.adjustment,
        contract_version=f"{revision.schema_version}:{revision.revision}",
        basis_evidence=revision,
    )

    receipt = smoke._basis_receipt(basis)

    assert receipt is not None
    assert receipt["basis"] == "depositary_receipt"
    assert receipt["market_units_per_reported_share"] == "0.2"
    evidence = receipt["evidence"]
    assert evidence["revision"] == revision.revision
    assert evidence["source_sha256"] == revision.source_sha256
    assert evidence["source_url"] == revision.source_url
    assert evidence["valid_from"] == date(2026, 4, 17)
    assert evidence["valid_to"] is None
    assert smoke._basis_receipt(None) is None


def test_workspace_identity_requires_an_active_v2_workspace(tmp_path: Path) -> None:
    legacy = WorkspaceService().initialize(tmp_path / "legacy")
    with pytest.raises(SystemExit, match="active v2 workspace"):
        smoke._workspace_identity(legacy.paths.root, None)

    active = WorkspaceService().initialize(tmp_path / "active", format_version=2)
    identity = smoke._workspace_identity(active.paths.root, None)
    assert identity["format_version"] == 2
    assert identity["status"] == "ready"
    assert identity["workspace_id"] == str(active.manifest.workspace_id)

    with pytest.raises(SystemExit, match="expected workspace id"):
        smoke._workspace_identity(active.paths.root, "00000000-0000-4000-8000-000000000000")


def test_run_rejects_timeout_and_memory_breaches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = WorkspaceService().initialize(tmp_path / "active", format_version=2)
    monkeypatch.setattr(smoke, "_git", lambda root, *arguments: "deadbeef")
    monkeypatch.setattr(
        smoke,
        "_valuation_receipt",
        lambda *args, **kwargs: {"measurements": [{"milliseconds": 999_999.0, "outcome": "ok"}]},
    )
    monkeypatch.setattr(smoke, "_coverage_receipt", lambda *args, **kwargs: {"measurements": []})

    timed_out = smoke.run(
        workspace=active.paths.root,
        known_at=datetime(2026, 10, 27, tzinfo=UTC),
        valuation_date=date(2026, 10, 24),
        output=tmp_path / "receipt.json",
        expected_workspace_id=None,
    )
    assert timed_out["admission"] == "rejected"
    assert timed_out["reason"] == "a read exceeded the per-query timeout"

    monkeypatch.setattr(
        smoke,
        "_valuation_receipt",
        lambda *args, **kwargs: {"measurements": [{"milliseconds": 1.0, "outcome": "ok"}]},
    )
    monkeypatch.setattr(
        smoke.resource,
        "getrusage",
        lambda who: SimpleNamespace(ru_maxrss=4 * 1024**3),
    )
    over_memory = smoke.run(
        workspace=active.paths.root,
        known_at=datetime(2026, 10, 27, tzinfo=UTC),
        valuation_date=date(2026, 10, 24),
        output=tmp_path / "receipt.json",
        expected_workspace_id=None,
    )
    assert over_memory["admission"] == "rejected"
    assert over_memory["reason"] == "process peak memory exceeded the admitted bound"
