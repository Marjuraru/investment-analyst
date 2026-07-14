"""Subprocess tests for the fixed Apple SEC document import script."""

import json
import os
import subprocess
import sys
from pathlib import Path

SUBMISSIONS_PATH = Path("tests/fixtures/sec/aapl_submissions.json").resolve()
COMPANY_FACTS_PATH = Path("tests/fixtures/sec/aapl_companyfacts.json").resolve()


def _launcher() -> str:
    return "\n".join(
        (
            "import runpy, sys",
            "from pathlib import Path",
            "from investment_analyst.providers.http import HttpResponse",
            "import investment_analyst.providers.http as http_module",
            "class FakeTransport:",
            "    def __init__(self, *args, **kwargs): pass",
            "    def get(self, url, *, headers, timeout_seconds):",
            f"        submissions = Path({str(SUBMISSIONS_PATH)!r}).read_bytes()",
            f"        company = Path({str(COMPANY_FACTS_PATH)!r}).read_bytes()",
            "        body = submissions if '/submissions/' in url else company",
            "        return HttpResponse(status_code=200, body=body, headers={}, url=url)",
            "http_module.UrlLibHttpTransport = FakeTransport",
            "sys.argv = ['fetch_sec_aapl_fundamentals.py', *sys.argv[1:]]",
            "runpy.run_path('scripts/fetch_sec_aapl_fundamentals.py', run_name='__main__')",
        )
    )


def _environment(*, include_identity: bool) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path("src").resolve()), environment.get("PYTHONPATH", "")]
    )
    if include_identity:
        environment["SEC_USER_AGENT"] = "Investment Analyst subprocess@example.com"
    else:
        environment.pop("SEC_USER_AGENT", None)
    return environment


def _run(root: Path, *, include_identity: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _launcher(),
            "--root",
            str(root),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_environment(include_identity=include_identity),
    )


def test_script_runs_offline_prints_compact_json_and_reuses_records(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    first = _run(root)
    second = _run(root)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_output = json.loads(first.stdout)
    second_output = json.loads(second.stdout)
    assert first_output["summary"]["raw_records_created"] == 2
    assert second_output["summary"]["raw_records_created"] == 0
    assert second_output["summary"]["raw_records_reused"] == 2
    assert "raw issuer documents" in first_output["notice"]
    combined = first.stdout + first.stderr + second.stdout + second.stderr
    assert "subprocess@example.com" not in combined
    assert "SyntheticRevenue" not in combined
    assert "Traceback" not in combined


def test_script_fails_cleanly_without_sec_user_agent(tmp_path: Path) -> None:
    result = _run(tmp_path / "storage", include_identity=False)

    assert result.returncode != 0
    assert "SEC_USER_AGENT is required" in result.stderr
    assert "Traceback" not in result.stderr
