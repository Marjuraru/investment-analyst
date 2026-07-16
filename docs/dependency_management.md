# Dependency management

The project keeps two complementary dependency contracts:

- `pyproject.toml` declares supported direct dependency ranges and development tools.
- `uv.lock` records the exact cross-platform resolution, artifact hashes, and transitive dependency
  graph used by development and CI.

`uv.lock` is generated and validated with `uv 0.11.29`. It is committed to Git and must not be
edited manually. Runtime application behavior does not import or depend on uv.

## Reproduce the validated environment

Install the required uv version as documented in `docs/development.md`, then run:

```bash
uv lock --check
uv sync --locked --extra dev
```

The first command checks consistency without changing the lock. The second performs an exact sync,
including the `dev` optional dependency set. A missing or stale lock is a hard failure.

For an isolated verification that does not reuse the repository `.venv`, select another absolute
environment path:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/investment-analyst-clean \
uv sync --locked --extra dev --python /usr/bin/python3.12
```

## Add or update a dependency

Edit the direct constraint in `pyproject.toml`, then deliberately resolve the change:

```bash
uv lock
bash scripts/check.sh
```

Upgrade only one already-declared package while preserving the rest of the resolution:

```bash
uv lock --upgrade-package PACKAGE
bash scripts/check.sh
```

Every dependency change must review both `pyproject.toml` and `uv.lock`, record the reason for the
upgrade, pass the full test and coverage gate, and pass the vulnerability audit. Do not regenerate
the complete lock merely to obtain unrelated newest versions.

## Coverage policy

Coverage measures production code under `src/investment_analyst`, with branch coverage enabled.
Coverage.py subprocess instrumentation includes Python commands launched by CLI integration tests.
The measured Step 27 baseline is 82.47%; CI enforces a conservative 82.00% floor to prevent material
regression without depending on rounding at the hundredth of a percent.

The threshold is a regression guard, not a claim that every behavior is tested. High-risk changes
still require focused success, failure, point-in-time, idempotence, and traceability tests.

## Vulnerability audit

`pip-audit` scans the exact synchronized environment, skips only the editable local project, and
returns a failing exit code for known vulnerabilities. It does not update packages automatically or
ignore advisories by default.

An ignored advisory requires an explicit documented risk assessment, exact advisory ID, expiration
or review condition, and separate user approval. Advisory-service availability is external state;
diagnose network or service failures separately from vulnerability findings.
