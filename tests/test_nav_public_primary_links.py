from pathlib import Path
import re


def _read(path: str) -> str:
    return Path(path).read_text(encoding='utf-8')


def _public_nav_block(nav_html: str) -> str:
    match = re.search(r"\{% else %\}(.*?)\{% endif %\}", nav_html, re.DOTALL)
    assert match, 'Could not locate logged-out nav block'
    return match.group(1)


def test_logged_out_primary_nav_contains_only_primary_marketing_links_and_account_actions():
    nav_html = _read('templates/nav.html')
    public_nav = _public_nav_block(nav_html)

    for href in ['/features', '/pricing', '/blog', '/about']:
        assert f'href="{href}"' in public_nav

    assert 'href="/login"' in nav_html
    assert 'href="/signup?plan=monthly"' in nav_html


def test_logged_out_primary_nav_excludes_secondary_detail_pages():
    nav_html = _read('templates/nav.html')
    public_nav = _public_nav_block(nav_html)

    for href in ['/playbook', '/broker-integration', '/transparency']:
        assert f'href="{href}"' not in public_nav


def test_secondary_pages_remain_discoverable_outside_primary_nav():
    footer_html = _read('templates/footer.html')
    for href in ['/playbook', '/broker-integration', '/transparency']:
        assert f'href="{href}"' in footer_html


def test_logged_out_pricing_link_uses_pricing_label():
    nav_html = _read('templates/nav.html')
    public_nav = _public_nav_block(nav_html)

    assert 'href="/pricing"' in public_nav
    assert '>Pricing</a>' in public_nav
    assert '>Monthly PRO</a>' not in public_nav
