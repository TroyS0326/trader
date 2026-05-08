import os
from pathlib import Path
import sys

os.environ['FLASK_ENV'] = 'testing'
os.environ['TESTING'] = '1'
os.environ['DATABASE_URL'] = 'sqlite:////tmp/trader_blog_edit_tests.sqlite3'
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

    redis_stub = types.SimpleNamespace(
        Redis=_FakeRedis,
    )
    sys.modules['redis'] = redis_stub
if 'requests' not in sys.modules:
    sys.modules['requests'] = types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None)
if 'stripe' not in sys.modules:
    sys.modules['stripe'] = types.SimpleNamespace(api_key='test')

import app as app_module
from models import db, User, BlogPost


def _mk_user(email):
    u = User(email=email, password_hash='hash')
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, uid):
    with client.session_transaction() as s:
        s['_user_id'] = str(uid)


def _reset(monkeypatch):
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, RATELIMIT_ENABLED=False)
    monkeypatch.setattr(app_module, 'ADMIN_EMAIL', 'admin@test.com')
    monkeypatch.setattr(app_module, 'analyze_blog_post_seo', lambda **kwargs: {'status': 'ok', 'warnings': [], 'suggestions': []})
    monkeypatch.setattr(app_module, 'analyze_human_quality', lambda **kwargs: {'score': 100})
    monkeypatch.setattr(app_module, 'suggest_internal_links', lambda **kwargs: [])
    monkeypatch.setattr(app_module, 'generate_image_alt_caption', lambda **kwargs: {'alt_text': 'alt', 'caption': 'cap'})
    with app_module.app.app_context():
        db.drop_all()
        db.create_all()
        admin = _mk_user('admin@test.com')
        return admin.id


def _post_data(**overrides):
    data = {
        'title': 'Updated Title',
        'slug': 'updated-title',
        'body_html': '<p>Updated</p>',
        'meta_title': '',
        'meta_description': '',
        'excerpt': '',
        'target_keyword': '',
        'canonical_url': '',
        'og_image': '',
        'featured_image_alt': '',
        'featured_image_caption': '',
        'action': 'publish',
    }
    data.update(overrides)
    return data


def test_edit_draft_publish_non_500_and_no_new_row(monkeypatch):
    admin_id = _reset(monkeypatch)
    with app_module.app.app_context():
        p = BlogPost(title='Draft', slug='draft', body_html='<p>x</p>', status='draft', og_image='/img.jpg')
        db.session.add(p)
        db.session.commit()
        post_id = p.id
        before = BlogPost.query.count()

    c = app_module.app.test_client()
    _login(c, admin_id)
    rv = c.post(f'/admin/blog/{post_id}/edit', data=_post_data(slug='draft'), follow_redirects=False)
    assert rv.status_code in (301, 302)

    with app_module.app.app_context():
        assert BlogPost.query.count() == before
        post = BlogPost.query.get(post_id)
        assert post.status == 'published'
        assert post.published_at is not None
        assert post.og_image == '/img.jpg'


def test_edit_published_update_non_500(monkeypatch):
    admin_id = _reset(monkeypatch)
    with app_module.app.app_context():
        p = BlogPost(title='Pub', slug='pub', body_html='<p>x</p>', status='published')
        db.session.add(p)
        db.session.commit()
        post_id = p.id

    c = app_module.app.test_client(); _login(c, admin_id)
    rv = c.post(f'/admin/blog/{post_id}/edit', data=_post_data(slug='pub', title='Pub 2'), follow_redirects=False)
    assert rv.status_code in (301, 302)


def test_changing_slug_to_other_post_slug_handled_safely(monkeypatch):
    admin_id = _reset(monkeypatch)
    with app_module.app.app_context():
        p1 = BlogPost(title='One', slug='one', body_html='<p>x</p>', status='draft')
        p2 = BlogPost(title='Two', slug='two', body_html='<p>x</p>', status='draft')
        db.session.add_all([p1, p2])
        db.session.commit()
        p1_id = p1.id

    c = app_module.app.test_client(); _login(c, admin_id)
    rv = c.post(f'/admin/blog/{p1_id}/edit', data=_post_data(slug='two', title='Two'), follow_redirects=False)
    assert rv.status_code in (301, 302)

    with app_module.app.app_context():
        updated = BlogPost.query.get(p1_id)
        assert updated.slug != 'two'
        assert updated.slug.startswith('two')


def test_new_blog_publish_no_nameerror(monkeypatch):
    admin_id = _reset(monkeypatch)
    c = app_module.app.test_client(); _login(c, admin_id)
    rv = c.post('/admin/blog/new', data=_post_data(title='New Publish', slug='new-publish', action='publish'), follow_redirects=False)
    assert rv.status_code in (301, 302)


def test_new_blog_save_draft_no_nameerror(monkeypatch):
    admin_id = _reset(monkeypatch)
    c = app_module.app.test_client(); _login(c, admin_id)
    rv = c.post('/admin/blog/new', data=_post_data(title='New Draft', slug='new-draft', action='save_draft'), follow_redirects=False)
    assert rv.status_code in (301, 302)


def test_new_blog_helper_failures_do_not_crash(monkeypatch):
    admin_id = _reset(monkeypatch)
    monkeypatch.setattr(app_module, 'analyze_blog_post_seo', lambda **kwargs: (_ for _ in ()).throw(Exception('seo fail')))
    monkeypatch.setattr(app_module, 'analyze_human_quality', lambda **kwargs: (_ for _ in ()).throw(Exception('hq fail')))
    monkeypatch.setattr(app_module, 'suggest_internal_links', lambda **kwargs: (_ for _ in ()).throw(Exception('links fail')))
    monkeypatch.setattr(app_module, 'generate_image_alt_caption', lambda **kwargs: (_ for _ in ()).throw(Exception('img fail')))
    c = app_module.app.test_client(); _login(c, admin_id)
    rv = c.post('/admin/blog/new', data=_post_data(title='Helpers Fail', slug='helpers-fail', action='publish'), follow_redirects=False)
    assert rv.status_code in (301, 302)


def test_edit_blog_helper_failures_do_not_crash(monkeypatch):
    admin_id = _reset(monkeypatch)
    with app_module.app.app_context():
        p = BlogPost(title='Edit Fails', slug='edit-fails', body_html='<p>x</p>', status='draft')
        db.session.add(p)
        db.session.commit()
        post_id = p.id
    monkeypatch.setattr(app_module, 'analyze_blog_post_seo', lambda **kwargs: (_ for _ in ()).throw(Exception('seo fail')))
    monkeypatch.setattr(app_module, 'analyze_human_quality', lambda **kwargs: (_ for _ in ()).throw(Exception('hq fail')))
    monkeypatch.setattr(app_module, 'suggest_internal_links', lambda **kwargs: (_ for _ in ()).throw(Exception('links fail')))
    monkeypatch.setattr(app_module, 'generate_image_alt_caption', lambda **kwargs: (_ for _ in ()).throw(Exception('img fail')))
    c = app_module.app.test_client(); _login(c, admin_id)
    rv = c.post(f'/admin/blog/{post_id}/edit', data=_post_data(slug='edit-fails', title='Edit Fails', action='publish'), follow_redirects=False)
    assert rv.status_code in (301, 302)
