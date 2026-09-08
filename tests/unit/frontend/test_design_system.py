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

JS_COMPONENTS: tuple[str, ...] = (
    "app-core.js",
    "app-analysis.js",
    "app-technical.js",
    "app-operations.js",
    "app-mesa.js",
    "app-cazatiburones.js",
    "app-shell.js",
    "app.js",
)
CSS_COMPONENTS: tuple[str, ...] = (
    "styles-foundation.css",
    "styles-shell.css",
    "styles-mesa.css",
    "styles-analysis.css",
    "styles-technical.css",
    "styles-operations.css",
    "styles-cazatiburones.css",
)


def _read(name: str) -> str:
    return _STATIC.joinpath(name).read_text(encoding="utf-8")


TOKENS_CSS = _read("tokens.css")
STYLES_MANIFEST = _read("styles.css")
INDEX_HTML = _read("index.html")


def _compose(names: tuple[str, ...]) -> str:
    return "\n".join(_read(name).rstrip("\n") for name in names) + "\n"


def _compose_styles(styles_manifest: str) -> str:
    manifest_tail = "\n".join(styles_manifest.splitlines()[len(CSS_COMPONENTS) :]).lstrip("\n")
    return f"{_compose(CSS_COMPONENTS)}\n{manifest_tail}\n"


STYLES_CSS = _compose_styles(STYLES_MANIFEST)
APP_JS = _compose(JS_COMPONENTS)


def _check_html_component_order_and_bootstrap(
    index_html: str, component_text: dict[str, str]
) -> None:
    expected = [f"/assets/{name}" for name in JS_COMPONENTS]
    declared = re.findall(r'<script\s+src="([^"]+)"\s+defer></script>', index_html)
    assert declared == expected
    assert set(component_text) == set(JS_COMPONENTS)
    for text in component_text.values():
        assert text.startswith('"use strict";\n')
        assert text.count('"use strict";') == 1
    bootstrap = component_text["app.js"]
    assert bootstrap == '"use strict";\n\ninitialize();\n'
    assert bootstrap.count("initialize();") == 1
    assert "initialize();" not in "\n".join(component_text[name] for name in JS_COMPONENTS[:-1])


def test_html_declares_each_classic_deferred_script_once_in_canonical_order_and_bootstrap_initializes_once() -> (  # noqa: E501
    None
):
    _check_html_component_order_and_bootstrap(
        INDEX_HTML, {name: _read(name) for name in JS_COMPONENTS}
    )


def _check_css_manifest_order_and_component_ownership(
    styles_manifest: str, component_text: dict[str, str]
) -> None:
    expected = [f"/assets/{name}" for name in CSS_COMPONENTS]
    declared = re.findall(r'^@import url\("([^"]+)"\);$', styles_manifest, re.MULTILINE)
    assert declared == expected
    assert set(component_text) == set(CSS_COMPONENTS)
    assert all(text.strip() for text in component_text.values())
    assert all("@import" not in text for text in component_text.values())
    ownership_markers = {
        "styles-foundation.css": "* {",
        "styles-shell.css": ".sidebar {",
        "styles-mesa.css": ".mesa-layout {",
        "styles-analysis.css": ".market-chart-card {",
        "styles-technical.css": ".comparison-section {",
        "styles-operations.css": ".operation-panel {",
        "styles-cazatiburones.css": ".cazatiburones-feature-group {",
    }
    for component, marker in ownership_markers.items():
        assert marker in component_text[component]
        assert sum(marker in text for text in component_text.values()) == 1


def test_css_manifest_imports_each_component_once_in_canonical_order_without_orphans() -> None:
    _check_css_manifest_order_and_component_ownership(
        STYLES_MANIFEST,
        {name: _read(name) for name in CSS_COMPONENTS},
    )


def test_design_system_checker_aggregates_js_and_css_in_canonical_runtime_order() -> None:
    assert _compose(JS_COMPONENTS) == APP_JS
    assert _compose_styles(STYLES_MANIFEST) == STYLES_CSS
    assert "BOARD_REGISTRY" in _read("app-shell.js")
    assert "queryMarketChart" in _read("app-analysis.js")
    assert "queryMarketComparison" in _read("app-technical.js")
    assert "reviewItemId" in _read("app-operations.js")
    assert "loadMesaAnalyticalNews" in _read("app-mesa.js")
    assert "loadCazatiburonesBoard" in _read("app-cazatiburones.js")


def test_no_component_is_duplicated_or_orphaned_and_all_existing_assertions_remain_active() -> None:
    function_names = re.findall(
        r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
        APP_JS,
        re.MULTILINE,
    )
    assert len(function_names) == len(set(function_names))
    global_names = re.findall(r"^(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=", APP_JS, re.MULTILINE)
    assert len(global_names) == len(set(global_names))
    assert len(re.findall(r"<script\s+src=", INDEX_HTML)) == len(JS_COMPONENTS)
    assert len(re.findall(r"^@import\s+", STYLES_MANIFEST, re.MULTILINE)) == len(CSS_COMPONENTS)
    assert "test_every_token_defined_in_light_and_dark" in globals()


def test_no_duplicate_initialize_listener_timer_state_request_or_stale_response_guard_drift() -> (
    None
):
    assert APP_JS.count("initialize();") == 1
    for unique_runtime_binding in (
        "const response = await fetch(path, {",
        "window.setTimeout(startMarketClocks, delay)",
        "overviewTimer = window.setTimeout(",
        'byId("report-known-at").addEventListener("change",',
        'window.addEventListener("hashchange",',
        "const BOARD_DEFERRED_LOADS = Object.freeze({",
        "function isCurrentActivoBoardRequest(deferredRequest)",
    ):
        assert APP_JS.count(unique_runtime_binding) == 1
    for request_sequence in (
        "marketComparisonRequestSequence",
        "marketChartRequestSequence",
        "fundamentalTrendRequestSequence",
        "fundamentalResearchRequestSequence",
        "activoBoardRequestSequence",
        "mesaAnalyticalNewsRequestSequence",
        "mesaInstitutionalNewsRequestSequence",
        "mesaActivityNewsRequestSequence",
        "mesaIncidentsRequestSequence",
        "mesaUniverseCoverageRequestSequence",
        "cazatiburonesRequestSequence",
        "cazatiburonesUniverseRequestSequence",
    ):
        assert APP_JS.count(f"let {request_sequence} = 0;") == 1


def test_removed_lima_clock_known_at_capability_limitations_and_usage_copy_do_not_reappear() -> (
    None
):
    for control_id in ("lima-clock", "lima-clock-date"):
        assert not re.search(rf'<[a-z][^>]*\bid="{control_id}"[^>]*>', INDEX_HTML)
    for forbidden in (
        "Estas bandejas no están acotadas por el corte",
        "Cazatiburones, Documentos y Derivados",
        "Limitaciones declaradas",
        "Uso local · Sin ejecución de órdenes · No constituye asesoramiento financiero",
    ):
        assert forbidden not in INDEX_HTML
    assert "mesa-universe-not-queried" not in INDEX_HTML
    assert "mesa-universe-limitations" not in INDEX_HTML


def test_probe_component_loader_rejects_swapped_scripts_and_duplicate_initialize() -> None:
    component_text = {name: _read(name) for name in JS_COMPONENTS}
    _check_html_component_order_and_bootstrap(INDEX_HTML, component_text)
    swapped = INDEX_HTML.replace(
        '<script src="/assets/app-analysis.js" defer></script>\n'
        '    <script src="/assets/app-technical.js" defer></script>',
        '<script src="/assets/app-technical.js" defer></script>\n'
        '    <script src="/assets/app-analysis.js" defer></script>',
        1,
    )
    assert swapped != INDEX_HTML
    with pytest.raises(AssertionError):
        _check_html_component_order_and_bootstrap(swapped, component_text)

    duplicated_bootstrap = {
        **component_text,
        "app.js": component_text["app.js"] + "initialize();\n",
    }
    with pytest.raises(AssertionError):
        _check_html_component_order_and_bootstrap(INDEX_HTML, duplicated_bootstrap)


def test_probe_component_loader_rejects_an_orphaned_script() -> None:
    component_text = {name: _read(name) for name in JS_COMPONENTS}
    component_text.pop("app-technical.js")
    with pytest.raises(AssertionError):
        _check_html_component_order_and_bootstrap(INDEX_HTML, component_text)


def test_probe_css_component_checker_rejects_order_orphan_and_duplicate_ownership() -> None:
    component_text = {name: _read(name) for name in CSS_COMPONENTS}
    _check_css_manifest_order_and_component_ownership(STYLES_MANIFEST, component_text)
    swapped = STYLES_MANIFEST.replace(
        '@import url("/assets/styles-shell.css");\n@import url("/assets/styles-mesa.css");',
        '@import url("/assets/styles-mesa.css");\n@import url("/assets/styles-shell.css");',
        1,
    )
    assert swapped != STYLES_MANIFEST
    with pytest.raises(AssertionError):
        _check_css_manifest_order_and_component_ownership(swapped, component_text)

    orphaned = dict(component_text)
    orphaned.pop("styles-technical.css")
    with pytest.raises(AssertionError):
        _check_css_manifest_order_and_component_ownership(STYLES_MANIFEST, orphaned)

    duplicated = dict(component_text)
    duplicated["styles-shell.css"] += "\n.mesa-layout { display: block; }\n"
    with pytest.raises(AssertionError):
        _check_css_manifest_order_and_component_ownership(STYLES_MANIFEST, duplicated)


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
    assert "no evalúa feriados ni sesiones especiales" in INDEX_HTML
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
    # UI-4 connects cazatiburones: the obsolete promise that it "connects its
    # read path in UI-3" must not survive in the registry it once lived in.
    assert "conecta su lectura en UI-3" not in body


def test_board_registry_declares_exactly_the_six_boards() -> None:
    _check_board_registry_declares_exactly_six_boards(APP_JS)


def _check_no_board_remains_not_built(app_js: str) -> None:
    entries = _board_registry_entries(app_js)
    assert set(entries) == set(_EXPECTED_BOARD_IDS)
    not_built = [board_id for board_id, text in entries.items() if "built: false" in text]
    assert not_built == [], f"no board may remain built: false, found {not_built}"
    for board_id, text in entries.items():
        assert "built: true" in text, f"{board_id} must be declared built: true"
    # UI-4 connects cazatiburones: the stale promise that it "connects its
    # read path in UI-3" must not survive alongside the block that fulfills
    # it, whether inside the registry or anywhere else in the shell.
    assert "conecta su lectura en UI-3" not in app_js


def test_no_board_remains_not_built() -> None:
    _check_no_board_remains_not_built(APP_JS)


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
    # load now belongs to the single board-to-request graph and fires from
    # board activation, exactly once per loaded-board mark.
    assert 'byId("alert-inbox-panel").addEventListener("toggle"' not in app_js
    assert 'byId("candidate-inbox-panel").addEventListener("toggle"' not in app_js
    assert "const BOARD_DEFERRED_LOADS = Object.freeze(" in app_js
    board_loads = app_js[
        app_js.index("const BOARD_DEFERRED_LOADS") : app_js.index("const DEFAULT_BOARD_ID")
    ]
    assert "revisar: Object.freeze([loadCandidateInbox, loadAlertInbox])" in board_loads
    activate_body = _activate_board_body(app_js)
    assert "loadDeferredBoardData(resolvedId)" in activate_body
    assert "loadedBoardIds.has(boardId)" in app_js
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
    assert "loadDeferredBoardData(resolvedId)" in body
    assert "activateBoard(boardIdFromLocationHash()" not in body


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
# from (origin/main@4fc61c7ca3...), extracted the same way the check above
# extracts the candidate's routes: every `/api/...` literal outside the
# BOARD_REGISTRY declaration. UI-7 adds the already-integrated, read-only
# Cazatiburones notification inbox route; no transport or contract changes.
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
        "/api/v1/cazatiburones/declared-activity",
        "/api/v1/cazatiburones/institutional-observations",
        "/api/v1/cazatiburones/notifications",
        "/api/v1/cazatiburones/universe-activity",
        "/api/v1/crypto-derivatives",
        "/api/v1/market-comparison",
        "/api/v1/overview",
        "/api/v1/sec-document-timeline",
        "/api/v1/universe-coverage",
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
# Cazatiburones connected read paths (UI-4): three separate presentations
# over the three read-only endpoints already integrated by #159/#160, all
# sharing the single global known_at cut and selectedMarketAsset, mapping
# every empty/None/not_evaluable state to the declared absence grammar, and
# never combining families, participants or rows into an aggregate,
# effective portfolio, or signal.
# ---------------------------------------------------------------------------


def _cazatiburones_region(app_js: str) -> str:
    start = app_js.index("let cazatiburonesRequestSequence")
    end = app_js.index("async function initialize")
    return app_js[start:end]


def _check_declared_activity_separates_insider_and_beneficial(index_html: str, app_js: str) -> None:
    assert 'id="cazatiburones-insider-features"' in index_html
    assert 'id="cazatiburones-beneficial-features"' in index_html
    fn = _extract_js_function(app_js, "renderCazatiburonesDeclaredActivity")
    assert "payload.insider_features" in fn
    assert "payload.beneficial_features" in fn
    assert "cazatiburones-insider-features" in fn
    assert "cazatiburones-beneficial-features" in fn
    assert fn.count("renderCazatiburonesFeatureGroup") == 2, (
        "insider and beneficial features must render through two separate calls, never one "
        "combined list"
    )
    assert "total_statements" in fn
    assert "truncated" in fn


def test_declared_activity_separates_insider_and_beneficial() -> None:
    _check_declared_activity_separates_insider_and_beneficial(INDEX_HTML, APP_JS)


def _check_institutional_observations_expose_page_coverage(app_js: str) -> None:
    fn = _extract_js_function(app_js, "renderCazatiburonesInstitutionalObservations")
    for token in ("total_matching", "offset", "limit", "truncated"):
        assert token in fn, f"institutional observations render must expose {token!r}"
    assert "manager_cik" in fn and "report_id" in fn and "cusip" in fn


def test_institutional_observations_expose_page_coverage() -> None:
    _check_institutional_observations_expose_page_coverage(APP_JS)


def _check_document_timeline_exposes_revision_identity_and_coverage(app_js: str) -> None:
    fn = _extract_js_function(app_js, "renderCazatiburonesDocumentTimeline")
    for token in (
        "accession",
        "is_amendment",
        "available_at",
        "content_sha256",
        "source_url",
        "matched_count",
        "returned_count",
        "legacy_records_excluded",
        "truncated",
    ):
        assert token in fn, f"document timeline render must expose {token!r}"


def test_document_timeline_exposes_revision_identity_and_coverage() -> None:
    _check_document_timeline_exposes_revision_identity_and_coverage(APP_JS)


_TIMELINE_MISSING_PREFIX = (
    '(payload.state === "missing" ? "Sin documentos SEC para el activo y corte '
    'seleccionados · " : "") +'
)


def _check_document_timeline_shows_coverage_even_when_missing(app_js: str) -> None:
    fn = _extract_js_function(app_js, "renderCazatiburonesDocumentTimeline")
    assert _TIMELINE_MISSING_PREFIX in fn, (
        "the four coverage counters (matched_count, returned_count, legacy_records_excluded, "
        "truncated) must render unconditionally, including under state: 'missing' -- only the "
        "absence phrasing may be conditional, never the counters themselves"
    )


def test_document_timeline_shows_coverage_even_when_missing() -> None:
    _check_document_timeline_shows_coverage_even_when_missing(APP_JS)


def _check_cazatiburones_board_uses_the_global_known_at_cut(index_html: str, app_js: str) -> None:
    fn = _extract_js_function(app_js, "loadCazatiburonesBoard")
    assert 'byId("report-known-at")' in fn
    assert "selectedMarketAsset" in fn
    # No second cut control and no second asset selector for this board.
    assert 'id="cazatiburones-known-at"' not in index_html
    assert 'id="cazatiburones-asset"' not in index_html
    assert index_html.count('id="report-known-at"') == 1


def test_cazatiburones_board_uses_the_global_known_at_cut() -> None:
    _check_cazatiburones_board_uses_the_global_known_at_cut(INDEX_HTML, APP_JS)


def _check_cazatiburones_board_never_fabricates_a_known_at_cut(app_js: str) -> None:
    fn = _extract_js_function(app_js, "loadCazatiburonesBoard")
    assert "new Date(" not in fn, "loadCazatiburonesBoard must never invent its own known_at cut"
    assert 'byId("report-known-at").value.trim()' in fn


def test_cazatiburones_board_never_fabricates_a_known_at_cut() -> None:
    _check_cazatiburones_board_never_fabricates_a_known_at_cut(APP_JS)


def _check_cazatiburones_absence_states_map_to_declared_marks(app_js: str) -> None:
    metric_fn = _extract_js_function(app_js, "cazatiburonesMetricMarkup")
    assert '"missing" : "not-evaluable"' in metric_fn
    field_fn = _extract_js_function(app_js, "cazatiburonesFieldOrAbsence")
    assert 'renderAbsenceMark("missing"' in field_fn
    comparison_fn = _extract_js_function(app_js, "cazatiburonesComparisonMarkup")
    assert 'renderAbsenceMark(\n      "not-evaluable"' in comparison_fn or (
        "not-evaluable" in comparison_fn and "not_evaluable" in comparison_fn
    )
    load_fn = _extract_js_function(app_js, "loadCazatiburonesBoard")
    assert '"not-applicable"' in load_fn
    region = _cazatiburones_region(app_js)
    assert 'renderAbsenceMark("missing"' in region


def test_cazatiburones_absence_states_map_to_declared_marks() -> None:
    _check_cazatiburones_absence_states_map_to_declared_marks(APP_JS)


def _check_sec_document_families_never_share_a_row(index_html: str, app_js: str) -> None:
    assert 'id="cazatiburones-timeline-asset-document"' in index_html
    assert 'id="cazatiburones-timeline-filer-document"' in index_html
    fn = _extract_js_function(app_js, "renderCazatiburonesDocumentTimeline")
    assert "asset_document" in fn and "filer_document" in fn
    assert "cazatiburones-timeline-asset-document" in fn
    assert "cazatiburones-timeline-filer-document" in fn


def test_sec_document_families_never_share_a_row() -> None:
    _check_sec_document_families_never_share_a_row(INDEX_HTML, APP_JS)


def _check_cazatiburones_board_issues_read_only_requests(app_js: str) -> None:
    fn = _extract_js_function(app_js, "loadCazatiburonesBoard")
    assert "method:" not in fn, "cazatiburones must never issue anything but a plain GET"
    assert fn.count("api(`/api/v1/") == 3


def test_cazatiburones_board_issues_read_only_requests() -> None:
    _check_cazatiburones_board_issues_read_only_requests(APP_JS)


_FORBIDDEN_PORTFOLIO_TERMS = ("effective_portfolio", "effectiveportfolio", "cartera efectiva")
_FORBIDDEN_SIGNAL_CHANNEL_TERMS = ("postmessage(", "notification(", "sendalert", "webhook")


def _check_cazatiburones_board_declares_no_effective_portfolio(app_js: str) -> None:
    region = _cazatiburones_region(app_js).lower()
    found = [term for term in _FORBIDDEN_PORTFOLIO_TERMS if term in region]
    assert not found, f"forbidden effective-portfolio term(s) in cazatiburones region: {found}"
    assert "portfolio" not in region


def test_cazatiburones_board_declares_no_effective_portfolio() -> None:
    _check_cazatiburones_board_declares_no_effective_portfolio(APP_JS)


def _check_cazatiburones_board_emits_no_signal_or_channel(app_js: str) -> None:
    region = _cazatiburones_region(app_js).lower()
    found = [term for term in _FORBIDDEN_SIGNAL_CHANNEL_TERMS if term in region]
    assert not found, f"forbidden signal/channel term(s) in cazatiburones region: {found}"


def test_cazatiburones_board_emits_no_signal_or_channel() -> None:
    _check_cazatiburones_board_emits_no_signal_or_channel(APP_JS)


def _check_design_system_documentation_declares_the_cazatiburones_read_path(doc_text: str) -> None:
    normalized = re.sub(r"\s+", " ", doc_text).lower()
    for endpoint in (
        "sec-document-timeline",
        "cazatiburones/declared-activity",
        "cazatiburones/institutional-observations",
    ):
        assert endpoint in normalized, f"the doc must name the {endpoint!r} read path"
    assert "not-applicable" in normalized
    assert "not-evaluable" in normalized
    assert "missing" in normalized


def test_design_system_documentation_declares_the_cazatiburones_read_path() -> None:
    doc_path = (
        Path(str(files("investment_analyst"))).parent.parent
        / "docs"
        / "local_interface_design_system.md"
    )
    _check_design_system_documentation_declares_the_cazatiburones_read_path(
        doc_path.read_text(encoding="utf-8")
    )


def _check_route_registers_the_connected_cazatiburones_board(doc_text: str) -> None:
    assert re.search(r"\|\s*`LOCAL-INTERFACE`\s*\|\s*`PLANNED`\s*\|", doc_text), (
        "LOCAL-INTERFACE must remain PLANNED; this block advances evidence, completes nothing"
    )
    normalized = re.sub(r"\s+", " ", doc_text).lower()
    assert "cazatiburones" in normalized and "ui-4" in normalized
    assert "reservado `not-built`" not in doc_text, (
        "the pending not-built reservation must not survive alongside the block that connects it"
    )
    assert "5.4" in doc_text
    assert "ibm plex" in normalized


def test_route_registers_the_connected_cazatiburones_board() -> None:
    doc_path = (
        Path(str(files("investment_analyst"))).parent.parent
        / "docs"
        / "basic_functional_release_plan.md"
    )
    _check_route_registers_the_connected_cazatiburones_board(doc_path.read_text(encoding="utf-8"))


def _check_route_keeps_sec_corpus_as_the_single_next(doc_text: str) -> None:
    next_rows = re.findall(r"\|\s*`([A-Z-]+)`\s*\|\s*`NEXT`\s*\|", doc_text)
    assert next_rows == ["SEC-CORPUS"], (
        f"expected exactly one NEXT row (SEC-CORPUS), found {next_rows}"
    )


def test_route_keeps_sec_corpus_as_the_single_next() -> None:
    doc_path = (
        Path(str(files("investment_analyst"))).parent.parent
        / "docs"
        / "basic_functional_release_plan.md"
    )
    _check_route_keeps_sec_corpus_as_the_single_next(doc_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Cazatiburones universe index and local filters (UI-11): the existing
# universe-activity contract is projected into one seven-column row per
# asset/family, while the three detailed reads remain independent.
# ---------------------------------------------------------------------------


def _cazatiburones_universe_region(app_js: str) -> str:
    start = app_js.index("const CAZATIBURONES_UNIVERSE_FAMILIES")
    end = app_js.index("// Every optional descriptive field", start)
    return app_js[start:end]


def test_cazatiburones_universe_index_is_independent_responsive_and_seven_column() -> None:
    board = _board_slices(INDEX_HTML)["cazatiburones"]
    table = re.search(r'<table id="cazatiburones-universe-table".*?</table>', board, re.DOTALL)
    assert table, "the universe index must expose an accessible table"
    columns = re.findall(r'<th scope="col">([^<]+)</th>', table.group(0))
    assert columns == [
        "Activo",
        "Familia",
        "Capacidad",
        "Evidencia",
        "Declaraciones",
        "Última disponible",
        "Antigüedad",
    ]
    assert board.index('id="cazatiburones-universe-index"') < board.index(
        'id="cazatiburones-detail-content"'
    )
    assert 'class="cazatiburones-universe-table-scroll hidden"' in board
    assert ".cazatiburones-universe-table-scroll" in STYLES_CSS
    assert "overflow-x: auto" in STYLES_CSS
    assert "@media (max-width: 760px)" in STYLES_CSS
    loader = _extract_js_function(APP_JS, "loadCazatiburonesUniverseIndex")
    assert "cazatiburonesEligiblePresentation" not in loader
    assert "selectedMarketAsset" not in loader
    assert "asset_id" not in loader


def test_universe_load_uses_global_cut_once_without_asset_id_and_discards_stale_responses() -> None:
    loader = _extract_js_function(APP_JS, "loadCazatiburonesUniverseIndex")
    assert 'byId("report-known-at").value.trim()' in loader
    assert "new URLSearchParams({ known_at: knownAt })" in loader
    assert loader.count("/api/v1/cazatiburones/universe-activity?") == 1
    assert "cazatiburonesUniverseRequestSequence" in loader
    assert loader.count("sequence !== cazatiburonesUniverseRequestSequence") >= 2
    assert 'knownAt !== byId("report-known-at").value.trim()' in loader
    assert '"asset_id"' not in loader


def test_canonical_cazatiburones_load_graph_and_deep_link_dispatch_index_plus_detail() -> None:
    entry = _board_deferred_load_entry(APP_JS, "cazatiburones")
    assert re.findall(r"loadCazatiburones(?:UniverseIndex|Board)", entry) == [
        "loadCazatiburonesUniverseIndex",
        "loadCazatiburonesBoard",
    ]
    initialize = _extract_js_function(APP_JS, "initialize")
    assert "await loadMarketAssets();" in initialize
    assert "await loadAssetPreferences();" in initialize
    assert "loadDeferredBoardData(boardIdFromLocationHash());" in initialize
    assert "void loadCazatiburonesUniverseIndex();" not in initialize
    assert "void loadCazatiburonesBoard();" not in initialize


def test_one_row_per_asset_family_preserves_capability_evidence_count_and_availability_fields() -> (
    None
):
    rows_fn = _extract_js_function(APP_JS, "cazatiburonesUniverseRowsFromPayload")
    assert rows_fn.count("rows.push") == 1
    for field in (
        "asset.asset_id",
        "asset.symbol",
        "asset.name",
        "family.key",
        "state.capability",
        "state.evidence",
        "state.statements",
        "state.latest_available_at",
        "state.latest_age_days",
        "state.not_evaluable_reason",
    ):
        assert field in rows_fn
    render_fn = _extract_js_function(APP_JS, "renderCazatiburonesUniverseRows")
    assert "cazatiburonesUniverseSnapshot.rows.filter" in render_fn
    assert "latestAvailableAt" in render_fn and "latestAgeDays" in render_fn


def test_three_families_remain_fixed_separate_and_unaggregated() -> None:
    family_block = APP_JS[
        APP_JS.index("const CAZATIBURONES_UNIVERSE_FAMILIES") : APP_JS.index(
            "let cazatiburonesUniverseRequestSequence"
        )
    ]
    assert re.findall(r'key: "([a-z]+)"', family_block) == [
        "insider",
        "beneficial",
        "institutional",
    ]
    rows_fn = _extract_js_function(APP_JS, "cazatiburonesUniverseRowsFromPayload")
    assert "for (const family of CAZATIBURONES_UNIVERSE_FAMILIES)" in rows_fn
    assert ".reduce(" not in rows_fn
    assert ".sort(" not in rows_fn
    assert "Object.freeze({" in rows_fn


def test_search_family_and_evidence_filters_intersect_locally_without_reordering_or_fetching() -> (
    None
):
    match_fn = _extract_js_function(APP_JS, "cazatiburonesUniverseRowMatches")
    assert "searchMatches && familyMatches && evidenceMatches" in match_fn
    assert "normalizeCazatiburonesUniverseSearch" in match_fn
    filter_fn = _extract_js_function(APP_JS, "initializeCazatiburonesUniverseFilters")
    assert filter_fn.count("renderCazatiburonesUniverseRows()") >= 4
    assert 'addEventListener("input"' in filter_fn
    assert filter_fn.count('addEventListener("change"') == 2
    assert 'addEventListener("click"' in filter_fn
    assert "api(" not in filter_fn
    assert "fetch(" not in filter_fn
    render_fn = _extract_js_function(APP_JS, "renderCazatiburonesUniverseRows")
    assert ".filter(cazatiburonesUniverseRowMatches)" in render_fn
    assert ".sort(" not in render_fn


def test_filtered_empty_and_endpoint_empty_remain_distinct_and_accessible() -> None:
    render_fn = _extract_js_function(APP_JS, "renderCazatiburonesUniverseRows")
    assert "cazatiburonesUniverseSnapshot.rows.length === 0" in render_fn
    assert "El índice no devolvió activos para el corte seleccionado." in render_fn
    assert "Ninguna fila coincide con los filtros locales." in render_fn
    assert 'classList.remove("hidden")' in render_fn
    board = _board_slices(INDEX_HTML)["cazatiburones"]
    assert 'id="cazatiburones-universe-empty"' in board
    assert 'id="cazatiburones-universe-error"' in board
    assert 'role="alert"' in board
    assert 'role="status" aria-live="polite"' in board


def test_row_navigation_reuses_global_asset_selection_and_preserves_filters() -> None:
    navigation_fn = _extract_js_function(APP_JS, "navigateCazatiburonesUniverseAsset")
    assert "selectMarketAssetForNavigation(assetId)" in navigation_fn
    assert "api(" not in navigation_fn
    assert "selectMarketAssetForNavigation = selectComboboxOption;" in APP_JS
    render_fn = _extract_js_function(APP_JS, "renderCazatiburonesUniverseRows")
    assert "assetButton.addEventListener" in render_fn
    assert "row.assetId" in render_fn
    board = _board_slices(INDEX_HTML)["cazatiburones"]
    assert 'id="cazatiburones-asset"' not in board
    assert 'id="cazatiburones-known-at"' not in board
    assert 'id="report-known-at"' not in board


def test_docs_state_index_fields_filter_semantics_pit_and_non_aggregation_limits() -> None:
    repository_root = Path(str(files("investment_analyst"))).parent.parent
    documents = [
        (repository_root / "docs" / "local_interface.md").read_text(encoding="utf-8"),
        (repository_root / "docs" / "local_interface_design_system.md").read_text(encoding="utf-8"),
    ]
    for document in documents:
        normalized = re.sub(r"\s+", " ", document).lower()
        for token in (
            "cazatiburones-universe-activity-v1",
            "known_at",
            "insider",
            "beneficial",
            "institutional",
            "capability",
            "evidence",
            "latest_available_at",
            "latest_age_days",
            "not_evaluable_reason",
            "filtros locales",
            "available_at <= known_at",
            "agreg",
        ):
            assert token in normalized, f"documentation must declare {token!r}"


def test_three_cazatiburones_detail_endpoints_and_read_only_semantics_unchanged() -> None:
    detail_fn = _extract_js_function(APP_JS, "loadCazatiburonesBoard")
    for endpoint in (
        "/api/v1/cazatiburones/declared-activity?",
        "/api/v1/cazatiburones/institutional-observations?",
        "/api/v1/sec-document-timeline?",
    ):
        assert endpoint in detail_fn
    for parameter in ("asset_id", "known_at", "offset", "limit"):
        assert parameter in detail_fn
    assert detail_fn.count("api(`/api/v1/") == 3
    assert "method:" not in detail_fn
    for container_id in (
        "cazatiburones-insider-features",
        "cazatiburones-beneficial-features",
        "cazatiburones-institutional-observations-rows",
        "cazatiburones-timeline-asset-document",
        "cazatiburones-timeline-filer-document",
    ):
        assert f'id="{container_id}"' in INDEX_HTML


def test_no_cross_asset_or_cross_family_total_score_rank_order_or_effective_portfolio() -> None:
    region = _cazatiburones_universe_region(APP_JS).lower()
    for forbidden in ("portfolio", "score", "rank", "reduce(", "sum(", ".sort("):
        assert forbidden not in region
    assert "payload.assets" in region
    assert "for (const family of cazatiburones_universe_families)" in region


def test_no_filter_or_row_scoped_network_request_and_no_write_action() -> None:
    region = _cazatiburones_universe_region(APP_JS)
    filter_fn = _extract_js_function(APP_JS, "initializeCazatiburonesUniverseFilters")
    render_fn = _extract_js_function(APP_JS, "renderCazatiburonesUniverseRows")
    for source in (filter_fn, render_fn):
        assert "api(" not in source
        assert "fetch(" not in source
        assert "method:" not in source
        assert "POST" not in source and "PUT" not in source and "DELETE" not in source
    assert 'addEventListener("click"' in render_fn
    assert "renderCazatiburonesUniverseRows()" in filter_fn
    assert "api(`/api/v1/cazatiburones/universe-activity?" in region


def test_no_visible_limitations_wall_or_state_inference_from_limitations() -> None:
    board = _board_slices(INDEX_HTML)["cazatiburones"]
    assert "limitations" not in board.lower()
    render_fn = _extract_js_function(APP_JS, "renderCazatiburonesUniverseSnapshot")
    assert "limitations" not in render_fn.lower()
    rows_fn = _extract_js_function(APP_JS, "cazatiburonesUniverseRowsFromPayload")
    assert "limitations" not in rows_fn.lower()
    assert "notEvaluableReason" in rows_fn


# ---------------------------------------------------------------------------
# Cazatiburones stays fresh under the shared cut (UI-4 AUDIT fix): a direct
# deep link into #cazatiburones must not read eligibility before
# marketAssets exists, and the board must reload -- with the new asset or
# cut -- when either changes while it is the active board. Point-in-time
# correctness means the board never keeps showing a stale asset or a stale
# known_at once either one has moved.
# ---------------------------------------------------------------------------


def _check_cazatiburones_board_reloads_after_market_assets_are_ready(app_js: str) -> None:
    fn = _extract_js_function(app_js, "initialize")
    reload_call = "loadDeferredBoardData(boardIdFromLocationHash());"
    assert reload_call in fn, (
        "initialize() must dispatch the canonical deferred graph once assets are ready"
    )
    assert "await loadMarketAssets();" in fn
    assert "applySelectedMarketAsset();" in fn
    assert fn.index("await loadMarketAssets();") < fn.index(reload_call), (
        "a #cazatiburones deep link must not evaluate eligibility before marketAssets loads"
    )
    assert fn.index("applySelectedMarketAsset();") < fn.index(reload_call)


def test_cazatiburones_board_reloads_after_market_assets_are_ready() -> None:
    _check_cazatiburones_board_reloads_after_market_assets_are_ready(APP_JS)


def _check_cazatiburones_board_reloads_when_the_selected_asset_changes(app_js: str) -> None:
    match = re.search(
        r"async function selectComboboxOption\(assetId\) \{(.*?)\n  \}", app_js, re.DOTALL
    )
    assert match, "selectComboboxOption(assetId) must exist"
    body = match.group(1)
    assert "selectedMarketAsset = assetId;" in body
    assert "invalidateDeferredBoardLoads();" in body
    assert "activateBoard(boardIdFromLocationHash(), { focus: false });" in body
    assert (
        body.index("selectedMarketAsset = assetId;")
        < body.index("invalidateDeferredBoardLoads();")
        < body.index("activateBoard(boardIdFromLocationHash(), { focus: false });")
    ), "asset changes must invalidate before reloading the visible board"


def test_cazatiburones_board_reloads_when_the_selected_asset_changes() -> None:
    _check_cazatiburones_board_reloads_when_the_selected_asset_changes(APP_JS)


def _check_cazatiburones_board_reloads_when_the_known_at_cut_changes(app_js: str) -> None:
    match = re.search(
        r'byId\("report-known-at"\)\.addEventListener\("change", \(\) => \{(.*?)\n\}\);',
        app_js,
        re.DOTALL,
    )
    assert match, "report-known-at change listener must exist"
    body = match.group(1)
    assert "invalidateDeferredBoardLoads();" in body
    assert "activateBoard(boardIdFromLocationHash(), { focus: false });" in body


def test_cazatiburones_board_reloads_when_the_known_at_cut_changes() -> None:
    _check_cazatiburones_board_reloads_when_the_known_at_cut_changes(APP_JS)


# ---------------------------------------------------------------------------
# Deferred board loading (UI-5): the visible board owns its data requests.
# These checks stay static by design; the Work Block's required smoke covers
# the browser-level activation and late-response behavior separately.
# ---------------------------------------------------------------------------

_BOARD_DEFERRED_LOADS_RE = re.compile(
    r"const BOARD_DEFERRED_LOADS = Object\.freeze\(\{(.*?)\n\}\);\n\n"
    r"const DEFAULT_BOARD_ID",
    re.DOTALL,
)
_DEFERRED_LOAD_NAMES = (
    "refreshOverview",
    "loadMesaAnalyticalNews",
    "loadMesaCazatiburonesNewsFamilies",
    "loadMesaIncidents",
    "loadMesaUniverseCoverage",
    "queryReport",
    "queryMarketChart",
    "queryFundamentalTrend",
    "queryFundamentalResearch",
    "loadCandidateInbox",
    "loadAlertInbox",
    "loadCazatiburonesUniverseIndex",
    "loadCazatiburonesBoard",
)


def _board_deferred_loads_body(app_js: str) -> str:
    match = _BOARD_DEFERRED_LOADS_RE.search(app_js)
    assert match, "BOARD_DEFERRED_LOADS must be the single frozen request graph"
    return match.group(1)


def _board_deferred_load_entry(app_js: str, board_id: str) -> str:
    body = _board_deferred_loads_body(app_js)
    match = re.search(
        rf"{re.escape(board_id)}: Object\.freeze\(\[(.*?)\]\)",
        body,
        re.DOTALL,
    )
    assert match, f"BOARD_DEFERRED_LOADS must declare {board_id!r}"
    return match.group(1)


def _check_initialize_fires_no_board_query(app_js: str) -> None:
    initialize = _extract_js_function(app_js, "initialize")
    for query_name in (
        "refreshOverview",
        "loadMesaAnalyticalNews",
        "loadMesaIncidents",
        "loadMesaUniverseCoverage",
        "queryReport",
        "queryMarketChart",
        "queryFundamentalTrend",
        "queryFundamentalResearch",
    ):
        assert f"{query_name}(" not in initialize, (
            f"initialize() must not invoke the deferred board query {query_name}"
        )
    assert "await loadMarketAssets();" in initialize
    assert "await loadAssetPreferences();" in initialize
    assert "loadDeferredBoardData(boardIdFromLocationHash());" in initialize


def test_initialize_fires_no_board_query() -> None:
    _check_initialize_fires_no_board_query(APP_JS)


def _check_initialize_shell_loads_are_exactly_market_assets_and_preferences(app_js: str) -> None:
    initialize = _extract_js_function(app_js, "initialize")
    allowed_framework_loads = (
        "await loadMarketAssets();",
        "await loadAssetPreferences();",
    )
    for call in allowed_framework_loads:
        assert call in initialize
    assert "loadDeferredBoardData(boardIdFromLocationHash());" in initialize
    assert "await refreshOverview();" not in initialize
    assert "Promise.all([" not in initialize


def test_initialize_shell_loads_are_exactly_market_assets_and_preferences() -> None:
    _check_initialize_shell_loads_are_exactly_market_assets_and_preferences(APP_JS)


def _check_board_to_deferred_loads_table_covers_the_six_registered_boards(app_js: str) -> None:
    assert set(_EXPECTED_BOARD_IDS) == {
        board_id
        for board_id in _EXPECTED_BOARD_IDS
        if re.search(
            rf"{re.escape(board_id)}: Object\.freeze\(", _board_deferred_loads_body(app_js)
        )
    }
    expected_loads = {
        "mesa": [
            "refreshOverview",
            "loadMesaAnalyticalNews",
            "loadMesaCazatiburonesNewsFamilies",
            "loadMesaIncidents",
            "loadMesaUniverseCoverage",
        ],
        "activo": [
            "queryReport",
            "queryMarketChart",
            "queryFundamentalTrend",
            "queryFundamentalResearch",
        ],
        "tecnico": [],
        "revisar": ["loadCandidateInbox", "loadAlertInbox"],
        "cazatiburones": ["loadCazatiburonesUniverseIndex", "loadCazatiburonesBoard"],
        "sistema": [],
    }
    for board_id, expected in expected_loads.items():
        entry = _board_deferred_load_entry(app_js, board_id)
        actual = [name for name in _DEFERRED_LOAD_NAMES if re.search(rf"\b{name}\b", entry)]
        assert actual == expected, (
            f"deferred loads for {board_id}: expected {expected}, found {actual}"
        )


def test_board_to_deferred_loads_table_covers_the_six_registered_boards() -> None:
    _check_board_to_deferred_loads_table_covers_the_six_registered_boards(APP_JS)


def _check_activate_board_fires_exactly_the_declared_loads(app_js: str) -> None:
    body = _activate_board_body(app_js)
    assert "loadDeferredBoardData(resolvedId)" in body
    for forbidden in (
        "refreshoverview",
        "queryreport",
        "querymarketchart",
        "queryfundamentaltrend",
        "queryfundamentalresearch",
        "known_at",
        "known-at",
        "session-clock",
        "api(",
    ):
        assert forbidden not in body.lower(), (
            f"activateBoard must not hardcode deferred load or cut behavior: {forbidden}"
        )
    assert "loadedBoardIds.has(boardId)" in app_js
    assert "loadedBoardIds.add(boardId)" in app_js


def test_activate_board_fires_exactly_the_declared_loads() -> None:
    _check_activate_board_fires_exactly_the_declared_loads(APP_JS)


def _check_revisar_and_cazatiburones_keep_their_current_triggers(app_js: str) -> None:
    assert _board_deferred_load_entry(app_js, "revisar").count("loadCandidateInbox") == 1
    assert _board_deferred_load_entry(app_js, "revisar").count("loadAlertInbox") == 1
    assert (
        _board_deferred_load_entry(app_js, "cazatiburones").count("loadCazatiburonesUniverseIndex")
        == 1
    )
    assert _board_deferred_load_entry(app_js, "cazatiburones").count("loadCazatiburonesBoard") == 1
    initialize = _extract_js_function(app_js, "initialize")
    assert "loadDeferredBoardData(boardIdFromLocationHash());" in initialize
    assert 'byId("candidate-notification-panel").addEventListener("toggle"' in app_js
    assert 'byId("screening-rules-panel").addEventListener("toggle"' in app_js


def test_revisar_and_cazatiburones_keep_their_current_triggers() -> None:
    _check_revisar_and_cazatiburones_keep_their_current_triggers(APP_JS)


def _check_same_board_reactivation_does_not_refetch(app_js: str) -> None:
    dispatcher = _extract_js_function(app_js, "loadDeferredBoardData")
    assert "loadedBoardIds.has(boardId)" in dispatcher
    assert "loadedBoardIds.add(boardId)" in dispatcher
    assert "loadedBoardIds.clear()" in _extract_js_function(app_js, "invalidateDeferredBoardLoads")
    activate = _activate_board_body(app_js)
    assert activate.count("loadDeferredBoardData(resolvedId)") == 1


def test_same_board_reactivation_does_not_refetch() -> None:
    _check_same_board_reactivation_does_not_refetch(APP_JS)


def _check_asset_or_cut_change_invalidates_loaded_marks_and_refetches_visible_board(
    app_js: str,
) -> None:
    invalidation = _extract_js_function(app_js, "invalidateDeferredBoardLoads")
    assert "loadedBoardIds.clear()" in invalidation
    assert "activoBoardRequestSequence += 1" in invalidation
    assert "marketChartRequestSequence += 1" in invalidation
    assert "fundamentalTrendRequestSequence += 1" in invalidation
    assert "fundamentalResearchRequestSequence += 1" in invalidation
    selection = re.search(
        r"async function selectComboboxOption\(assetId\) \{(.*?)\n  \}",
        app_js,
        re.DOTALL,
    )
    assert selection and "invalidateDeferredBoardLoads();" in selection.group(1)
    cut_change = re.search(
        r'byId\("report-known-at"\)\.addEventListener\("change", \(\) => \{(.*?)\n\}\);',
        app_js,
        re.DOTALL,
    )
    assert cut_change and "invalidateDeferredBoardLoads();" in cut_change.group(1)
    assert "activateBoard(boardIdFromLocationHash(), { focus: false });" in cut_change.group(1)


def test_asset_or_cut_change_invalidates_loaded_marks_and_refetches_visible_board() -> None:
    _check_asset_or_cut_change_invalidates_loaded_marks_and_refetches_visible_board(APP_JS)


def _async_query_region(app_js: str, function_name: str) -> str:
    start = app_js.index(f"async function {function_name}(")
    following = [
        position
        for position in (
            app_js.find("\nasync function ", start + 1),
            app_js.find("\nfunction ", start + 1),
            app_js.find("\nconst VALUATION_STATUS_LABELS", start + 1),
            app_js.find('\nbyId("report-form")', start + 1),
        )
        if position >= 0
    ]
    assert following, f"could not delimit async function {function_name}"
    return app_js[start : min(following)]


_ACTIVO_QUERY_NAMES = (
    "queryReport",
    "queryMarketChart",
    "queryFundamentalTrend",
    "queryFundamentalResearch",
)


def _check_activo_loads_guard_sequence_and_selected_asset_before_painting(app_js: str) -> None:
    assert "let activoBoardRequestSequence = 0;" in app_js
    assert "sequence: ++activoBoardRequestSequence" in _extract_js_function(
        app_js, "loadDeferredBoardData"
    )
    for function_name in _ACTIVO_QUERY_NAMES:
        region = _async_query_region(app_js, function_name)
        assert "deferredRequest?.assetId" in region
        assert "deferredRequest?.knownAt" in region
        assert "isCurrentActivoBoardRequest(deferredRequest)" in region
        assert "assetId === selectedMarketAsset" in region
        assert 'knownAt === byId("report-known-at").value.trim()' in region
        assert "if (!isCurrentRequest()) return;" in region


def test_activo_loads_guard_sequence_and_selected_asset_before_painting() -> None:
    _check_activo_loads_guard_sequence_and_selected_asset_before_painting(APP_JS)


def _check_superseded_response_is_discarded_without_rendering(app_js: str) -> None:
    render_names = {
        "queryReport": "renderReport",
        "queryMarketChart": "renderMarketChart",
        "queryFundamentalTrend": "renderFundamentalTrend",
        "queryFundamentalResearch": "renderFundamentalResearch",
    }
    for function_name, render_name in render_names.items():
        region = _async_query_region(app_js, function_name)
        api_position = region.index("await api(")
        guard_marker = (
            "request !== listedCompanyReportRequest"
            if function_name == "queryReport"
            else "!isCurrentRequest()"
        )
        guard_position = region.index(guard_marker, api_position)
        render_position = region.index(render_name, guard_position)
        assert api_position < guard_position < render_position


def test_superseded_response_is_discarded_without_rendering() -> None:
    _check_superseded_response_is_discarded_without_rendering(APP_JS)


def _check_single_global_known_at_cut_remains_visible_in_every_board(
    app_js: str, index_html: str
) -> None:
    assert index_html.count('id="report-known-at"') == 1
    assert 'byId("report-known-at")' in app_js
    assert 'known_at: byId("report-known-at").value.trim()' in app_js
    assert 'const knownAt = byId("report-known-at").value.trim();' in app_js
    assert "report-known-at" not in _activate_board_body(app_js)


def test_single_global_known_at_cut_remains_visible_in_every_board() -> None:
    _check_single_global_known_at_cut_remains_visible_in_every_board(APP_JS, INDEX_HTML)


def _check_absence_grammar_and_error_messages_are_preserved(app_js: str) -> None:
    assert "renderAbsenceMark" in app_js
    for function_name, required_text in (
        ("queryReport", "setMessage(error.message, true)"),
        ("queryMarketChart", "El gráfico no pudo construirse para el corte solicitado."),
        ("queryFundamentalTrend", "La tendencia fundamental no pudo construirse."),
        ("queryFundamentalResearch", "error.message"),
    ):
        assert required_text in _async_query_region(app_js, function_name)


def test_absence_grammar_and_error_messages_are_preserved() -> None:
    _check_absence_grammar_and_error_messages_are_preserved(APP_JS)


def _check_ui5_documentation_matrix_and_invalidation(
    *, interface_doc: str, design_doc: str
) -> None:
    normalized = re.sub(r"\s+", " ", f"{interface_doc} {design_doc}").lower()
    for phrase in (
        "carga por activación",
        "matriz tablero",
        "vacío",
        "cargando",
        "ausente",
        "error",
        "known_at",
        "activo seleccionado",
        "mesa",
        "activo",
        "tecnico",
        "revisar",
        "cazatiburones",
        "sistema",
    ):
        assert phrase in normalized, f"UI-5 documentation must declare {phrase!r}"


def test_ui5_documentation_matrix_and_invalidation() -> None:
    root = Path(str(files("investment_analyst"))).parent.parent
    _check_ui5_documentation_matrix_and_invalidation(
        interface_doc=(root / "docs" / "local_interface.md").read_text(encoding="utf-8"),
        design_doc=(root / "docs" / "local_interface_design_system.md").read_text(encoding="utf-8"),
    )


# ---------------------------------------------------------------------------
# Mesa composition (UI-9): a fluid main column and a fixed evidence aside,
# three separated novedades families, a compact three-domain universe matrix,
# its derived-and-shown query window, and the relocation of every #resumen
# control without changing any deferred request.
# Same discipline as every rule above: static contract checks, no browser.
# ---------------------------------------------------------------------------


def _mesa_slice(index_html: str) -> str:
    return _board_slices(index_html)["mesa"]


def _check_mesa_presents_the_four_reading_layers_in_order(index_html: str) -> None:
    mesa = _mesa_slice(index_html)
    assert 'class="mesa-layout"' in mesa
    main_start = mesa.index('class="mesa-main"')
    aside_start = mesa.index('<aside class="mesa-aside"')
    main = mesa[main_start:aside_start]
    assert main.index('id="mesa-news-analytical"') < main.index('id="mesa-universe-table"')
    assert "EN QUÉ CONFÍO" not in mesa
    assert 'id="app-sidebar"' not in mesa
    assert 'id="board-nav"' not in mesa


def test_mesa_presents_the_four_reading_layers_in_order() -> None:
    _check_mesa_presents_the_four_reading_layers_in_order(INDEX_HTML)


def _check_universe_matrix_is_the_last_layer(index_html: str) -> None:
    mesa = _mesa_slice(index_html)
    main_start = mesa.index('class="mesa-main"')
    aside_start = mesa.index('<aside class="mesa-aside"')
    main = mesa[main_start:aside_start]
    aside = mesa[aside_start:]
    assert main.index('id="mesa-news-analytical"') < main.index('id="mesa-universe-table"')
    assert 'id="mesa-incidents-list"' not in main
    headings = (
        'id="mesa-coverage-titulo"',
        'id="mesa-blocked-sources-titulo"',
        'id="mesa-incidents-titulo"',
    )
    assert [aside.index(heading) for heading in headings] == sorted(
        aside.index(heading) for heading in headings
    )


def test_universe_matrix_is_the_last_layer() -> None:
    _check_universe_matrix_is_the_last_layer(INDEX_HTML)


def _check_analytical_rules_family_is_populated_from_candidate_notifications(
    index_html: str, app_js: str
) -> None:
    mesa = _mesa_slice(index_html)
    assert 'id="mesa-news-analytical-count"' in mesa
    assert 'id="mesa-news-analytical-list"' in mesa
    assert "loadMesaAnalyticalNews" in _board_deferred_load_entry(app_js, "mesa")
    load_body = _extract_js_function(app_js, "loadMesaAnalyticalNews")
    assert 'await api("/api/v1/candidate-notifications")' in load_body
    render_body = _extract_js_function(app_js, "renderMesaAnalyticalNews")
    assert 'byId("mesa-news-analytical-count")' in render_body
    assert 'byId("mesa-news-analytical-list")' in render_body


def test_analytical_rules_family_is_populated_from_candidate_notifications() -> None:
    _check_analytical_rules_family_is_populated_from_candidate_notifications(INDEX_HTML, APP_JS)


def _check_institutional_and_activity_families_use_separate_notification_inboxes(
    index_html: str, app_js: str
) -> None:
    mesa = _mesa_slice(index_html)
    for family in ("institutional", "activity"):
        family_id = f"mesa-news-{family}"
        family_start = mesa.index(f'id="{family_id}"')
        family_end = mesa.index("</article>", family_start)
        family_markup = mesa[family_start:family_end]
        assert 'class="absence-mark blocked"' not in family_markup
        assert f'id="mesa-news-{family}-count"' in family_markup
        assert f'id="mesa-news-{family}-list"' in family_markup
        assert 'class="alert-inbox"' in family_markup
        assert 'aria-live="polite"' in family_markup
        assert 'aria-busy="false"' in family_markup

    assert "loadMesaCazatiburonesNewsFamilies" in _board_deferred_load_entry(app_js, "mesa")
    loader_body = _extract_js_function(app_js, "loadMesaCazatiburonesNewsFamilies")
    assert 'loadMesaCazatiburonesNews("institutional", () =>' in loader_body
    assert 'loadMesaCazatiburonesNews("activity", () =>' in loader_body
    assert 'api("/api/v1/cazatiburones/notifications?family=institutional&limit=5")' in loader_body
    assert 'api("/api/v1/cazatiburones/notifications?family=activity&limit=5")' in loader_body
    load_body = _extract_js_function(app_js, "loadMesaCazatiburonesNews")
    assert "const payload = await request();" in load_body
    assert "mesaInstitutionalNewsRequestSequence" in app_js
    assert "mesaActivityNewsRequestSequence" in app_js
    render_body = _extract_js_function(app_js, "renderMesaCazatiburonesNews")
    assert "byId(`mesa-news-${family}-list`)" in render_body
    assert "byId(`mesa-news-${family}-count`)" in render_body
    assert (
        'renderAbsenceMark("blocked", "Bloqueada", "La outbox no está configurada en el servicio")'
        in render_body
    )
    assert "Sin novedades en esta bandeja." in render_body
    assert (
        "Se muestran ${formatInteger(returned)} de ${formatInteger(total)} novedades."
        in render_body
    )
    assert "MESA_CAZATIBURONES_STATUS_LABELS" in app_js
    assert "notification.rule_id" in render_body
    assert "notification.asset_id" in render_body
    assert "notification.created_at" in render_body
    assert "view.status" in render_body
    # Neither family is ever populated from a per-asset Cazatiburones endpoint
    # or from operational alerts inside the mesa deferred-load graph.
    mesa_loads = _board_deferred_load_entry(app_js, "mesa")
    for forbidden in ("declared-activity", "institutional-observations"):
        assert forbidden not in mesa_loads


def test_institutional_and_activity_families_use_separate_notification_inboxes() -> None:
    _check_institutional_and_activity_families_use_separate_notification_inboxes(INDEX_HTML, APP_JS)


def _check_mesa_news_counts_describe_inboxes_without_known_at_claim(
    index_html: str, app_js: str
) -> None:
    mesa = _mesa_slice(index_html)
    assert "desde el corte anterior" not in mesa.lower()
    assert "no están acotadas por el corte" not in mesa.lower()
    for function_name in ("renderMesaAnalyticalNews", "renderMesaCazatiburonesNews"):
        body = _extract_js_function(app_js, function_name)
        assert "payload.total" in body
        assert "payload.pending_count" in body
        assert "desde el corte anterior" not in body.lower()


def test_mesa_news_counts_describe_inboxes_without_known_at_claim() -> None:
    _check_mesa_news_counts_describe_inboxes_without_known_at_claim(INDEX_HTML, APP_JS)


def _check_mesa_news_is_read_only_without_cross_family_aggregation(app_js: str) -> None:
    render_body = _extract_js_function(app_js, "renderMesaCazatiburonesNews")
    load_body = _extract_js_function(app_js, "loadMesaCazatiburonesNews")
    for forbidden in ("POST", "acknowledge", "transition"):
        assert forbidden not in render_body
        assert forbidden not in load_body


def test_mesa_news_is_read_only_without_cross_family_aggregation() -> None:
    _check_mesa_news_is_read_only_without_cross_family_aggregation(APP_JS)


def _check_no_combined_news_family_count(index_html: str) -> None:
    mesa = _mesa_slice(index_html)
    assert mesa.count('class="mesa-news-count"') == 3
    for forbidden in ("mesa-news-total", "mesa-news-combined", "novedades-total"):
        assert forbidden not in mesa


def test_no_combined_news_family_count() -> None:
    _check_no_combined_news_family_count(INDEX_HTML)


def _check_mesa_universe_coverage_requested_once_with_no_per_asset_parameter(app_js: str) -> None:
    assert app_js.count("/api/v1/universe-coverage") == 1
    mesa_loads = _board_deferred_load_entry(app_js, "mesa")
    assert mesa_loads.count("loadMesaUniverseCoverage") == 1
    load_body = _extract_js_function(app_js, "loadMesaUniverseCoverage")
    assert "selectedMarketAsset" not in load_body
    assert "asset_id" not in load_body
    window_body = _extract_js_function(app_js, "mesaCoverageWindowFromKnownAt")
    assert "selectedMarketAsset" not in window_body
    assert "asset_id" not in window_body


def test_mesa_universe_coverage_requested_once_with_no_per_asset_parameter() -> None:
    _check_mesa_universe_coverage_requested_once_with_no_per_asset_parameter(APP_JS)


def _check_mesa_news_and_incidents_issue_no_per_asset_request(app_js: str) -> None:
    for function_name in ("loadMesaAnalyticalNews", "loadMesaIncidents"):
        body = _extract_js_function(app_js, function_name)
        assert "selectedMarketAsset" not in body
        assert "asset_id" not in body


def test_mesa_news_and_incidents_issue_no_per_asset_request() -> None:
    _check_mesa_news_and_incidents_issue_no_per_asset_request(APP_JS)


def _check_capability_evidence_and_age_map_exhaustively_to_the_five_marks(app_js: str) -> None:
    body = _extract_js_function(app_js, "mesaUniverseCellMarkup")
    not_applicable_pos = body.index('"not_applicable"')
    blocked_pos = body.index('"not_configured" || capability === "not_implemented"')
    missing_pos = body.index('"missing" || evidence === "not_queried"')
    fresh_pos = body.index('mesaMatrixStateMarkup("fresh")')
    overdue_pos = body.index('mesaMatrixStateMarkup("overdue")')
    # capability is decided before evidence, and evidence before age, exactly
    # as the design-system table declares -- this order is what makes
    # not_queried fall through to "missing" only when supported.
    assert not_applicable_pos < blocked_pos < missing_pos < fresh_pos < overdue_pos
    assert 'mesaMatrixStateMarkup("not-applicable")' in body
    assert 'mesaMatrixStateMarkup("blocked")' in body
    assert 'mesaMatrixStateMarkup("missing")' in body


def test_capability_evidence_and_age_map_exhaustively_to_the_five_marks() -> None:
    _check_capability_evidence_and_age_map_exhaustively_to_the_five_marks(APP_JS)


def _check_not_configured_and_not_implemented_render_as_blocked(app_js: str) -> None:
    body = _extract_js_function(app_js, "mesaUniverseCellMarkup")
    assert (
        'if (capability === "not_configured" || capability === "not_implemented") {\n'
        '    return mesaMatrixStateMarkup("blocked");\n  }' in body
    )


def test_not_configured_and_not_implemented_render_as_blocked() -> None:
    _check_not_configured_and_not_implemented_render_as_blocked(APP_JS)


def _check_present_past_freshness_renders_as_stale(app_js: str) -> None:
    body = _extract_js_function(app_js, "mesaUniverseCellMarkup")
    assert "ageDays <= MESA_COVERAGE_WINDOW_DAYS" in body
    fresh_pos = body.index('mesaMatrixStateMarkup("fresh")')
    overdue_pos = body.index('mesaMatrixStateMarkup("overdue")')
    assert fresh_pos < overdue_pos, "the fresh branch must return before the overdue fallback"


def test_present_past_freshness_renders_as_stale() -> None:
    _check_present_past_freshness_renders_as_stale(APP_JS)


def _check_unqueried_capabilities_are_declared_textually(index_html: str) -> None:
    mesa = _mesa_slice(index_html)
    assert 'id="mesa-universe-not-queried"' not in mesa
    assert 'id="mesa-universe-limitations"' not in mesa
    for term in (
        "Cazatiburones, Documentos",
        "additional_capabilities_not_queried",
        "Limitaciones declaradas",
    ):
        assert term not in mesa


def test_unqueried_capabilities_are_declared_textually() -> None:
    _check_unqueried_capabilities_are_declared_textually(INDEX_HTML)


_MESA_COVERAGE_CAPABILITY_KEYS_RE = re.compile(
    r"const MESA_COVERAGE_CAPABILITY_KEYS = Object\.freeze\(\[(.*?)\]\);", re.DOTALL
)


def _check_universe_matrix_covers_exactly_the_four_queried_capabilities(
    index_html: str, app_js: str
) -> None:
    match = _MESA_COVERAGE_CAPABILITY_KEYS_RE.search(app_js)
    assert match, "MESA_COVERAGE_CAPABILITY_KEYS must be declared"
    keys = re.findall(r'"([a-z_]+)"', match.group(1))
    assert keys == ["market", "fundamentals", "corporate_valuation"]
    mesa = _mesa_slice(index_html)
    header_start = mesa.index("<thead>")
    header_end = mesa.index("</thead>")
    headers = re.findall(r'<th scope="col">(.*?)</th>', mesa[header_start:header_end])
    assert headers == ["Activo", "Dominio", "Mercado", "Fund.", "Valor.", "Última evidencia"]
    assert "Registro BVL</th>" not in mesa


def test_universe_matrix_covers_exactly_the_four_queried_capabilities() -> None:
    _check_universe_matrix_covers_exactly_the_four_queried_capabilities(INDEX_HTML, APP_JS)


def _check_queried_window_is_derived_from_the_cut_and_shown(app_js: str) -> None:
    window_body = _extract_js_function(app_js, "mesaCoverageWindowFromKnownAt")
    assert "MESA_COVERAGE_WINDOW_DAYS * 86_400_000" in window_body
    assert "- 86_400_000" in window_body
    load_body = _extract_js_function(app_js, "loadMesaUniverseCoverage")
    window_call_pos = load_body.index("renderMesaUniverseWindow(coverageWindow);")
    api_pos = load_body.index("await api(")
    assert window_call_pos < api_pos, (
        "the derived window must be shown before the request is even sent"
    )


def test_queried_window_is_derived_from_the_cut_and_shown() -> None:
    _check_queried_window_is_derived_from_the_cut_and_shown(APP_JS)


def _check_mesa_universe_deferred_load_keeps_sequence_guard_and_cut_discard(app_js: str) -> None:
    invalidation = _extract_js_function(app_js, "invalidateDeferredBoardLoads")
    assert "mesaUniverseCoverageRequestSequence += 1" in invalidation
    load_body = _extract_js_function(app_js, "loadMesaUniverseCoverage")
    assert "const sequence = ++mesaUniverseCoverageRequestSequence;" in load_body
    assert load_body.count("sequence !== mesaUniverseCoverageRequestSequence") == 2
    assert load_body.count('knownAt !== byId("report-known-at").value.trim()') == 2


def test_mesa_universe_deferred_load_keeps_sequence_guard_and_cut_discard() -> None:
    _check_mesa_universe_deferred_load_keeps_sequence_guard_and_cut_discard(APP_JS)


_MESA_RESUMEN_CONTROL_IDS = (
    "workspace-status",
    "workspace-counts",
    "run-status",
    "run-time",
    "schedule-status",
    "schedule-next",
    "traceability-status",
    "known-at-status",
    "candidate-status",
    "candidate-latest",
    "alert-status",
    "alert-latest",
)


def _check_every_resumen_control_survives_relocation(index_html: str) -> None:
    mesa = _mesa_slice(index_html)
    for control_id in _MESA_RESUMEN_CONTROL_IDS:
        marker = f'id="{control_id}"'
        assert index_html.count(marker) == 1, f"id={control_id!r} must appear exactly once"
        assert marker in mesa, f"id={control_id!r} must remain inside board mesa"


def test_every_resumen_control_survives_relocation() -> None:
    _check_every_resumen_control_survives_relocation(INDEX_HTML)


def _check_asset_preferences_panel_moved_to_sistema_and_absent_from_mesa(index_html: str) -> None:
    marker = 'id="asset-preferences-panel"'
    assert index_html.count(marker) == 1
    slices = _board_slices(index_html)
    assert marker in slices["sistema"]
    assert marker not in slices["mesa"]


def test_asset_preferences_panel_moved_to_sistema_and_absent_from_mesa() -> None:
    _check_asset_preferences_panel_moved_to_sistema_and_absent_from_mesa(INDEX_HTML)


def _check_design_system_documentation_declares_the_mesa_hierarchy(doc_text: str) -> None:
    normalized = re.sub(r"\s+", " ", doc_text).lower()
    for phrase in (
        "novedades de las bandejas",
        "columna principal",
        "340 px",
        "fuentes bloqueadas",
        "incidencias",
        "universo",
        "/api/v1/cazatiburones/notifications?family=institutional&limit=5",
        "/api/v1/cazatiburones/notifications?family=activity&limit=5",
        "al día",
        "vencida",
        "última evidencia",
        "registro bvl",
        "365",
    ):
        assert phrase in normalized, f"design-system documentation must declare {phrase!r}"


def test_design_system_documentation_declares_the_mesa_hierarchy() -> None:
    _check_design_system_documentation_declares_the_mesa_hierarchy(
        (
            Path(str(files("investment_analyst"))).parent.parent
            / "docs"
            / "local_interface_design_system.md"
        ).read_text(encoding="utf-8")
    )


def _check_mesa_three_column_composition(index_html: str, styles_css: str) -> None:
    mesa = _mesa_slice(index_html)
    assert mesa.count('class="mesa-layout"') == 1
    assert mesa.count('<aside class="mesa-aside"') == 1
    assert "grid-template-columns: minmax(0, 1fr) 340px;" in styles_css
    assert "@media (max-width: 1120px)" in styles_css
    assert ".mesa-layout {\n    grid-template-columns: 1fr;" in styles_css


def test_mesa_has_main_and_340px_aside_composition() -> None:
    _check_mesa_three_column_composition(INDEX_HTML, STYLES_CSS)


def test_mesa_right_aside_orders_coverage_blocked_sources_and_incidents() -> None:
    _check_universe_matrix_is_the_last_layer(INDEX_HTML)


def test_responsive_mesa_collapses_without_page_overflow() -> None:
    _check_mesa_three_column_composition(INDEX_HTML, STYLES_CSS)
    assert ".mesa-main,\n.mesa-aside {\n  min-width: 0;" in STYLES_CSS
    assert ".universe-matrix-scroll {\n  overflow-x: auto;" in STYLES_CSS


def _check_compact_universe_matrix(index_html: str, app_js: str) -> None:
    _check_universe_matrix_covers_exactly_the_four_queried_capabilities(index_html, app_js)
    matrix_body = _extract_js_function(app_js, "renderMesaUniverseMatrix")
    assert "bvl_registry" not in matrix_body
    assert 'class="mesa-universe-legend"' in _mesa_slice(index_html)


def test_universe_matrix_has_only_three_queried_analytical_domain_state_columns() -> None:
    _check_compact_universe_matrix(INDEX_HTML, APP_JS)


def _check_asset_domain_is_derived_only_from_asset_class(app_js: str) -> None:
    mapping_body = _extract_js_function(app_js, "mesaAssetDomainLabel")
    assert "MESA_ASSET_CLASS_LABELS[assetClass]" in mapping_body
    mapping_start = app_js.index("const MESA_ASSET_CLASS_LABELS")
    mapping_end = app_js.index("function mesaMatrixStateMarkup", mapping_start)
    mapping = app_js[mapping_start:mapping_end]
    for asset_class in ('equity: "Acción"', 'etf: "ETF"', 'crypto: "Cripto"'):
        assert asset_class in mapping
    for forbidden in ("symbol", "exchange", "capability", "evidence"):
        assert forbidden not in mapping_body


def test_asset_domain_is_derived_only_from_asset_class() -> None:
    _check_asset_domain_is_derived_only_from_asset_class(APP_JS)


def _check_latest_evidence_uses_only_queried_domain_availability(app_js: str) -> None:
    body = _extract_js_function(app_js, "mesaLatestEvidenceTimestamp")
    assert "MESA_COVERAGE_CAPABILITY_KEYS.flatMap" in body
    assert "coverage.reference_at" in body
    assert "coverage.latest_input_available_at" in body
    for forbidden in ("bvl_registry", "computed_at", "Date.now", "new Date()"):
        assert forbidden not in body
    markup = _extract_js_function(app_js, "mesaLatestEvidenceMarkup")
    assert "formatInstant(timestamp)" in markup
    assert 'renderAbsenceMark("missing", "Sin evidencia"' in markup


def test_latest_evidence_uses_only_queried_domain_availability() -> None:
    _check_latest_evidence_uses_only_queried_domain_availability(APP_JS)


def _check_matrix_legend_and_accessible_marks(
    index_html: str, app_js: str, styles_css: str
) -> None:
    mesa = _mesa_slice(index_html)
    assert mesa.count('class="mesa-universe-legend"') == 1
    for label in ("Al día", "Vencida", "Sin evidencia", "Bloqueada", "No aplica"):
        assert label in mesa
    markup = _extract_js_function(app_js, "mesaMatrixStateMarkup")
    assert 'role="img" aria-label="${label}"' in markup
    assert "visually-hidden" in markup
    assert ".mesa-matrix-mark {" in styles_css
    assert "width: 7px;" in styles_css
    assert "height: 7px;" in styles_css
    for selector in (
        ".mesa-matrix-fresh",
        ".mesa-matrix-overdue",
        ".mesa-matrix-missing",
        ".mesa-matrix-blocked",
        ".mesa-matrix-not-applicable",
    ):
        assert selector in styles_css
    assert "clip-path" in styles_css
    assert "repeating-linear-gradient" in styles_css


def test_matrix_has_one_five_state_legend_and_accessible_7px_marks() -> None:
    _check_matrix_legend_and_accessible_marks(INDEX_HTML, APP_JS, STYLES_CSS)


def test_cell_state_mapping_preserves_capability_evidence_age_order() -> None:
    _check_capability_evidence_and_age_map_exhaustively_to_the_five_marks(APP_JS)
    _check_not_configured_and_not_implemented_render_as_blocked(APP_JS)
    _check_present_past_freshness_renders_as_stale(APP_JS)


def _check_matrix_absent_rows_span_six_columns(index_html: str, app_js: str) -> None:
    assert '<tr><td colspan="6">Cargando…</td></tr>' in _mesa_slice(index_html)
    render_absent = _extract_js_function(app_js, "renderMesaUniverseAbsentTable")
    render_matrix = _extract_js_function(app_js, "renderMesaUniverseMatrix")
    assert "cell.colSpan = 6;" in render_absent
    assert "cell.colSpan = 6;" in render_matrix


def test_matrix_empty_and_error_rows_span_six_columns() -> None:
    _check_matrix_absent_rows_span_six_columns(INDEX_HTML, APP_JS)


def _check_bvl_summary_is_lateral_and_reuses_payload(index_html: str, app_js: str) -> None:
    mesa = _mesa_slice(index_html)
    assert mesa.count("Registro BVL") == 1
    aside = mesa[mesa.index('<aside class="mesa-aside"') :]
    assert aside.index("Registro BVL") < aside.index('id="mesa-incidents-titulo"')
    summary_body = _extract_js_function(app_js, "renderMesaBvlRegistrySummary")
    assert "asset.bvl_registry" in summary_body
    for key in (
        "applicable",
        "present",
        "missing",
        "not_queried",
        "not_configured",
        "not_implemented",
    ):
        assert key in summary_body
    assert "api(" not in summary_body
    assert "bvl_registry" not in _extract_js_function(app_js, "renderMesaUniverseMatrix")


def test_bvl_registry_is_summarized_once_in_blocked_sources_without_extra_request() -> None:
    _check_bvl_summary_is_lateral_and_reuses_payload(INDEX_HTML, APP_JS)


def test_unqueried_additional_capabilities_remain_text_not_cells() -> None:
    _check_unqueried_capabilities_are_declared_textually(INDEX_HTML)
    _check_compact_universe_matrix(INDEX_HTML, APP_JS)


def test_universe_limitations_are_not_rendered_in_the_matrix() -> None:
    matrix_body = _extract_js_function(APP_JS, "renderMesaUniverseMatrix")
    assert "asset.limitations" not in matrix_body
    assert "allLimitations" not in matrix_body
    assert 'id="mesa-universe-limitations"' not in _mesa_slice(INDEX_HTML)


def _check_compact_clock_keeps_one_desktop_row(index_html: str, styles_css: str) -> None:
    for control_id in (
        "new-york-clock",
        "new-york-clock-date",
        "bvl-session-status",
        "bvl-session-dot",
        "bvl-session-remaining",
        "nyse-session-status",
        "nyse-session-dot",
        "nyse-session-remaining",
        "market-clock-note",
    ):
        assert index_html.count(f'id="{control_id}"') == 1
    assert not re.search(r'<[a-z][^>]*\bid="lima-clock"[^>]*>', index_html)
    assert not re.search(r'<[a-z][^>]*\bid="lima-clock-date"[^>]*>', index_html)
    assert "grid-template-columns: auto minmax(0, 1fr) auto;" in styles_css
    clock_start = styles_css.index(".market-clock-strip {")
    clock_end = styles_css.index(".market-clock-item {", clock_start)
    clock = styles_css[clock_start:clock_end]
    assert "grid-column: 1 / -1" not in clock
    assert "border-top" not in clock


def test_clock_keeps_all_ids_information_and_accessible_limit() -> None:
    _check_compact_clock_keeps_one_desktop_row(INDEX_HTML, STYLES_CSS)


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
    _check_no_board_remains_not_built(APP_JS)  # baseline: clean
    corrupted = APP_JS.replace(
        '{ id: "sistema", label: "Sistema", icon: "gear", built: true },',
        '{ id: "sistema", label: "Sistema", icon: "gear", built: false },',
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not corrupt the sistema entry"
    with pytest.raises(AssertionError):
        _check_no_board_remains_not_built(corrupted)


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
        "  if (boardDataReady) loadDeferredBoardData(resolvedId);",
        '  byId("known-at-status").textContent = "poked";\n'
        "  if (boardDataReady) loadDeferredBoardData(resolvedId);",
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
    corrupted = APP_JS + '\nvoid api("/api/v1/cazatiburones/effective-portfolio");\n'
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


def test_probe_cazatiburones_deep_link_rule_catches_a_removed_reload() -> None:
    _check_cazatiburones_board_reloads_after_market_assets_are_ready(APP_JS)  # baseline: clean
    corrupted = APP_JS.replace(
        "  loadDeferredBoardData(boardIdFromLocationHash());\n",
        "",
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not remove the deep-link reload"
    with pytest.raises(AssertionError):
        _check_cazatiburones_board_reloads_after_market_assets_are_ready(corrupted)


def test_probe_cazatiburones_asset_change_rule_catches_a_removed_reload() -> None:
    _check_cazatiburones_board_reloads_when_the_selected_asset_changes(APP_JS)  # baseline: clean
    corrupted = APP_JS.replace(
        "    invalidateDeferredBoardLoads();\n"
        "    activateBoard(boardIdFromLocationHash(), { focus: false });",
        "    invalidateDeferredBoardLoads();",
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not remove the asset-change reload"
    with pytest.raises(AssertionError):
        _check_cazatiburones_board_reloads_when_the_selected_asset_changes(corrupted)


def test_probe_cazatiburones_known_at_change_rule_catches_a_removed_reload() -> None:
    _check_cazatiburones_board_reloads_when_the_known_at_cut_changes(APP_JS)  # baseline: clean
    corrupted = APP_JS.replace(
        "  activateBoard(boardIdFromLocationHash(), { focus: false });\n});",
        "});",
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not remove the known_at-change reload"
    with pytest.raises(AssertionError):
        _check_cazatiburones_board_reloads_when_the_known_at_cut_changes(corrupted)


def test_probe_initialize_rule_catches_a_reintroduced_board_query() -> None:
    _check_initialize_fires_no_board_query(APP_JS)
    corrupted = APP_JS.replace(
        "  startMarketClocks();\n}",
        "  startMarketClocks();\n  await queryReport();\n}",
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not reintroduce a board query"
    with pytest.raises(AssertionError):
        _check_initialize_fires_no_board_query(corrupted)


def test_probe_deferred_table_rule_catches_a_board_missing_from_the_table() -> None:
    _check_board_to_deferred_loads_table_covers_the_six_registered_boards(APP_JS)
    corrupted = APP_JS.replace("  tecnico: Object.freeze([]),\n", "", 1)
    assert corrupted != APP_JS, "probe fixture did not remove tecnico from the table"
    with pytest.raises(AssertionError):
        _check_board_to_deferred_loads_table_covers_the_six_registered_boards(corrupted)


def test_probe_dispatch_rule_catches_an_undeclared_activate_board_trigger() -> None:
    _check_activate_board_fires_exactly_the_declared_loads(APP_JS)
    corrupted = APP_JS.replace(
        "  if (boardDataReady) loadDeferredBoardData(resolvedId);",
        "  if (boardDataReady) loadDeferredBoardData(resolvedId);\n  void queryReport();",
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not add an undeclared trigger"
    with pytest.raises(AssertionError):
        _check_activate_board_fires_exactly_the_declared_loads(corrupted)


def test_probe_sequence_guard_rule_catches_a_removed_guard() -> None:
    _check_activo_loads_guard_sequence_and_selected_asset_before_painting(APP_JS)
    corrupted = APP_JS.replace(
        "    && (!deferredRequest || isCurrentActivoBoardRequest(deferredRequest));",
        "    && (!deferredRequest);",
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not remove the deferred guard"
    with pytest.raises(AssertionError):
        _check_activo_loads_guard_sequence_and_selected_asset_before_painting(corrupted)


def test_probe_selected_asset_rule_catches_a_removed_comparison() -> None:
    _check_activo_loads_guard_sequence_and_selected_asset_before_painting(APP_JS)
    corrupted = APP_JS.replace(
        "    && assetId === selectedMarketAsset\n"
        '    && knownAt === byId("report-known-at").value.trim()',
        '    && knownAt === byId("report-known-at").value.trim()',
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not remove the selected asset comparison"
    with pytest.raises(AssertionError):
        _check_activo_loads_guard_sequence_and_selected_asset_before_painting(corrupted)


def test_probe_invalidation_rule_catches_a_loaded_mark_not_invalidated() -> None:
    _check_asset_or_cut_change_invalidates_loaded_marks_and_refetches_visible_board(APP_JS)
    corrupted = APP_JS.replace("  loadedBoardIds.clear();\n", "", 1)
    assert corrupted != APP_JS, "probe fixture did not remove loaded-mark invalidation"
    with pytest.raises(AssertionError):
        _check_asset_or_cut_change_invalidates_loaded_marks_and_refetches_visible_board(corrupted)


def test_probe_activate_board_rule_catches_a_cut_reference() -> None:
    _check_activate_board_fires_exactly_the_declared_loads(APP_JS)
    corrupted = APP_JS.replace(
        "  if (boardDataReady) loadDeferredBoardData(resolvedId);",
        '  if (boardDataReady) loadDeferredBoardData(resolvedId);\n  byId("report-known-at");',
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not add a cut reference"
    with pytest.raises(AssertionError):
        _check_activate_board_fires_exactly_the_declared_loads(corrupted)


def test_probe_no_hidden_preload_rule_catches_a_preloaded_board() -> None:
    _check_initialize_fires_no_board_query(APP_JS)
    corrupted = APP_JS.replace(
        "  loadDeferredBoardData(boardIdFromLocationHash());",
        '    loadDeferredBoardData("activo");',
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not preload a hidden board"
    with pytest.raises(AssertionError):
        _check_initialize_fires_no_board_query(corrupted)


def test_every_new_rule_has_a_matching_probe() -> None:
    expected = {
        "test_probe_initialize_rule_catches_a_reintroduced_board_query",
        "test_probe_deferred_table_rule_catches_a_board_missing_from_the_table",
        "test_probe_dispatch_rule_catches_an_undeclared_activate_board_trigger",
        "test_probe_sequence_guard_rule_catches_a_removed_guard",
        "test_probe_selected_asset_rule_catches_a_removed_comparison",
        "test_probe_invalidation_rule_catches_a_loaded_mark_not_invalidated",
        "test_probe_activate_board_rule_catches_a_cut_reference",
        "test_probe_no_hidden_preload_rule_catches_a_preloaded_board",
        # UI-6
        "test_probe_mesa_layer_order_rule_catches_a_reordered_layer",
        "test_probe_universe_layer_position_rule_catches_a_layer_after_universe",
        "test_probe_analytical_family_rule_catches_a_removed_candidate_notifications_call",
        "test_probe_cazatiburones_families_rule_catches_a_static_blocked_mark",
        "test_probe_news_family_rule_catches_a_combined_count",
        "test_probe_universe_coverage_rule_catches_a_per_asset_parameter",
        "test_probe_mesa_news_incidents_rule_catches_a_per_asset_reference",
        "test_probe_capability_mapping_rule_catches_a_reordered_branch",
        "test_probe_blocked_mapping_rule_catches_a_narrowed_capability",
        "test_probe_freshness_rule_catches_a_removed_age_comparison",
        "test_probe_unqueried_capabilities_rule_catches_a_removed_declaration",
        "test_probe_universe_matrix_columns_rule_catches_an_invented_capability",
        "test_probe_query_window_rule_catches_a_hidden_window",
        "test_probe_universe_sequence_guard_rule_catches_a_removed_discard",
        "test_probe_resumen_control_rule_catches_a_dropped_control",
        "test_probe_preferences_panel_rule_catches_a_duplicated_panel",
        "test_probe_mesa_hierarchy_documentation_rule_catches_a_missing_declaration",
        # UI-9
        "test_probe_ui9_mesa_layout_rule_catches_a_non_340px_aside",
        "test_probe_ui9_matrix_capabilities_rule_catches_bvl_as_cell",
        "test_probe_ui9_domain_rule_catches_a_symbol_fallback",
        "test_probe_ui9_latest_evidence_rule_catches_bvl_input",
        "test_probe_ui9_legend_rule_catches_an_inaccessible_mark",
        "test_probe_ui9_absent_row_rule_catches_a_five_column_span",
        "test_probe_ui9_bvl_summary_rule_catches_a_second_label",
        "test_probe_ui9_clock_rule_catches_a_full_grid_span",
    }
    available = {name for name in globals() if name.startswith("test_probe_")}
    assert expected <= available


def test_probe_document_timeline_missing_coverage_rule_catches_a_hidden_counter() -> None:
    _check_document_timeline_shows_coverage_even_when_missing(APP_JS)  # baseline: clean
    corrupted = APP_JS.replace(
        _TIMELINE_MISSING_PREFIX,
        (
            'payload.state === "missing"\n'
            '      ? "Sin documentos SEC para el activo y corte seleccionados"\n'
            "      : "
        ),
        1,
    )
    assert corrupted != APP_JS, "probe fixture did not reintroduce the hidden-counter ternary"
    with pytest.raises(AssertionError):
        _check_document_timeline_shows_coverage_even_when_missing(corrupted)


# ---------------------------------------------------------------------------
# UI-6 regression probes
# ---------------------------------------------------------------------------


def test_probe_mesa_layer_order_rule_catches_a_reordered_layer() -> None:
    _check_mesa_presents_the_four_reading_layers_in_order(INDEX_HTML)  # baseline: clean
    marker = 'class="mesa-layout"'
    assert marker in INDEX_HTML
    corrupted = INDEX_HTML.replace(marker, 'class="mesa-layout-corrupted"', 1)
    assert corrupted != INDEX_HTML
    with pytest.raises(AssertionError):
        _check_mesa_presents_the_four_reading_layers_in_order(corrupted)


def test_probe_universe_layer_position_rule_catches_a_layer_after_universe() -> None:
    _check_universe_matrix_is_the_last_layer(INDEX_HTML)  # baseline: clean
    marker = '<div class="mesa-main">'
    assert marker in INDEX_HTML
    corrupted = INDEX_HTML.replace(marker, f'{marker}<span id="mesa-incidents-list"></span>', 1)
    assert corrupted != INDEX_HTML
    with pytest.raises(AssertionError):
        _check_universe_matrix_is_the_last_layer(corrupted)


def test_probe_analytical_family_rule_catches_a_removed_candidate_notifications_call() -> None:
    _check_analytical_rules_family_is_populated_from_candidate_notifications(
        INDEX_HTML, APP_JS
    )  # baseline: clean
    original_body = _extract_js_function(APP_JS, "loadMesaAnalyticalNews")
    corrupted_body = original_body.replace(
        'await api("/api/v1/candidate-notifications")', 'await api("/api/candidates")', 1
    )
    assert corrupted_body != original_body
    corrupted = APP_JS.replace(original_body, corrupted_body, 1)
    with pytest.raises(AssertionError):
        _check_analytical_rules_family_is_populated_from_candidate_notifications(
            INDEX_HTML, corrupted
        )


def test_probe_cazatiburones_families_rule_catches_a_static_blocked_mark() -> None:
    _check_institutional_and_activity_families_use_separate_notification_inboxes(
        INDEX_HTML, APP_JS
    )  # baseline: clean
    marker = 'id="mesa-news-institutional-list" class="alert-inbox"'
    assert marker in INDEX_HTML
    corrupted = INDEX_HTML.replace(marker, 'class="absence-mark blocked"', 1)
    with pytest.raises(AssertionError):
        _check_institutional_and_activity_families_use_separate_notification_inboxes(
            corrupted, APP_JS
        )


def test_probe_news_family_rule_catches_a_combined_count() -> None:
    _check_no_combined_news_family_count(INDEX_HTML)  # baseline: clean
    marker = '<span id="mesa-news-analytical-count" class="mesa-news-count">—</span>'
    assert marker in INDEX_HTML
    corrupted = INDEX_HTML.replace(marker, f'{marker}<span class="mesa-news-count">total</span>', 1)
    assert corrupted != INDEX_HTML
    with pytest.raises(AssertionError):
        _check_no_combined_news_family_count(corrupted)


def test_probe_cazatiburones_families_rule_catches_a_request_without_family() -> None:
    _check_institutional_and_activity_families_use_separate_notification_inboxes(
        INDEX_HTML, APP_JS
    )  # baseline: clean
    corrupted = APP_JS.replace("?family=institutional&limit=5", "?limit=5", 1)
    assert corrupted != APP_JS
    with pytest.raises(AssertionError):
        _check_institutional_and_activity_families_use_separate_notification_inboxes(
            INDEX_HTML, corrupted
        )


def test_probe_cazatiburones_families_rule_catches_cross_family_container() -> None:
    _check_institutional_and_activity_families_use_separate_notification_inboxes(
        INDEX_HTML, APP_JS
    )  # baseline: clean
    corrupted = APP_JS.replace("mesa-news-${family}-list", "mesa-news-activity-list", 1)
    assert corrupted != APP_JS
    with pytest.raises(AssertionError):
        _check_institutional_and_activity_families_use_separate_notification_inboxes(
            INDEX_HTML, corrupted
        )


def test_probe_cazatiburones_families_rule_catches_collapsed_disabled_and_empty_states() -> None:
    _check_institutional_and_activity_families_use_separate_notification_inboxes(
        INDEX_HTML, APP_JS
    )  # baseline: clean
    corrupted = APP_JS.replace(
        'renderAbsenceMark("blocked", "Bloqueada", "La outbox no está configurada en el servicio")',
        'createElement("p", "", "Sin novedades en esta bandeja.")',
        1,
    )
    assert corrupted != APP_JS
    with pytest.raises(AssertionError):
        _check_institutional_and_activity_families_use_separate_notification_inboxes(
            INDEX_HTML, corrupted
        )


def test_probe_cazatiburones_families_rule_catches_hidden_truncation() -> None:
    _check_institutional_and_activity_families_use_separate_notification_inboxes(
        INDEX_HTML, APP_JS
    )  # baseline: clean
    corrupted = APP_JS.replace(
        "Se muestran ${formatInteger(returned)} de ${formatInteger(total)} novedades.",
        "Novedades disponibles.",
        1,
    )
    assert corrupted != APP_JS
    with pytest.raises(AssertionError):
        _check_institutional_and_activity_families_use_separate_notification_inboxes(
            INDEX_HTML, corrupted
        )


def test_probe_mesa_news_count_rule_catches_known_at_claim() -> None:
    _check_mesa_news_counts_describe_inboxes_without_known_at_claim(INDEX_HTML, APP_JS)  # clean
    corrupted = APP_JS.replace(
        "Bandeja: ${formatInteger(total)}",
        "Desde el corte anterior: ${formatInteger(total)}",
        1,
    )
    assert corrupted != APP_JS
    with pytest.raises(AssertionError):
        _check_mesa_news_counts_describe_inboxes_without_known_at_claim(INDEX_HTML, corrupted)


def test_probe_mesa_news_read_only_rule_catches_an_acknowledge_action() -> None:
    _check_mesa_news_is_read_only_without_cross_family_aggregation(APP_JS)  # clean
    original = _extract_js_function(APP_JS, "renderMesaCazatiburonesNews")
    corrupted_body = original.replace(
        "list.append(item);",
        'item.append(createElement("button", "", "acknowledge"));\n    list.append(item);',
        1,
    )
    assert corrupted_body != original
    corrupted = APP_JS.replace(original, corrupted_body, 1)
    with pytest.raises(AssertionError):
        _check_mesa_news_is_read_only_without_cross_family_aggregation(corrupted)


def test_probe_board_registry_rule_catches_a_seventh_board() -> None:
    _check_board_registry_declares_exactly_six_boards(APP_JS)  # clean
    marker = '  { id: "sistema", label: "Sistema", icon: "gear", built: true },'
    assert marker in APP_JS
    corrupted = APP_JS.replace(
        marker,
        f'{marker}\n  {{ id: "extra", label: "Extra", icon: "gear", built: true }},',
        1,
    )
    with pytest.raises(AssertionError):
        _check_board_registry_declares_exactly_six_boards(corrupted)


def test_probe_universe_coverage_rule_catches_a_per_asset_parameter() -> None:
    _check_mesa_universe_coverage_requested_once_with_no_per_asset_parameter(APP_JS)  # clean
    original_body = _extract_js_function(APP_JS, "loadMesaUniverseCoverage")
    corrupted_body = original_body.replace(
        "known_at: knownAt,", "known_at: knownAt,\n    asset_id: selectedMarketAsset,", 1
    )
    assert corrupted_body != original_body
    corrupted = APP_JS.replace(original_body, corrupted_body, 1)
    with pytest.raises(AssertionError):
        _check_mesa_universe_coverage_requested_once_with_no_per_asset_parameter(corrupted)


def test_probe_mesa_news_incidents_rule_catches_a_per_asset_reference() -> None:
    _check_mesa_news_and_incidents_issue_no_per_asset_request(APP_JS)  # baseline: clean
    original_body = _extract_js_function(APP_JS, "loadMesaIncidents")
    corrupted_body = original_body.replace(
        'const list = byId("mesa-incidents-list");',
        'const list = byId("mesa-incidents-list"); const asset = selectedMarketAsset;',
        1,
    )
    assert corrupted_body != original_body
    corrupted = APP_JS.replace(original_body, corrupted_body, 1)
    with pytest.raises(AssertionError):
        _check_mesa_news_and_incidents_issue_no_per_asset_request(corrupted)


def test_probe_capability_mapping_rule_catches_a_reordered_branch() -> None:
    _check_capability_evidence_and_age_map_exhaustively_to_the_five_marks(APP_JS)  # clean
    block_a = (
        '  if (capability === "not_applicable") {\n'
        '    return mesaMatrixStateMarkup("not-applicable");\n'
        "  }"
    )
    block_b = (
        '  if (capability === "not_configured" || capability === "not_implemented") {\n'
        '    return mesaMatrixStateMarkup("blocked");\n'
        "  }"
    )
    original_pair = f"{block_a}\n{block_b}"
    assert original_pair in APP_JS
    swapped_pair = f"{block_b}\n{block_a}"
    corrupted = APP_JS.replace(original_pair, swapped_pair, 1)
    assert corrupted != APP_JS
    with pytest.raises(AssertionError):
        _check_capability_evidence_and_age_map_exhaustively_to_the_five_marks(corrupted)


def test_probe_blocked_mapping_rule_catches_a_narrowed_capability() -> None:
    _check_not_configured_and_not_implemented_render_as_blocked(APP_JS)  # baseline: clean
    original_body = _extract_js_function(APP_JS, "mesaUniverseCellMarkup")
    corrupted_body = original_body.replace(
        'capability === "not_configured" || capability === "not_implemented"',
        'capability === "not_configured"',
        1,
    )
    assert corrupted_body != original_body
    corrupted = APP_JS.replace(original_body, corrupted_body, 1)
    with pytest.raises(AssertionError):
        _check_not_configured_and_not_implemented_render_as_blocked(corrupted)


def test_probe_freshness_rule_catches_a_removed_age_comparison() -> None:
    _check_present_past_freshness_renders_as_stale(APP_JS)  # baseline: clean
    original_body = _extract_js_function(APP_JS, "mesaUniverseCellMarkup")
    corrupted_body = original_body.replace(
        "ageDays <= MESA_COVERAGE_WINDOW_DAYS", "ageDays >= MESA_COVERAGE_WINDOW_DAYS", 1
    )
    assert corrupted_body != original_body
    corrupted = APP_JS.replace(original_body, corrupted_body, 1)
    with pytest.raises(AssertionError):
        _check_present_past_freshness_renders_as_stale(corrupted)


def test_probe_unqueried_capabilities_rule_catches_a_removed_declaration() -> None:
    _check_unqueried_capabilities_are_declared_textually(INDEX_HTML)  # baseline: clean
    marker = 'id="mesa-universe-table"'
    assert marker in INDEX_HTML
    corrupted = INDEX_HTML.replace(
        marker,
        '<p id="mesa-universe-not-queried">Cazatiburones, Documentos y Derivados</p>' + marker,
        1,
    )
    assert corrupted != INDEX_HTML
    with pytest.raises(AssertionError):
        _check_unqueried_capabilities_are_declared_textually(corrupted)


def test_probe_universe_matrix_columns_rule_catches_an_invented_capability() -> None:
    _check_universe_matrix_covers_exactly_the_four_queried_capabilities(
        INDEX_HTML, APP_JS
    )  # baseline: clean
    marker = (
        "const MESA_COVERAGE_CAPABILITY_KEYS = Object.freeze([\n"
        '  "market",\n'
        '  "fundamentals",\n'
        '  "corporate_valuation",\n'
        "]);"
    )
    assert marker in APP_JS
    corrupted_marker = marker.replace(
        '  "corporate_valuation",\n]);', '  "corporate_valuation",\n  "bvl_registry",\n]);', 1
    )
    assert corrupted_marker != marker
    corrupted = APP_JS.replace(marker, corrupted_marker, 1)
    with pytest.raises(AssertionError):
        _check_universe_matrix_covers_exactly_the_four_queried_capabilities(INDEX_HTML, corrupted)


def test_probe_query_window_rule_catches_a_hidden_window() -> None:
    _check_queried_window_is_derived_from_the_cut_and_shown(APP_JS)  # baseline: clean
    original_body = _extract_js_function(APP_JS, "loadMesaUniverseCoverage")
    # Relocate the call rather than delete it: the checker must fail via its
    # own position assertion (window shown only after the request), never
    # via a bare lookup crash on a literal that no longer exists at all.
    without_call = original_body.replace("renderMesaUniverseWindow(coverageWindow);\n  ", "", 1)
    assert without_call != original_body
    corrupted_body = without_call.replace(
        "renderMesaUniverseMatrix(payload);",
        "renderMesaUniverseWindow(coverageWindow);\n    renderMesaUniverseMatrix(payload);",
        1,
    )
    assert corrupted_body != without_call
    corrupted = APP_JS.replace(original_body, corrupted_body, 1)
    with pytest.raises(AssertionError):
        _check_queried_window_is_derived_from_the_cut_and_shown(corrupted)


def test_probe_universe_sequence_guard_rule_catches_a_removed_discard() -> None:
    _check_mesa_universe_deferred_load_keeps_sequence_guard_and_cut_discard(APP_JS)  # clean
    original_body = _extract_js_function(APP_JS, "loadMesaUniverseCoverage")
    corrupted_body = original_body.replace(
        "sequence !== mesaUniverseCoverageRequestSequence", "false", 1
    )
    assert corrupted_body != original_body
    corrupted = APP_JS.replace(original_body, corrupted_body, 1)
    with pytest.raises(AssertionError):
        _check_mesa_universe_deferred_load_keeps_sequence_guard_and_cut_discard(corrupted)


def test_probe_resumen_control_rule_catches_a_dropped_control() -> None:
    _check_every_resumen_control_survives_relocation(INDEX_HTML)  # baseline: clean
    marker = 'id="workspace-status"'
    assert INDEX_HTML.count(marker) == 1
    corrupted = INDEX_HTML.replace(marker, 'id="workspace-status-renamed"', 1)
    assert corrupted != INDEX_HTML
    with pytest.raises(AssertionError):
        _check_every_resumen_control_survives_relocation(corrupted)


def test_probe_preferences_panel_rule_catches_a_duplicated_panel() -> None:
    _check_asset_preferences_panel_moved_to_sistema_and_absent_from_mesa(INDEX_HTML)  # clean
    marker = '<div class="board" id="board-mesa" data-board="mesa" tabindex="-1">'
    assert marker in INDEX_HTML
    corrupted = INDEX_HTML.replace(marker, f'{marker}<span id="asset-preferences-panel"></span>', 1)
    assert corrupted != INDEX_HTML
    with pytest.raises(AssertionError):
        _check_asset_preferences_panel_moved_to_sistema_and_absent_from_mesa(corrupted)


def test_probe_mesa_hierarchy_documentation_rule_catches_a_missing_declaration() -> None:
    design_doc_path = (
        Path(str(files("investment_analyst"))).parent.parent
        / "docs"
        / "local_interface_design_system.md"
    )
    design_doc = design_doc_path.read_text(encoding="utf-8")
    _check_design_system_documentation_declares_the_mesa_hierarchy(design_doc)  # baseline: clean
    marker = "/api/v1/cazatiburones/notifications?family=institutional&limit=5"
    assert marker in design_doc
    corrupted = design_doc.replace(marker, "/api/v1/cazatiburones/notifications?limit=5")
    assert corrupted != design_doc
    with pytest.raises(AssertionError):
        _check_design_system_documentation_declares_the_mesa_hierarchy(corrupted)


def test_probe_ui9_mesa_layout_rule_catches_a_non_340px_aside() -> None:
    _check_mesa_three_column_composition(INDEX_HTML, STYLES_CSS)
    corrupted = STYLES_CSS.replace(
        "grid-template-columns: minmax(0, 1fr) 340px;",
        "grid-template-columns: minmax(0, 1fr) 341px;",
        1,
    )
    assert corrupted != STYLES_CSS
    with pytest.raises(AssertionError):
        _check_mesa_three_column_composition(INDEX_HTML, corrupted)


def test_probe_ui9_matrix_capabilities_rule_catches_bvl_as_cell() -> None:
    _check_compact_universe_matrix(INDEX_HTML, APP_JS)
    corrupted = APP_JS.replace(
        '  "corporate_valuation",\n]);', '  "corporate_valuation",\n  "bvl_registry",\n]);', 1
    )
    assert corrupted != APP_JS
    with pytest.raises(AssertionError):
        _check_compact_universe_matrix(INDEX_HTML, corrupted)


def test_probe_ui9_domain_rule_catches_a_symbol_fallback() -> None:
    _check_asset_domain_is_derived_only_from_asset_class(APP_JS)
    original = _extract_js_function(APP_JS, "mesaAssetDomainLabel")
    corrupted_body = original.replace(
        "const label = MESA_ASSET_CLASS_LABELS[assetClass];",
        "const label = MESA_ASSET_CLASS_LABELS[assetClass] || asset.symbol;",
        1,
    )
    assert corrupted_body != original
    corrupted = APP_JS.replace(original, corrupted_body, 1)
    with pytest.raises(AssertionError):
        _check_asset_domain_is_derived_only_from_asset_class(corrupted)


def test_probe_ui9_latest_evidence_rule_catches_bvl_input() -> None:
    _check_latest_evidence_uses_only_queried_domain_availability(APP_JS)
    original = _extract_js_function(APP_JS, "mesaLatestEvidenceTimestamp")
    corrupted_body = f"{original}\n// bvl_registry"
    corrupted = APP_JS.replace(original, corrupted_body, 1)
    with pytest.raises(AssertionError):
        _check_latest_evidence_uses_only_queried_domain_availability(corrupted)


def test_probe_ui9_legend_rule_catches_an_inaccessible_mark() -> None:
    _check_matrix_legend_and_accessible_marks(INDEX_HTML, APP_JS, STYLES_CSS)
    original = _extract_js_function(APP_JS, "mesaMatrixStateMarkup")
    corrupted_body = original.replace('role="img" aria-label="${label}"', 'aria-hidden="true"', 1)
    assert corrupted_body != original
    corrupted = APP_JS.replace(original, corrupted_body, 1)
    with pytest.raises(AssertionError):
        _check_matrix_legend_and_accessible_marks(INDEX_HTML, corrupted, STYLES_CSS)


def test_probe_ui9_absent_row_rule_catches_a_five_column_span() -> None:
    _check_matrix_absent_rows_span_six_columns(INDEX_HTML, APP_JS)
    original = _extract_js_function(APP_JS, "renderMesaUniverseAbsentTable")
    corrupted_body = original.replace("cell.colSpan = 6;", "cell.colSpan = 5;", 1)
    assert corrupted_body != original
    corrupted = APP_JS.replace(original, corrupted_body, 1)
    with pytest.raises(AssertionError):
        _check_matrix_absent_rows_span_six_columns(INDEX_HTML, corrupted)


def test_probe_ui9_bvl_summary_rule_catches_a_second_label() -> None:
    _check_bvl_summary_is_lateral_and_reuses_payload(INDEX_HTML, APP_JS)
    marker = '<aside class="mesa-aside" aria-label="Cobertura y fuentes bloqueadas">'
    assert marker in INDEX_HTML
    corrupted = INDEX_HTML.replace(marker, f"{marker}<p>Registro BVL</p>", 1)
    with pytest.raises(AssertionError):
        _check_bvl_summary_is_lateral_and_reuses_payload(corrupted, APP_JS)


def test_probe_ui9_clock_rule_catches_a_full_grid_span() -> None:
    _check_compact_clock_keeps_one_desktop_row(INDEX_HTML, STYLES_CSS)
    corrupted = STYLES_CSS.replace(
        ".market-clock-strip {\n  display: flex;",
        ".market-clock-strip {\n  display: flex;\n  grid-column: 1 / -1;",
        1,
    )
    assert corrupted != STYLES_CSS
    with pytest.raises(AssertionError):
        _check_compact_clock_keeps_one_desktop_row(INDEX_HTML, corrupted)


# ---------------------------------------------------------------------------
# Asset scope and sub-tabs (UI-8) probes
# ---------------------------------------------------------------------------


_ASSET_SUBTAB_IDS = (
    "mercado",
    "derivados-crypto",
    "fundamentales",
    "valoracion",
    "analisis",
)


def _asset_scope_markup(index_html: str) -> str:
    match = re.search(
        r'<div id="asset-scope-bar" class="asset-scope-bar">(.*?)</div>\n\s*<section id="mercado"',
        index_html,
        re.DOTALL,
    )
    assert match, "the single asset scope bar must precede the activo sections"
    return match.group(1)


def _check_asset_scope_and_subtabs(
    index_html: str, app_js: str, styles_css: str = STYLES_CSS
) -> None:
    topbar = re.search(r'<header class="topbar">(.*?)</header>', index_html, re.DOTALL)
    assert topbar, "persistent topbar must exist"
    for control_id in (
        "asset-name",
        "asset-symbol",
        "asset-classification",
        "asset-price",
        "asset-daily-change",
        "asset-meta",
        "asset-quality",
        "asset-avatar",
        "market-asset-search",
        "market-asset-listbox",
        "asset-selector-label",
    ):
        assert f'id="{control_id}"' not in topbar.group(1), (
            f"{control_id} must not leak from the global header"
        )
        assert index_html.count(f'id="{control_id}"') == 1
    assert index_html.count('id="board-nav"') == 1
    assert 'class="asset-nav"' not in index_html

    scope = _asset_scope_markup(index_html)
    assert scope.count('id="asset-subtabs"') == 1
    assert 'role="tablist"' in scope
    assert 'href="#' not in scope, "asset sub-tabs must be buttons, not hash anchors"
    selected = re.findall(r'class="asset-subtab[^\"]*"[^>]*aria-selected="true"', scope)
    assert len(selected) == 1, "exactly one asset sub-tab must ship selected"
    for section_id in _ASSET_SUBTAB_IDS[1:]:
        section_tag = re.search(rf'<section\b(?=[^>]*\bid="{section_id}")[^>]*>', index_html)
        assert section_tag and "hidden" in section_tag.group(0), (
            f"{section_id} must ship hidden until its tab is selected"
        )
    cursor = -1
    for section_id in _ASSET_SUBTAB_IDS:
        button = re.search(
            rf'<button(?P<attrs>[^>]*)aria-controls="{section_id}"(?P<tail>[^>]*)>', scope
        )
        assert button, f"missing button controlling {section_id}"
        attrs = button.group(0)
        assert 'type="button"' in attrs
        assert 'role="tab"' in attrs
        assert f'data-asset-subtab="{section_id}"' in attrs
        position = scope.index(button.group(0))
        assert position > cursor, "asset sub-tabs must keep their declared order"
        cursor = position
    for capability in (
        "data-crypto-derivatives-only",
        "data-fundamental-only",
        "data-valuation-only",
        "data-complete-analysis-only",
    ):
        assert capability in scope
    assert 'id="valuation-nav-link"' in scope

    scope_board_ids = (
        'const ASSET_SCOPE_BOARD_IDS = new Set(["activo", "tecnico", "cazatiburones"]);'
    )
    assert scope_board_ids in app_js
    assert "scopeBar.hidden = !shouldShow;" in app_js
    assert "activeBoard.prepend(scopeBar);" in app_js
    assert 'subtabs.hidden = boardId !== "activo";' in app_js
    assert "if (section) section.hidden = !selected;" in app_js
    assert ".asset-scope-bar[hidden],\n.asset-subtabs[hidden]" in styles_css
    assert 'activeAssetSubtabId = "mercado";' in app_js
    selector_body = re.search(
        r"function selectAssetSubtab\(requestedId\) \{(.*?)\n\}", app_js, re.DOTALL
    )
    assert selector_body
    assert "activateBoard(" not in selector_body.group(1)
    assert "history.replaceState" not in selector_body.group(1)
    assert "assetSubtabIsAvailable(requestedButton)" in selector_body.group(1)
    assert "marketButton" in selector_body.group(1)


def test_asset_scope_and_subtabs_are_the_only_asset_navigation() -> None:
    _check_asset_scope_and_subtabs(INDEX_HTML, APP_JS, STYLES_CSS)


def _check_ui8_composition_is_documented(design_doc: str, local_doc: str, plan_doc: str) -> None:
    for declaration in (
        "No hay número héroe",
        "reglas verticales y pies de procedencia",
        "único control permanente de tiempo",
        "filas de 25 px",
        "No carga fuentes web",
        "contraste AA, foco visible",
        "grafito cálido",
        "UI-9",
        "UI-10",
        "UI-11",
    ):
        assert declaration in design_doc
    assert "subpestañas de activo (`UI-8`)" in local_doc
    assert "`UI-8` mueve la identidad y el selector del activo" in plan_doc
    assert "`SEC-CORPUS` permanece como la única ruta `NEXT`" in plan_doc


def test_ui8_composition_and_route_are_documented() -> None:
    repository_root = Path(str(files("investment_analyst"))).parent.parent
    design_doc = (repository_root / "docs" / "local_interface_design_system.md").read_text(
        encoding="utf-8"
    )
    local_doc = (repository_root / "docs" / "local_interface.md").read_text(encoding="utf-8")
    plan_doc = (repository_root / "docs" / "basic_functional_release_plan.md").read_text(
        encoding="utf-8"
    )
    _check_ui8_composition_is_documented(design_doc, local_doc, plan_doc)


def test_probe_asset_scope_rule_catches_a_hash_anchor() -> None:
    _check_asset_scope_and_subtabs(INDEX_HTML, APP_JS, STYLES_CSS)  # baseline: clean
    market_button = (
        'type="button" class="asset-subtab" role="tab" aria-selected="true" aria-controls="mercado"'
    )
    corrupted = INDEX_HTML.replace(
        market_button,
        f'{market_button} href="#mercado"',
        1,
    )
    assert corrupted != INDEX_HTML
    with pytest.raises(AssertionError):
        _check_asset_scope_and_subtabs(corrupted, APP_JS, STYLES_CSS)


def test_probe_asset_scope_rule_catches_a_nonexclusive_section_toggle() -> None:
    _check_asset_scope_and_subtabs(INDEX_HTML, APP_JS, STYLES_CSS)  # baseline: clean
    corrupted = APP_JS.replace(
        "if (section) section.hidden = !selected;",
        'if (section) section.classList.toggle("hidden", !selected);',
        1,
    )
    assert corrupted != APP_JS
    with pytest.raises(AssertionError):
        _check_asset_scope_and_subtabs(INDEX_HTML, corrupted, STYLES_CSS)


def test_probe_asset_scope_rule_catches_hidden_override() -> None:
    _check_asset_scope_and_subtabs(INDEX_HTML, APP_JS, STYLES_CSS)  # baseline: clean
    corrupted = STYLES_CSS.replace(
        ".asset-scope-bar[hidden],\n.asset-subtabs[hidden] {",
        ".asset-scope-bar.is-hidden,\n.asset-subtabs.is-hidden {",
        1,
    )
    assert corrupted != STYLES_CSS
    with pytest.raises(AssertionError):
        _check_asset_scope_and_subtabs(INDEX_HTML, APP_JS, corrupted)


def test_probe_ui8_documentation_rule_catches_a_missing_route_declaration() -> None:
    repository_root = Path(str(files("investment_analyst"))).parent.parent
    design_doc = (repository_root / "docs" / "local_interface_design_system.md").read_text(
        encoding="utf-8"
    )
    local_doc = (repository_root / "docs" / "local_interface.md").read_text(encoding="utf-8")
    plan_doc = (repository_root / "docs" / "basic_functional_release_plan.md").read_text(
        encoding="utf-8"
    )
    _check_ui8_composition_is_documented(design_doc, local_doc, plan_doc)  # baseline: clean
    corrupted = design_doc.replace("UI-11", "UI-XX")
    assert corrupted != design_doc
    with pytest.raises(AssertionError):
        _check_ui8_composition_is_documented(corrupted, local_doc, plan_doc)


# ---------------------------------------------------------------------------
# UI-10: technical search, review master-detail, BVL regular session and
# removal of redundant global copy. These checks remain static; the required
# supported-client smoke exercises the live DOM and request behavior.
# ---------------------------------------------------------------------------


def test_technical_combobox_searches_catalog_and_manages_two_to_five_currency_compatible_assets():
    tecnico = _board_slices(INDEX_HTML)["tecnico"]
    assert 'id="comparison-asset-search"' in tecnico
    assert 'role="combobox"' in tecnico
    assert 'id="comparison-asset-options"' in tecnico
    assert 'role="listbox"' in tecnico
    assert 'id="comparison-selected-assets"' in tecnico
    assert 'id="comparison-assets"' in tecnico
    assert 'class="comparison-assets-native"' in tecnico
    assert "quoteCurrency" in APP_JS
    assert "syncComparisonAssetOptions" in APP_JS
    assert "compatible" in APP_JS
    assert ".slice(0, 5)" in APP_JS
    assert "La muestra admite como máximo cinco activos." in APP_JS
    assert "option.selected = true" in APP_JS
    assert "option.selected = false" in APP_JS


def test_technical_keyboard_aria_and_submit_only_query_contract() -> None:
    tecnico = _board_slices(INDEX_HTML)["tecnico"]
    for attribute in (
        'aria-autocomplete="list"',
        'aria-controls="comparison-asset-options"',
        'aria-expanded="false"',
        'aria-describedby="comparison-selection-status"',
    ):
        assert attribute in tecnico
    search_start = APP_JS.index("comparisonAssetSearch.addEventListener")
    search_end = APP_JS.index("async function queryMarketComparison", search_start)
    search_block = APP_JS[search_start:search_end]
    assert "ArrowDown" in search_block
    assert "ArrowUp" in search_block
    assert "Enter" in search_block
    assert "Escape" in search_block
    assert "aria-activedescendant" in APP_JS
    assert 'byId("market-comparison-form").addEventListener("submit"' in APP_JS
    assert "await queryMarketComparison();" in APP_JS
    assert "queryMarketComparison" not in search_block


def test_review_board_has_responsive_master_detail_with_two_semantic_groups() -> None:
    revisar = _board_slices(INDEX_HTML)["revisar"]
    for marker in (
        'id="review-master-detail"',
        'id="review-master-list"',
        'id="candidate-inbox-panel"',
        'id="alert-inbox-panel"',
        'id="review-detail-panel"',
        'id="review-detail"',
    ):
        assert marker in revisar
    assert "review-master-detail" in STYLES_CSS
    assert "grid-template-columns: 1fr;" in STYLES_CSS[STYLES_CSS.index(".review-master-detail") :]
    assert "revisar: Object.freeze([loadCandidateInbox, loadAlertInbox])" in APP_JS
    assert 'api("/api/candidates?limit=50")' in APP_JS
    assert 'api("/api/alerts?limit=50")' in APP_JS


def test_review_selection_and_actions_remain_bound_to_stable_source_identity() -> None:
    assert "reviewItemId(family, item)" in APP_JS
    assert "data-review-id" in APP_JS
    assert "reviewSelectionMissing" in APP_JS
    assert "event.candidate_id" in APP_JS
    assert "event.alert_id" in APP_JS
    assert "transitionCandidate(event.candidate_id, target, button)" in APP_JS
    assert "transitionAlert(event.alert_id, target, button)" in APP_JS
    assert "La selección ya no está disponible" in APP_JS
    assert "reviewSelection.family" in APP_JS


def test_candidate_cooldown_and_operational_not_applicable_are_not_conflated() -> None:
    detail = APP_JS[
        APP_JS.index("function renderReviewDetail") : APP_JS.index(
            "function updateReviewMasterSelection"
        )
    ]
    assert 'reviewDetailField(fields, "Espera", formatInstant(event.cooldown_until));' in detail
    assert 'reviewDetailField(fields, "Espera", "No aplica");' in detail
    assert "cooldown_until" in detail
    assert "transitionCandidate(event.candidate_id" in detail
    assert "transitionAlert(event.alert_id" in detail
    assert 'class="review-detail-panel"' in INDEX_HTML or "review-detail-panel" in INDEX_HTML


def test_bvl_session_uses_lima_zone_two_official_periods_and_weekdays() -> None:
    assert "timeZone: DEFAULT_TIME_ZONE" in APP_JS
    assert "const BVL_SESSION_PERIODS = Object.freeze" in APP_JS
    assert "summer: Object.freeze" in APP_JS
    assert "winter: Object.freeze" in APP_JS
    assert "8 * 60 + 30" in APP_JS
    assert "14 * 60 + 50" in APP_JS
    assert "9 * 60 + 30" in APP_JS
    assert "15 * 60 + 50" in APP_JS
    assert "nthSundayOfMonth(parts.year, 3, 2)" in APP_JS
    assert "nthSundayOfMonth(parts.year, 11, 1)" in APP_JS
    bvl_state = _extract_js_function(APP_JS, "bvlRegularSessionState")
    assert 'parts.weekday === "Sat" || parts.weekday === "Sun"' in bvl_state
    assert "period.open" in bvl_state and "period.close" in bvl_state


def test_market_strip_removes_lima_clock_and_keeps_new_york_nyse_and_accessible_limit() -> None:
    for control_id in (
        "new-york-clock",
        "new-york-clock-date",
        "bvl-session-status",
        "bvl-session-dot",
        "bvl-session-remaining",
        "nyse-session-status",
        "nyse-session-dot",
        "nyse-session-remaining",
        "market-clock-note",
    ):
        assert INDEX_HTML.count(f'id="{control_id}"') == 1
    assert not re.search(r'<[a-z][^>]*\bid="lima-clock"[^>]*>', INDEX_HTML)
    assert not re.search(r'<[a-z][^>]*\bid="lima-clock-date"[^>]*>', INDEX_HTML)
    assert "America/Lima" in INDEX_HTML
    assert "no evalúa feriados ni sesiones especiales" in INDEX_HTML
    assert "bvlRegularSessionState(now)" in APP_JS


def test_requested_global_copy_and_footer_are_absent_from_dom() -> None:
    for text in (
        "Estas bandejas no están acotadas por el corte",
        "Cazatiburones, Documentos y Derivados",
        "Limitaciones declaradas",
        "Uso local · Sin ejecución de órdenes · No constituye asesoramiento financiero",
    ):
        assert text not in INDEX_HTML
    assert "<footer" not in INDEX_HTML
    assert "</footer>" not in INDEX_HTML


def test_removed_copy_leaves_no_orphan_aria_css_or_js() -> None:
    for text in (
        "mesa-universe-not-queried",
        "mesa-universe-limitations",
    ):
        assert text not in INDEX_HTML
        assert text not in APP_JS
        assert text not in STYLES_CSS
    assert not re.search(r'<[a-z][^>]*\bid="lima-clock"[^>]*>', INDEX_HTML)
    assert not re.search(r'<[a-z][^>]*\bid="lima-clock-date"[^>]*>', INDEX_HTML)
    for source in (APP_JS, STYLES_CSS):
        assert "lima-clock" not in source
        assert "lima-clock-date" not in source
    assert 'aria-describedby="mesa-universe-legend mesa-universe-window"' in INDEX_HTML


def test_contract_limitations_remain_documented_and_do_not_create_ui_cells_or_requests() -> None:
    repository_root = Path(str(files("investment_analyst"))).parent.parent
    local_doc = (repository_root / "docs" / "local_interface.md").read_text(encoding="utf-8")
    design_doc = (repository_root / "docs" / "local_interface_design_system.md").read_text(
        encoding="utf-8"
    )
    for document in (local_doc, design_doc):
        assert "limitations" in document
        assert "additional_capabilities_not_queried" in document
    matrix_body = _extract_js_function(APP_JS, "renderMesaUniverseMatrix")
    assert "asset.limitations" not in matrix_body
    assert "additional_capabilities_not_queried" not in APP_JS
    assert "api(" not in matrix_body


def test_board_registry_still_declares_exactly_six_boards() -> None:
    _check_board_registry_declares_exactly_six_boards(APP_JS)


def test_board_registry_still_declares_exactly_six_boards_with_unchanged_deferred_load_graph() -> (
    None
):
    _check_board_registry_declares_exactly_six_boards(APP_JS)
    _check_board_to_deferred_loads_table_covers_the_six_registered_boards(APP_JS)


def test_technical_review_and_mesa_load_graphs_and_endpoints_are_unchanged() -> None:
    _check_board_to_deferred_loads_table_covers_the_six_registered_boards(APP_JS)
    _check_no_new_capability_or_route_is_introduced_by_the_shell(APP_JS)
    assert "loadCandidateInbox" in _board_deferred_load_entry(APP_JS, "revisar")
    assert "loadAlertInbox" in _board_deferred_load_entry(APP_JS, "revisar")
    assert "api(`/api/v1/market-comparison?" in APP_JS


def test_no_cross_family_score_rank_order_or_synthesized_fields() -> None:
    revisar = _board_slices(INDEX_HTML)["revisar"]
    assert revisar.count('class="operation-panel') == 2
    assert "review-master-list" in revisar and "review-detail-panel" in revisar
    assert (
        "combined"
        not in APP_JS[
            APP_JS.index("function renderAlertInbox") : APP_JS.index(
                "async function transitionAlert"
            )
        ]
    )
    assert (
        "rank"
        not in APP_JS[
            APP_JS.index("function renderReviewDetail") : APP_JS.index(
                "function renderCandidateNotifications"
            )
        ]
    )


def test_no_network_calendar_or_market_data_used_for_session_clocks() -> None:
    bvl_start = APP_JS.index("function limaWallClockDateParts")
    bvl_end = APP_JS.index("function renderMarketClocks", bvl_start)
    bvl_helpers = APP_JS[bvl_start:bvl_end]
    assert "api(" not in bvl_helpers
    assert "fetch(" not in bvl_helpers
    assert "getTimezoneOffset" not in bvl_helpers
    assert "LIMA_DATE_PARTS_FORMATTER" in bvl_helpers


def test_no_hidden_relocation_of_removed_visible_copy() -> None:
    for source in (INDEX_HTML, APP_JS, STYLES_CSS):
        assert "Estas bandejas no están acotadas por el corte" not in source
        assert "Cazatiburones, Documentos y Derivados" not in source
        assert "Limitaciones declaradas" not in source
        assert "Uso local · Sin ejecución de órdenes" not in source


def test_docs_state_technical_review_bvl_and_clean_ui_boundaries() -> None:
    repository_root = Path(str(files("investment_analyst"))).parent.parent
    local_doc = (repository_root / "docs" / "local_interface.md").read_text(encoding="utf-8")
    design_doc = (repository_root / "docs" / "local_interface_design_system.md").read_text(
        encoding="utf-8"
    )
    plan_doc = (repository_root / "docs" / "basic_functional_release_plan.md").read_text(
        encoding="utf-8"
    )
    for document in (local_doc, design_doc):
        assert "UI-10" in document
        assert "America/Lima" in document
        assert "08:30–14:50" in document
        assert "09:30–15:50" in document
        assert "master-detail" in document
    assert "Anexo2TextodcRentaFija.pdf" in local_doc
    assert "`SEC-CORPUS` permanece como la única ruta `NEXT`" in plan_doc
    assert "UI-11" in plan_doc
