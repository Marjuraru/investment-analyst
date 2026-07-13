"""Integration tests for the complete deterministic simulated pipeline."""

import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from investment_analyst.core.models import DiagnosticMode
from investment_analyst.simulation import SimulatedPipeline
from investment_analyst.storage import LocalStorage, StoragePaths


def test_complete_pipeline_round_trip_traceability_and_idempotency(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths) as storage:
        first = SimulatedPipeline(storage).run()
        second = SimulatedPipeline(storage).run()

        assert first == second
        assert first.traceability_verified is True
        assert first.counts.to_dict() == {
            "assets": 2,
            "sources": 1,
            "raw_records": 6,
            "observations": 36,
            "metric_definitions": 2,
            "metric_results": 4,
            "diagnostics": 2,
        }
        assert first.asset_ids == ("equity:us:aapl", "crypto:btc-usd")
        assert len(set(first.raw_record_ids)) == 6
        assert len(set(first.observation_ids)) == 36
        assert len(set(first.metric_result_ids)) == 4
        assert len(set(first.diagnostic_ids)) == 2
        assert all(UUID(identifier).version == 5 for identifier in first.raw_record_ids)

        assets = storage.assets.list_all()
        raw_records = storage.raw_records.list(source_id="simulated:daily-bars")
        observations = storage.observations.list()
        metrics = storage.metric_results.list()
        diagnostics = storage.diagnostics.list(mode=DiagnosticMode.MARKET)

        assert {asset.symbol for asset in assets} == {"AAPL", "BTC"}
        assert len(raw_records) == 6
        assert len(observations) == 36
        assert len(metrics) == 4
        assert len(diagnostics) == 2
        assert {result.asset_id for result in metrics} == set(first.asset_ids)
        assert {result.asset_id for result in diagnostics} == set(first.asset_ids)
        assert all(result.algorithm_version == "simulated-metrics-v1" for result in metrics)
        assert all(result.algorithm_version == "simulated-diagnostic-v1" for result in diagnostics)
        assert all(record.event_time.tzinfo is UTC for record in raw_records)
        assert all(
            record.event_time < record.available_at < record.received_at for record in raw_records
        )
        assert all(observation.observed_at.tzinfo is UTC for observation in observations)
        assert all(isinstance(result.value, Decimal) for result in metrics)
        assert all(not isinstance(result.value, float) for result in metrics)
        assert all(
            sum(component.weight for component in result.components) == Decimal("1.00")
            for result in diagnostics
        )
        assert all(
            result.final_score
            == sum(component.weighted_contribution for component in result.components)
            for result in diagnostics
        )

        metric_definitions = {
            definition.metric_key: definition
            for definition in storage.metric_definitions.list_all()
        }
        assert metric_definitions["market.simple_return_1d"].formula == (
            "(current_close / previous_close) - 1"
        )
        assert metric_definitions["market.volume_ratio_1d"].formula == (
            "current_volume / previous_volume"
        )

        aapl_observations = storage.observations.list(asset_id="equity:us:aapl")
        btc_observations = storage.observations.list(asset_id="crypto:btc-usd")
        assert len(aapl_observations) == 18
        assert len(btc_observations) == 18
        assert not (
            {item.observation_id for item in aapl_observations}
            & {item.observation_id for item in btc_observations}
        )

        for metric in metrics:
            input_observations = [
                storage.observations.get(identifier) for identifier in metric.input_observation_ids
            ]
            input_observations.sort(key=lambda item: item.observed_at)
            assert len(input_observations) == 2
            assert all(item.asset_id == metric.asset_id for item in input_observations)
            assert all(item.available_at <= metric.computed_at for item in input_observations)
            previous, current = input_observations
            if metric.metric_key == "market.simple_return_1d":
                assert metric.value == current.value / previous.value - Decimal("1")
                assert [item.field_name for item in input_observations] == ["close", "close"]
            else:
                assert metric.metric_key == "market.volume_ratio_1d"
                assert metric.value == current.value / previous.value
                assert [item.field_name for item in input_observations] == ["volume", "volume"]

        for diagnostic in diagnostics:
            referenced = {
                identifier
                for component in diagnostic.components
                for identifier in component.metric_result_ids
            }
            referenced.update(evidence.metric_result_id for evidence in diagnostic.evidence)
            assert all(
                storage.metric_results.get(identifier).asset_id == diagnostic.asset_id
                for identifier in referenced
            )

    assert json.loads(json.dumps(first.to_dict()))["traceability_verified"] is True
    assert asdict(first)["counts"]["raw_records"] == 6


def test_script_runs_with_explicit_root_and_prints_json(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[3]
    script = project_root / "scripts" / "run_simulated_pipeline.py"
    simulation_root = tmp_path / "script-storage"
    environment = os.environ.copy()
    source_path = str(project_root / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (source_path, environment.get("PYTHONPATH", "")) if item
    )

    completed = subprocess.run(
        [sys.executable, str(script), "--root", str(simulation_root)],
        cwd=project_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["counts"]["raw_records"] == 6
    assert payload["counts"]["observations"] == 36
    assert payload["traceability_verified"] is True
    assert (simulation_root / "data" / "processed" / "investment_analyst.duckdb").is_file()


def test_simulation_sources_contain_no_network_or_real_provider_calls() -> None:
    project_root = Path(__file__).resolve().parents[3]
    simulation_dir = project_root / "src" / "investment_analyst" / "simulation"
    content = "\n".join(path.read_text(encoding="utf-8") for path in simulation_dir.glob("*.py"))

    forbidden = (
        "requests",
        "httpx",
        "urllib",
        "socket",
        "alpaca",
        "coinbase",
        "coingecko",
        "random",
    )
    assert not any(token in content.lower() for token in forbidden)
