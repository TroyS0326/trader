from pathlib import Path
import re


def _read(path: str) -> str:
    return Path(path).read_text(encoding='utf-8')


def test_marketing_google_fonts_requests_keep_display_swap_and_preconnect():
    pages = [
        'templates/features.html',
        'templates/playbook.html',
        'templates/broker_integration.html',
        'templates/transparency.html',
        'templates/upgrade.html',
    ]
    for path in pages:
        html = _read(path)
        assert 'href="https://fonts.googleapis.com' in html
        assert 'display=swap' in html
        assert 'rel="preconnect" href="https://fonts.googleapis.com"' in html
        assert 'rel="preconnect" href="https://fonts.gstatic.com" crossorigin' in html


def test_blog_featured_image_is_not_lazy_and_has_dimensions():
    html = _read('templates/blog_post.html')
    match = re.search(r'<img[^>]+src="\{\{ post\.og_image \}\}"[^>]*>', html)
    assert match, 'Expected featured blog image tag in blog post template'
    tag = match.group(0)

    assert 'loading="lazy"' not in tag
    assert 'loading="eager"' in tag
    assert 'fetchpriority="high"' in tag
    assert 'width="1200"' in tag
    assert 'height="630"' in tag
