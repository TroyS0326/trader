import os
for k in ["SECRET_KEY","TOKEN_ENCRYPTION_KEY","ALPACA_CLIENT_ID","ALPACA_CLIENT_SECRET","ALPACA_REDIRECT_URI","FINNHUB_API_KEY","GEMINI_API_KEY"]:
    os.environ.setdefault(k, "test")

from pathlib import Path
import sys
sys.path.append(str(Path(__file__).resolve().parents[1]))
import app as app_module


def test_sitemap_uses_public_allowlist_only():
    with app_module.app.test_request_context('/sitemap.xml'):
        response = app_module.sitemap_xml()
        xml = response.get_data(as_text=True)

    # excluded/private/noindex/api pages
    for forbidden in [
        '/learn',
        '/login',
        '/billing',
        '/api/admin/conversion-summary',
    ]:
        assert forbidden not in xml

    # public indexable pages
    for allowed in [
        '/features',
        '/pricing',
        '/playbook',
        '/broker-integration',
        '/transparency',
    ]:
        assert f'https://xeanvi.com{allowed}' in xml
