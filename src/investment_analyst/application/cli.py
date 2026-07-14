"""Shared command-line helpers for centralized application storage resolution."""

import argparse
from pathlib import Path

from investment_analyst.application.runtime import StorageLocationRequest


def add_storage_location_arguments(parser: argparse.ArgumentParser) -> None:
    """Add compatible mutually exclusive workspace and legacy-root arguments."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--workspace",
        type=Path,
        help="Initialized investment-analyst workspace.",
    )
    group.add_argument(
        "--root",
        type=Path,
        help="Legacy direct storage root retained for compatibility.",
    )


def storage_location_from_namespace(namespace: argparse.Namespace) -> StorageLocationRequest:
    """Build one validated location request from a parsed argparse namespace."""
    return StorageLocationRequest(
        workspace=getattr(namespace, "workspace", None),
        legacy_root=getattr(namespace, "root", None),
    )
