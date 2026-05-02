"""
Test: resolveAnchors captures hrefs from ChatGPT-style table links.

ChatGPT renders links in tables as <a class="decorated-link cursor-pointer">
without an href *HTML attribute*, but the URL is accessible via either:
  - The DOM property a.href (if React set it via element.href = url)
  - React fiber props (if React stored it only in the virtual DOM)

This test validates the full pipeline:
  1. resolveAnchors() injects href from DOM property OR React fiber
  2. markdownify() converts the href-injected HTML to [text](url)
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Scripts"))

from markdownify import markdownify as md

# The resolveAnchors JavaScript — must stay in sync with browser.py
RESOLVE_JS = """
function resolveAnchors(node) {
    if (!node) return '';
    const clone = node.cloneNode(true);
    const origAnchors = Array.from(node.querySelectorAll('a'));
    const cloneAnchors = Array.from(clone.querySelectorAll('a'));
    origAnchors.forEach((a, i) => {
        const ca = cloneAnchors[i];
        if (!ca) return;
        let href = a.href;
        if (!href || href === location.href) {
            const fk = Object.keys(a).find(k =>
                k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance')
            );
            if (fk) {
                let fiber = a[fk]; let depth = 0;
                while (fiber && !href && depth < 10) {
                    const p = fiber.memoizedProps || fiber.pendingProps || {};
                    if (p.href && typeof p.href === 'string' &&
                        p.href.startsWith('http') && p.href !== location.href)
                        href = p.href;
                    fiber = fiber.return; depth++;
                }
            }
        }
        // Strategy 3: window._chatgpt_link_map (from /backend-api/conversation/ JSON)
        if (!href || href === '' || href === location.href) {
            const lm = window._chatgpt_link_map || {};
            const txt = (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
            if (txt && lm[txt]) href = lm[txt];
        }
        if (href && href !== '' && href !== location.href && !href.startsWith('javascript:')) {
            ca.setAttribute('href', href);
        }
    });
    return clone.innerHTML;
}
"""

# Minimal page that mimics ChatGPT's rendered table with links.
# Scenario A: href as HTML attribute (control — must always work).
# Scenario B: href set via JS property after parse (React element.href = url).
# Scenario C: no href at all (should remain plain text).
# Scenario D: href only in React fiber props (no DOM property set).
# Scenario E: href only in window._chatgpt_link_map (Strategy 3 — API JSON).
PAGE_HTML = """<!DOCTYPE html>
<html><body>
<table id="tbl">
<tbody>
<tr>
  <td id="cell-a">
    <a id="link-a" class="decorated-link cursor-pointer"
       rel="noopener" target="_new"
       href="https://conexaovida.org/?id=membros&acao=ver&id_membro=111">
      Patient With Attr Href
    </a>
  </td>
</tr>
<tr>
  <td id="cell-b">
    <a id="link-b" class="decorated-link cursor-pointer"
       rel="noopener" target="_new">
      Matheus de Sousa Fatta
    </a>
  </td>
</tr>
<tr>
  <td id="cell-c">
    <a id="link-c" class="decorated-link cursor-pointer"
       rel="noopener" target="_new">
      Patient No Link
    </a>
  </td>
</tr>
<tr>
  <td id="cell-d">
    <a id="link-d" class="decorated-link cursor-pointer"
       rel="noopener" target="_new">
      Tiago Galvao Monteiro
    </a>
  </td>
</tr>
<tr>
  <td id="cell-e">
    <a id="link-e" class="decorated-link cursor-pointer"
       rel="noopener" target="_new">
      Gabriel Rodrigues Machado
    </a>
  </td>
</tr>
</tbody>
</table>
<script>
// Scenario B: simulate React setting href via DOM property.
document.getElementById('link-b').href =
    'https://conexaovida.org/?id=membros&acao=ver&id_membro=1667505560';

// Scenario D: simulate React fiber.
(function() {
    const el = document.getElementById('link-d');
    el['__reactFiber$test'] = {
        memoizedProps: {
            href: 'https://conexaovida.org/?id=membros&acao=ver&id_membro=9999999',
            className: 'decorated-link cursor-pointer'
        },
        pendingProps: null,
        return: null
    };
})();

// Scenario E: window._chatgpt_link_map (Strategy 3 — injected by _inject_link_map_to_page).
// No DOM href, no fiber — URL available only via the link map from the API JSON.
window._chatgpt_link_map = {
    'Gabriel Rodrigues Machado': 'https://conexaovida.org/?id=membros&acao=ver&id_membro=8888888'
};
</script>
</body></html>"""


@pytest.fixture(scope="module")
def browser_page():
    """Shared Playwright browser page for all tests in this module."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(PAGE_HTML)
        yield page
        browser.close()


# ── Diagnostic: show raw a.href / getAttribute values ────────────────────────

def test_diagnostic_href_properties(browser_page):
    """Show what a.href and getAttribute('href') return for each scenario."""
    page = browser_page
    info = page.evaluate("""() => {
        return ['link-a', 'link-b', 'link-c', 'link-d'].map(id => {
            const a = document.getElementById(id);
            const fiberKey = Object.keys(a).find(k =>
                k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance')
            );
            const fiberHref = fiberKey
                ? (a[fiberKey].memoizedProps || {}).href || ''
                : '';
            return {
                id,
                hrefProp: a.href,
                hrefAttr: a.getAttribute('href'),
                fiberHref,
                currentPage: location.href,
            };
        });
    }""")
    for item in info:
        print(f"\n{item['id']}:")
        print(f"  a.href (property) : {item['hrefProp']}")
        print(f"  getAttribute(href): {item['hrefAttr']}")
        print(f"  fiber href        : {item['fiberHref']}")
        print(f"  location.href     : {item['currentPage']}")


# ── resolveAnchors tests ──────────────────────────────────────────────────────

def test_resolveAnchors_preserves_existing_href(browser_page):
    """Scenario A: link already has href attribute — must be preserved."""
    page = browser_page
    html = page.evaluate(f"""() => {{
        {RESOLVE_JS}
        return resolveAnchors(document.getElementById('cell-a'));
    }}""")
    print("\nScenario A HTML:", html)
    assert 'id_membro=111' in html, f"href must be present, got: {html}"
    assert 'href=' in html, f"href attribute expected, got: {html}"


def test_resolveAnchors_captures_js_set_href(browser_page):
    """Scenario B: href set via DOM property (no initial attribute) — must be captured."""
    page = browser_page
    html = page.evaluate(f"""() => {{
        {RESOLVE_JS}
        return resolveAnchors(document.getElementById('cell-b'));
    }}""")
    print("\nScenario B HTML:", html)
    assert '1667505560' in html, f"Patient ID must be in href, got: {html}"
    assert 'href=' in html, f"href attribute expected, got: {html}"


def test_resolveAnchors_no_href_remains_plain(browser_page):
    """Scenario C: link with absolutely no href — text preserved, no href injected."""
    page = browser_page
    html = page.evaluate(f"""() => {{
        {RESOLVE_JS}
        return resolveAnchors(document.getElementById('cell-c'));
    }}""")
    print("\nScenario C HTML:", html)
    assert 'Patient No Link' in html, "Text must be preserved"
    assert 'conexaovida.org' not in html, "No URL expected for Scenario C"


def test_resolveAnchors_captures_react_fiber_href(browser_page):
    """
    Scenario D: href stored only in React fiber props (no DOM attribute/property).
    This is the suspected ChatGPT behaviour for decorated-link elements in tables.
    """
    page = browser_page
    html = page.evaluate(f"""() => {{
        {RESOLVE_JS}
        return resolveAnchors(document.getElementById('cell-d'));
    }}""")
    print("\nScenario D HTML:", html)
    assert '9999999' in html, \
        f"Fiber-only href must be captured, got: {html}"
    assert 'href=' in html, \
        f"href attribute must be injected from fiber, got: {html}"


def test_resolveAnchors_captures_link_map_href(browser_page):
    """
    Scenario E: href available only in window._chatgpt_link_map (Strategy 3).
    This is the primary fallback for sync — _inject_link_map_to_page() puts
    the link text→URL dict from /backend-api/conversation/ JSON into the page.
    """
    page = browser_page
    html = page.evaluate(f"""() => {{
        {RESOLVE_JS}
        return resolveAnchors(document.getElementById('cell-e'));
    }}""")
    print("\nScenario E HTML:", html)
    assert '8888888' in html, \
        f"Link-map href must be captured, got: {html}"
    assert 'href=' in html, \
        f"href attribute must be injected from link map, got: {html}"


def test_resolveAnchors_full_table(browser_page):
    """Full table: only links WITH hrefs get href injected."""
    page = browser_page
    html = page.evaluate(f"""() => {{
        {RESOLVE_JS}
        return resolveAnchors(document.getElementById('tbl'));
    }}""")
    print("\nFull table HTML (first 1000 chars):", html[:1000])
    assert '1667505560' in html, "Patient B link must have URL"
    assert 'id_membro=111' in html, "Patient A link must have URL"
    assert '9999999' in html, "Patient D fiber link must have URL"
    assert '8888888' in html, "Patient E link-map link must have URL"


# ── markdownify pipeline (end-to-end) ────────────────────────────────────────

def test_markdownify_renders_links_from_resolved_html(browser_page):
    """
    End-to-end: resolveAnchors → markdownify produces [text](url) for all
    linked patients, which chat.js.php can render as clickable links.
    """
    page = browser_page
    html = page.evaluate(f"""() => {{
        {RESOLVE_JS}
        return resolveAnchors(document.getElementById('tbl'));
    }}""")
    markdown = md(html, heading_style='ATX').strip()
    markdown = markdown.replace('\\_', '_').replace('\\*', '*')
    print("\nMarkdown output:\n", markdown)

    assert '[Matheus de Sousa Fatta]' in markdown, \
        "Patient B must appear as markdown link text"
    assert '1667505560' in markdown, \
        "Patient B URL must appear in markdown"
    assert '[Matheus de Sousa Fatta](https://conexaovida.org/' in markdown, \
        f"Expected full markdown link, got:\n{markdown}"
    assert '[Tiago Galvao Monteiro]' in markdown or 'Tiago' in markdown, \
        "Patient D must appear in output"
    assert '[Gabriel Rodrigues Machado]' in markdown or 'Gabriel' in markdown, \
        "Patient E must appear in output"
    assert '8888888' in markdown, \
        "Patient E link-map URL must appear in markdown"
    assert 'Patient No Link' in markdown, \
        "Patient C (no link) must still appear as plain text"


def test_markdownify_no_link_is_plain_text(browser_page):
    """Patient with no href renders as plain text, not a broken link."""
    page = browser_page
    html = page.evaluate(f"""() => {{
        {RESOLVE_JS}
        return resolveAnchors(document.getElementById('cell-c'));
    }}""")
    markdown = md(html, heading_style='ATX').strip()
    print("\nNo-link markdown:", markdown)
    assert 'Patient No Link' in markdown
    # Must not have broken markdown link syntax like [text]()
    assert '](http' not in markdown or 'Patient No Link' not in markdown.split('](')[0].split('\n')[-1]
