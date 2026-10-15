"""Contract tests for workspace-v2 lifecycle state."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.workspace.workspace_v2_lifecycle_models import (
    FrozenSourceCut,
    LifecycleContentRole,
    LifecycleFile,
    LifecycleInventory,
)


def _source() -> FrozenSourceCut:
    return FrozenSourceCut(
        operation_id=uuid4(),
        source_root=Path("/tmp/source"),
        workspace_id=uuid4(),
        workspace_format_version=1,
        created_at=datetime(2026, 10, 13, tzinfo=UTC),
        source_fingerprint="a" * 64,
        frozen_at=datetime(2026, 10, 13, tzinfo=UTC),
    )


def test_inventory_requires_exact_role_totals_and_unknown_count() -> None:
    source = _source()
    roles = {role: 0 for role in LifecycleContentRole}
    roles[LifecycleContentRole.UNKNOWN] = 1
    inventory = LifecycleInventory(
        frozen_source=source,
        file_count=1,
        logical_bytes=0,
        roles=roles,
        unknown_file_count=1,
    )
    assert inventory.roles[LifecycleContentRole.UNKNOWN] == 1

    with pytest.raises(ValidationError, match="unknown count"):
        LifecycleInventory(
            frozen_source=source,
            file_count=1,
            logical_bytes=0,
            roles=roles,
            unknown_file_count=0,
        )


def test_lifecycle_file_rejects_unsafe_path_and_digest() -> None:
    with pytest.raises(ValidationError, match="normalized"):
        LifecycleFile(
            path="../outside",
            role=LifecycleContentRole.UNKNOWN,
            size_bytes=0,
            sha256="a" * 64,
        )
    with pytest.raises(ValidationError, match="sha256"):
        LifecycleFile(
            path="state/a.json",
            role=LifecycleContentRole.OPERATIONAL_HISTORIC,
            size_bytes=0,
            sha256="not-a-digest",
        )
