#!/usr/bin/env bash
set -euo pipefail

ruff format --check .
ruff check .
pytest
