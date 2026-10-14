"""Guards that the templates only use assets the vendored bundles actually ship.

The stylesheets (`theme/static/css/*.css`, written by hand) and the icon font
(`materialsymbols-subset.woff2`, a prebuilt subset) are committed to the repo:
there is no CSS build and no subsetting script here. So a template can reference
a class or an icon that simply does not exist, and nothing fails — the markup
renders silently unstyled, or an icon name prints as literal text.

These tests are cheap and catch that whole class of bug at its source.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIRS = [
    ROOT / "mtg_gauntlet" / "templates",
    ROOT / "decks" / "templates",
    ROOT / "playtest" / "templates",
]
CSS_DIR = ROOT / "theme" / "static" / "css"
# Every page links these, in this order: tokens and reset before what uses them.
STYLESHEETS = ["styles.css", "layout.css", "components.css"]
LINKS = ROOT / "mtg_gauntlet" / "templates" / "partials" / "stylesheets.html"


def _css() -> str:
    return "\n".join((CSS_DIR / name).read_text() for name in STYLESHEETS)

# Characters the committed icon-font subset actually carries. Note the absence
# of "j" and "q": the subsetter dropped them, so every Material Symbols name
# containing either (query_stats, qr_code, join_full, ...) renders as text.
ICON_CHARS = set("_abcdefghiklmnoprstuvwxyz")

# Classes a template may use without a rule of its own. Keep it empty: a new
# entry means markup that does nothing.
KNOWN_UNCOMPILED = set()


def _template_sources():
    """Every template, with Django tags and variables stripped out."""
    for path in sorted(p for d in TEMPLATE_DIRS for p in d.rglob("*.html")):
        src = path.read_text()
        src = re.sub(r"\{%.*?%\}", " ", src, flags=re.S)
        src = re.sub(r"\{\{.*?\}\}", " ", src, flags=re.S)
        yield path, src


# An icon span: `ms` as a class, whatever the attribute order.
ICON_SPAN = re.compile(r'<span\b([^>]*\bclass="ms(?:\s[^"]*)?"[^>]*)>\s*([a-z_]+)\s*</span>')


def _compiled_classes():
    css = _css()
    return {
        re.sub(r"\\(.)", r"\1", match)
        for match in re.findall(r"\.((?:[A-Za-z0-9_-]|\\.)+)", css)
    }


def test_every_template_class_exists_in_the_stylesheet():
    compiled = _compiled_classes()
    missing = {}
    for path, src in _template_sources():
        used = {
            cls
            for attr in re.findall(r'class="([^"]*)"', src)
            for cls in attr.split()
        }
        gaps = sorted(used - compiled - KNOWN_UNCOMPILED)
        if gaps:
            missing[path.name] = gaps
    assert not missing, (
        "These classes are used in templates but are not in the compiled "
        f"stylesheet, so they do nothing: {missing}"
    )


def test_every_icon_name_is_renderable_by_the_font_subset():
    offenders = {}
    for path, src in _template_sources():
        for _, name in ICON_SPAN.findall(src):
            unsupported = sorted(set(name) - ICON_CHARS)
            if unsupported:
                offenders[f"{path.name}:{name}"] = unsupported
    assert not offenders, (
        "These icon names use characters the font subset does not carry, so "
        f"they render as literal text: {offenders}"
    )


def test_every_icon_is_opted_out_of_page_translation():
    # Icons are ligatures: the glyph is the English word itself. A page
    # translator rewrites "arrow_back" into "freccia_indietro", which the font
    # does not know, so it renders as literal text.
    offenders = [
        f"{path.name}:{name}"
        for path, src in _template_sources()
        for attrs, name in ICON_SPAN.findall(src)
        if 'translate="no"' not in attrs
    ]
    assert not offenders, (
        "These icons lack translate=\"no\", so browser translation turns them "
        f"into text: {offenders}"
    )


def test_the_stylesheet_defines_the_design_tokens_and_no_tailwind():
    css = _css()
    for token in ("--bg:", "--surface:", "--accent:", "--teal:", "--gold:", "--danger:"):
        assert token in css
    assert "tailwind" not in css.lower() and "--tw-" not in css


def test_every_stylesheet_is_linked_in_order():
    assert sorted(p.name for p in CSS_DIR.glob("*.css")) == sorted(STYLESHEETS)
    links = re.findall(r"\{% static 'css/([^']+)' %\}", LINKS.read_text())
    assert links == STYLESHEETS
    for page in ("base.html", "registration/login.html"):
        src = (ROOT / "mtg_gauntlet" / "templates" / page).read_text()
        assert '{% include "partials/stylesheets.html" %}' in src and "css/styles.css" not in src
