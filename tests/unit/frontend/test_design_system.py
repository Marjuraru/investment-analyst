"""Contract tests for local-interface-design-system-v1.

These tests verify the RULES of the design system declared in tokens.css
and styles.css: every token exists in both themes, no color literal escapes
tokens.css, declared text/surface pairs meet WCAG contrast, the five
absence marks are mutually distinguishable, no external network reference
is emitted, and figures are tabular/monospace/right-aligned.

Each rule's check is a small `_check_*` function that both the declarative
test AND its regression probe call: the declarative test calls it against
the real shipped text and asserts it passes, the probe calls it against a
corrupted copy and asserts (via `pytest.raises`) that it fails. This keeps
every probe honest -- it exercises the exact same code path the real test
relies on, not a hand-rolled restatement of the rule.

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


def _check_token_parity(tokens_css: str) -> None:
    blocks = _theme_blocks(tokens_css)
    assert set(blocks) == {"light", "dark"}
    light_names, dark_names = set(blocks["light"]), set(blocks["dark"])
    assert light_names, "tokens.css must declare at least one token"
    assert light_names == dark_names, (
        f"tokens declared only in one theme: "
        f"light-only={light_names - dark_names} dark-only={dark_names - light_names}"
    )


def test_every_token_defined_in_light_and_dark() -> None:
    _check_token_parity(TOKENS_CSS)


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


def _check_no_color_literal(text: str, *, label: str) -> None:
    found = _COLOR_LITERAL_RE.findall(text)
    assert not found, f"color literal(s) in {label}: {found}"


def test_no_color_literal_in_stylesheet() -> None:
    _check_no_color_literal(STYLES_CSS, label="styles.css")


def test_no_color_literal_in_app_js() -> None:
    _check_no_color_literal(APP_JS, label="app.js")
    # app.js must derive its runtime meta/theme-color assignment from a
    # token, never restate a literal.
    assert _THEME_COLOR_JS_RE.search(APP_JS), "theme-color must be read from --canvas"


def _check_index_html_literal_exceptions_match_the_declared_tokens(
    index_html: str, tokens_css: str
) -> None:
    stripped = _strip_exceptions(index_html)
    leftover = _COLOR_LITERAL_RE.findall(stripped)
    assert not leftover, (
        f"color literal(s) in index.html outside the declared exceptions: {leftover}"
    )

    blocks = _theme_blocks(tokens_css)
    canvas_light = blocks["light"]["canvas"].lower()
    canvas_dark = blocks["dark"]["canvas"].lower()
    meta_match = _THEME_COLOR_META_RE.search(index_html)
    assert meta_match, "index.html must declare an initial <meta name=theme-color>"
    assert meta_match.group(1).lower() == canvas_dark, (
        "static theme-color must equal --canvas (dark is the default data-theme)"
    )

    icon_fills = {f"#{value.lower()}" for value in _ICON_FILL_RE.findall(index_html)}
    assert canvas_dark in icon_fills, "favicon ink fill must equal dark --canvas"
    assert canvas_light != canvas_dark  # sanity: themes are genuinely distinct

    sma_inputs = {
        input_name: value.lower() for input_name, value in _SMA_COLOR_INPUT_RE.findall(index_html)
    }
    assert set(sma_inputs) == set(_SMA_TOKEN_BY_INPUT)
    for input_name, token_name in _SMA_TOKEN_BY_INPUT.items():
        assert sma_inputs[input_name] == blocks["dark"][token_name].lower(), (
            f"sma-{input_name}-color default must equal dark-theme --{token_name} "
            "(dark is the default data-theme, and app.js overwrites this value "
            "from designToken() on load anyway)"
        )


def test_index_html_color_literals_are_the_declared_meta_and_favicon_exceptions() -> None:
    _check_index_html_literal_exceptions_match_the_declared_tokens(INDEX_HTML, TOKENS_CSS)


# Every var(--x) reference in styles.css, and every designToken("--x") read
# in app.js, must resolve to a token actually declared in tokens.css. This
# is distinct from "no color literal": a var() call to an undeclared custom
# property is syntactically a token reference, not a literal, so the literal
# checks above cannot catch it -- an undeclared reference silently falls
# back to the browser's initial/inherited value instead of erroring.
_VAR_REFERENCE_RE = re.compile(r"var\(--([a-zA-Z0-9-]+)\)")
_DESIGN_TOKEN_CALL_RE = re.compile(r'designToken\("--([a-zA-Z0-9-]+)"\)')
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _check_every_var_reference_is_declared(styles_css: str, app_js: str, tokens_css: str) -> None:
    declared = set(_theme_blocks(tokens_css)["light"])  # parity already enforced separately
    # Strip comments first: prose like "resolves through var(--token)" must
    # not be mistaken for an actual reference.
    without_comments = _CSS_COMMENT_RE.sub("", styles_css)
    missing_css = set(_VAR_REFERENCE_RE.findall(without_comments)) - declared
    missing_js = set(_DESIGN_TOKEN_CALL_RE.findall(app_js)) - declared
    assert not missing_css, f"styles.css references undeclared token(s): {sorted(missing_css)}"
    assert not missing_js, f"app.js designToken() reads undeclared token(s): {sorted(missing_js)}"


def test_every_var_reference_resolves_to_a_declared_token() -> None:
    _check_every_var_reference_is_declared(STYLES_CSS, APP_JS, TOKENS_CSS)


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


def _check_contrast_pairs(tokens: dict[str, str], pairs: tuple[tuple[str, str], ...]) -> None:
    failures = []
    for text_name, surface_name in pairs:
        text_value, surface_value = tokens[text_name], tokens[surface_name]
        ratio = contrast_ratio(text_value, surface_value)
        if ratio < _MIN_CONTRAST:
            failures.append((text_name, surface_name, round(ratio, 2)))
    assert not failures, f"pairs under {_MIN_CONTRAST}:1: {failures}"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_on_surface_meets_contrast_threshold(theme: str) -> None:
    tokens = _theme_blocks(TOKENS_CSS)[theme]
    _check_contrast_pairs(tokens, _THEMED_TEXT_SURFACE_PAIRS)


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


def _absence_mark_declarations(styles_css: str) -> dict[str, dict[str, str]]:
    declarations: dict[str, dict[str, str]] = {}
    for match in _ABSENCE_RULE_RE.finditer(styles_css):
        kind = match.group("kind")
        body = match.group("body")
        props = dict(re.findall(r"([a-z-]+)\s*:\s*([^;]+);", body))
        declarations.setdefault(kind, {}).update(props)
    return declarations


def _absence_mark_icons(styles_css: str) -> dict[str, str]:
    return {
        match.group("kind"): match.group("glyph") for match in _ABSENCE_ICON_RE.finditer(styles_css)
    }


def test_five_absence_marks_are_declared() -> None:
    declarations = _absence_mark_declarations(STYLES_CSS)
    assert set(declarations) == set(_ABSENCE_KINDS), (
        f"expected exactly {_ABSENCE_KINDS}, found {sorted(declarations)}"
    )


def _check_absence_marks_distinguishable(styles_css: str) -> None:
    declarations = _absence_mark_declarations(styles_css)
    icons = _absence_mark_icons(styles_css)
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


def test_five_absence_marks_are_mutually_distinguishable() -> None:
    _check_absence_marks_distinguishable(STYLES_CSS)


def _check_state_never_colour_alone(styles_css: str, app_js: str) -> None:
    # Every .absence-mark carries a label element in addition to its icon;
    # the CSS grammar never relies on background/color changes alone.
    assert ".absence-mark-label" in styles_css
    assert ".absence-mark-icon" in styles_css
    # Also true of the pre-existing quality-chip / market-session-status
    # marks touched by this block: each pairs a text label with its tone.
    assert "renderAbsenceMark" in app_js


def test_state_never_encoded_by_colour_alone() -> None:
    _check_state_never_colour_alone(STYLES_CSS, APP_JS)


def _check_absence_never_zero(app_js: str) -> None:
    # Every kind literal must appear at least once, either as a direct
    # renderAbsenceMark("<kind>", ...) call site, or as the literal string
    # a dispatch function (e.g. valuationAbsenceKind) returns before it is
    # threaded into renderAbsenceMark(<dynamicKind>, ...).
    for kind in _ABSENCE_KINDS:
        direct_call = (
            f'renderAbsenceMark("{kind}"' in app_js or f"renderAbsenceMark('{kind}'" in app_js
        )
        literal_kind = f'"{kind}"' in app_js or f"'{kind}'" in app_js
        assert direct_call or literal_kind, f"no live reference to the '{kind}' absence mark kind"
    assert "renderAbsenceMark(" in app_js
    # The two known_at call sites that used to fall back to a bare em dash
    # now route through renderKnownAtCut instead.
    assert 'byId("known-at-status").textContent = "—"' not in app_js
    assert "renderKnownAtCut(" in app_js


def test_absence_never_rendered_as_zero_or_empty() -> None:
    _check_absence_never_zero(APP_JS)


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


def _check_no_external_reference(text: str, *, label: str) -> None:
    found = _EXTERNAL_REF_RE.findall(text)
    assert not found, f"external network reference in {label}: {found}"


def test_no_external_network_reference_in_static_surface() -> None:
    for name, text in (("styles.css", STYLES_CSS), ("index.html", INDEX_HTML), ("app.js", APP_JS)):
        _check_no_external_reference(text, label=name)
    assert "fonts.googleapis" not in INDEX_HTML
    assert "fonts.googleapis" not in STYLES_CSS
    assert "cdn." not in INDEX_HTML.lower()
    assert '<link rel="preconnect"' not in INDEX_HTML


# ---------------------------------------------------------------------------
# Figures: tabular, monospace, right-aligned
# ---------------------------------------------------------------------------

# Every CSS class that renders a numeric figure directly (not merely a
# fallback dash or a label) must resolve through this exact trio: the
# monospaced figure family, tabular digit widths, and right alignment.
_FIGURE_CSS_CLASSES = (
    "figure",
    "metric-value",
    "asset-price",
    "fundamental-research-metric-value",
    "fundamental-research-exact-value",
)


def _check_figure_class_is_tabular_monospace_right_aligned(css_text: str, class_name: str) -> None:
    rule_match = re.search(rf"\.{re.escape(class_name)}\s*\{{([^}}]*)\}}", css_text, re.DOTALL)
    assert rule_match, f".{class_name} rule must be declared"
    rule_body = rule_match.group(1)
    assert "var(--figure-font)" in rule_body, f".{class_name} must use var(--figure-font)"
    assert "tabular-nums" in rule_body, f".{class_name} must set font-variant-numeric: tabular-nums"
    assert "text-align: right" in rule_body, f".{class_name} must be text-align: right"


def test_figures_use_tabular_monospace_right_aligned() -> None:
    assert "font-variant-numeric: tabular-nums" in STYLES_CSS
    for class_name in _FIGURE_CSS_CLASSES:
        _check_figure_class_is_tabular_monospace_right_aligned(STYLES_CSS, class_name)


_COMPARISON_CARD_FN_RE = re.compile(
    r"function renderMarketComparison\(payload\) \{(.*?)\n\}", re.DOTALL
)
# retorno total, drawdown máximo, volatilidad diaria, correlación, beta.
_COMPARISON_CARD_FIGURE_COUNT = 5


def _check_comparison_card_figures_wrapped(app_js: str) -> None:
    render_match = _COMPARISON_CARD_FN_RE.search(app_js)
    assert render_match, "renderMarketComparison must exist"
    count = render_match.group(1).count('class="figure"')
    assert count >= _COMPARISON_CARD_FIGURE_COUNT, (
        "each comparison metric (retorno, drawdown, volatilidad, correlación, "
        f"beta) must wrap its numeric value in the .figure utility class; found {count}"
    )


def test_comparison_card_figures_use_the_figure_class() -> None:
    _check_comparison_card_figures_wrapped(APP_JS)


def _check_comparison_unavailable_uses_absence_mark(app_js: str) -> None:
    # The backend contract (comparison_models.py) declares three statuses --
    # available, unavailable, not_applicable -- not a binary split. A metric
    # that was attempted but could not be computed ("unavailable") must not
    # fall through to comparisonPercent()'s bare "-" for a null value or to
    # plain "No disponible" text; it needs its own absence mark, distinct
    # from "not_applicable" (a structural non-fit, e.g. the benchmark itself).
    render_match = _COMPARISON_CARD_FN_RE.search(app_js)
    assert render_match, "renderMarketComparison must exist"
    body = render_match.group(1)
    assert 'correlation_status === "unavailable"' in body, (
        "correlation must branch on the 'unavailable' status, not fall through to a bare em dash"
    )
    assert 'beta_status === "unavailable"' in body, (
        "beta must branch on the 'unavailable' status, not fall through to plain text"
    )
    assert body.count('"not-evaluable"') >= 2, (
        "both the correlation and beta 'unavailable' branches must render "
        "through the not-evaluable absence mark"
    )


def test_comparison_unavailable_status_uses_absence_mark() -> None:
    _check_comparison_unavailable_uses_absence_mark(APP_JS)


def _check_valuation_history_table_value_column_is_figure(css_text: str) -> None:
    # A <td> is display: table-cell by default; .figure's own
    # display: inline-block would break the table's column layout if
    # applied directly, so this column gets its own compound-selector rule
    # instead of relying on .figure alone.
    rule_match = re.search(
        r"\.valuation-history-table td\.figure\s*\{([^}]*)\}", css_text, re.DOTALL
    )
    assert rule_match, ".valuation-history-table td.figure rule must be declared"
    body = rule_match.group(1)
    assert "var(--figure-font)" in body, "exact-value column must use var(--figure-font)"
    assert "tabular-nums" in body, "exact-value column must set font-variant-numeric: tabular-nums"
    assert "text-align: right" in body, "exact-value column must be text-align: right"


def test_valuation_history_table_value_column_is_figure() -> None:
    _check_valuation_history_table_value_column_is_figure(STYLES_CSS)


_FUNDAMENTAL_RESEARCH_AUDIT_ITEM_RE = re.compile(
    r"function fundamentalResearchAuditItem\(metric, history\) \{(.*?)\n\}", re.DOTALL
)
_RENDER_VALUATION_HISTORY_RE = re.compile(
    r"function renderValuationHistory\(payload, "
    r"\{ preserveSelection = false \} = \{\}\) \{(.*?)\n\}",
    re.DOTALL,
)
_RENDER_VALUATION_HISTORY_RULE_RE = re.compile(
    r"function renderValuationHistoryRule\(payload\) \{(.*?)\n\}", re.DOTALL
)


def _check_valuation_and_research_history_figures_wrapped(app_js: str) -> None:
    audit_match = _FUNDAMENTAL_RESEARCH_AUDIT_ITEM_RE.search(app_js)
    assert audit_match, "fundamentalResearchAuditItem must exist"
    assert 'createElement("dd", "figure", value)' in audit_match.group(1), (
        "fundamentalResearchAuditItem's exact-Decimal statistics must render "
        "through the .figure utility class"
    )

    history_match = _RENDER_VALUATION_HISTORY_RE.search(app_js)
    assert history_match, "renderValuationHistory must exist"
    assert 'createElement("dd", "figure", value)' in history_match.group(1), (
        "renderValuationHistory's series statistics must render through .figure"
    )
    assert 'createElement("td", "figure", point.value)' in history_match.group(1), (
        "renderValuationHistory's history table exact-value column must render through .figure"
    )

    rule_match = _RENDER_VALUATION_HISTORY_RULE_RE.search(app_js)
    assert rule_match, "renderValuationHistoryRule must exist"
    assert 'createElement("dd", isFigure ? "figure" : "", value)' in rule_match.group(1), (
        "renderValuationHistoryRule's numeric entries (Percentil, Puntos previos) "
        "must render through .figure"
    )


def test_valuation_and_research_history_figures_use_the_figure_class() -> None:
    _check_valuation_and_research_history_figures_wrapped(APP_JS)


# ---------------------------------------------------------------------------
# SMA default colors follow the active theme, including a runtime toggle
# ---------------------------------------------------------------------------

_THEME_TOGGLE_HANDLER_RE = re.compile(
    r'byId\("theme-toggle"\)\.addEventListener\("click", \(\) => \{(.*?)\n\}\);', re.DOTALL
)


def _check_theme_toggle_refreshes_sma_defaults(app_js: str) -> None:
    handler_match = _THEME_TOGGLE_HANDLER_RE.search(app_js)
    assert handler_match, "theme-toggle click handler must exist"
    body = handler_match.group(1)
    assert "captureDefaultSmaColors()" in body, (
        "the theme toggle must refresh DEFAULT_SMA_COLORS for the newly applied "
        "theme, or SMA line colors keep the previous theme's values"
    )
    assert body.index("applyTheme(next)") < body.index("captureDefaultSmaColors()"), (
        "SMA defaults must be captured after applyTheme(), not before"
    )


def test_theme_toggle_refreshes_sma_defaults() -> None:
    _check_theme_toggle_refreshes_sma_defaults(APP_JS)


_CAPTURE_DEFAULT_SMA_COLORS_FN_RE = re.compile(
    r"function captureDefaultSmaColors\(\) \{(.*?)\n\}", re.DOTALL
)


def _check_capture_default_sma_colors_bypasses_inline_override(app_js: str) -> None:
    # applyChartSettings() pins an INLINE style for each --series-sma-N
    # property so the chart SVG responds immediately to a settings change.
    # That inline value outranks tokens.css's :root[data-theme] rule in the
    # cascade, so a naive getComputedStyle() read after a theme toggle would
    # see the last-applied color (old theme, or a user customization)
    # instead of the newly active theme's own declared value. The capture
    # must clear any inline override before reading, then restore it.
    match = _CAPTURE_DEFAULT_SMA_COLORS_FN_RE.search(app_js)
    assert match, "captureDefaultSmaColors must exist"
    body = match.group(1)
    assert "removeProperty" in body, (
        "captureDefaultSmaColors must clear any inline SMA color override "
        "before reading the theme's cascade value"
    )
    assert "setProperty" in body, (
        "captureDefaultSmaColors must restore whatever inline override was "
        "present (a genuine user customization) after reading the cascade"
    )


def test_capture_default_sma_colors_bypasses_inline_override() -> None:
    _check_capture_default_sma_colors_bypasses_inline_override(APP_JS)


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


def _check_known_at_initial_placeholder(index_html: str) -> None:
    # Before any script runs -- and on any error path that never reaches
    # renderKnownAtCut() -- the static markup must already follow the
    # declared absence grammar (point 5), never a bare em dash.
    topbar_match = re.search(r'<header class="topbar">(.*?)</header>', index_html, re.DOTALL)
    assert topbar_match
    cut_match = re.search(
        r'<strong id="known-at-cut-value">(.*?)</strong>', topbar_match.group(1), re.DOTALL
    )
    assert cut_match, "known-at-cut-value control must exist in the topbar"
    initial_markup = cut_match.group(1)
    assert "—" not in initial_markup, (
        "the initial known_at cut placeholder must not be a bare em dash; "
        "it must render the declared absence-mark grammar instead"
    )
    assert 'class="absence-mark missing"' in initial_markup
    assert "absence-mark-icon" in initial_markup
    assert "absence-mark-label" in initial_markup

    trace_match = re.search(r'<small id="known-at-status">(.*?)</small>', index_html, re.DOTALL)
    assert trace_match, "known-at-status control must exist in the traceability detail"
    trace_markup = trace_match.group(1)
    assert "—" not in trace_markup
    assert 'class="absence-mark missing"' in trace_markup


def test_known_at_cut_initial_placeholder_uses_absence_mark_not_em_dash() -> None:
    _check_known_at_initial_placeholder(INDEX_HTML)


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


def _check_no_aggregate_terms(text: str, *, label: str) -> None:
    lowered = text.lower()
    found = [term for term in _FORBIDDEN_AGGREGATE_TERMS if term in lowered]
    assert not found, f"forbidden aggregate/verdict term(s) in {label}: {found}"


def test_no_aggregate_score_verdict_or_ranking_rendered() -> None:
    for text, name in ((APP_JS, "app.js"), (INDEX_HTML, "index.html"), (STYLES_CSS, "styles.css")):
        _check_no_aggregate_terms(text, label=name)


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
# Board shell (UI-2): six-board registry, routing, section survival, the
# isolated not-built grammar, canvas grid/density tokens, and the route/
# documentation updates that declare the shell. Every rule below is a static
# contract check over the shipped text, exactly like every rule above: no
# browser, no real DOM, no computed layout. The mandatory browser-driven
# behavior (exactly one board visible at runtime, deep-linking, focus) is
# exercised separately by the Work Block's real smoke, not by this suite.
# ---------------------------------------------------------------------------

_BOARD_REGISTRY_RE = re.compile(r"const BOARD_REGISTRY = Object\.freeze\(\[(.*?)\n\]\);", re.DOTALL)
_BOARD_ENTRY_ID_RE = re.compile(r'\{\s*id:\s*"([a-z-]+)"')
_EXPECTED_BOARD_IDS = ("mesa", "activo", "tecnico", "revisar", "cazatiburones", "sistema")


def _board_registry_body(app_js: str) -> str:
    match = _BOARD_REGISTRY_RE.search(app_js)
    assert match, "BOARD_REGISTRY must be declared as a frozen array in app.js"
    return match.group(1)


def _board_registry_entries(app_js: str) -> dict[str, str]:
    body = _board_registry_body(app_js)
    positions = [m.start() for m in _BOARD_ENTRY_ID_RE.finditer(body)]
    positions.append(len(body))
    entries: dict[str, str] = {}
    for start, end in zip(positions, positions[1:], strict=False):
        entry_id = _BOARD_ENTRY_ID_RE.match(body[start:]).group(1)
        entries[entry_id] = body[start:end]
    return entries


def _check_board_registry_declares_exactly_six_boards(app_js: str) -> None:
    body = _board_registry_body(app_js)
    ids = _BOARD_ENTRY_ID_RE.findall(body)
    assert ids == list(_EXPECTED_BOARD_IDS), (
        f"BOARD_REGISTRY must declare exactly {_EXPECTED_BOARD_IDS} in order, found {ids}"
    )
    # The nav, the not-built grammar and the routing table all derive from
    # this same array -- not a second hardcoded list -- so it must have more
    # than one live consumer.
    consumers = app_js.count("for (const board of BOARD_REGISTRY)")
    assert consumers >= 3, (
        "BOARD_REGISTRY must be the single source for nav, not-built rendering "
        f"and routing (found only {consumers} consumer loop(s))"
    )


def test_board_registry_declares_exactly_the_six_boards() -> None:
    _check_board_registry_declares_exactly_six_boards(APP_JS)


def _check_cazatiburones_is_the_only_not_built_board(app_js: str) -> None:
    entries = _board_registry_entries(app_js)
    assert set(entries) == set(_EXPECTED_BOARD_IDS)
    not_built = [board_id for board_id, text in entries.items() if "built: false" in text]
    assert not_built == ["cazatiburones"], (
        f"exactly 'cazatiburones' must be built: false, found {not_built}"
    )
    for board_id, text in entries.items():
        if board_id == "cazatiburones":
            assert re.search(r'reason:\s*\n?\s*"\S', text) or "reason:\n" in text, (
                "cazatiburones must declare a non-empty reason"
            )
            assert "UI-3" in text, "the declared reason must name the block that builds it"
        else:
            assert "built: true" in text, f"{board_id} must be declared built: true"


def test_cazatiburones_is_the_only_not_built_board_and_declares_its_reason() -> None:
    _check_cazatiburones_is_the_only_not_built_board(APP_JS)


_BOARD_DIV_RE = re.compile(r'<div class="board" id="board-([a-z-]+)"[^>]*>')


def _board_div_tags(index_html: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(0)) for m in _BOARD_DIV_RE.finditer(index_html)]


def _check_exactly_one_board_is_visible_and_routing_is_deep_linkable(
    index_html: str, app_js: str
) -> None:
    tags = _board_div_tags(index_html)
    board_ids = [board_id for board_id, _ in tags]
    assert board_ids == list(_EXPECTED_BOARD_IDS), (
        f"expected board wrappers in registry order {_EXPECTED_BOARD_IDS}, found {board_ids}"
    )
    visible = [board_id for board_id, tag in tags if "hidden" not in tag]
    hidden = [board_id for board_id, tag in tags if "hidden" in tag]
    assert visible == ["mesa"], f"exactly 'mesa' must ship visible by default, found {visible}"
    assert set(hidden) == {"activo", "tecnico", "revisar", "cazatiburones", "sistema"}
    # Routing must toggle the native [hidden] attribute -- never .hidden or
    # inline style.display -- and must track a real, deep-linkable board id
    # through the URL fragment, with an exclusive aria-current pair.
    assert "section.hidden = board.id !== resolvedId" in app_js
    assert "style.display" not in _activate_board_body(app_js)
    assert "boardIdFromLocationHash" in app_js
    assert 'history.replaceState(null, "", `#${resolvedId}`)' in app_js
    assert 'link.setAttribute("aria-current", "page")' in app_js
    assert 'link.removeAttribute("aria-current")' in app_js


def test_exactly_one_board_is_visible_and_routing_is_deep_linkable() -> None:
    _check_exactly_one_board_is_visible_and_routing_is_deep_linkable(INDEX_HTML, APP_JS)


_SURVIVAL_SECTION_IDS = (
    "resumen",
    "mercado",
    "derivados-crypto",
    "fundamentales",
    "valoracion",
    "analisis",
    "report-area",
    "comparacion-mercado",
    "operacion",
    "candidate-inbox-panel",
    "alert-inbox-panel",
)

_EXPECTED_BOARD_OF_SECTION = {
    "resumen": "mesa",
    "mercado": "activo",
    "derivados-crypto": "activo",
    "fundamentales": "activo",
    "valoracion": "activo",
    "analisis": "activo",
    "report-area": "activo",
    "comparacion-mercado": "tecnico",
    "operacion": "sistema",
    "candidate-inbox-panel": "revisar",
    "alert-inbox-panel": "revisar",
}


def _board_slices(index_html: str) -> dict[str, str]:
    starts = [(m.start(), m.group(1)) for m in _BOARD_DIV_RE.finditer(index_html)]
    slices: dict[str, str] = {}
    for index, (start, board_id) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(index_html)
        slices[board_id] = index_html[start:end]
    return slices


def _check_every_baseline_section_id_survives_in_exactly_one_board(index_html: str) -> None:
    for section_id in _SURVIVAL_SECTION_IDS:
        count = len(re.findall(rf'id="{re.escape(section_id)}"', index_html))
        assert count == 1, f"id={section_id!r} must appear exactly once, found {count}"
    slices = _board_slices(index_html)
    for section_id, expected_board in _EXPECTED_BOARD_OF_SECTION.items():
        assert f'id="{section_id}"' in slices.get(expected_board, ""), (
            f"id={section_id!r} must live inside board {expected_board!r}"
        )


def test_every_baseline_section_id_survives_in_exactly_one_board() -> None:
    _check_every_baseline_section_id_survives_in_exactly_one_board(INDEX_HTML)


# The full set of ids app.js resolved via byId("literal") on the base this
# Work Block started from (origin/main@3eee98f5e1...), captured before any
# section was moved. "chart-selection-line" is the one documented exception:
# app.js creates that <line> element itself (id: "chart-selection-line" at
# its createElementNS call site) and only byId()s it afterward, so it never
# appears in static markup, in the base or here.
_BASELINE_BY_ID_IDS = frozenset(
    {
        "alert-inbox",
        "alert-inbox-panel",
        "alert-inbox-summary",
        "alert-latest",
        "alert-status",
        "app-sidebar",
        "asset-avatar-text",
        "asset-combobox-container",
        "asset-daily-change",
        "asset-meta",
        "asset-name",
        "asset-preferences-form",
        "asset-preferences-list",
        "asset-preferences-status",
        "asset-preferences-summary",
        "asset-price",
        "asset-symbol",
        "bollinger-multiplier",
        "bollinger-window",
        "candidate-inbox",
        "candidate-inbox-panel",
        "candidate-inbox-summary",
        "candidate-latest",
        "candidate-notification-panel",
        "candidate-notification-summary",
        "candidate-notifications",
        "candidate-status",
        "chart-data-caption",
        "chart-data-disclosure",
        "chart-empty",
        "chart-interval",
        "chart-latest-close",
        "chart-latest-date",
        "chart-latest-sma-20",
        "chart-latest-sma-5",
        "chart-latest-sma-50",
        "chart-point-bollinger",
        "chart-point-close",
        "chart-point-date",
        "chart-point-high",
        "chart-point-low",
        "chart-point-open",
        "chart-point-period-label",
        "chart-point-sma-20",
        "chart-point-sma-5",
        "chart-point-sma-50",
        "chart-point-volume",
        "chart-point-volume-label",
        "chart-price-scale",
        "chart-range-change",
        "chart-settings",
        "chart-settings-error",
        "chart-settings-form",
        "chart-settings-reset",
        "chart-settings-summary",
        "chart-status",
        "chart-table-body",
        "chart-table-volume-label",
        "chart-visible-sessions",
        "chart-visible-sessions-label",
        "company-profile",
        "company-profile-categories",
        "company-profile-explanation",
        "company-profile-requirements-list",
        "company-profile-requirements-summary",
        "company-profile-status",
        "company-profile-title",
        "comparison-assets",
        "comparison-benchmark",
        "comparison-cards",
        "comparison-chart",
        "comparison-end",
        "comparison-json",
        "comparison-results",
        "comparison-start",
        "comparison-status",
        "comparison-submit",
        "crypto-derivatives-content",
        "crypto-derivatives-context",
        "crypto-derivatives-coverage",
        "crypto-derivatives-panel",
        "crypto-derivatives-status",
        "derivatives-current-funding",
        "derivatives-diagnostic-status",
        "derivatives-dvol-7d",
        "derivatives-dvol-direction",
        "derivatives-evidence",
        "derivatives-funding-168h",
        "derivatives-funding-direction",
        "derivatives-known-at",
        "derivatives-limitations",
        "derivatives-missing",
        "derivatives-open-interest",
        "derivatives-range",
        "derivatives-source-ids",
        "derivatives-spread",
        "derivatives-traceability",
        "export-fundamental-csv",
        "export-fundamental-research-csv",
        "export-market-csv",
        "export-report-json",
        "export-valuation-history-json",
        "export-valuation-history-rule-json",
        "export-valuation-json",
        "fundamental-as-of",
        "fundamental-chart",
        "fundamental-chart-symbol",
        "fundamental-completeness",
        "fundamental-empty",
        "fundamental-form",
        "fundamental-latest-context",
        "fundamental-report",
        "fundamental-research-audit",
        "fundamental-research-context",
        "fundamental-research-coverage",
        "fundamental-research-empty",
        "fundamental-research-grid",
        "fundamental-research-panel",
        "fundamental-status",
        "fundamental-table-body",
        "fundamental-trend-card",
        "global-message",
        "health-badge",
        "known-at-cut-value",
        "known-at-status",
        "market-as-of",
        "market-asset-listbox",
        "market-asset-search",
        "market-chart",
        "market-chart-card",
        "market-chart-symbol",
        "market-comparison-form",
        "market-end",
        "market-report",
        "market-start",
        "nyse-session-dot",
        "nyse-session-remaining",
        "nyse-session-status",
        "operacion-titulo",
        "price-series-legend-label",
        "price-series-swatch",
        "query-valuation",
        "query-valuation-history",
        "query-valuation-history-rule",
        "refresh-mode",
        "refresh-overview",
        "report-area",
        "report-button",
        "report-form",
        "report-frequency",
        "report-json",
        "report-known-at",
        "report-limitations",
        "report-status",
        "report-traceability",
        "run-button",
        "run-form",
        "run-frequency",
        "run-known-at",
        "run-note",
        "run-source-label",
        "run-status",
        "run-time",
        "save-asset-preferences",
        "schedule-next",
        "schedule-status",
        "screening-rules",
        "screening-rules-panel",
        "screening-rules-summary",
        "sidebar-toggle",
        "sma-long-color",
        "sma-long-window",
        "sma-short-color",
        "sma-short-window",
        "sma-third-color",
        "sma-third-window",
        "snapshot-day-range",
        "snapshot-open",
        "snapshot-quality",
        "snapshot-range-cagr",
        "snapshot-range-drawdown",
        "snapshot-range-high",
        "snapshot-range-low",
        "snapshot-range-return",
        "snapshot-range-title",
        "snapshot-relative-volume",
        "snapshot-return-1d",
        "snapshot-sma-20-distance",
        "snapshot-sma-5-distance",
        "snapshot-sma-50-distance",
        "snapshot-trades",
        "snapshot-volatility",
        "snapshot-volume",
        "snapshot-volume-label",
        "snapshot-vwap",
        "theme-toggle",
        "traceability-status",
        "unavailable-metrics-disclosure",
        "unavailable-metrics-grid",
        "unavailable-metrics-summary",
        "valuation-card",
        "valuation-context",
        "valuation-coverage",
        "valuation-date",
        "valuation-evidence",
        "valuation-filing-context",
        "valuation-history-end",
        "valuation-history-metric",
        "valuation-history-series",
        "valuation-history-start",
        "valuation-history-status",
        "valuation-history-summary",
        "valuation-metrics",
        "valuation-nav-link",
        "valuation-period-context",
        "valuation-price-context",
        "valuation-rule-evidence",
        "valuation-rule-metric",
        "valuation-rule-minimum",
        "valuation-rule-operator",
        "valuation-rule-result",
        "valuation-rule-status",
        "valuation-rule-threshold",
        "valuation-status",
        "valuation-unit-context",
        "workspace-counts",
        "workspace-status",
    }
)

_LITERAL_BY_ID_RE = re.compile(r'byId\("([a-zA-Z0-9-]+)"\)')

# Ids app.js creates itself (via an element-creation call site setting
# `id: "..."`) before ever byId()-ing them back -- legitimately absent from
# static markup, in the base and here. "board-<id>"/"board-<id>-not-built"
# are template-literal byId() calls (`byId(\`board-${board.id}\`)`), never a
# literal string, so they never match _LITERAL_BY_ID_RE in the first place.
_DYNAMICALLY_CREATED_IDS = frozenset({"chart-selection-line"})


def _html_ids(index_html: str) -> set[str]:
    return set(re.findall(r'id="([a-zA-Z0-9-]+)"', index_html))


def _check_no_control_id_is_orphaned_between_markup_and_script(
    index_html: str, app_js: str
) -> None:
    html_ids = _html_ids(index_html)
    dropped = _BASELINE_BY_ID_IDS - html_ids
    assert not dropped, (
        f"id(s) app.js resolved on the base are missing from markup: {sorted(dropped)}"
    )
    orphaned = _LITERAL_BY_ID_RE.findall(app_js)
    missing_targets = {
        control_id
        for control_id in orphaned
        if control_id not in html_ids and control_id not in _DYNAMICALLY_CREATED_IDS
    }
    assert not missing_targets, (
        f"app.js resolves id(s) absent from markup: {sorted(missing_targets)}"
    )


def test_no_control_id_is_orphaned_between_markup_and_script() -> None:
    _check_no_control_id_is_orphaned_between_markup_and_script(INDEX_HTML, APP_JS)


def _check_deferred_inbox_and_valuation_loads_are_preserved(app_js: str) -> None:
    # Valuation's deferred load keeps its original trigger untouched.
    valuation_click = re.search(
        r'byId\("valuation-nav-link"\)\.addEventListener\("click", \(\) => \{(.*?)\n\}\);',
        app_js,
        re.DOTALL,
    )
    assert valuation_click, "valuation-nav-link click handler must still exist"
    assert "valuationPayload === null" in valuation_click.group(1)
    assert "void queryValuation()" in valuation_click.group(1)
    # The two promoted panels lost their <details> "toggle" event; their
    # load now fires from board activation instead, exactly once per call.
    assert 'byId("alert-inbox-panel").addEventListener("toggle"' not in app_js
    assert 'byId("candidate-inbox-panel").addEventListener("toggle"' not in app_js
    activate_body = _activate_board_body(app_js)
    assert "void loadCandidateInbox()" in activate_body
    assert "void loadAlertInbox()" in activate_body
    assert 'resolvedId === "revisar"' in activate_body
    # The two panels NOT promoted keep their pre-existing toggle-based loads.
    assert 'byId("candidate-notification-panel").addEventListener("toggle"' in app_js
    assert 'byId("screening-rules-panel").addEventListener("toggle"' in app_js


def test_deferred_inbox_and_valuation_loads_are_preserved() -> None:
    _check_deferred_inbox_and_valuation_loads_are_preserved(APP_JS)


_ACTIVATE_BOARD_RE = re.compile(
    r"function activateBoard\(boardId, \{ focus = true \} = \{\}\) \{(.*?)\n\}", re.DOTALL
)


def _activate_board_body(app_js: str) -> str:
    match = _ACTIVATE_BOARD_RE.search(app_js)
    assert match, "activateBoard(boardId, { focus }) must exist"
    return match.group(1)


def _check_board_switch_never_touches_the_known_at_cut_or_session_clock(app_js: str) -> None:
    body = _activate_board_body(app_js)
    for forbidden in ("known-at", "known_at", "nyse", "session-clock", "queryReport", "api("):
        assert forbidden not in body.lower(), (
            f"activateBoard must never reference {forbidden!r}: switching boards must not "
            "touch the known_at cut, the session clock, or issue any query of its own"
        )
    # The only two query-triggering calls activateBoard is allowed to make
    # are the "revisar" board's promoted-panel loads (checked separately).
    calls = re.findall(r"void (\w+)\(\)", body)
    assert set(calls) <= {"loadCandidateInbox", "loadAlertInbox"}, calls


def test_board_switch_never_touches_the_known_at_cut_or_session_clock() -> None:
    _check_board_switch_never_touches_the_known_at_cut_or_session_clock(APP_JS)


_CANVAS_GRID_DENSITY_TOKENS = (
    "canvas-gutter",
    "canvas-row-gap",
    "canvas-block-gap",
    "canvas-density",
)


def _check_canvas_grid_and_density_are_tokens_with_theme_parity(
    tokens_css: str, styles_css: str
) -> None:
    blocks = _theme_blocks(tokens_css)
    for token_name in _CANVAS_GRID_DENSITY_TOKENS:
        assert token_name in blocks["light"], f"--{token_name} must be declared in :root"
        assert token_name in blocks["dark"], (
            f'--{token_name} must be declared in :root[data-theme="dark"]'
        )
        assert blocks["light"][token_name] == blocks["dark"][token_name], (
            f"--{token_name} is a layout value, theme-invariant like --sidebar-width: "
            f"light={blocks['light'][token_name]!r} dark={blocks['dark'][token_name]!r}"
        )
    for token_name in _CANVAS_GRID_DENSITY_TOKENS:
        assert f"var(--{token_name})" in styles_css, (
            f"--{token_name} must actually be consumed from styles.css, not merely declared"
        )


def test_canvas_grid_and_density_are_tokens_with_theme_parity() -> None:
    _check_canvas_grid_and_density_are_tokens_with_theme_parity(TOKENS_CSS, STYLES_CSS)


def _check_design_system_documentation_declares_the_board_shell(doc_text: str) -> None:
    normalized = re.sub(r"\s+", " ", doc_text).lower()
    assert "seis tablero" in normalized, "the doc must declare the six-board registry"
    assert "not-built" in doc_text, "the doc must name the not-built grammar"
    assert "marca de ausencia" in normalized or "marcas de ausencia" in normalized
    # The distinction from the five absence marks must be explicit, not
    # merely implied by proximity.
    assert re.search(r"not-built[^.]*no es[^.]*marca de ausencia", normalized) or re.search(
        r"marca de ausencia[^.]*no es[^.]*not-built", normalized
    ), "the doc must explicitly distinguish not-built from an absence mark"


def test_design_system_documentation_declares_the_board_shell() -> None:
    doc_path = (
        Path(str(files("investment_analyst"))).parent.parent
        / "docs"
        / "local_interface_design_system.md"
    )
    _check_design_system_documentation_declares_the_board_shell(
        doc_path.read_text(encoding="utf-8")
    )


def _check_route_declares_local_interface_planned_and_sec_corpus_next(doc_text: str) -> None:
    assert re.search(r"\|\s*`LOCAL-INTERFACE`\s*\|\s*`PLANNED`\s*\|", doc_text), (
        "the route table must declare LOCAL-INTERFACE as PLANNED"
    )
    assert re.search(r"\|\s*`SEC-CORPUS`\s*\|\s*`NEXT`\s*\|", doc_text), (
        "SEC-CORPUS must remain the sole NEXT candidate; this block advances a new row, "
        "it does not complete anything"
    )


def test_route_declares_local_interface_planned_and_sec_corpus_next() -> None:
    doc_path = (
        Path(str(files("investment_analyst"))).parent.parent
        / "docs"
        / "basic_functional_release_plan.md"
    )
    _check_route_declares_local_interface_planned_and_sec_corpus_next(
        doc_path.read_text(encoding="utf-8")
    )


def _check_not_built_grammar_is_isolated_from_absence_marks(styles_css: str, app_js: str) -> None:
    # not-built describes a capability that does not exist yet; an absence
    # mark describes a missing datum under a cut. They must never share a
    # class or a rendering function.
    assert ".board-not-built" in styles_css
    assert "absence-mark" not in _extract_css_rule(styles_css, "board-not-built")
    assert "renderAbsenceMark" not in _extract_js_function(app_js, "renderNotBuiltBoards")
    declarations = _absence_mark_declarations(styles_css)
    assert set(declarations) == set(_ABSENCE_KINDS), (
        "adding not-built must not grow or shrink the five declared absence marks"
    )


def _extract_css_rule(css_text: str, class_name: str) -> str:
    match = re.search(rf"\.{re.escape(class_name)}\s*\{{([^}}]*)\}}", css_text, re.DOTALL)
    return match.group(1) if match else ""


def _extract_js_function(app_js: str, function_name: str) -> str:
    match = re.search(
        rf"function {re.escape(function_name)}\([^)]*\) \{{(.*?)\n\}}", app_js, re.DOTALL
    )
    assert match, f"function {function_name} must exist"
    return match.group(1)


def test_not_built_grammar_is_isolated_from_absence_marks() -> None:
    _check_not_built_grammar_is_isolated_from_absence_marks(STYLES_CSS, APP_JS)


def _check_no_new_capability_or_route_is_introduced_by_the_shell(app_js: str) -> None:
    without_registry = _BOARD_REGISTRY_RE.sub("", app_js)
    routes = set(re.findall(r"/api/[a-zA-Z0-9/_-]*", without_registry))
    assert routes == _BASELINE_API_ROUTES, (
        f"requested routes changed: added={routes - _BASELINE_API_ROUTES} "
        f"removed={_BASELINE_API_ROUTES - routes}"
    )


# The exact route set app.js requested on the base this Work Block started
# from (origin/main@3eee98f5e1...), extracted the same way the check above
# extracts the candidate's routes: every `/api/...` literal outside the
# BOARD_REGISTRY declaration (whose cazatiburones reason text names two
# already-integrated read endpoints in prose, never calls them).
_BASELINE_API_ROUTES = frozenset(
    {
        "/api/alerts",
        "/api/alerts/transition",
        "/api/candidates",
        "/api/candidates/transition",
        "/api/fundamental-analysis",
        "/api/fundamental-refresh",
        "/api/fundamental-trend",
        "/api/listed-company-report",
        "/api/market-assets",
        "/api/market-chart",
        "/api/market-intraday",
        "/api/market-intraday-refresh",
        "/api/market-refresh",
        "/api/screening-backtest",
        "/api/screening-rules",
        "/api/screening-rules/update",
        "/api/v1/asset-preferences",
        "/api/v1/candidate-notifications",
        "/api/v1/candidate-notifications/acknowledge",
        "/api/v1/crypto-derivatives",
        "/api/v1/market-comparison",
        "/api/v1/overview",
        "/api/v1/valuation",
        "/api/v1/valuation-history",
        "/api/v1/valuation-history-rule",
    }
)


def test_no_new_capability_or_route_is_introduced_by_the_shell() -> None:
    _check_no_new_capability_or_route_is_introduced_by_the_shell(APP_JS)


def _check_shell_is_local_only_with_no_javascript_runner_or_dependency(app_js: str) -> None:
    board_shell_js = app_js[
        app_js.index("const BOARD_REGISTRY") : app_js.index("async function initialize")
    ]
    for banned in ("import ", "require(", "<script", 'fetch("http', "fetch('http"):
        assert banned not in board_shell_js, f"board shell code must not introduce {banned!r}"


def test_shell_is_local_only_with_no_javascript_runner_or_dependency() -> None:
    _check_shell_is_local_only_with_no_javascript_runner_or_dependency(APP_JS)


# ---------------------------------------------------------------------------
# Canvas convergence (UI-3): the approved canvas's warm palette, its 1px-rule
# surface grammar (no elevation, no rounded data surfaces), and its row
# density, all expressed as tokens and verified as static contract rules --
# same discipline as every rule above: no browser, no computed layout.
# ---------------------------------------------------------------------------

# Exempt from the warm (R >= G >= B) rule, by name, never by omission:
# semantic-state families (color encodes direction/state, not temperature)
# and the categorical chart/series family (colors must stay mutually
# distinguishable from each other on the same chart, not warm relative to
# each other). --series-close is deliberately NOT here: it is the single
# primary price line, so it carries the warm accent language.
_WARM_EXEMPT_TOKENS = frozenset(
    {
        "positive",
        "positive-soft",
        "positive-ink",
        "warning",
        "warning-soft",
        "warning-ink",
        "negative",
        "negative-soft",
        "negative-ink",
        "blocked-ink",
        "blocked-soft",
        "series-sma-5",
        "series-sma-20",
        "series-sma-50",
        "series-revenue",
        "series-net-income",
        "compare-series-1",
        "compare-series-2",
        "compare-series-3",
        "compare-series-4",
        "compare-series-5",
    }
)

# Declared in tokens.css but not a color at all -- nothing to check.
_NON_COLOR_TOKENS = frozenset(
    {
        "font-sans",
        "figure-font",
        "sidebar-width",
        "canvas-gutter",
        "canvas-row-gap",
        "canvas-block-gap",
        "canvas-density",
        "row-height",
        "row-padding-inline",
        "row-rule-width",
        "base-font-size",
        "label-font-size",
        "label-tracking",
    }
)

_HEX_COLOR_VALUE_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_RGB_FUNC_VALUE_RE = re.compile(r"^rgba?\(\s*(\d+)\s+(\d+)\s+(\d+)(?:\s*/\s*[\d.]+%?)?\s*\)$")


def _token_rgb(value: str) -> tuple[int, int, int] | None:
    value = value.strip()
    hex_match = _HEX_COLOR_VALUE_RE.match(value)
    if hex_match:
        digits = hex_match.group(1)
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        return tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    rgb_match = _RGB_FUNC_VALUE_RE.match(value)
    if rgb_match:
        return tuple(int(part) for part in rgb_match.groups())  # type: ignore[return-value]
    return None


def _check_surface_ink_rule_and_accent_tokens_are_warm_in_both_themes(tokens_css: str) -> None:
    blocks = _theme_blocks(tokens_css)
    failures = []
    for theme in ("light", "dark"):
        for name, value in blocks[theme].items():
            if name in _WARM_EXEMPT_TOKENS or name in _NON_COLOR_TOKENS:
                continue
            rgb = _token_rgb(value)
            if rgb is None:
                continue
            r, g, b = rgb
            if not (r >= g >= b):
                failures.append((theme, name, value))
    assert not failures, f"non-warm token(s), R>=G>=B violated: {failures}"


def test_surface_ink_rule_and_accent_tokens_are_warm_in_both_themes() -> None:
    _check_surface_ink_rule_and_accent_tokens_are_warm_in_both_themes(TOKENS_CSS)


def _check_every_ink_level_meets_contrast_after_repalette(tokens_css: str) -> None:
    blocks = _theme_blocks(tokens_css)
    ink_pairs = tuple(
        (level, surface)
        for level in ("ink-strong", "ink", "muted-strong", "muted")
        for surface in ("surface", "surface-subtle", "canvas")
    )
    for theme in ("light", "dark"):
        _check_contrast_pairs(blocks[theme], ink_pairs)


def test_every_ink_level_meets_contrast_after_repalette() -> None:
    _check_every_ink_level_meets_contrast_after_repalette(TOKENS_CSS)


# The approved canvas's box-shadow survivors: the rail brand mark's inset
# highlight and the sidebar's active-board-bar inset indicator. A third
# category the Work Block names -- the keyboard focus ring -- has zero live
# instances in this file because focus is already expressed via `outline`
# (a different property the shadow ban does not reach), not `box-shadow`.
_SURVIVING_BOX_SHADOWS = (
    "box-shadow: inset 0 1px var(--rail-overlay-strong);",
    "box-shadow: inset 3px 0 var(--rail-active-bar);",
)
_EXPECTED_BOX_SHADOW_COUNT = len(_SURVIVING_BOX_SHADOWS)
# Circular status/bullet points (status-dot, session-status-dot, badge
# bullet, limitations bullet) plus native <input>/<select> form-control
# styling (search input, interval select, SMA number/color inputs, chart
# settings select, the global input/select base rule, and the screening
# rule field select) -- the two categories the Work Block names as
# border-radius survivors.
_EXPECTED_BORDER_RADIUS_COUNT = 13


def _check_data_surfaces_separate_with_rules_not_elevation(styles_css: str) -> None:
    shadow_count = len(re.findall(r"box-shadow:", styles_css))
    assert shadow_count == _EXPECTED_BOX_SHADOW_COUNT, (
        f"expected exactly {_EXPECTED_BOX_SHADOW_COUNT} box-shadow declaration(s) "
        f"(rail inset + active-board-bar inset), found {shadow_count}"
    )
    for survivor in _SURVIVING_BOX_SHADOWS:
        assert survivor in styles_css, f"missing declared box-shadow survivor: {survivor!r}"
    radius_count = len(re.findall(r"border-radius:", styles_css))
    assert radius_count == _EXPECTED_BORDER_RADIUS_COUNT, (
        f"expected exactly {_EXPECTED_BORDER_RADIUS_COUNT} border-radius declaration(s) "
        f"(circular status points + native form controls), found {radius_count}"
    )


def test_data_surfaces_separate_with_rules_not_elevation() -> None:
    _check_data_surfaces_separate_with_rules_not_elevation(STYLES_CSS)


_ROW_DENSITY_TOKENS = (
    "row-height",
    "row-padding-inline",
    "row-rule-width",
    "base-font-size",
    "label-font-size",
    "label-tracking",
)


def _check_canvas_row_density_are_tokens_consumed_by_data_rows(
    tokens_css: str, styles_css: str
) -> None:
    blocks = _theme_blocks(tokens_css)
    for token in _ROW_DENSITY_TOKENS:
        assert token in blocks["light"], f"--{token} must be declared in :root"
        assert token in blocks["dark"], f'--{token} must be declared in :root[data-theme="dark"]'
        assert blocks["light"][token] == blocks["dark"][token], (
            f"--{token} is a layout value, theme-invariant like --sidebar-width"
        )
    for token in _ROW_DENSITY_TOKENS:
        assert f"var(--{token})" in styles_css, (
            f"--{token} must actually be consumed by a data row rule in styles.css"
        )


def test_canvas_row_density_are_tokens_consumed_by_data_rows() -> None:
    _check_canvas_row_density_are_tokens_consumed_by_data_rows(TOKENS_CSS, STYLES_CSS)


def _check_design_system_documentation_declares_the_canvas_convergence(doc_text: str) -> None:
    normalized = re.sub(r"\s+", " ", doc_text).lower()
    assert "r ≥ g ≥ b" in normalized or "r >= g >= b" in normalized, (
        "the doc must state the mechanical warm-palette rule"
    )
    assert "series" in normalized and "distinguibles" in normalized, (
        "the doc must name why the categorical chart/series family is exempt"
    )
    assert "ink4" in normalized or "ink 4" in normalized, (
        "the doc must document the measured ink4 contrast deviation"
    )
    assert "4,5:1" in doc_text or "4.5:1" in doc_text
    assert "retícula" in normalized and "4 px" in normalized, (
        "the doc must record that the '4px grid' proposal is not what the canvas draws"
    )
    assert "ibm plex" in normalized, "the doc must keep declaring IBM Plex as deferred"


def test_design_system_documentation_declares_the_canvas_convergence() -> None:
    doc_path = (
        Path(str(files("investment_analyst"))).parent.parent
        / "docs"
        / "local_interface_design_system.md"
    )
    _check_design_system_documentation_declares_the_canvas_convergence(
        doc_path.read_text(encoding="utf-8")
    )


def _check_route_registers_canvas_convergence_and_reassigns_cazatiburones(doc_text: str) -> None:
    assert re.search(r"\|\s*`LOCAL-INTERFACE`\s*\|\s*`PLANNED`\s*\|", doc_text), (
        "LOCAL-INTERFACE must remain PLANNED; this block advances evidence, completes nothing"
    )
    assert re.search(r"\|\s*`SEC-CORPUS`\s*\|\s*`NEXT`\s*\|", doc_text), (
        "SEC-CORPUS must remain the sole NEXT candidate"
    )
    normalized = re.sub(r"\s+", " ", doc_text).lower()
    assert "cazatiburones" in normalized and "ui-4" in normalized, (
        "the route must reassign the cazatiburones board connection to UI-4"
    )


def test_route_registers_canvas_convergence_and_reassigns_cazatiburones() -> None:
    doc_path = (
        Path(str(files("investment_analyst"))).parent.parent
        / "docs"
        / "basic_functional_release_plan.md"
    )
    _check_route_registers_canvas_convergence_and_reassigns_cazatiburones(
        doc_path.read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# Regression probes: each rule above must fail on a deliberately corrupted
# fixture, proving the checker is not vacuously true. Every probe below
# calls the SAME `_check_*` function its declarative test calls -- against
# the real text first (must pass) and then against a corrupted copy (must
# raise) -- so a probe can never pass while the checker itself is broken or
# vacuous.
# ---------------------------------------------------------------------------


def test_probe_token_parity_rule_catches_a_removed_dark_token() -> None:
    _check_token_parity(TOKENS_CSS)  # baseline: real tokens.css is clean
    corrupted = TOKENS_CSS.replace("--blocked-ink: #c9a3ec;\n  --blocked-soft: #2c2140;\n\n", "", 1)
    assert corrupted != TOKENS_CSS, "probe fixture did not actually change tokens.css"
    with pytest.raises(AssertionError):
        _check_token_parity(corrupted)


def test_probe_color_literal_rule_catches_an_injected_literal() -> None:
    _check_no_color_literal(STYLES_CSS, label="styles.css")  # baseline: clean
    corrupted = STYLES_CSS + "\n.probe { color: #ff00ff; }\n"
    with pytest.raises(AssertionError):
        _check_no_color_literal(corrupted, label="styles.css")


def test_probe_contrast_rule_catches_a_low_contrast_pair() -> None:
    light_tokens = dict(_theme_blocks(TOKENS_CSS)["light"])
    _check_contrast_pairs(light_tokens, _THEMED_TEXT_SURFACE_PAIRS)  # baseline: clean

    # Corrupt a real, currently-passing pair (muted text on its own
    # surface) to a near-white-on-white value, using the exact tokens dict
    # the real test builds from TOKENS_CSS -- not an unrelated hardcoded
    # pair -- so this exercises the real checker against a real regression.
    assert contrast_ratio(light_tokens["muted"], light_tokens["surface"]) >= _MIN_CONTRAST
    corrupted_tokens = {**light_tokens, "muted": "#f8f8f8"}
    assert contrast_ratio(corrupted_tokens["muted"], corrupted_tokens["surface"]) < _MIN_CONTRAST
    with pytest.raises(AssertionError):
        _check_contrast_pairs(corrupted_tokens, _THEMED_TEXT_SURFACE_PAIRS)


def test_probe_absence_mark_rule_catches_a_duplicated_icon() -> None:
    _check_absence_marks_distinguishable(STYLES_CSS)  # baseline: clean
    # "overdue" (▲) collides with "missing" (○) once corrupted.
    corrupted = STYLES_CSS.replace('content: "▲";', 'content: "○";')
    assert corrupted != STYLES_CSS, "probe fixture did not change any icon glyph"
    with pytest.raises(AssertionError):
        _check_absence_marks_distinguishable(corrupted)


def test_probe_external_network_rule_catches_a_remote_url() -> None:
    _check_no_external_reference(STYLES_CSS, label="styles.css")  # baseline: clean
    corrupted = STYLES_CSS + "\n/* @import url(https://fonts.example.com/a.css); */\n"
    with pytest.raises(AssertionError):
        _check_no_external_reference(corrupted, label="styles.css")


def test_probe_aggregate_term_rule_catches_an_injected_combined_verdict() -> None:
    _check_no_aggregate_terms(APP_JS, label="app.js")  # baseline: clean
    corrupted = APP_JS + '\nconst x = "combined_verdict";\n'
    with pytest.raises(AssertionError):
        _check_no_aggregate_terms(corrupted, label="app.js")


def test_probe_state_shape_rule_catches_a_removed_label_class() -> None:
    # test_state_never_encoded_by_colour_alone asserts ".absence-mark-label"
    # is declared in styles.css; dropping it would collapse every absence
    # mark to icon+color alone, violating "state never by colour alone".
    _check_state_never_colour_alone(STYLES_CSS, APP_JS)  # baseline: clean
    # Renaming the selector to append a suffix (".absence-mark-label-x")
    # would leave ".absence-mark-label" present as a substring and not
    # actually corrupt the checker's input -- replace it outright instead.
    corrupted = STYLES_CSS.replace(".absence-mark-label {", ".removed-label {")
    assert ".absence-mark-label" not in corrupted, "probe fixture did not remove the label rule"
    with pytest.raises(AssertionError):
        _check_state_never_colour_alone(corrupted, APP_JS)


def test_probe_absence_zero_rule_catches_a_reintroduced_bare_dash_fallback() -> None:
    # test_absence_never_rendered_as_zero_or_empty asserts this exact
    # fallback string is absent from app.js; reintroducing it would silently
    # collapse the known_at traceability absence back to a bare dash.
    _check_absence_never_zero(APP_JS)  # baseline: clean
    corrupted = APP_JS + '\nbyId("known-at-status").textContent = "—";\n'
    with pytest.raises(AssertionError):
        _check_absence_never_zero(corrupted)


def test_probe_known_at_initial_placeholder_rule_catches_a_reintroduced_em_dash() -> None:
    # test_known_at_cut_initial_placeholder_uses_absence_mark_not_em_dash
    # asserts the static topbar placeholder is never a bare em dash.
    _check_known_at_initial_placeholder(INDEX_HTML)  # baseline: clean
    corrupted = re.sub(
        r'(<strong id="known-at-cut-value">).*?(</strong>)',
        r"\1—\2",
        INDEX_HTML,
        count=1,
        flags=re.DOTALL,
    )
    corrupted_match = re.search(
        r'<strong id="known-at-cut-value">(.*?)</strong>', corrupted, re.DOTALL
    )
    assert corrupted_match and "—" in corrupted_match.group(1), (
        "probe fixture did not reintroduce a bare em dash"
    )
    with pytest.raises(AssertionError):
        _check_known_at_initial_placeholder(corrupted)


def test_probe_var_reference_rule_catches_an_undeclared_token() -> None:
    _check_every_var_reference_is_declared(STYLES_CSS, APP_JS, TOKENS_CSS)  # baseline: clean
    corrupted_css = STYLES_CSS + "\n.probe { color: var(--totally-undeclared-token); }\n"
    with pytest.raises(AssertionError):
        _check_every_var_reference_is_declared(corrupted_css, APP_JS, TOKENS_CSS)
    corrupted_js = APP_JS + '\ndesignToken("--also-undeclared");\n'
    with pytest.raises(AssertionError):
        _check_every_var_reference_is_declared(STYLES_CSS, corrupted_js, TOKENS_CSS)


def test_probe_figure_class_rule_catches_a_missing_right_align() -> None:
    _check_figure_class_is_tabular_monospace_right_aligned(STYLES_CSS, "asset-price")  # baseline
    corrupted = re.sub(
        r"(\.asset-price\s*\{[^}]*?)text-align: right;\n",
        r"\1",
        STYLES_CSS,
        count=1,
        flags=re.DOTALL,
    )
    assert corrupted != STYLES_CSS, "probe fixture did not remove text-align: right"
    with pytest.raises(AssertionError):
        _check_figure_class_is_tabular_monospace_right_aligned(corrupted, "asset-price")


def test_probe_comparison_figures_rule_catches_an_unwrapped_value() -> None:
    _check_comparison_card_figures_wrapped(APP_JS)  # baseline: clean
    corrupted = APP_JS.replace(
        '<dd><span class="figure">${comparisonPercent(series.metrics.total_return)}</span></dd>',
        "<dd>${comparisonPercent(series.metrics.total_return)}</dd>",
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not unwrap the total-return figure"
    with pytest.raises(AssertionError):
        _check_comparison_card_figures_wrapped(corrupted)


def test_probe_theme_toggle_refresh_rule_catches_a_removed_capture_call() -> None:
    _check_theme_toggle_refreshes_sma_defaults(APP_JS)  # baseline: clean
    corrupted = APP_JS.replace(
        "persistTheme(next);\n  captureDefaultSmaColors();\n",
        "persistTheme(next);\n",
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not remove the capture call"
    with pytest.raises(AssertionError):
        _check_theme_toggle_refreshes_sma_defaults(corrupted)


def test_probe_valuation_history_table_figure_rule_catches_a_missing_right_align() -> None:
    _check_valuation_history_table_value_column_is_figure(STYLES_CSS)  # baseline: clean
    corrupted = re.sub(
        r"(\.valuation-history-table td\.figure\s*\{[^}]*?)text-align: right;\n",
        r"\1",
        STYLES_CSS,
        count=1,
        flags=re.DOTALL,
    )
    assert corrupted != STYLES_CSS, "probe fixture did not remove text-align: right"
    with pytest.raises(AssertionError):
        _check_valuation_history_table_value_column_is_figure(corrupted)


def test_probe_valuation_and_research_history_figures_rule_catches_an_unwrapped_value() -> None:
    _check_valuation_and_research_history_figures_wrapped(APP_JS)  # baseline: clean
    corrupted = APP_JS.replace(
        'createElement("td", "figure", point.value)',
        'createElement("td", "", point.value)',
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not unwrap the exact-value column"
    with pytest.raises(AssertionError):
        _check_valuation_and_research_history_figures_wrapped(corrupted)


def test_probe_comparison_unavailable_rule_catches_a_removed_branch() -> None:
    _check_comparison_unavailable_uses_absence_mark(APP_JS)  # baseline: clean
    corrupted = APP_JS.replace(
        'correlation_status === "unavailable"', 'correlation_status === "never"', 1
    )
    assert corrupted != APP_JS, "probe fixture did not remove the unavailable branch"
    with pytest.raises(AssertionError):
        _check_comparison_unavailable_uses_absence_mark(corrupted)


def test_probe_capture_sma_defaults_rule_catches_a_removed_inline_bypass() -> None:
    _check_capture_default_sma_colors_bypasses_inline_override(APP_JS)  # baseline: clean
    corrupted = re.sub(
        r"function captureDefaultSmaColors\(\) \{.*?\n\}",
        "function captureDefaultSmaColors() {\n"
        "  DEFAULT_SMA_COLORS = {\n"
        '    shortColor: designToken("--series-sma-5"),\n'
        '    longColor: designToken("--series-sma-20"),\n'
        '    thirdColor: designToken("--series-sma-50"),\n'
        "  };\n"
        "}",
        APP_JS,
        count=1,
        flags=re.DOTALL,
    )
    assert corrupted != APP_JS, "probe fixture did not replace the function body"
    with pytest.raises(AssertionError):
        _check_capture_default_sma_colors_bypasses_inline_override(corrupted)


# ---------------------------------------------------------------------------
# Board shell (UI-2) probes
# ---------------------------------------------------------------------------


def test_probe_board_registry_rule_catches_a_removed_board() -> None:
    _check_board_registry_declares_exactly_six_boards(APP_JS)  # baseline: clean
    corrupted = APP_JS.replace(
        '{ id: "sistema", label: "Sistema", icon: "gear", built: true },\n', "", 1
    )
    assert corrupted != APP_JS, "probe fixture did not remove a board entry"
    with pytest.raises(AssertionError):
        _check_board_registry_declares_exactly_six_boards(corrupted)


def test_probe_not_built_board_rule_catches_a_second_not_built_board() -> None:
    _check_cazatiburones_is_the_only_not_built_board(APP_JS)  # baseline: clean
    corrupted = APP_JS.replace(
        '{ id: "sistema", label: "Sistema", icon: "gear", built: true },',
        '{ id: "sistema", label: "Sistema", icon: "gear", built: false },',
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not corrupt the sistema entry"
    with pytest.raises(AssertionError):
        _check_cazatiburones_is_the_only_not_built_board(corrupted)


def test_probe_two_visible_boards_rule_catches_a_missing_hidden_attribute() -> None:
    _check_exactly_one_board_is_visible_and_routing_is_deep_linkable(
        INDEX_HTML, APP_JS
    )  # baseline: clean
    corrupted = INDEX_HTML.replace(
        '<div class="board" id="board-activo" data-board="activo" tabindex="-1" hidden>',
        '<div class="board" id="board-activo" data-board="activo" tabindex="-1">',
        1,
    )
    assert corrupted != INDEX_HTML, "probe fixture did not remove the hidden attribute"
    with pytest.raises(AssertionError):
        _check_exactly_one_board_is_visible_and_routing_is_deep_linkable(corrupted, APP_JS)


def test_probe_two_visible_boards_rule_catches_a_style_display_toggle() -> None:
    _check_exactly_one_board_is_visible_and_routing_is_deep_linkable(
        INDEX_HTML, APP_JS
    )  # baseline: clean
    corrupted = APP_JS.replace(
        "if (section) section.hidden = board.id !== resolvedId;",
        'if (section) section.style.display = board.id !== resolvedId ? "none" : "";',
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not switch to style.display"
    with pytest.raises(AssertionError):
        _check_exactly_one_board_is_visible_and_routing_is_deep_linkable(INDEX_HTML, corrupted)


def test_probe_section_survival_rule_catches_a_removed_section() -> None:
    _check_every_baseline_section_id_survives_in_exactly_one_board(INDEX_HTML)  # baseline: clean
    corrupted = INDEX_HTML.replace(
        '<section id="comparacion-mercado" class="comparison-section"',
        '<section id="comparacion-mercado-removed" class="comparison-section"',
        1,
    )
    assert corrupted != INDEX_HTML, "probe fixture did not remove the section id"
    with pytest.raises(AssertionError):
        _check_every_baseline_section_id_survives_in_exactly_one_board(corrupted)


def test_probe_orphaned_control_id_rule_catches_a_renamed_markup_id() -> None:
    _check_no_control_id_is_orphaned_between_markup_and_script(
        INDEX_HTML, APP_JS
    )  # baseline: clean
    corrupted = INDEX_HTML.replace('id="run-button"', 'id="run-button-renamed"', 1)
    assert corrupted != INDEX_HTML, "probe fixture did not rename the control id"
    with pytest.raises(AssertionError):
        _check_no_control_id_is_orphaned_between_markup_and_script(corrupted, APP_JS)


def test_probe_deferred_loads_rule_catches_a_reintroduced_toggle_listener() -> None:
    _check_deferred_inbox_and_valuation_loads_are_preserved(APP_JS)  # baseline: clean
    corrupted = APP_JS + (
        '\nbyId("alert-inbox-panel").addEventListener("toggle", (event) => {\n'
        "  if (event.currentTarget.open) void loadAlertInbox();\n"
        "});\n"
    )
    with pytest.raises(AssertionError):
        _check_deferred_inbox_and_valuation_loads_are_preserved(corrupted)


def test_probe_board_switch_rule_catches_a_known_at_reference() -> None:
    _check_board_switch_never_touches_the_known_at_cut_or_session_clock(APP_JS)  # baseline: clean
    corrupted = APP_JS.replace(
        '  if (resolvedId === "revisar") {',
        '  byId("known-at-status").textContent = "poked";\n  if (resolvedId === "revisar") {',
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not inject a known_at reference"
    with pytest.raises(AssertionError):
        _check_board_switch_never_touches_the_known_at_cut_or_session_clock(corrupted)


def test_probe_canvas_grid_density_rule_catches_a_dark_pair_dropped() -> None:
    _check_canvas_grid_and_density_are_tokens_with_theme_parity(
        TOKENS_CSS, STYLES_CSS
    )  # baseline: clean
    corrupted = TOKENS_CSS.replace(
        '  --canvas-density: 1.42;\n}\n\n:root[data-theme="dark"] {',
        '  --canvas-density: 1.42;\n}\n\n:root[data-theme="dark"] {',
        1,
    )
    # Remove only the SECOND (dark-theme) declaration of --canvas-block-gap.
    dark_start = corrupted.index(':root[data-theme="dark"]')
    corrupted = corrupted[:dark_start] + corrupted[dark_start:].replace(
        "  --canvas-block-gap: 1.55rem;\n", "", 1
    )
    assert corrupted != TOKENS_CSS, "probe fixture did not drop the dark-theme token"
    with pytest.raises(AssertionError):
        _check_canvas_grid_and_density_are_tokens_with_theme_parity(corrupted, STYLES_CSS)


def test_probe_documentation_board_shell_rule_catches_a_missing_distinction() -> None:
    doc_path = (
        Path(str(files("investment_analyst"))).parent.parent
        / "docs"
        / "local_interface_design_system.md"
    )
    text = doc_path.read_text(encoding="utf-8")
    _check_design_system_documentation_declares_the_board_shell(text)  # baseline: clean
    corrupted = text.replace(
        "`not-built` **no es una sexta marca de ausencia**",
        "`not-built` es compatible con una marca de ausencia",
        1,
    )
    assert corrupted != text, "probe fixture did not remove the distinction sentence"
    with pytest.raises(AssertionError):
        _check_design_system_documentation_declares_the_board_shell(corrupted)


def test_probe_route_local_interface_rule_catches_a_missing_row() -> None:
    doc_path = (
        Path(str(files("investment_analyst"))).parent.parent
        / "docs"
        / "basic_functional_release_plan.md"
    )
    text = doc_path.read_text(encoding="utf-8")
    _check_route_declares_local_interface_planned_and_sec_corpus_next(text)  # baseline: clean
    corrupted = re.sub(r"\| `LOCAL-INTERFACE` \| `PLANNED` \|.*\|\n", "", text, count=1)
    assert corrupted != text, "probe fixture did not remove the LOCAL-INTERFACE row"
    with pytest.raises(AssertionError):
        _check_route_declares_local_interface_planned_and_sec_corpus_next(corrupted)


def test_probe_not_built_isolation_rule_catches_reuse_as_a_sixth_absence_mark() -> None:
    _check_not_built_grammar_is_isolated_from_absence_marks(STYLES_CSS, APP_JS)  # baseline: clean
    corrupted = STYLES_CSS + "\n.absence-mark.not-built {\n  color: var(--muted-strong);\n}\n"
    assert corrupted != STYLES_CSS, "probe fixture did not add a sixth absence-mark rule"
    with pytest.raises(AssertionError):
        _check_not_built_grammar_is_isolated_from_absence_marks(corrupted, APP_JS)


def test_probe_no_new_route_rule_catches_an_added_endpoint_call() -> None:
    _check_no_new_capability_or_route_is_introduced_by_the_shell(APP_JS)  # baseline: clean
    corrupted = APP_JS + '\nvoid api("/api/v1/cazatiburones/declared-activity");\n'
    assert corrupted != APP_JS, "probe fixture did not add a new route call"
    with pytest.raises(AssertionError):
        _check_no_new_capability_or_route_is_introduced_by_the_shell(corrupted)


def test_probe_shell_local_only_rule_catches_an_introduced_import() -> None:
    _check_shell_is_local_only_with_no_javascript_runner_or_dependency(APP_JS)  # baseline: clean
    corrupted = APP_JS.replace(
        "const DEFAULT_BOARD_ID = BOARD_REGISTRY[0].id;",
        'import "left-pad";\nconst DEFAULT_BOARD_ID = BOARD_REGISTRY[0].id;',
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not inject an import"
    with pytest.raises(AssertionError):
        _check_shell_is_local_only_with_no_javascript_runner_or_dependency(corrupted)


# ---------------------------------------------------------------------------
# Canvas convergence (UI-3) probes
# ---------------------------------------------------------------------------


def test_probe_warm_palette_rule_catches_a_cool_hue_token() -> None:
    _check_surface_ink_rule_and_accent_tokens_are_warm_in_both_themes(TOKENS_CSS)  # baseline: clean
    corrupted = TOKENS_CSS.replace("--accent: #96570b;", "--accent: #0b5796;", 1)
    assert corrupted != TOKENS_CSS, "probe fixture did not corrupt --accent"
    with pytest.raises(AssertionError):
        _check_surface_ink_rule_and_accent_tokens_are_warm_in_both_themes(corrupted)


def test_probe_ink_level_contrast_rule_catches_a_low_contrast_correction() -> None:
    _check_every_ink_level_meets_contrast_after_repalette(TOKENS_CSS)  # baseline: clean
    corrupted = TOKENS_CSS.replace("--muted: #6d685e;", "--muted: #d9d5cc;", 1)
    assert corrupted != TOKENS_CSS, "probe fixture did not corrupt --muted"
    with pytest.raises(AssertionError):
        _check_every_ink_level_meets_contrast_after_repalette(corrupted)


def test_probe_surface_grammar_rule_catches_a_reintroduced_elevation() -> None:
    _check_data_surfaces_separate_with_rules_not_elevation(STYLES_CSS)  # baseline: clean
    corrupted = STYLES_CSS + "\n.probe-card {\n  box-shadow: 0 8px 30px rgb(0 0 0 / 10%);\n}\n"
    assert corrupted != STYLES_CSS, "probe fixture did not add a box-shadow"
    with pytest.raises(AssertionError):
        _check_data_surfaces_separate_with_rules_not_elevation(corrupted)


def test_probe_surface_grammar_rule_catches_a_reintroduced_radius() -> None:
    _check_data_surfaces_separate_with_rules_not_elevation(STYLES_CSS)  # baseline: clean
    corrupted = STYLES_CSS + "\n.probe-card {\n  border-radius: 12px;\n}\n"
    assert corrupted != STYLES_CSS, "probe fixture did not add a border-radius"
    with pytest.raises(AssertionError):
        _check_data_surfaces_separate_with_rules_not_elevation(corrupted)


def test_probe_row_density_rule_catches_a_hardcoded_row_height() -> None:
    _check_canvas_row_density_are_tokens_consumed_by_data_rows(
        TOKENS_CSS, STYLES_CSS
    )  # baseline: clean
    corrupted = STYLES_CSS.replace("height: var(--row-height);", "height: 25px;")
    assert corrupted != STYLES_CSS, "probe fixture did not hardcode the row height"
    with pytest.raises(AssertionError):
        _check_canvas_row_density_are_tokens_consumed_by_data_rows(TOKENS_CSS, corrupted)


def test_probe_pinned_literal_exception_rule_catches_a_stale_palette_value() -> None:
    _check_index_html_literal_exceptions_match_the_declared_tokens(
        INDEX_HTML, TOKENS_CSS
    )  # baseline: clean
    corrupted = INDEX_HTML.replace(
        '<meta name="theme-color" content="#141310">',
        '<meta name="theme-color" content="#0b111c">',
        1,
    )
    assert corrupted != INDEX_HTML, "probe fixture did not reintroduce the stale theme-color"
    with pytest.raises(AssertionError):
        _check_index_html_literal_exceptions_match_the_declared_tokens(corrupted, TOKENS_CSS)


def test_probe_external_network_rule_catches_a_web_font_link() -> None:
    _check_no_external_reference(INDEX_HTML, label="index.html")  # baseline: clean
    corrupted = INDEX_HTML.replace(
        "</head>",
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans">'
        "</head>",
        1,
    )
    assert corrupted != INDEX_HTML, "probe fixture did not add a web font link"
    with pytest.raises(AssertionError):
        _check_no_external_reference(corrupted, label="index.html")
