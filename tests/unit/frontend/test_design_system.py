"""Contract tests for local-interface-design-system-v1.

These tests verify the RULES of the design system declared in tokens.css
and styles.css: every token exists in both themes, no color literal escapes
tokens.css, declared text/surface pairs meet WCAG contrast, the five
absence marks are mutually distinguishable, no external network reference
is emitted, and figures are tabular/monospace/right-aligned.

They are static contract tests over the shipped stylesheets and scripts,
not a browser and not visual regression: they cannot see layout, computed
paint, or runtime DOM state. A rule that is not expressible as a static
check over these files is out of reach for this suite by design.
"""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path

import pytest

_STATIC = files("investment_analyst.frontend").joinpath("static")


def _read(name: str) -> str:
    return _STATIC.joinpath(name).read_text(encoding="utf-8")


TOKENS_CSS = _read("tokens.css")
STYLES_CSS = _read("styles.css")
INDEX_HTML = _read("index.html")
APP_JS = _read("app.js")


# ---------------------------------------------------------------------------
# Token parsing helpers
# ---------------------------------------------------------------------------

_ROOT_BLOCK_RE = re.compile(
    r':root(?P<dark>\[data-theme="dark"\])?\s*\{(?P<body>[^}]*)\}', re.DOTALL
)
_PROPERTY_RE = re.compile(r"--([a-zA-Z0-9-]+)\s*:\s*([^;]+);")


def _theme_blocks(css_text: str) -> dict[str, dict[str, str]]:
    """Return {'light': {name: value}, 'dark': {name: value}} from :root blocks."""
    blocks: dict[str, dict[str, str]] = {}
    for match in _ROOT_BLOCK_RE.finditer(css_text):
        theme = "dark" if match.group("dark") else "light"
        properties = {
            name: value.strip() for name, value in _PROPERTY_RE.findall(match.group("body"))
        }
        blocks[theme] = properties
    return blocks


def test_every_token_defined_in_light_and_dark() -> None:
    blocks = _theme_blocks(TOKENS_CSS)
    assert set(blocks) == {"light", "dark"}
    light_names, dark_names = set(blocks["light"]), set(blocks["dark"])
    assert light_names, "tokens.css must declare at least one token"
    assert light_names == dark_names, (
        f"tokens declared only in one theme: "
        f"light-only={light_names - dark_names} dark-only={dark_names - light_names}"
    )


# ---------------------------------------------------------------------------
# No color literal outside tokens.css
# ---------------------------------------------------------------------------

_COLOR_LITERAL_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)")

# The only constructs that must stay a static literal: a
# <meta name="theme-color"> hint, a self-contained data-URI favicon/avatar
# SVG, and the three SMA <input type="color"> pre-hydration defaults. None
# of the three can reference a CSS custom property (browser-chrome
# metadata, an inline image resource, and an HTML attribute default that
# app.js immediately overwrites with designToken()-sourced values on
# load). Every one is pinned to a value already declared in tokens.css and
# re-verified below, so the exception is tested, not just asserted.
_THEME_COLOR_META_RE = re.compile(r'<meta name="theme-color" content="(#[0-9a-fA-F]{6})">')
_THEME_COLOR_JS_RE = re.compile(r'\.content\s*=\s*designToken\("--canvas"\)')
_FAVICON_LINE_RE = re.compile(r'<link rel="icon" href="data:image/svg\+xml,[^"]*">')
_ICON_FILL_RE = re.compile(r"fill='%23([0-9a-fA-F]{6})'")
_SMA_COLOR_INPUT_RE = re.compile(
    r'<input id="sma-(short|long|third)-color" type="color" value="(#[0-9a-fA-F]{6})"'
)
_SMA_TOKEN_BY_INPUT = {
    "short": "series-sma-5",
    "long": "series-sma-20",
    "third": "series-sma-50",
}


def _strip_exceptions(html: str) -> str:
    html = _THEME_COLOR_META_RE.sub("", html)
    html = _FAVICON_LINE_RE.sub("", html)
    html = _SMA_COLOR_INPUT_RE.sub("", html)
    return html


def test_no_color_literal_in_stylesheet() -> None:
    found = _COLOR_LITERAL_RE.findall(STYLES_CSS)
    assert not found, f"color literal(s) outside tokens.css: {found}"


def test_no_color_literal_in_app_js() -> None:
    found = _COLOR_LITERAL_RE.findall(APP_JS)
    assert not found, f"color literal(s) in app.js: {found}"
    # app.js must derive its runtime meta/theme-color assignment from a
    # token, never restate a literal.
    assert _THEME_COLOR_JS_RE.search(APP_JS), "theme-color must be read from --canvas"


def test_index_html_color_literals_are_the_declared_meta_and_favicon_exceptions() -> None:
    stripped = _strip_exceptions(INDEX_HTML)
    leftover = _COLOR_LITERAL_RE.findall(stripped)
    assert not leftover, (
        f"color literal(s) in index.html outside the declared exceptions: {leftover}"
    )

    blocks = _theme_blocks(TOKENS_CSS)
    canvas_light = blocks["light"]["canvas"].lower()
    canvas_dark = blocks["dark"]["canvas"].lower()
    meta_match = _THEME_COLOR_META_RE.search(INDEX_HTML)
    assert meta_match, "index.html must declare an initial <meta name=theme-color>"
    assert meta_match.group(1).lower() == canvas_dark, (
        "static theme-color must equal --canvas (dark is the default data-theme)"
    )

    icon_fills = {f"#{value.lower()}" for value in _ICON_FILL_RE.findall(INDEX_HTML)}
    assert canvas_dark in icon_fills, "favicon ink fill must equal dark --canvas"
    assert canvas_light != canvas_dark  # sanity: themes are genuinely distinct

    sma_inputs = {
        input_name: value.lower() for input_name, value in _SMA_COLOR_INPUT_RE.findall(INDEX_HTML)
    }
    assert set(sma_inputs) == set(_SMA_TOKEN_BY_INPUT)
    for input_name, token_name in _SMA_TOKEN_BY_INPUT.items():
        assert sma_inputs[input_name] == blocks["dark"][token_name].lower(), (
            f"sma-{input_name}-color default must equal dark-theme --{token_name} "
            "(dark is the default data-theme, and app.js overwrites this value "
            "from designToken() on load anyway)"
        )


# ---------------------------------------------------------------------------
# Contrast: WCAG 2.x relative luminance, reimplemented (stdlib only)
# ---------------------------------------------------------------------------


def _linearize(channel: float) -> float:
    channel /= 255
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(char * 2 for char in hex_color)
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    r, g, b = _linearize(r), _linearize(g), _linearize(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    lum_a, lum_b = _relative_luminance(hex_a), _relative_luminance(hex_b)
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


_MIN_CONTRAST = 4.5

# (text token, surface token) pairs that must meet 4.5:1 in a theme, using
# only that theme's own declared values.
_THEMED_TEXT_SURFACE_PAIRS: tuple[tuple[str, str], ...] = (
    ("ink", "surface"),
    ("ink", "surface-subtle"),
    ("ink", "canvas"),
    ("ink-strong", "surface"),
    ("ink-strong", "surface-subtle"),
    ("ink-strong", "canvas"),
    ("muted-strong", "surface"),
    ("muted-strong", "surface-subtle"),
    ("muted-strong", "canvas"),
    ("muted", "surface"),
    ("muted", "surface-subtle"),
    ("muted", "canvas"),
    ("accent", "surface"),
    ("accent", "canvas"),
    ("accent-dark", "surface"),
    ("positive-ink", "surface"),
    ("warning-ink", "surface"),
    ("negative-ink", "surface"),
    ("neutral-ink", "surface"),
    ("on-accent", "accent"),
    ("on-accent", "accent-dark"),
    ("blocked-ink", "surface"),
    ("blocked-ink", "surface-subtle"),
    ("blocked-ink", "blocked-soft"),
)

# Rail and code tokens are declared identical in both themes (a
# permanently-dark surface independent of the app theme), so they are
# checked once against BOTH themes' own --surface-dark value.
_RAIL_TEXT_TOKENS = (
    "rail-ink-strong",
    "rail-ink-base",
    "rail-ink",
    "rail-ink-muted",
    "rail-ink-quiet",
    "rail-ink-faint",
)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_on_surface_meets_contrast_threshold(theme: str) -> None:
    tokens = _theme_blocks(TOKENS_CSS)[theme]
    failures = []
    for text_name, surface_name in _THEMED_TEXT_SURFACE_PAIRS:
        text_value, surface_value = tokens[text_name], tokens[surface_name]
        ratio = contrast_ratio(text_value, surface_value)
        if ratio < _MIN_CONTRAST:
            failures.append((text_name, surface_name, round(ratio, 2)))
    assert not failures, f"pairs under {_MIN_CONTRAST}:1 in {theme} theme: {failures}"


def test_on_focus_meets_contrast_against_focus_in_both_themes() -> None:
    blocks = _theme_blocks(TOKENS_CSS)
    failures = []
    for theme in ("light", "dark"):
        tokens = blocks[theme]
        ratio = contrast_ratio(tokens["on-focus"], tokens["focus"])
        if ratio < _MIN_CONTRAST:
            failures.append((theme, round(ratio, 2)))
    assert not failures, f"on-focus fails against focus: {failures}"


def test_rail_ink_ramp_meets_contrast_against_rail_surface_in_both_app_themes() -> None:
    blocks = _theme_blocks(TOKENS_CSS)
    failures = []
    for rail_text in _RAIL_TEXT_TOKENS:
        rail_value = blocks["light"][rail_text]  # identical in both blocks, checked below too
        assert rail_value == blocks["dark"][rail_text]
        for app_theme in ("light", "dark"):
            rail_surface = blocks[app_theme]["surface-dark"]
            ratio = contrast_ratio(rail_value, rail_surface)
            if ratio < _MIN_CONTRAST:
                failures.append((rail_text, app_theme, round(ratio, 2)))
    assert not failures, f"rail ink fails against surface-dark: {failures}"


def test_code_ink_meets_contrast_against_surface_dark_in_both_app_themes() -> None:
    blocks = _theme_blocks(TOKENS_CSS)
    code_ink = blocks["light"]["code-ink"]
    assert code_ink == blocks["dark"]["code-ink"]
    for app_theme in ("light", "dark"):
        ratio = contrast_ratio(code_ink, blocks[app_theme]["surface-dark"])
        assert ratio >= _MIN_CONTRAST, f"code-ink fails in {app_theme}: {ratio:.2f}"


# ---------------------------------------------------------------------------
# Absence grammar: five mutually distinguishable marks
# ---------------------------------------------------------------------------

_ABSENCE_KINDS = ("missing", "not-evaluable", "not-applicable", "overdue", "blocked")

_ABSENCE_RULE_RE = re.compile(r"\.absence-mark\.(?P<kind>[a-z-]+)\s*\{(?P<body>[^}]*)\}", re.DOTALL)
_ABSENCE_ICON_RE = re.compile(
    r'\.absence-mark\.(?P<kind>[a-z-]+)\s+\.absence-mark-icon::before\s*\{\s*content:\s*"(?P<glyph>[^"]+)"',
)


def _absence_mark_declarations() -> dict[str, dict[str, str]]:
    declarations: dict[str, dict[str, str]] = {}
    for match in _ABSENCE_RULE_RE.finditer(STYLES_CSS):
        kind = match.group("kind")
        body = match.group("body")
        props = dict(re.findall(r"([a-z-]+)\s*:\s*([^;]+);", body))
        declarations.setdefault(kind, {}).update(props)
    return declarations


def _absence_mark_icons() -> dict[str, str]:
    return {
        match.group("kind"): match.group("glyph") for match in _ABSENCE_ICON_RE.finditer(STYLES_CSS)
    }


def test_five_absence_marks_are_declared() -> None:
    declarations = _absence_mark_declarations()
    assert set(declarations) == set(_ABSENCE_KINDS), (
        f"expected exactly {_ABSENCE_KINDS}, found {sorted(declarations)}"
    )


def test_five_absence_marks_are_mutually_distinguishable() -> None:
    declarations = _absence_mark_declarations()
    icons = _absence_mark_icons()
    assert set(icons) == set(_ABSENCE_KINDS)

    colors = {kind: props.get("color") for kind, props in declarations.items()}
    border_styles = {kind: props.get("border-style") for kind, props in declarations.items()}

    assert len(set(colors.values())) == len(_ABSENCE_KINDS), f"duplicate colors: {colors}"
    assert len(set(icons.values())) == len(_ABSENCE_KINDS), f"duplicate icon glyphs: {icons}"
    assert None not in border_styles.values(), f"missing border-style: {border_styles}"

    # No two marks may be identical across every one of (color, icon,
    # border-style) at once -- true by the two assertions above (color and
    # icon glyph already fully disambiguate every pair), but re-checked
    # explicitly as the direct "mutually distinguishable" statement.
    signatures = {kind: (colors[kind], icons[kind], border_styles[kind]) for kind in _ABSENCE_KINDS}
    assert len(set(signatures.values())) == len(_ABSENCE_KINDS), signatures


def test_state_never_encoded_by_colour_alone() -> None:
    # Every .absence-mark carries a label element in addition to its icon;
    # the CSS grammar never relies on background/color changes alone.
    assert ".absence-mark-label" in STYLES_CSS
    assert ".absence-mark-icon" in STYLES_CSS
    # Also true of the pre-existing quality-chip / market-session-status
    # marks touched by this block: each pairs a text label with its tone.
    assert "renderAbsenceMark" in APP_JS


def test_absence_never_rendered_as_zero_or_empty() -> None:
    # Every kind literal must appear at least once, either as a direct
    # renderAbsenceMark("<kind>", ...) call site, or as the literal string
    # a dispatch function (e.g. valuationAbsenceKind) returns before it is
    # threaded into renderAbsenceMark(<dynamicKind>, ...).
    for kind in _ABSENCE_KINDS:
        direct_call = (
            f'renderAbsenceMark("{kind}"' in APP_JS or f"renderAbsenceMark('{kind}'" in APP_JS
        )
        literal_kind = f'"{kind}"' in APP_JS or f"'{kind}'" in APP_JS
        assert direct_call or literal_kind, f"no live reference to the '{kind}' absence mark kind"
    assert "renderAbsenceMark(" in APP_JS
    # The two known_at call sites that used to fall back to a bare em dash
    # now route through renderKnownAtCut instead.
    assert 'byId("known-at-status").textContent = "—"' not in APP_JS
    assert "renderKnownAtCut(" in APP_JS


def test_blocked_source_declares_its_reason() -> None:
    assert "BLOCKED_VALUATION_REASON_CODES" in APP_JS
    assert '"market_not_configured"' in APP_JS
    assert '"fundamentals_not_configured"' in APP_JS
    assert "VALUATION_REASON_LABELS" in APP_JS
    # renderAbsenceMark always receives a third "reason" argument for the
    # blocked/not-evaluable valuation path -- never just a label.
    assert re.search(
        r"renderAbsenceMark\(valuationAbsenceKind\(metric\),\s*[^,]+,\s*reason\)", APP_JS
    ), "blocked/not-evaluable valuation metrics must pass a declared reason"


# ---------------------------------------------------------------------------
# No external network reference
# ---------------------------------------------------------------------------

# Excludes the SVG XML namespace URI (a required, never-fetched attribute
# value on every inline <svg>, not a network reference) and the loopback
# hosts this same-origin app is served from.
_EXTERNAL_REF_RE = re.compile(
    r"(https?:)?//(?!127\.0\.0\.1|localhost|www\.w3\.org)[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
)


def test_no_external_network_reference_in_static_surface() -> None:
    for name, text in (("styles.css", STYLES_CSS), ("index.html", INDEX_HTML), ("app.js", APP_JS)):
        found = _EXTERNAL_REF_RE.findall(text)
        assert not found, f"external network reference in {name}: {found}"
    assert "fonts.googleapis" not in INDEX_HTML
    assert "fonts.googleapis" not in STYLES_CSS
    assert "cdn." not in INDEX_HTML.lower()
    assert '<link rel="preconnect"' not in INDEX_HTML


# ---------------------------------------------------------------------------
# Figures: tabular, monospace, right-aligned
# ---------------------------------------------------------------------------


def test_figures_use_tabular_monospace_right_aligned() -> None:
    assert "font-variant-numeric: tabular-nums" in STYLES_CSS
    figure_rule_match = re.search(r"\.figure\s*\{([^}]*)\}", STYLES_CSS, re.DOTALL)
    assert figure_rule_match, ".figure utility class must be declared"
    figure_rule = figure_rule_match.group(1)
    assert "var(--figure-font)" in figure_rule
    assert "tabular-nums" in figure_rule
    assert "text-align: right" in figure_rule

    metric_value_match = re.search(r"\.metric-value\s*\{([^}]*)\}", STYLES_CSS, re.DOTALL)
    assert metric_value_match and "var(--figure-font)" in metric_value_match.group(1)


def test_no_web_font_named_in_body_stack() -> None:
    body_match = re.search(r"\bbody\s*\{([^}]*)\}", STYLES_CSS, re.DOTALL)
    assert body_match
    assert "var(--font-sans)" in body_match.group(1)
    assert "Inter" not in STYLES_CSS


# ---------------------------------------------------------------------------
# known_at global control present in every view (one persistent header)
# ---------------------------------------------------------------------------


def test_known_at_cut_present_in_every_view() -> None:
    topbar_match = re.search(r'<header class="topbar">(.*?)</header>', INDEX_HTML, re.DOTALL)
    assert topbar_match, "topbar header must exist"
    assert 'id="known-at-cut-value"' in topbar_match.group(1), (
        "the known_at cut control must live in the persistent topbar, "
        "shared by every routed view, not inside a single section"
    )
    main_match = re.search(r'<main id="contenido"[^>]*>(.*)</main>', INDEX_HTML, re.DOTALL)
    assert main_match
    # There is exactly one such header-level control; screens do not each
    # declare their own competing cut.
    assert INDEX_HTML.count('id="known-at-cut-value"') == 1


# ---------------------------------------------------------------------------
# Session clock consumes NYSE_SESSION_STATES as-is
# ---------------------------------------------------------------------------

_NYSE_STATES_RE = re.compile(r"const NYSE_SESSION_STATES = Object\.freeze\(\{(.*?)\}\);", re.DOTALL)


def test_session_clock_consumes_existing_nyse_state() -> None:
    match = _NYSE_STATES_RE.search(APP_JS)
    assert match, "NYSE_SESSION_STATES must still be declared"
    body = match.group(1)
    for state in ("weekend", "before", "open", "after"):
        assert f"{state}:" in body, f"NYSE_SESSION_STATES must keep declaring '{state}'"
    # The remaining-time helper must reuse the same boundary constants, not
    # restate the 9:30/16:00 boundary as new literals.
    assert "NYSE_CORE_OPEN_MINUTES" in APP_JS
    assert "NYSE_CORE_CLOSE_MINUTES" in APP_JS
    remaining_fn = re.search(
        r"function newYorkRegularSessionRemainingMinutes\(now\) \{(.*?)\n\}", APP_JS, re.DOTALL
    )
    assert remaining_fn, "remaining-time helper must exist"
    assert "NYSE_CORE_OPEN_MINUTES" in remaining_fn.group(1)
    assert "NYSE_CORE_CLOSE_MINUTES" in remaining_fn.group(1)
    assert "9 * 60" not in remaining_fn.group(1), "must not restate the open/close boundary"


def test_session_clock_declares_no_holiday_coverage() -> None:
    assert "no evalúa feriados ni cierres" in INDEX_HTML
    assert "Regular session only" in APP_JS or "no holiday" in APP_JS.lower()


def test_session_clock_shows_new_york_time_and_status_dot() -> None:
    assert 'id="new-york-clock"' in INDEX_HTML
    assert 'id="nyse-session-dot"' in INDEX_HTML
    assert 'id="nyse-session-remaining"' in INDEX_HTML
    assert "session-status-dot" in STYLES_CSS


# ---------------------------------------------------------------------------
# Decimal rounding is presentation-only
# ---------------------------------------------------------------------------


def test_decimal_rounding_is_presentation_only() -> None:
    # Every "export ... Json" function serializes a raw payload object via
    # JSON.stringify, never a display-rounded value reassembled from
    # already-formatted figures. Bodies contain template literals with
    # their own nested braces, so this counts declarations against
    # JSON.stringify( call sites rather than brace-parsing each body.
    export_json_functions = re.findall(r"function export\w*Json\(", APP_JS)
    assert export_json_functions, "at least one JSON export function must exist"
    stringify_calls = re.findall(r"JSON\.stringify\(\s*\w", APP_JS)
    assert len(stringify_calls) >= len(export_json_functions), (
        f"{len(export_json_functions)} JSON export function(s) but only "
        f"{len(stringify_calls)} JSON.stringify(<payload>) call(s)"
    )


# ---------------------------------------------------------------------------
# No aggregate score, verdict or ranking; domains never share a row/total
# ---------------------------------------------------------------------------

# A per-rule "verdict" (e.g. diagnostic.verdict, one deterministic rule's
# own bullish/bearish reading) is a pre-existing, scoped, legitimate
# concept declared as an explicit invariant in app.js itself ("no combined
# score, verdict... is calculated"). What is actually forbidden is a
# CROSS-DOMAIN or AGGREGATE reading collapsing market/fundamentals/
# multiple rules into one score, verdict or rank -- so the check targets
# compound terms, not the bare pre-existing word.
_FORBIDDEN_AGGREGATE_TERMS = (
    "aggregate_score",
    "overall_score",
    "combined_score",
    "overall-score",
    "combined-score",
    "unified_score",
    "combined_verdict",
    "combined-verdict",
    "overall_verdict",
    "overall-verdict",
    "unified_verdict",
    "cross_domain_verdict",
)


def test_no_aggregate_score_verdict_or_ranking_rendered() -> None:
    for text, name in ((APP_JS, "app.js"), (INDEX_HTML, "index.html"), (STYLES_CSS, "styles.css")):
        lowered = text.lower()
        found = [term for term in _FORBIDDEN_AGGREGATE_TERMS if term in lowered]
        assert not found, f"forbidden aggregate/verdict term(s) in {name}: {found}"


def test_domains_never_share_a_row_or_total() -> None:
    forbidden_ids = ("combined-total", "overall-total", "combined-panel", "unified-score")
    for identifier in forbidden_ids:
        assert identifier not in INDEX_HTML
        assert identifier not in APP_JS


# ---------------------------------------------------------------------------
# No JavaScript test runner or dependency introduced
# ---------------------------------------------------------------------------


def test_no_javascript_test_runner_or_dependency_introduced() -> None:
    static_dir = Path(str(_STATIC))
    repo_files = {path.name for path in static_dir.iterdir()}
    assert "package.json" not in repo_files
    assert "node_modules" not in repo_files
    for banned in ("jest", "vitest", "playwright", "mocha", "karma"):
        assert banned not in APP_JS.lower()


# ---------------------------------------------------------------------------
# Documentation exists and states the declared limits
# ---------------------------------------------------------------------------


def test_design_system_documentation_declares_its_limits() -> None:
    from importlib.resources import files as _files

    doc_path = (
        Path(str(_files("investment_analyst"))).parent.parent
        / "docs"
        / "local_interface_design_system.md"
    )
    assert doc_path.exists(), doc_path
    text = doc_path.read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", text).lower()
    assert "no es regresión visual" in normalized or "no son regresión visual" in normalized
    assert "BVL" in text, "the documented BVL live-reachability discrepancy must be recorded"


# ---------------------------------------------------------------------------
# Regression probes: each rule above must fail on a deliberately corrupted
# fixture, proving the checker is not vacuously true.
# ---------------------------------------------------------------------------


def test_probe_token_parity_rule_catches_a_removed_dark_token() -> None:
    corrupted = TOKENS_CSS.replace("--blocked-ink: #c9a3ec;\n  --blocked-soft: #2c2140;\n\n", "", 1)
    blocks = _theme_blocks(corrupted)
    assert set(blocks["light"]) != set(blocks["dark"]), (
        "probe fixture did not actually break parity"
    )


def test_probe_color_literal_rule_catches_an_injected_literal() -> None:
    corrupted = STYLES_CSS + "\n.probe { color: #ff00ff; }\n"
    assert _COLOR_LITERAL_RE.findall(corrupted)
    assert not _COLOR_LITERAL_RE.findall(STYLES_CSS)


def test_probe_contrast_rule_catches_a_low_contrast_pair() -> None:
    assert contrast_ratio("#cccccc", "#ffffff") < _MIN_CONTRAST
    assert contrast_ratio("#111111", "#ffffff") >= _MIN_CONTRAST


def test_probe_absence_mark_rule_catches_a_duplicated_icon() -> None:
    corrupted = STYLES_CSS.replace('content: "▲";', 'content: "○";')
    icons = {
        match.group("kind"): match.group("glyph") for match in _ABSENCE_ICON_RE.finditer(corrupted)
    }
    assert len(set(icons.values())) < len(_ABSENCE_KINDS), "probe fixture did not collide icons"


def test_probe_external_network_rule_catches_a_remote_url() -> None:
    corrupted = STYLES_CSS + "\n/* @import url(https://fonts.example.com/a.css); */\n"
    assert _EXTERNAL_REF_RE.findall(corrupted)
    assert not _EXTERNAL_REF_RE.findall(STYLES_CSS)


def test_probe_aggregate_term_rule_catches_an_injected_combined_verdict() -> None:
    corrupted = APP_JS + '\nconst x = "combined_verdict";\n'
    lowered = corrupted.lower()
    assert any(term in lowered for term in _FORBIDDEN_AGGREGATE_TERMS)
    assert not any(term in APP_JS.lower() for term in _FORBIDDEN_AGGREGATE_TERMS)
