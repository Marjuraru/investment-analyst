"""Plan, validate, and aggregate the partitioned deterministic CI run.

This module belongs to the CI workflow only. It never imports product code and it
does not select tests by substring or by narrative: every partition is built from
the complete pytest node-id universe that the workflow collected through the
``pytest_partition`` plugin.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

COLLECTION_SCHEMA = "ci-collection-v1"
MANIFEST_SCHEMA = "ci-partition-manifest-v1"
SELECTION_SCHEMA = "ci-shard-selection-v1"
RECEIPT_SCHEMA = "ci-shard-receipt-v1"
WEIGHTS_SCHEMA = "ci-module-weights-v1"

TERMINAL_OUTCOMES = frozenset({"passed", "failed", "error", "skipped", "xfailed", "xpassed"})
SUCCESS_OUTCOMES = frozenset({"passed", "skipped", "xfailed", "xpassed"})
FULL_SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
RUN_NUMBER = re.compile(r"[0-9]+")
WEIGHT_SOURCES = frozenset({"measured", "default"})


class ContractError(RuntimeError):
    """Raised when CI evidence is missing, contradictory, or malformed."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ContractError(f"non-finite JSON number: {value}")


def read_json(path: Path | str) -> object:
    try:
        text = Path(path).read_text(encoding="utf-8")
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except ContractError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read strict JSON {path}: {error}") from error


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise ContractError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def universe_sha256(nodeids: Sequence[str]) -> str:
    return hashlib.sha256(canonical_json(list(nodeids))).hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be a JSON array")
    return value


def _exact_keys(mapping: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(mapping) != expected:
        raise ContractError(f"{label} has unknown or missing fields")


def _text(mapping: Mapping[str, object], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ContractError(f"{label}.{key} must be non-empty text")
    return value


def _integer(mapping: Mapping[str, object], key: str, label: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{label}.{key} must be a non-negative integer")
    return value


def _number(mapping: Mapping[str, object], key: str, label: str) -> float:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{label}.{key} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ContractError(f"{label}.{key} must be finite")
    return number


def _positive_number(mapping: Mapping[str, object], key: str, label: str) -> float:
    number = _number(mapping, key, label)
    if number <= 0:
        raise ContractError(f"{label}.{key} must be positive")
    return number


def validate_nodeids(
    value: object,
    label: str,
    *,
    ordered: bool = False,
) -> tuple[str, ...]:
    entries = _sequence(value, label)
    nodeids: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if (
            not isinstance(entry, str)
            or not entry.startswith("tests/")
            or "::" not in entry
            or "\n" in entry
            or "\0" in entry
        ):
            raise ContractError(f"{label} contains an invalid complete node id")
        if entry in seen:
            raise ContractError(f"{label} contains duplicate node ids")
        seen.add(entry)
        nodeids.append(entry)
    if ordered and nodeids != sorted(nodeids):
        raise ContractError(f"{label} is not ordered")
    return tuple(nodeids)


def load_collection(path: Path | str) -> tuple[str, ...]:
    root = _mapping(read_json(path), "collection")
    _exact_keys(root, {"schema_version", "universe", "count", "universe_sha256"}, "collection")
    if root["schema_version"] != COLLECTION_SCHEMA:
        raise ContractError("collection schema_version is invalid")
    universe = validate_nodeids(root["universe"], "collection universe", ordered=True)
    if _integer(root, "count", "collection") != len(universe):
        raise ContractError("collection count differs from its universe")
    if _text(root, "universe_sha256", "collection") != universe_sha256(universe):
        raise ContractError("collection universe digest is invalid")
    if not universe:
        raise ContractError("collection universe is empty")
    return universe


@dataclass(frozen=True, slots=True)
class Weights:
    version: str
    source: dict[str, object]
    default_weight_seconds: float
    modules: dict[str, float]


def load_weights(path: Path | str) -> Weights:
    root = _mapping(read_json(path), "weights")
    _exact_keys(
        root,
        {"schema_version", "version", "source", "default_weight_seconds", "modules"},
        "weights",
    )
    if root["schema_version"] != WEIGHTS_SCHEMA:
        raise ContractError("weights schema_version is invalid")
    version = _text(root, "version", "weights")
    source_mapping = _mapping(root["source"], "weights source")
    _exact_keys(
        source_mapping,
        {"workflow_run_id", "job_id", "source_sha", "method", "observed_seconds"},
        "weights source",
    )
    source = {
        "workflow_run_id": _text(source_mapping, "workflow_run_id", "weights source"),
        "job_id": _text(source_mapping, "job_id", "weights source"),
        "source_sha": _text(source_mapping, "source_sha", "weights source"),
        "method": _text(source_mapping, "method", "weights source"),
        "observed_seconds": _number(source_mapping, "observed_seconds", "weights source"),
    }
    if FULL_SHA.fullmatch(source["source_sha"]) is None:
        raise ContractError("weights source_sha is not a complete SHA")
    default_weight = _positive_number(root, "default_weight_seconds", "weights")
    raw_modules = _mapping(root["modules"], "weights modules")
    modules: dict[str, float] = {}
    for module, weight in raw_modules.items():
        if (
            not isinstance(module, str)
            or not module.startswith("tests/")
            or not module.endswith(".py")
        ):
            raise ContractError("weights module path is invalid")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ContractError("weights module weight must be a number")
        number = float(weight)
        if not math.isfinite(number) or number <= 0:
            raise ContractError("weights module weight must be finite and positive")
        modules[module] = number
    if not modules:
        raise ContractError("weights modules are absent")
    return Weights(version, source, default_weight, modules)


@dataclass(frozen=True, slots=True)
class ModuleAssignment:
    path: str
    weight_seconds: float
    source: str


@dataclass(frozen=True, slots=True)
class Partition:
    shard: int
    nodeids: tuple[str, ...]
    modules: tuple[ModuleAssignment, ...]
    weight_seconds: float


def _round_weight(value: float) -> float:
    return round(value, 6)


def build_partitions(
    universe: Sequence[str], weights: Weights, shard_count: int = 4
) -> tuple[Partition, ...]:
    if shard_count < 2:
        raise ContractError("at least two shards are required")
    modules: dict[str, list[str]] = {}
    for nodeid in universe:
        modules.setdefault(nodeid.split("::", 1)[0], []).append(nodeid)
    assignments: dict[str, ModuleAssignment] = {}
    for path in modules:
        measured = weights.modules.get(path)
        if measured is None:
            assignments[path] = ModuleAssignment(path, weights.default_weight_seconds, "default")
        else:
            assignments[path] = ModuleAssignment(path, measured, "measured")
    ordered = sorted(assignments.values(), key=lambda item: (-item.weight_seconds, item.path))
    loads = [0.0] * shard_count
    grouped: list[list[ModuleAssignment]] = [[] for _ in range(shard_count)]
    for assignment in ordered:
        index = min(range(shard_count), key=lambda candidate: (loads[candidate], candidate))
        loads[index] += assignment.weight_seconds
        grouped[index].append(assignment)
    partitions: list[Partition] = []
    for index in range(shard_count):
        nodeids = tuple(sorted(node for module in grouped[index] for node in modules[module.path]))
        if not nodeids:
            raise ContractError(f"partition {index + 1} is empty")
        partitions.append(
            Partition(
                index + 1,
                nodeids,
                tuple(sorted(grouped[index], key=lambda item: item.path)),
                _round_weight(loads[index]),
            )
        )
    return tuple(partitions)


def _check_identity(
    mapping: Mapping[str, object], source_sha: str, run_id: str, run_attempt: str, label: str
) -> None:
    if _text(mapping, "source_sha", label) != source_sha:
        raise ContractError(f"{label} source SHA differs from the live run")
    if _text(mapping, "run_id", label) != run_id:
        raise ContractError(f"{label} run ID differs from the live run")
    if _text(mapping, "run_attempt", label) != run_attempt:
        raise ContractError(f"{label} run attempt differs from the live run")


@dataclass(frozen=True, slots=True)
class Manifest:
    source_sha: str
    run_id: str
    run_attempt: str
    python_version: str
    lock_sha256: str
    weights_version: str
    weights_sha256: str
    universe: tuple[str, ...]
    partitions: tuple[Partition, ...]
    parallel_started_at_epoch: int
    content: dict[str, object]


def load_manifest(path: Path | str, shard_count: int = 4) -> Manifest:
    root = _mapping(read_json(path), "manifest")
    _exact_keys(
        root,
        {
            "schema_version",
            "source_sha",
            "run_id",
            "run_attempt",
            "python_version",
            "lock_sha256",
            "weights_version",
            "weights_sha256",
            "universe_count",
            "universe_sha256",
            "parallel_started_at_epoch",
            "partitions",
            "unused_weight_modules",
        },
        "manifest",
    )
    if root["schema_version"] != MANIFEST_SCHEMA:
        raise ContractError("manifest schema_version is invalid")
    source_sha = _text(root, "source_sha", "manifest")
    if FULL_SHA.fullmatch(source_sha) is None:
        raise ContractError("manifest source_sha is not a complete SHA")
    run_id = _text(root, "run_id", "manifest")
    run_attempt = _text(root, "run_attempt", "manifest")
    if RUN_NUMBER.fullmatch(run_id) is None or RUN_NUMBER.fullmatch(run_attempt) is None:
        raise ContractError("manifest run identity is invalid")
    python_version = _text(root, "python_version", "manifest")
    if not re.fullmatch(r"3\.12\.\d+", python_version):
        raise ContractError("manifest python_version is not Python 3.12")
    lock_sha256 = _text(root, "lock_sha256", "manifest")
    weights_sha256 = _text(root, "weights_sha256", "manifest")
    if SHA256.fullmatch(lock_sha256) is None or SHA256.fullmatch(weights_sha256) is None:
        raise ContractError("manifest lock or weights digest is invalid")
    partitions_raw = _sequence(root["partitions"], "manifest partitions")
    if len(partitions_raw) != shard_count:
        raise ContractError("manifest shard count differs from the workflow")
    partitions: list[Partition] = []
    seen_shards: set[int] = set()
    for raw_partition in partitions_raw:
        partition_map = _mapping(raw_partition, "manifest partition")
        _exact_keys(
            partition_map,
            {"shard", "nodeids", "modules", "module_count", "weight_seconds"},
            "manifest partition",
        )
        shard = _integer(partition_map, "shard", "manifest partition")
        if shard < 1 or shard > shard_count or shard in seen_shards:
            raise ContractError("manifest shard number is invalid or duplicate")
        seen_shards.add(shard)
        nodeids = validate_nodeids(
            partition_map["nodeids"], "manifest partition nodeids", ordered=True
        )
        raw_modules = _sequence(partition_map["modules"], "manifest partition modules")
        modules: list[ModuleAssignment] = []
        seen_modules: set[str] = set()
        for raw_module in raw_modules:
            module_map = _mapping(raw_module, "manifest partition module")
            _exact_keys(module_map, {"path", "weight_seconds", "source"}, "manifest module")
            path = _text(module_map, "path", "manifest module")
            if not path.startswith("tests/") or not path.endswith(".py") or path in seen_modules:
                raise ContractError("manifest module path is invalid or duplicate")
            seen_modules.add(path)
            source = _text(module_map, "source", "manifest module")
            if source not in WEIGHT_SOURCES:
                raise ContractError("manifest module weight source is invalid")
            modules.append(
                ModuleAssignment(
                    path,
                    _positive_number(module_map, "weight_seconds", "manifest module"),
                    source,
                )
            )
        module_paths = {module.path for module in modules}
        for nodeid in nodeids:
            if nodeid.split("::", 1)[0] not in module_paths:
                raise ContractError("manifest node id belongs to no declared module")
        if _integer(partition_map, "module_count", "manifest partition") != len(modules):
            raise ContractError("manifest module count differs from its modules")
        weight = _positive_number(partition_map, "weight_seconds", "manifest partition")
        if abs(_round_weight(sum(module.weight_seconds for module in modules)) - weight) > 1e-6:
            raise ContractError("manifest partition weight differs from its modules")
        if not nodeids:
            raise ContractError("manifest partition is empty")
        partitions.append(
            Partition(shard, nodeids, tuple(sorted(modules, key=lambda item: item.path)), weight)
        )
    partitions.sort(key=lambda item: item.shard)
    union = tuple(sorted(node for partition in partitions for node in partition.nodeids))
    if len(union) != len(set(union)):
        raise ContractError("manifest partitions overlap")
    if len(union) != _integer(root, "universe_count", "manifest"):
        raise ContractError("manifest universe count differs from its partitions")
    if universe_sha256(union) != _text(root, "universe_sha256", "manifest"):
        raise ContractError("manifest universe digest differs from its partitions")
    unused = _sequence(root["unused_weight_modules"], "manifest unused weights")
    unused_modules: list[str] = []
    for entry in unused:
        if not isinstance(entry, str) or not entry.startswith("tests/"):
            raise ContractError("manifest unused weight module is invalid")
        if entry in unused_modules:
            raise ContractError("manifest unused weight modules are duplicated")
        unused_modules.append(entry)
    if unused_modules != sorted(unused_modules):
        raise ContractError("manifest unused weight modules are not ordered")
    content = dict(root)
    return Manifest(
        source_sha,
        run_id,
        run_attempt,
        python_version,
        lock_sha256,
        _text(root, "weights_version", "manifest"),
        weights_sha256,
        union,
        tuple(partitions),
        _integer(root, "parallel_started_at_epoch", "manifest"),
        content,
    )


def plan(
    collection_path: Path | str,
    weights_path: Path | str,
    *,
    shards: int,
    source_sha: str,
    run_id: str,
    run_attempt: str,
    python_version: str,
    lock_path: Path | str,
    parallel_started_at_epoch: int,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    universe = load_collection(collection_path)
    weights = load_weights(weights_path)
    if FULL_SHA.fullmatch(source_sha) is None:
        raise ContractError("plan source_sha is not a complete SHA")
    if RUN_NUMBER.fullmatch(run_id) is None or RUN_NUMBER.fullmatch(run_attempt) is None:
        raise ContractError("plan run identity is invalid")
    if not re.fullmatch(r"3\.12\.\d+", python_version):
        raise ContractError("plan python_version is not Python 3.12")
    if parallel_started_at_epoch <= 0:
        raise ContractError("plan parallel start timestamp is invalid")
    partitions = build_partitions(universe, weights, shards)
    manifest: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA,
        "source_sha": source_sha,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "python_version": python_version,
        "lock_sha256": file_sha256(lock_path),
        "weights_version": weights.version,
        "weights_sha256": file_sha256(weights_path),
        "universe_count": len(universe),
        "universe_sha256": universe_sha256(universe),
        "parallel_started_at_epoch": parallel_started_at_epoch,
        "unused_weight_modules": sorted(
            set(weights.modules) - {node.split("::", 1)[0] for node in universe}
        ),
        "partitions": [
            {
                "shard": partition.shard,
                "nodeids": list(partition.nodeids),
                "module_count": len(partition.modules),
                "weight_seconds": partition.weight_seconds,
                "modules": [
                    {
                        "path": module.path,
                        "weight_seconds": module.weight_seconds,
                        "source": module.source,
                    }
                    for module in partition.modules
                ],
            }
            for partition in partitions
        ],
    }
    selections: list[dict[str, object]] = []
    for partition in partitions:
        selections.append(_selection(manifest, partition.shard, partition.nodeids))
    return manifest, tuple(selections)


def _selection(
    manifest: Mapping[str, object], shard: int, nodeids: Sequence[str]
) -> dict[str, object]:
    ordered = tuple(sorted(nodeids))
    return {
        "schema_version": SELECTION_SCHEMA,
        "shard": shard,
        "source_sha": manifest["source_sha"],
        "run_id": manifest["run_id"],
        "run_attempt": manifest["run_attempt"],
        "universe_count": manifest["universe_count"],
        "universe_sha256": manifest["universe_sha256"],
        "selected_count": len(ordered),
        "selected_sha256": universe_sha256(ordered),
        "nodeids": list(ordered),
    }


def load_selection(
    path: Path | str,
    *,
    mode: str,
    shard: int,
    source_sha: str,
    run_id: str,
    run_attempt: str,
    expected_universe_sha256: str,
    expected_universe_count: int,
) -> tuple[str, ...]:
    root = _mapping(read_json(path), "selection")
    _exact_keys(
        root,
        {
            "schema_version",
            "shard",
            "source_sha",
            "run_id",
            "run_attempt",
            "universe_count",
            "universe_sha256",
            "selected_count",
            "selected_sha256",
            "nodeids",
        },
        "selection",
    )
    if root["schema_version"] != SELECTION_SCHEMA:
        raise ContractError("selection schema_version is invalid")
    _check_identity(root, source_sha, run_id, run_attempt, "selection")
    if _text(root, "universe_sha256", "selection") != expected_universe_sha256:
        raise ContractError("selection universe digest differs from the collected universe")
    if _integer(root, "universe_count", "selection") != expected_universe_count:
        raise ContractError("selection universe count differs from the collected universe")
    selected_shard = _integer(root, "shard", "selection")
    expected_shard = 0 if mode == "serial" else shard
    if selected_shard != expected_shard:
        raise ContractError("selection shard differs from the runner")
    nodeids = validate_nodeids(root["nodeids"], "selection nodeids", ordered=True)
    if not nodeids:
        raise ContractError("selection nodeids are empty")
    if _integer(root, "selected_count", "selection") != len(nodeids):
        raise ContractError("selection count differs from its node ids")
    if _text(root, "selected_sha256", "selection") != universe_sha256(nodeids):
        raise ContractError("selection digest is invalid")
    return nodeids


def choose_selection(manifest: Manifest, *, shard: int, serial: bool) -> dict[str, object]:
    if serial:
        nodeids = manifest.universe
        shard_number = 0
    else:
        match = next(
            (partition for partition in manifest.partitions if partition.shard == shard), None
        )
        if match is None:
            raise ContractError("manifest does not contain the requested shard")
        nodeids = match.nodeids
        shard_number = shard
    return _selection(manifest.content, shard_number, nodeids)


def _allowed_shard_files(directory: Path, shard_count: int) -> list[Path]:
    files: list[Path] = []
    for index in range(1, shard_count + 1):
        base = directory / f"coverage-shard-{index}"
        if not base.is_file():
            raise ContractError(f"coverage evidence is missing: {base.name}")
        files.append(base)
        files.extend(sorted(directory.glob(f"coverage-shard-{index}.*")))
    expected_names = {path.name for path in files}
    unexpected = sorted(
        path.name for path in directory.glob("coverage-shard-*") if path.name not in expected_names
    )
    if unexpected:
        raise ContractError(f"unexpected coverage evidence: {unexpected[0]}")
    return files


def _receipt_path(directory: Path, shard: int) -> Path:
    return directory / f"receipt-shard-{shard}.json"


def load_receipt(
    path: Path | str,
    *,
    mode: str,
    shard: int,
    manifest: Manifest,
) -> dict[str, object]:
    root = _mapping(read_json(path), "receipt")
    _exact_keys(
        root,
        {
            "schema_version",
            "mode",
            "shard",
            "source_sha",
            "run_id",
            "run_attempt",
            "universe_count",
            "universe_sha256",
            "selected_count",
            "selected_sha256",
            "outcomes",
            "durations",
            "terminal",
            "exitstatus",
            "duration_seconds",
        },
        "receipt",
    )
    if root["schema_version"] != RECEIPT_SCHEMA:
        raise ContractError("receipt schema_version is invalid")
    if root["mode"] != mode:
        raise ContractError("receipt mode differs from its artifact")
    if _integer(root, "shard", "receipt") != shard:
        raise ContractError("receipt shard differs from its artifact")
    _check_identity(root, manifest.source_sha, manifest.run_id, manifest.run_attempt, "receipt")
    if _integer(root, "universe_count", "receipt") != len(manifest.universe):
        raise ContractError("receipt universe count differs from the manifest")
    if _text(root, "universe_sha256", "receipt") != universe_sha256(manifest.universe):
        raise ContractError("receipt universe digest differs from the manifest")
    if root["terminal"] is not True:
        raise ContractError("receipt is not terminal")
    if _integer(root, "exitstatus", "receipt") != 0:
        raise ContractError("receipt records a non-zero pytest exit status")
    _number(root, "duration_seconds", "receipt")
    raw_outcomes = _mapping(root["outcomes"], "receipt outcomes")
    outcomes: dict[str, str] = {}
    for nodeid, outcome in raw_outcomes.items():
        if not isinstance(nodeid, str) or not isinstance(outcome, str):
            raise ContractError("receipt outcome entry is invalid")
        if outcome not in TERMINAL_OUTCOMES:
            raise ContractError(f"receipt outcome is not terminal: {nodeid}")
        if outcome not in SUCCESS_OUTCOMES:
            raise ContractError(f"receipt records a non-success outcome: {nodeid}")
        outcomes[nodeid] = outcome
    selected_count = _integer(root, "selected_count", "receipt")
    if mode == "serial":
        expected = manifest.universe
    else:
        expected = next(
            partition.nodeids for partition in manifest.partitions if partition.shard == shard
        )
    if selected_count != len(expected):
        raise ContractError("receipt selected count differs from the manifest")
    if _text(root, "selected_sha256", "receipt") != universe_sha256(expected):
        raise ContractError("receipt selected digest differs from the manifest")
    if set(outcomes) != set(expected):
        raise ContractError("receipt outcomes do not match the selected node ids")
    raw_durations = _mapping(root["durations"], "receipt durations")
    durations: dict[str, float] = {}
    for nodeid, duration in raw_durations.items():
        if (
            not isinstance(nodeid, str)
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) < 0
        ):
            raise ContractError("receipt duration entry is invalid")
        durations[nodeid] = float(duration)
    if set(durations) != set(expected):
        raise ContractError("receipt durations do not match the selected node ids")
    return {
        "outcomes": outcomes,
        "durations": durations,
        "selected_count": selected_count,
        "selected_sha256": universe_sha256(expected),
        "duration_seconds": _number(root, "duration_seconds", "receipt"),
    }


def validate_receipts(
    manifest: Manifest, receipts_dir: Path | str, shard_count: int = 4
) -> dict[str, object]:
    directory = Path(receipts_dir)
    seen: dict[str, int] = {}
    shard_summary: list[dict[str, object]] = []
    outcomes: dict[str, str] = {}
    for shard in range(1, shard_count + 1):
        path = _receipt_path(directory, shard)
        if not path.is_file():
            raise ContractError(f"shard receipt is missing: {path.name}")
        receipt = load_receipt(path, mode="shard", shard=shard, manifest=manifest)
        partition = next(item for item in manifest.partitions if item.shard == shard)
        for nodeid, outcome in receipt["outcomes"].items():
            if nodeid in seen:
                raise ContractError(f"node id appears in two shard receipts: {nodeid}")
            seen[nodeid] = shard
            outcomes[nodeid] = outcome
        shard_summary.append(
            {
                "shard": shard,
                "selected_count": receipt["selected_count"],
                "duration_seconds": receipt["duration_seconds"],
                "weight_seconds": partition.weight_seconds,
                "outcomes": dict(sorted(receipt["outcomes"].items())),
            }
        )
    if set(outcomes) != set(manifest.universe):
        raise ContractError("shard receipts do not cover the planned universe")
    return {
        "universe_count": len(manifest.universe),
        "universe_sha256": universe_sha256(manifest.universe),
        "shards": shard_summary,
        "outcomes": dict(sorted(outcomes.items())),
    }


def _read_timing(path: Path, label: str) -> tuple[int, int]:
    root = _mapping(read_json(path), label)
    started = _integer(root, "started_at_epoch", label)
    finished = _integer(root, "finished_at_epoch", label)
    if finished < started:
        raise ContractError(f"{label} finish precedes its start")
    return started, finished


def _coverage_report_total(path: Path, label: str) -> dict[str, float]:
    root = _mapping(read_json(path), label)
    totals = _mapping(root.get("totals"), f"{label} totals")
    return {
        "percent_covered": _number(totals, "percent_covered", f"{label} totals"),
        "covered_lines": _integer(totals, "covered_lines", f"{label} totals"),
        "num_statements": _integer(totals, "num_statements", f"{label} totals"),
        "covered_branches": _integer(totals, "covered_branches", f"{label} totals"),
        "num_branches": _integer(totals, "num_branches", f"{label} totals"),
    }


def compare_serial(
    manifest: Manifest,
    receipts_dir: Path | str,
    serial_dir: Path | str,
    *,
    combined_coverage_path: Path | str,
    serial_coverage_path: Path | str,
    aggregate_started_at_epoch: int,
    aggregate_finished_at_epoch: int,
    max_ratio: float = 0.6,
    shard_count: int = 4,
) -> dict[str, object]:
    parallel = validate_receipts(manifest, receipts_dir, shard_count)
    serial_receipt_path = Path(serial_dir) / "receipt-serial.json"
    if not serial_receipt_path.is_file():
        raise ContractError("serial reference receipt is missing")
    serial = load_receipt(serial_receipt_path, mode="serial", shard=0, manifest=manifest)
    if serial["outcomes"] != parallel["outcomes"]:
        raise ContractError("serial reference outcomes differ from the parallel universe")
    serial_started, serial_finished = _read_timing(
        Path(serial_dir) / "timing-serial.json", "serial timing"
    )
    plan_started, plan_finished = _read_timing(Path(serial_dir) / "timing-plan.json", "plan timing")
    if plan_started != manifest.parallel_started_at_epoch:
        raise ContractError("plan timing differs from the partition manifest")
    if aggregate_finished_at_epoch < aggregate_started_at_epoch:
        raise ContractError("aggregate timing is inconsistent")
    serial_seconds = serial_finished - serial_started
    if serial_seconds <= 0:
        raise ContractError("serial comparison duration is not positive")
    shard_timings: list[dict[str, int]] = []
    for shard in range(1, shard_count + 1):
        started, finished = _read_timing(
            Path(receipts_dir) / f"timing-shard-{shard}.json", f"shard {shard} timing"
        )
        shard_timings.append(
            {"shard": shard, "started_at_epoch": started, "finished_at_epoch": finished}
        )
    shard_path_finished = max(item["finished_at_epoch"] for item in shard_timings)
    if shard_path_finished < manifest.parallel_started_at_epoch:
        raise ContractError("shard timing precedes the parallel path start")
    shard_path_seconds = shard_path_finished - manifest.parallel_started_at_epoch
    aggregate_seconds = aggregate_finished_at_epoch - aggregate_started_at_epoch
    parallel_seconds = shard_path_seconds + aggregate_seconds
    if parallel_seconds <= 0:
        raise ContractError("parallel comparison duration is not positive")
    ratio = parallel_seconds / serial_seconds
    if ratio > max_ratio:
        raise ContractError(
            f"parallel path is not at least {round((1 - max_ratio) * 100)}% faster: "
            f"{parallel_seconds}s of {serial_seconds}s"
        )
    combined = _coverage_report_total(combined_coverage_path, "combined coverage")
    serial_coverage = _coverage_report_total(serial_coverage_path, "serial coverage")
    if serial_coverage["percent_covered"] < 82:
        raise ContractError("serial reference coverage is below the global floor")
    if combined["percent_covered"] < serial_coverage["percent_covered"]:
        raise ContractError("parallel combined coverage is lower than the serial reference")
    queue_seconds = max(0, min(item["started_at_epoch"] for item in shard_timings) - plan_finished)
    execution_seconds = max(item["finished_at_epoch"] for item in shard_timings) - min(
        item["started_at_epoch"] for item in shard_timings
    )
    plan_seconds = plan_finished - plan_started
    shard_runner_seconds = sum(
        item["finished_at_epoch"] - item["started_at_epoch"] for item in shard_timings
    )
    parallel_runner_seconds = plan_seconds + shard_runner_seconds + aggregate_seconds
    return {
        "parallel_seconds": parallel_seconds,
        "serial_seconds": serial_seconds,
        "ratio": round(ratio, 6),
        "speedup_percent": round((1 - ratio) * 100, 2),
        "queue_seconds": queue_seconds,
        "execution_seconds": execution_seconds,
        "shard_path_seconds": shard_path_seconds,
        "aggregate_seconds": aggregate_seconds,
        "plan_seconds": plan_seconds,
        "shard_runner_seconds": shard_runner_seconds,
        "parallel_runner_seconds": parallel_runner_seconds,
        "parallel_runner_minutes": round(parallel_runner_seconds / 60, 2),
        "serial_runner_seconds": serial_seconds,
        "serial_runner_minutes": round(serial_seconds / 60, 2),
        "plan_started_at_epoch": plan_started,
        "plan_finished_at_epoch": plan_finished,
        "shard_path_finished_at_epoch": shard_path_finished,
        "aggregate_started_at_epoch": aggregate_started_at_epoch,
        "aggregate_finished_at_epoch": aggregate_finished_at_epoch,
        "shard_timings": shard_timings,
        "combined_coverage": combined,
        "serial_coverage": serial_coverage,
        "shard_count": shard_count,
    }


def _coverage_command(*arguments: str) -> list[str]:
    return [sys.executable, "-m", "coverage", *arguments]


def run_coverage(
    shards_dir: Path | str,
    *,
    shard_count: int,
    data_file: Path | str,
    json_out: Path | str,
    source_root: Path | str,
    fail_under: float,
    repo_root: Path | str,
) -> dict[str, object]:
    directory = Path(shards_dir)
    files = _allowed_shard_files(directory, shard_count)
    data_path = Path(data_file)
    if data_path.exists():
        data_path.unlink()
    combine = subprocess.run(
        _coverage_command(
            "combine",
            "--keep",
            f"--data-file={data_path}",
            *(str(path) for path in files),
        ),
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if combine.returncode != 0:
        raise ContractError(f"coverage combine failed: {combine.stderr.strip()}")
    json_path = Path(json_out)
    export = subprocess.run(
        _coverage_command(
            "json",
            "--fail-under=0",
            f"--data-file={data_path}",
            "-o",
            str(json_path),
        ),
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if export.returncode != 0:
        raise ContractError(f"coverage json failed: {export.stderr.strip()}")
    exported = _mapping(read_json(json_path), "coverage export")
    measured = _mapping(exported.get("files"), "coverage files")
    if not measured:
        raise ContractError("coverage export contains no measured files")
    root = Path(repo_root).resolve()
    source = (root / source_root).resolve()
    for raw_path in measured:
        path = Path(raw_path)
        resolved = (path if path.is_absolute() else root / path).resolve()
        if not resolved.is_relative_to(source):
            raise ContractError(f"coverage path is outside the declared source root: {raw_path}")
    report = subprocess.run(
        _coverage_command(
            "report",
            f"--data-file={data_path}",
            f"--fail-under={fail_under}",
        ),
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if report.returncode == 2:
        raise ContractError(
            f"combined coverage is below the global floor of {fail_under}: {report.stdout.strip()}"
        )
    if report.returncode != 0:
        raise ContractError(f"coverage report failed: {report.stderr.strip()}")
    totals = _mapping(exported.get("totals"), "coverage totals")
    return {
        "percent_covered": _number(totals, "percent_covered", "coverage totals"),
        "covered_lines": _integer(totals, "covered_lines", "coverage totals"),
        "num_statements": _integer(totals, "num_statements", "coverage totals"),
        "covered_branches": _integer(totals, "covered_branches", "coverage totals"),
        "num_branches": _integer(totals, "num_branches", "coverage totals"),
        "measured_files": sorted(str(path) for path in measured),
        "source_root": str(source_root),
        "fail_under": fail_under,
        "shard_count": shard_count,
    }


def _write_json(path: Path | str, value: object) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _parse_compare_serial(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"", "false"}:
        return False
    if normalized == "true":
        return True
    raise ContractError("compare_serial input is invalid")


def evaluate_gate(
    *,
    plan_result: str,
    tests_result: str,
    audit_result: str,
    serial_result: str,
    compare_serial: str,
) -> dict[str, object]:
    requested = _parse_compare_serial(compare_serial)
    for label, result in (
        ("plan", plan_result),
        ("tests", tests_result),
        ("audit", audit_result),
    ):
        if result != "success":
            raise ContractError(f"required job {label} finished {result or 'unknown'}")
    if requested:
        if serial_result != "success":
            raise ContractError(f"requested serial reference finished {serial_result or 'unknown'}")
    elif serial_result != "skipped":
        raise ContractError("serial reference must be skipped when it was not requested")
    return {"compare_serial": requested, "required_jobs": "success"}


def _command_plan(arguments: argparse.Namespace) -> int:
    manifest, selections = plan(
        arguments.collection,
        arguments.weights,
        shards=arguments.shards,
        source_sha=arguments.source_sha,
        run_id=arguments.run_id,
        run_attempt=arguments.run_attempt,
        python_version=arguments.python_version,
        lock_path=arguments.lock_file,
        parallel_started_at_epoch=arguments.parallel_started_at,
    )
    output = Path(arguments.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    _write_json(Path(arguments.manifest), manifest)
    for selection in selections:
        _write_json(output / f"selected-shard-{selection['shard']}.json", selection)
    _write_json(
        output / "partition-summary.json",
        {
            "universe_count": manifest["universe_count"],
            "universe_sha256": manifest["universe_sha256"],
            "partitions": [
                {
                    "shard": partition["shard"],
                    "selected_count": len(partition["nodeids"]),
                    "module_count": partition["module_count"],
                    "weight_seconds": partition["weight_seconds"],
                }
                for partition in manifest["partitions"]
            ],
            "unused_weight_modules": manifest["unused_weight_modules"],
        },
    )
    print(
        "planned "
        f"{manifest['universe_count']} node ids in {arguments.shards} shards; "
        f"weights={manifest['weights_version']}"
    )
    return 0


def _command_select(arguments: argparse.Namespace) -> int:
    manifest = load_manifest(arguments.manifest, arguments.shards)
    if (
        manifest.source_sha != arguments.source_sha
        or manifest.run_id != arguments.run_id
        or manifest.run_attempt != arguments.run_attempt
    ):
        raise ContractError("manifest identity differs from the live run")
    selection = choose_selection(manifest, shard=arguments.shard, serial=arguments.serial)
    _write_json(arguments.output, selection)
    print(
        f"selected {selection['selected_count']} node ids for "
        f"{'serial reference' if arguments.serial else f'shard {arguments.shard}'}"
    )
    return 0


def _command_validate(arguments: argparse.Namespace) -> int:
    manifest = load_manifest(arguments.manifest, arguments.shards)
    if (
        manifest.source_sha != arguments.source_sha
        or manifest.run_id != arguments.run_id
        or manifest.run_attempt != arguments.run_attempt
    ):
        raise ContractError("manifest identity differs from the live run")
    summary = validate_receipts(manifest, arguments.receipts_dir, arguments.shards)
    _write_json(arguments.summary_out, summary)
    print(
        f"validated {summary['universe_count']} node ids across "
        f"{len(summary['shards'])} shard receipts"
    )
    return 0


def _command_coverage(arguments: argparse.Namespace) -> int:
    summary = run_coverage(
        arguments.shards_dir,
        shard_count=arguments.shards,
        data_file=arguments.data_file,
        json_out=arguments.json_out,
        source_root=arguments.source_root,
        fail_under=arguments.fail_under,
        repo_root=arguments.repo_root,
    )
    _write_json(arguments.summary_out, summary)
    print(
        f"combined coverage {summary['percent_covered']:.2f}% "
        f"across {summary['shard_count']} shards"
    )
    return 0


def _command_compare(arguments: argparse.Namespace) -> int:
    manifest = load_manifest(arguments.manifest, arguments.shards)
    summary = compare_serial(
        manifest,
        arguments.receipts_dir,
        arguments.serial_dir,
        combined_coverage_path=arguments.combined_coverage,
        serial_coverage_path=arguments.serial_coverage,
        aggregate_started_at_epoch=arguments.aggregate_started_at,
        aggregate_finished_at_epoch=arguments.aggregate_finished_at,
        max_ratio=arguments.max_ratio,
        shard_count=arguments.shards,
    )
    _write_json(arguments.summary_out, summary)
    print(
        f"parallel {summary['parallel_seconds']}s vs serial {summary['serial_seconds']}s "
        f"({summary['speedup_percent']}% faster)"
    )
    return 0


def _command_gate(arguments: argparse.Namespace) -> int:
    summary = evaluate_gate(
        plan_result=arguments.plan_result,
        tests_result=arguments.tests_result,
        audit_result=arguments.audit_result,
        serial_result=arguments.serial_result,
        compare_serial=arguments.compare_serial,
    )
    print(f"required jobs passed; compare_serial={summary['compare_serial']}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="build deterministic shard selections")
    plan_parser.add_argument("--collection", required=True)
    plan_parser.add_argument("--weights", required=True)
    plan_parser.add_argument("--shards", type=int, default=4)
    plan_parser.add_argument("--source-sha", required=True)
    plan_parser.add_argument("--run-id", required=True)
    plan_parser.add_argument("--run-attempt", required=True)
    plan_parser.add_argument("--python-version", required=True)
    plan_parser.add_argument("--lock-file", required=True)
    plan_parser.add_argument("--parallel-started-at", type=int, required=True)
    plan_parser.add_argument("--output-dir", required=True)
    plan_parser.add_argument("--manifest", required=True)
    plan_parser.set_defaults(handler=_command_plan)

    select_parser = subparsers.add_parser("select", help="write one validated selection")
    select_parser.add_argument("--manifest", required=True)
    select_parser.add_argument("--shards", type=int, default=4)
    group = select_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--shard", type=int)
    group.add_argument("--serial", action="store_true")
    select_parser.add_argument("--source-sha", required=True)
    select_parser.add_argument("--run-id", required=True)
    select_parser.add_argument("--run-attempt", required=True)
    select_parser.add_argument("--output", required=True)
    select_parser.set_defaults(handler=_command_select)

    validate_parser = subparsers.add_parser("validate", help="validate all shard receipts")
    validate_parser.add_argument("--manifest", required=True)
    validate_parser.add_argument("--shards", type=int, default=4)
    validate_parser.add_argument("--receipts-dir", required=True)
    validate_parser.add_argument("--source-sha", required=True)
    validate_parser.add_argument("--run-id", required=True)
    validate_parser.add_argument("--run-attempt", required=True)
    validate_parser.add_argument("--summary-out", required=True)
    validate_parser.set_defaults(handler=_command_validate)

    coverage_parser = subparsers.add_parser("coverage", help="combine shard coverage")
    coverage_parser.add_argument("--shards-dir", required=True)
    coverage_parser.add_argument("--shards", type=int, default=4)
    coverage_parser.add_argument("--data-file", required=True)
    coverage_parser.add_argument("--json-out", required=True)
    coverage_parser.add_argument("--source-root", default="src/investment_analyst")
    coverage_parser.add_argument("--fail-under", type=float, default=82.0)
    coverage_parser.add_argument("--repo-root", default=".")
    coverage_parser.add_argument("--summary-out", required=True)
    coverage_parser.set_defaults(handler=_command_coverage)

    compare_parser = subparsers.add_parser("compare", help="compare serial and parallel evidence")
    compare_parser.add_argument("--manifest", required=True)
    compare_parser.add_argument("--shards", type=int, default=4)
    compare_parser.add_argument("--receipts-dir", required=True)
    compare_parser.add_argument("--serial-dir", required=True)
    compare_parser.add_argument("--combined-coverage", required=True)
    compare_parser.add_argument("--serial-coverage", required=True)
    compare_parser.add_argument("--aggregate-started-at", type=int, required=True)
    compare_parser.add_argument("--aggregate-finished-at", type=int, required=True)
    compare_parser.add_argument("--max-ratio", type=float, default=0.6)
    compare_parser.add_argument("--summary-out", required=True)
    compare_parser.set_defaults(handler=_command_compare)

    gate_parser = subparsers.add_parser("gate", help="require every branch to be green")
    gate_parser.add_argument("--plan-result", required=True)
    gate_parser.add_argument("--tests-result", required=True)
    gate_parser.add_argument("--audit-result", required=True)
    gate_parser.add_argument("--serial-result", required=True)
    gate_parser.add_argument("--compare-serial", default="")
    gate_parser.set_defaults(handler=_command_gate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except ContractError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
