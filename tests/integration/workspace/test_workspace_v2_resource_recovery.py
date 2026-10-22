"""Native WAL fixtures and pre-connect budgets; no real workspace is opened."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from investment_analyst.storage.resource_policy import DuckDBResourcePolicy
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService


def test_workspace_replays_native_wal_with_initial_budget(tmp_path: Path) -> None:
    root = tmp_path / "fixture"
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(root, format_version=2)
    database = root / "storage/v2/index.duckdb"
    code = """
import os,sys
from pathlib import Path
from investment_analyst.storage.resource_policy import DuckDBResourcePolicy
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService
root=Path(sys.argv[1]); database=root/'storage/v2/index.duckdb'
policy=DuckDBResourcePolicy(memory_limit_bytes=256000000,threads=1,spill_limit_bytes=0)
service=WorkspaceService(resource_policies={database:policy})
storage=service.open_storage(service.resolve(root),WorkspaceAccessMode.READ_WRITE)
connection=storage.store.connection
connection.execute('CREATE TABLE wal_probe AS SELECT range AS id FROM range(4096)')
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(root)],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    wal = database.with_suffix(".duckdb.wal")
    assert wal.is_file() and wal.stat().st_size > 0
    policy = DuckDBResourcePolicy(
        memory_limit_bytes=256_000_000,
        threads=1,
        spill_limit_bytes=0,
        temp_directory=tmp_path / "temporary",
    )
    bounded = WorkspaceService(
        environ={}, home=tmp_path / "home", resource_policies={database: policy}
    )
    with bounded.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        connection = storage.store.connection
        assert connection.execute("SELECT count(*),sum(id) FROM wal_probe").fetchone() == (
            4096,
            4095 * 4096 // 2,
        )
        assert connection.execute("SELECT current_setting('threads')").fetchone() == (1,)
        assert connection.execute(
            "SELECT current_setting('max_temp_directory_size')"
        ).fetchone() == ("0 bytes",)
    with bounded.open_storage(initialized.paths, WorkspaceAccessMode.READ_ONLY) as storage:
        assert storage.store.connection.execute("SELECT count(*) FROM wal_probe").fetchone() == (
            4096,
        )


def test_prepared_44_reader_keeps_preconnect_caps_without_installed_edits(tmp_path: Path) -> None:
    release = Path(
        "/home/marjuraru/.local/share/investment-analyst/runtime/releases/dbbdfd021b0fee20be31b2d2a845874b0e861067"
    )
    interpreter = release / ".venv/bin/python"
    if not interpreter.is_file():
        pytest.skip("operational prepared release 44 is not installed in this environment")
    root = tmp_path / "fixture"
    WorkspaceService(environ={}, home=tmp_path / "home").initialize(root, format_version=2)
    helper = (
        Path(__file__).resolve().parents[3] / "src/investment_analyst/storage/resource_policy.py"
    )
    code = """
import sys,importlib.util,json,inspect
from pathlib import Path
root=Path(sys.argv[1]); helper=Path(sys.argv[2]); release=Path(sys.argv[3])
spec=importlib.util.spec_from_file_location('maintenance_policy45',helper)
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.models import WorkspaceAccessMode
policy=module.DuckDBResourcePolicy(memory_limit_bytes=2000000000,threads=2,
    spill_limit_bytes=1000000000,temp_directory=root.parent/'temporary44')
database=root/'storage/v2/index.duckdb'
service=WorkspaceService()
with module.compatible_reader_instance(database,read_only=True,policy=policy):
    with service.open_storage(service.resolve(root),WorkspaceAccessMode.READ_ONLY) as storage:
        path=Path(inspect.getfile(type(storage.store))).resolve()
        assert path==release/'src/investment_analyst/storage/workspace_v2.py'
        settings=module.verify_connection_policy(storage.store.connection,policy)
        print(json.dumps({'reader_module':str(path),'settings':dict(settings)}))
"""
    completed = subprocess.run(
        [str(interpreter), "-c", code, str(root), str(helper), str(release)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(release / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["settings"]["threads"] == "2"
    assert result["settings"]["max_temp_directory_size"] == "953.6 MiB"
