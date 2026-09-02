"""Integration coverage for the reproducible evidence read-path benchmark."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_read_path_benchmark_uses_temporary_corpus_and_reduces_repeated_work() -> None:
    repository_root = Path(__file__).parents[3]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repository_root / "src")

    completed = subprocess.run(
        [sys.executable, str(repository_root / "scripts" / "benchmark_evidence_read_path.py")],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        check=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["schema_version"] == "evidence-read-path-benchmark-v1"
    assert result["baseline"]["checksum_reads"] == 128
    assert result["optimized"]["checksum_reads"] == 32
    assert result["baseline"]["semantic_parses"] == 128
    assert result["optimized"]["semantic_parses"] == 32
    assert result["baseline"]["cache_invalidations"] == 10
    assert result["optimized"]["cache_invalidations"] == 7
