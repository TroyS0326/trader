import os
from pathlib import Path
import sys
from unittest.mock import MagicMock

os.environ['FLASK_ENV'] = 'testing'
os.environ['TESTING'] = '1'
os.environ['DATABASE_URL'] = 'sqlite:////tmp/trader_blog_rhythm_tests.sqlite3'
os.environ['RATELIMIT_STORAGE_URI'] = 'memory://'
for k in ['SECRET_KEY','TOKEN_ENCRYPTION_KEY','ALPACA_CLIENT_ID','ALPACA_CLIENT_SECRET','ALPACA_REDIRECT_URI','FINNHUB_API_KEY','GEMINI_API_KEY']:
    os.environ.setdefault(k, 'test')

sys.path.append(str(Path(__file__).resolve().parents[1]))
import app as app_module
from models import db, User, BlogPost, BlogPublishingPlan


def _mk_user(email):
    u = User(email=email, password_hash='hash')
    db.session.add(u); db.session.commit(); return u

def _login(c, uid):
    with c.session_transaction() as s: s['_user_id'] = str(uid)

def _reset(monkeypatch):
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, RATELIMIT_ENABLED=False)
    monkeypatch.setattr(app_module, 'ADMIN_EMAIL', 'admin@test.com')
    with app_module.app.app_context():
        assert db.engine.url.drivername.startswith('sqlite')
        db.drop_all(); db.create_all()
        admin = _mk_user('admin@test.com')
        user = _mk_user('user@test.com')
        return admin.id, user.id

def test_blog_rhythm_routes(monkeypatch):
    admin_id, user_id = _reset(monkeypatch)
    c = app_module.app.test_client()
    assert c.get('/admin/blog-rhythm').status_code in (301,302)
    _login(c, user_id); assert c.get('/admin/blog-rhythm').status_code == 403
    _login(c, admin_id); assert c.get('/admin/blog-rhythm').status_code == 200
    c.post('/admin/blog-rhythm/add', data={'title':'Topic A'})
    with app_module.app.app_context():
        p = BlogPublishingPlan.query.filter_by(title='Topic A').first(); assert p is not None
    c.post('/admin/blog-rhythm/add', data={'title':''})
    c.post(f'/admin/blog-rhythm/{p.id}/update', data={'status':'queued'})
    with app_module.app.app_context():
        assert db.session.get(BlogPublishingPlan, p.id).status == 'queued'
    c.post(f'/admin/blog-rhythm/{p.id}/update', data={'status':'bad'})
    with app_module.app.app_context():
        assert db.session.get(BlogPublishingPlan, p.id).status == 'queued'
    c.post(f'/admin/blog-rhythm/{p.id}/create-draft')
    with app_module.app.app_context():
        p2 = db.session.get(BlogPublishingPlan, p.id); post = db.session.get(BlogPost, p2.related_blog_post_id)
        assert post.status == 'draft'
        before = BlogPost.query.count()
    c.post(f'/admin/blog-rhythm/{p.id}/create-draft')
    with app_module.app.app_context():
        assert BlogPost.query.count() == before

def test_create_draft_ai_call_uses_supported_kwargs_only(monkeypatch):
    admin_id, _ = _reset(monkeypatch)
    with app_module.app.app_context():
        p = BlogPublishingPlan(
            title='AI Draft Topic',
            target_keyword='keyword',
            search_intent='informational',
            funnel_stage='top',
            content_type='guide',
            notes='Base note',
            status='queued',
        )
        db.session.add(p)
        db.session.commit()
        plan_id = p.id
    monkeypatch.setenv('GEMINI_API_KEY', 'test')
    spy = MagicMock(return_value={'body_html': '<p>Generated</p>'})
    monkeypatch.setattr(app_module, 'generate_blog_draft', spy)
    c = app_module.app.test_client(); _login(c, admin_id)
    rv = c.post(f'/admin/blog-rhythm/{plan_id}/create-draft')
    assert rv.status_code in (301, 302)
    assert spy.call_count == 1
    kwargs = spy.call_args.kwargs
    assert set(kwargs.keys()) == {'title', 'target_keyword', 'notes'}
    assert 'Search intent: informational' in kwargs['notes']
    assert 'Funnel stage: top' in kwargs['notes']
    assert 'Content type: guide' in kwargs['notes']

def test_create_draft_ai_failure_creates_placeholder(monkeypatch):
    admin_id, _ = _reset(monkeypatch)
    with app_module.app.app_context():
        p = BlogPublishingPlan(title='Fallback Draft Topic', status='queued')
        db.session.add(p); db.session.commit(); plan_id = p.id
    monkeypatch.setenv('GEMINI_API_KEY', 'test')
    def _fail(*args, **kwargs):
        raise RuntimeError('ai fail')
    monkeypatch.setattr(app_module, 'generate_blog_draft', _fail)
    c = app_module.app.test_client(); _login(c, admin_id)
    rv = c.post(f'/admin/blog-rhythm/{plan_id}/create-draft', follow_redirects=True)
    assert rv.status_code == 200
    with app_module.app.app_context():
        plan = db.session.get(BlogPublishingPlan, plan_id)
        post = db.session.get(BlogPost, plan.related_blog_post_id)
        assert post.status == 'draft'
        assert 'Draft placeholder' in post.body_html

def test_add_update_invalid_inputs_do_not_500(monkeypatch):
    admin_id, _ = _reset(monkeypatch)
    c = app_module.app.test_client(); _login(c, admin_id)
    rv = c.post('/admin/blog-rhythm/add', data={'title':'Topic B', 'priority':'bad', 'planned_publish_date':'2026-13-99'})
    assert rv.status_code in (301, 302)
    rv = c.post('/admin/blog-rhythm/add', data={'title':'Topic C', 'priority':'1000'})
    assert rv.status_code in (301, 302)
    with app_module.app.app_context():
        p = BlogPublishingPlan.query.filter_by(title='Topic C').first()
        assert p is not None
        assert p.priority == 5
        plan_id = p.id
    rv = c.post(f'/admin/blog-rhythm/{plan_id}/update', data={'priority':'bad', 'planned_publish_date':'not-a-date'})
    assert rv.status_code in (301, 302)

def test_create_draft_never_autopublishes(monkeypatch):
    admin_id, _ = _reset(monkeypatch)
    with app_module.app.app_context():
        p = BlogPublishingPlan(title='No Publish Topic', status='queued')
        db.session.add(p); db.session.commit(); plan_id = p.id
    c = app_module.app.test_client(); _login(c, admin_id)
    c.post(f'/admin/blog-rhythm/{plan_id}/create-draft')
    with app_module.app.app_context():
        plan = db.session.get(BlogPublishingPlan, plan_id)
        post = db.session.get(BlogPost, plan.related_blog_post_id)
        assert post.status == 'draft'
        assert plan.status != 'published'

def test_seed_script_idempotent(monkeypatch):
    _reset(monkeypatch)
    import scripts.seed_blog_rhythm as seed
    with app_module.app.app_context():
        db.session.query(BlogPublishingPlan).delete(); db.session.commit()
        created_1 = seed.seed_blog_rhythm(force=False)
        assert created_1 == 12
    with app_module.app.app_context():
        assert BlogPublishingPlan.query.count() == 12
        created_2 = seed.seed_blog_rhythm(force=False)
        assert created_2 == 0
    with app_module.app.app_context():
        assert BlogPublishingPlan.query.count() == 12

def test_no_secret_exposure(monkeypatch):
    admin_id, _ = _reset(monkeypatch)
    c = app_module.app.test_client(); _login(c, admin_id)
    txt = c.get('/admin/blog-rhythm').get_data(as_text=True)
    assert 'GEMINI_API_KEY' not in txt and 'SECRET_KEY' not in txt
