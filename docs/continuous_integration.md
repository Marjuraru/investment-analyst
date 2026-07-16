# Continuous integration

The `CI` GitHub Actions workflow validates the repository with the same static and test checks used
locally. It runs for every pull request, every push to `main`, and manual dispatches. Requiring the
check for merges remains a separate branch-protection setting.

## Environment and commands

The job uses the fixed `ubuntu-24.04` runner label and Python 3.12, matching the supported project
runtime. It installs the project and its development extra from `pyproject.toml`, then executes:

```bash
python -m ruff check .
python -m ruff format --check .
python -m pytest
```

`setup-python` caches pip downloads using `pyproject.toml` as the dependency cache key input. The
cache improves installation time but is never treated as a source of truth; every run still executes
the editable installation.

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

## Scope and limitations

CI validates the deterministic local test suite on one supported Linux/Python combination. It does
not run the real-provider Apple bootstrap, mutate the permanent workspace, deploy software, execute
operations, or replace the real read-only validation required for provider-sensitive changes.

Dependency versions currently follow the ranges declared in `pyproject.toml`; there is no committed
lock file. Action SHAs and dependency ranges therefore need explicit maintenance over time.

After this workflow is merged and observed passing, repository branch protection can require the
`Python 3.12 quality` check before merging future pull requests.
