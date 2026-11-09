"""Static behavior contract for the descriptive outcomes UI panel."""

from importlib.resources import files
from pathlib import Path

from investment_analyst.alerts.descriptive_outcome_models import (
    FORWARD_CLOSE_INTERVALS_HORIZON,
)

_STATIC = files("investment_analyst.frontend").joinpath("static")


def _read(name: str) -> str:
    return _STATIC.joinpath(name).read_text(encoding="utf-8")


OPERATIONS = _read("app-operations.js")
CORE = _read("app-core.js")
DESIGN_TEST = Path(__file__).with_name("test_design_system.py").read_text(encoding="utf-8")
LOCAL_DOC = (Path(__file__).resolve().parents[3] / "docs" / "local_interface.md").read_text(
    encoding="utf-8"
)


def _function_slice(source: str, signature: str) -> str:
    start = source.index(signature)
    endings = tuple(
        position
        for marker in ("\nfunction ", "\nasync function ", "\nbyId(", "\nconst ")
        if (position := source.find(marker, start + len(signature))) != -1
    )
    return source[start : min(endings)] if endings else source[start:]


def test_only_the_two_incremental_rules_expose_the_action() -> None:
    definitions = _function_slice(OPERATIONS, "const DESCRIPTIVE_OUTCOME_RULE_IDS")
    assert '"market.technical.rsi-incremental-low-review"' in definitions
    assert '"market.technical.macd-incremental-positive-histogram-review"' in definitions
    assert definitions.count('"market.technical.') == 2
    render = _function_slice(OPERATIONS, "function renderDescriptiveOutcomeAction(")
    assert "if (!DESCRIPTIVE_OUTCOME_RULE_IDS.has(ruleId)) return;" in render
    screening = _function_slice(OPERATIONS, "function renderScreeningRule(")
    assert "renderDescriptiveOutcomeAction(configuration, card);" in screening
    assert "screening-backtest-result screening-outcomes" in render


def test_action_is_manual_bounded_and_shows_dates_cut_and_fingerprint() -> None:
    render = _function_slice(OPERATIONS, "function renderDescriptiveOutcomeAction(")
    assert '"Resultados posteriores"' in render
    assert '"Descargar JSON"' in render
    assert "DESCRIPTIVE_OUTCOME_DEFAULT_DAYS" in render
    assert "DESCRIPTIVE_OUTCOME_WARNING" in OPERATIONS
    assert "configuration.fingerprint" in render
    assert "knownAt" in render
    assert 'startInput.type = "date"' in render
    assert 'endInput.type = "date"' in render
    assert "runDescriptiveOutcomes(" in render
    assert 'run.addEventListener("click"' in render
    assert "void runDescriptiveOutcomes(" in render
    # No automatic request: the handler is the only caller in rendering.
    assert render.count("runDescriptiveOutcomes(") == 1
    load = _function_slice(OPERATIONS, "async function loadScreeningRules(")
    assert "runDescriptiveOutcomes(" not in load
    assert "fetchDescriptiveOutcomes(" not in load


def test_run_uses_only_the_declared_route_fingerprint_cut_and_generation_guard() -> None:
    run = _function_slice(OPERATIONS, "async function runDescriptiveOutcomes(")
    assert "fetchDescriptiveOutcomes(query, controller.signal)" in run
    assert "AbortController" in run
    assert "state.generation += 1;" in run
    assert "state.requestKey !== requestKey" in run
    assert "new URLSearchParams({" in run
    assert "asset_id: selectedMarketAsset" in run
    assert "rule_id: ruleId" in run
    assert "expected_rule_fingerprint: configuration.fingerprint" in run
    assert "start_date: start" in run
    assert "end_date: end" in run
    assert "known_at: knownAt" in run
    assert "days < 1 || days > DESCRIPTIVE_OUTCOME_MAX_DAYS" in run
    fetch = _function_slice(OPERATIONS, "async function fetchDescriptiveOutcomes(")
    assert "`/api/v1/screening/outcomes?${query.toString()}`" in fetch
    assert "response.text()" in fetch
    assert "DESCRIPTIVE_OUTCOME_MAX_BYTES" in fetch
    assert "JSON.parse(text)" in fetch
    assert "DESCRIPTIVE_OUTCOME_ERROR_MESSAGES[error.code]" in fetch


def test_invalidation_covers_asset_cut_rule_edit_save_and_late_responses() -> None:
    assert (
        'if (typeof invalidateScreeningOutcomes === "function") invalidateScreeningOutcomes();'
        in CORE
    )
    render_cut = _function_slice(CORE, "function renderKnownAtCut(")
    expected_cut_guard = (
        "const cutChanged = renderedKnownAtCut !== null && renderedKnownAtCut !== knownAt;"
    )
    assert expected_cut_guard in render_cut
    assert "renderedKnownAtCut = knownAt;" in render_cut
    assert "invalidateScreeningOutcomes();" in render_cut
    assert "let renderedKnownAtCut = null;" in CORE
    assert (
        'byId("report-known-at").addEventListener("change", () => invalidateScreeningOutcomes());'
        not in OPERATIONS
    )
    asset_start = CORE.index("async function selectComboboxOption(")
    asset_guard = CORE.index(
        'if (typeof invalidateScreeningOutcomes === "function") invalidateScreeningOutcomes();',
        asset_start,
    )
    assert asset_guard > asset_start
    assert asset_guard < CORE.index(
        'byId("report-known-at").value = knownAtByAsset.get(assetId)',
        asset_start,
    )
    form = _function_slice(OPERATIONS, "function renderScreeningRule(")
    assert 'form.addEventListener("input"' in form
    assert 'form.addEventListener("change"' in form
    update = _function_slice(OPERATIONS, "async function updateScreeningRule(")
    assert "invalidateDescriptiveOutcome(configuration.rule.rule_id);" in update
    invalidate = _function_slice(OPERATIONS, "function invalidateDescriptiveOutcome(")
    assert "state.controller.abort();" in invalidate
    assert "state.payload = null;" in invalidate
    assert "state.text = null;" in invalidate
    assert "Consulta invalidada por un cambio de activo, corte o regla." in invalidate


def test_download_uses_the_received_body_bounded_to_256_kib_and_safe_text() -> None:
    download = _function_slice(OPERATIONS, "function downloadDescriptiveOutcomeJson(")
    assert "state.text" in download
    assert "downloadText(" in download
    assert "state.text," in download
    assert '"application/json"' in download
    assert "new TextEncoder().encode(state.text).byteLength" in download
    assert "DESCRIPTIVE_OUTCOME_MAX_BYTES" in download
    assert "innerHTML" not in OPERATIONS
    render = _function_slice(OPERATIONS, "function renderDescriptiveOutcomeReport(")
    assert "createElement(" in render
    assert "preferences-table" in render
    assert "chart-table-scroll" in render


def test_report_table_is_bounded_and_declares_the_required_warning() -> None:
    render = _function_slice(OPERATIONS, "function renderDescriptiveOutcomeReport(")
    assert ".slice(0, DESCRIPTIVE_OUTCOME_MAX_DAYS)" in render
    assert "DESCRIPTIVE_OUTCOME_WARNING" in render
    assert '"Período", "Condición", "Resultado", "R (5 intervalos)", "Duración"' in OPERATIONS
    expected_warning = (
        "Revisión descriptiva de condiciones; no mide rentabilidad ejecutable ni valida predicción."
    )
    assert expected_warning in OPERATIONS
    assert "matched" in render and "not_matched" in render and "not_evaluable" in render
    assert "mean_return_5_intervals" in render
    assert "median_return_5_intervals" in render
    assert "ranking" not in render.lower()
    assert "score" not in render.lower()


def test_previous_replay_remains_separate_and_unchanged() -> None:
    backtest = _function_slice(OPERATIONS, "async function runScreeningBacktest(")
    assert "/api/screening-backtest?" in backtest
    assert "renderScreeningBacktest(" in backtest
    assert "runDescriptiveOutcomes(" not in backtest
    assert "renderDescriptiveOutcomeAction(" not in backtest
    screening = _function_slice(OPERATIONS, "function renderScreeningRule(")
    assert "runScreeningBacktest(configuration, replay, backtest)" in screening
    assert "renderDescriptiveOutcomeAction(configuration, card);" in screening


def test_route_and_design_baseline_are_documented_and_statically_known() -> None:
    assert "/api/v1/screening/outcomes" in OPERATIONS
    assert '"/api/v1/screening/outcomes",' in DESIGN_TEST
    for token in (
        "Resultados posteriores",
        "/api/v1/screening/outcomes",
        "descriptive-condition-outcomes-v1",
        "forward_observed_close_intervals_5-v1",
        "expected_rule_fingerprint",
        "256 KiB",
        "no mide rentabilidad ejecutable",
    ):
        assert token in LOCAL_DOC, token
    assert FORWARD_CLOSE_INTERVALS_HORIZON == "forward_observed_close_intervals_5-v1"
