# Continuous integration

The `CI` GitHub Actions workflow validates the repository with the same static and test checks used
locally. It runs for every pull request, every push to `main`, and manual dispatches. Branch
protection requires the uniquely named `Python 3.12 quality` check before merging into `main`.

## Environment and commands

The job uses the fixed `ubuntu-24.04` runner label and Python 3.12, matching the supported project
runtime. It bootstraps the pinned `uv 0.11.29` release, verifies that `uv.lock` matches
`pyproject.toml`, synchronizes the exact locked development environment, then executes:

```bash
uv lock --check
uv sync --locked --extra dev
uv run --locked --extra dev python -m ruff check .
uv run --locked --extra dev python -m ruff format --check .
uv run --locked --extra dev python -m pytest \
  --cov=investment_analyst \
  --cov-report=term-missing
uv run --locked --extra dev pip-audit \
  --local \
  --skip-editable \
  --progress-spinner off
```

`--locked` checks project metadata instead of silently changing the lock. `uv sync` performs an exact
environment synchronization and installs the project in editable mode. `setup-python` caches only
pip downloads used to bootstrap the pinned uv version; the lock remains the dependency source of
truth.

Coverage uses branch measurement and subprocess instrumentation from `pyproject.toml`. The current
525-test baseline is 82.47%, and the enforced floor is 82.00%. `pip-audit` examines the exact
installed locked environment and fails when its vulnerability service reports a known issue.

## Security and resource limits

- The workflow-level token permission is limited to `contents: read`.
- Checkout does not persist GitHub credentials in the working copy.
- Third-party workflow code is limited to official GitHub actions pinned to full immutable commit
  SHAs. The adjacent release comments record the reviewed versions.
- The workflow uses `pull_request`, not `pull_request_target`, so untrusted pull-request code does
  not receive a privileged base-repository context.
- A concurrency group cancels superseded runs for the same workflow and ref.
- The job has a 20-minute timeout and never calls market or fundamental providers.
- CI requires no Alpaca, SEC, broker, or trading credentials.
- Dependency synchronization cannot update `uv.lock` during CI.
- The security audit never applies automatic dependency fixes.

## Scope and limitations

CI validates the deterministic local test suite on one supported Linux/Python combination. It does
not run the real-provider Apple bootstrap, mutate the permanent workspace, deploy software, execute
operations, or replace the real read-only validation required for provider-sensitive changes.

The committed lock makes installation repeatable, but vulnerability findings and package releases
can change over time. Dependency upgrades therefore remain explicit reviewed changes. The security
audit requires outbound access to its public advisory service; an advisory-service outage is an
environmental CI failure and must not be confused with a product-test failure.

See `docs/dependency_management.md` for lock maintenance and upgrade rules.
