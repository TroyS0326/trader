import os
from pathlib import Path
import sys

os.environ['FLASK_ENV'] = 'testing'
os.environ['TESTING'] = '1'
os.environ['DATABASE_URL'] = 'sqlite:////tmp/trader_blog_rhythm_tests.sqlite3'
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
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    monkeypatch.setattr(app_module, 'ADMIN_EMAIL', 'admin@test.com')
    with app_module.app.app_context():
        assert db.engine.url.drivername.startswith('sqlite')
        db.drop_all(); db.create_all()
        return _mk_user('admin@test.com'), _mk_user('user@test.com')

def test_blog_rhythm_routes(monkeypatch):
    admin, user = _reset(monkeypatch)
    c = app_module.app.test_client()
    assert c.get('/admin/blog-rhythm').status_code in (301,302)
    _login(c, user.id); assert c.get('/admin/blog-rhythm').status_code == 403
    _login(c, admin.id); assert c.get('/admin/blog-rhythm').status_code == 200
    c.post('/admin/blog-rhythm/add', data={'title':'Topic A'})
    with app_module.app.app_context():
        p = BlogPublishingPlan.query.filter_by(title='Topic A').first(); assert p is not None
    c.post('/admin/blog-rhythm/add', data={'title':''})
    c.post(f'/admin/blog-rhythm/{p.id}/update', data={'status':'queued'})
    with app_module.app.app_context():
        assert BlogPublishingPlan.query.get(p.id).status == 'queued'
    c.post(f'/admin/blog-rhythm/{p.id}/update', data={'status':'bad'})
    with app_module.app.app_context():
        assert BlogPublishingPlan.query.get(p.id).status == 'queued'
    c.post(f'/admin/blog-rhythm/{p.id}/create-draft')
    with app_module.app.app_context():
        p2 = BlogPublishingPlan.query.get(p.id); post = BlogPost.query.get(p2.related_blog_post_id)
        assert post.status == 'draft'
        before = BlogPost.query.count()
    c.post(f'/admin/blog-rhythm/{p.id}/create-draft')
    with app_module.app.app_context():
        assert BlogPost.query.count() == before

def test_seed_script_idempotent(monkeypatch):
    _reset(monkeypatch)
    import scripts.seed_blog_rhythm as seed
    with app_module.app.app_context():
        db.session.query(BlogPublishingPlan).delete(); db.session.commit()
    os.system('python scripts/seed_blog_rhythm.py')
    with app_module.app.app_context():
        assert BlogPublishingPlan.query.count() == 12
    os.system('python scripts/seed_blog_rhythm.py')
    with app_module.app.app_context():
        assert BlogPublishingPlan.query.count() == 12

def test_no_secret_exposure(monkeypatch):
    admin, _ = _reset(monkeypatch)
    c = app_module.app.test_client(); _login(c, admin.id)
    txt = c.get('/admin/blog-rhythm').get_data(as_text=True)
    assert 'GEMINI_API_KEY' not in txt and 'SECRET_KEY' not in txt
