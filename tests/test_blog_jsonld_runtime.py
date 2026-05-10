import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ['FLASK_ENV'] = 'testing'
os.environ['TESTING'] = '1'
os.environ['DATABASE_URL'] = 'sqlite:////tmp/trader_blog_jsonld_tests.sqlite3'
os.environ['RATELIMIT_STORAGE_URI'] = 'memory://'
for k in ['SECRET_KEY','TOKEN_ENCRYPTION_KEY','ALPACA_CLIENT_ID','ALPACA_CLIENT_SECRET','ALPACA_REDIRECT_URI','FINNHUB_API_KEY','GEMINI_API_KEY']:
    os.environ.setdefault(k, 'test')

sys.path.append(str(Path(__file__).resolve().parents[1]))

import types

if 'redis' not in sys.modules:
    class _FakeRedisClient:
        def ping(self):
            return True

    class _FakeRedis:
        @staticmethod
        def from_url(*args, **kwargs):
            return _FakeRedisClient()

    sys.modules['redis'] = types.SimpleNamespace(Redis=_FakeRedis)
if 'requests' not in sys.modules:
    sys.modules['requests'] = types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None)
if 'stripe' not in sys.modules:
    sys.modules['stripe'] = types.SimpleNamespace(api_key='test')

import app as app_module
from models import BlogPost, db

JSONLD_SCRIPT_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
TEMPLATE_MARKERS = ("{{", "}}", "{%", "%}")


def _reset_db():
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, RATELIMIT_ENABLED=False)
    with app_module.app.app_context():
        db.drop_all()
        db.create_all()


def _create_post(*, slug: str, og_image: str | None = None):
    now = datetime(2026, 5, 1, 10, 30, tzinfo=timezone.utc)
    with app_module.app.app_context():
        post = BlogPost(
            title='Algorithmic Trading Risk Controls for Volatile Sessions',
            slug=slug,
            meta_title='Risk Controls for Volatile Trading Sessions | XeanVI',
            meta_description='Practical steps to define position sizing, stop policies, and monitoring for volatile market conditions.',
            excerpt='A practical guide to strengthening risk controls before market volatility increases.',
            body_html='<p>Use scenario planning and defined risk limits.</p>',
            status='published',
            author_name='XeanVI Research Team',
            canonical_url=f'https://xeanvi.com/blog/{slug}',
            og_image=og_image,
            featured_image_alt='Risk dashboard overview' if og_image else None,
            published_at=now,
            updated_at=now,
        )
        db.session.add(post)
        db.session.commit()


def _render_blog_post(slug: str) -> str:
    client = app_module.app.test_client()
    rv = client.get(f'/blog/{slug}')
    assert rv.status_code == 200
    return rv.get_data(as_text=True)


def _extract_jsonld_blocks(rendered_html: str):
    blocks = [m.strip() for m in JSONLD_SCRIPT_RE.findall(rendered_html)]
    assert blocks, 'Expected at least one JSON-LD script block on rendered blog post page.'
    return blocks


def _find_blogposting_object(parsed_blocks):
    for block in parsed_blocks:
        if isinstance(block, dict) and block.get('@type') == 'BlogPosting':
            return block
        if isinstance(block, dict) and isinstance(block.get('@graph'), list):
            for node in block['@graph']:
                if isinstance(node, dict) and node.get('@type') == 'BlogPosting':
                    return node
    return None


def test_runtime_jsonld_blog_post_parses_and_has_required_fields_without_image():
    _reset_db()
    slug = 'risk-controls-volatility'
    _create_post(slug=slug, og_image=None)

    html = _render_blog_post(slug)
    blocks = _extract_jsonld_blocks(html)

    parsed = []
    for block in blocks:
        for marker in TEMPLATE_MARKERS:
            assert marker not in block
        parsed.append(json.loads(block))

    blogposting = _find_blogposting_object(parsed)
    assert blogposting is not None, 'Expected BlogPosting JSON-LD in rendered blog page.'

    assert blogposting.get('@context') == 'https://schema.org'
    assert blogposting.get('@type') == 'BlogPosting'
    assert blogposting.get('headline') == 'Risk Controls for Volatile Trading Sessions | XeanVI'
    assert blogposting.get('description')
    assert blogposting.get('datePublished')
    assert blogposting.get('dateModified')
    assert blogposting.get('author') or blogposting.get('publisher')
    assert blogposting.get('mainEntityOfPage') or blogposting.get('url')
    assert blogposting.get('url', '').endswith(f'/blog/{slug}')
    assert 'image' not in blogposting


def test_runtime_jsonld_blog_post_includes_featured_image_when_present():
    _reset_db()
    slug = 'risk-controls-with-image'
    _create_post(slug=slug, og_image='/static/uploads/blog/risk-controls.png')

    html = _render_blog_post(slug)
    blocks = _extract_jsonld_blocks(html)
    parsed = [json.loads(block) for block in blocks]
    blogposting = _find_blogposting_object(parsed)

    assert blogposting is not None
    assert blogposting.get('image'), 'Expected image in BlogPosting schema when featured image exists.'
    image = blogposting['image'][0] if isinstance(blogposting.get('image'), list) else blogposting.get('image')
    assert image.startswith('http')
    assert image.endswith('/static/uploads/blog/risk-controls.png')
