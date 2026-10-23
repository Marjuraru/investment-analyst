"""Executed JavaScript behavior for view reopen, preview bounds and request coalescing."""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib.resources import files

import pytest

_APP_CORE = (
    files("investment_analyst.frontend").joinpath("static/app-core.js").read_text(encoding="utf-8")
)
_FUNCTIONS = (
    "currentViewContext",
    "readViewContexts",
    "persistViewContext",
    "restoreLastViewContext",
    "clearViewContext",
    "showPreviousCutNotice",
    "clearPreviousCutNotice",
    "updateReadRuntimeContext",
    "rememberViewPreview",
    "loadViewPreview",
    "api",
)


def _function_source(name: str) -> str:
    start = _APP_CORE.index(f"function {name}(")
    brace = _APP_CORE.index("{\n", start)
    depth = 0
    for index in range(brace, len(_APP_CORE)):
        character = _APP_CORE[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return _APP_CORE[start : index + 1]
    raise AssertionError(f"function {name} is not closed")


def _node_script() -> str:
    preamble = r"""
const VIEW_CONTEXT_STORAGE_KEY = 'investment-analyst-view-context-v1';
const VIEW_CONTEXT_PREVIEW_PREFIX = 'investment-analyst-view-preview-v1:';
const VIEW_CONTEXT_MAX_ENTRIES = 8;
const VIEW_CONTEXT_MAX_BYTES = 2 * 1024 * 1024;
const VIEW_CONTEXT_PREVIEW_MAX_BYTES = 256 * 1024;
const ERROR_MESSAGES = {};
const apiInFlightRequests = new Map();
let currentWorkspaceId = null;
let readRuntimeGeneration = null;
let selectedMarketAsset = 'equity:us:amd';
let selectedFundamentalFrequency = 'annual';
const location = { hash: '#activo' };
const memory = new Map();
const sessionStorage = {
  getItem: (key) => (memory.has(key) ? memory.get(key) : null),
  setItem: (key, value) => memory.set(key, String(value)),
  removeItem: (key) => memory.delete(key),
  get length() { return memory.size; },
  key: (index) => Array.from(memory.keys())[index] ?? null,
};
const notice = {
  textContent: '',
  dataset: {},
  classList: { remove() {}, add() {} },
};
function byId(id) {
  if (id === 'report-known-at') return { value: '2026-10-22T12:00:00Z' };
  if (id === 'global-message') return notice;
  return null;
}
"""
    body = r"""
async function run() {
  let passed = 0;
  updateReadRuntimeContext({
    workspace_id: '70c64668-1738-4dfe-ba1f-7e72026bb7fe',
    read_runtime: { generation: 'gen-1' },
  });
  const first = currentViewContext();
  if (first.workspaceId !== '70c64668-1738-4dfe-ba1f-7e72026bb7fe') {
    throw new Error('workspace');
  }
  if (!persistViewContext(first)) throw new Error('persist');
  const restored = restoreLastViewContext();
  if (restored.knownAt !== '2026-10-22T12:00:00Z' || restored.board !== 'activo') {
    throw new Error('restore');
  }
  passed += 1;
  for (let index = 0; index < 10; index += 1) {
    persistViewContext({ ...first, board: `board-${index}` });
  }
  if (readViewContexts().length !== VIEW_CONTEXT_MAX_ENTRIES) {
    throw new Error('bound');
  }
  passed += 1;
  if (rememberViewPreview('small', { value: 1 }) !== true) {
    throw new Error('preview-store');
  }
  if (loadViewPreview('small').value !== 1) throw new Error('preview-load');
  const large = { value: 'x'.repeat(300 * 1024) };
  if (rememberViewPreview('large', large) !== false) {
    throw new Error('preview-limit');
  }
  passed += 1;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  const [a, b] = await Promise.all([
    api('/api/v1/overview'),
    api('/api/v1/overview'),
  ]);
  if (!a.ok || !b.ok || calls !== 1) throw new Error('coalesce');
  passed += 1;
  let attempts = 0;
  globalThis.fetch = async () => {
    attempts += 1;
    if (attempts === 1) throw new Error('network');
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  try { await api('/api/v1/retry'); } catch (error) { /* expected */ }
  const retry = await api('/api/v1/retry');
  if (!retry.ok || attempts !== 2) throw new Error('release');
  passed += 1;
  console.log(JSON.stringify({ passed }));
}
run().catch((error) => { console.error(error); process.exit(1); });
"""
    functions = "\n".join(_function_source(name) for name in _FUNCTIONS)
    return f"{preamble}\n{functions}\n{body}"


def test_view_context_reopen_and_request_coalescing_behavior() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the real JavaScript reopen smoke")
    result = subprocess.run(
        [node, "-"],
        input=_node_script(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": 5}
