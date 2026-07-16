#!/usr/bin/env bash
set -euo pipefail

readonly UV_REQUIRED_VERSION="0.11.29"
readonly SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIRECTORY}/.." && pwd)"

cd "${REPOSITORY_ROOT}"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv ${UV_REQUIRED_VERSION} is required; see docs/development.md" >&2
    exit 2
fi

uv_version_output="$(uv --version)"
uv_version="${uv_version_output#uv }"
uv_version="${uv_version%% *}"
if [[ "${uv_version}" != "${UV_REQUIRED_VERSION}" ]]; then
    echo "uv ${UV_REQUIRED_VERSION} is required; found ${uv_version}" >&2
    exit 2
fi

uv lock --check
uv sync --locked --extra dev
uv run --locked --extra dev python -m ruff format --check .
uv run --locked --extra dev python -m ruff check .
uv run --locked --extra dev python -m pytest \
    --cov=investment_analyst \
    --cov-report=term-missing

audit_arguments=(--local --skip-editable --progress-spinner off)
if [[ -n "${PIP_AUDIT_CACHE_DIR:-}" ]]; then
    audit_arguments+=(--cache-dir "${PIP_AUDIT_CACHE_DIR}")
fi
uv run --locked --extra dev pip-audit "${audit_arguments[@]}"
