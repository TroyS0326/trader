from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding='utf-8')


def test_signup_success_redirect_flag_added_to_upgrade_route():
    app_py = _read('app.py')
    assert "url_for('upgrade', plan=intended_plan, signup_success='1')" in app_py


def test_signup_success_query_triggers_one_time_google_ads_signup_conversion():
    html = _read('templates/partials/google_ads_conversions.html')
    assert "signup_success" in html
    assert "xeanvi_signup_conversion_fired" in html
    assert "data-google-ads-conversion') return 'signup'" in html


def test_signup_success_query_triggers_one_time_meta_complete_registration():
    html = _read('templates/partials/meta_pixel.html')
    assert "signup_success" in html
    assert "xeanvi_signup_meta_conversion_fired" in html
    assert "fbq('track', 'CompleteRegistration'" in html




def test_paid_landing_and_signup_templates_do_not_fire_premature_completed_signup():
    paid_landing = _read('templates/paid_ads_landing.html')
    signup = _read('templates/signup.html')
    assert 'data-google-ads-conversion="signup"' not in paid_landing
    assert 'data-google-ads-conversion="signup"' not in signup
    assert 'data-meta-pixel-event="CompleteRegistration"' not in signup

def test_pricing_forms_keep_checkout_tracking_attributes_for_both_plans():
    html = _read('templates/upgrade.html')
    assert html.count('data-meta-pixel-event="InitiateCheckout"') >= 2
    assert html.count('data-google-ads-conversion="checkout"') >= 2
    assert 'data-google-ads-value="19.99"' in html
    assert 'data-google-ads-value="199.99"' in html
    assert html.count('data-google-ads-currency="USD"') >= 2


def test_google_ads_checkout_submit_fallback_is_non_blocking():
    html = _read('templates/partials/google_ads_conversions.html')
    assert 'event.preventDefault();' in html
    assert 'window.setTimeout(finishSubmit, 1000);' in html
    assert 'submitFormWithoutTrackingLoop' in html


def test_tracking_templates_do_not_expose_password_or_secrets():
    combined = '\n'.join([
        _read('templates/partials/google_ads_conversions.html').lower(),
        _read('templates/partials/meta_pixel.html').lower(),
        _read('templates/partials/utm_persistence.html').lower(),
    ])
    assert 'password' not in combined
    assert 'stripe_secret' not in combined
    assert 'api_key' not in combined


def test_purchase_browser_tracking_limitation_is_documented():
    doc = _read('docs/paid_ads_launch_plan.md')
    assert 'Conversion Tracking Readiness' in doc
    assert 'browser-side google ads/meta' in doc.lower()
    assert 'purchase' in doc.lower()
    assert 'not' in doc.lower() and 'currently emitted' in doc.lower()
    assert 'dedicated post-payment page' in doc.lower()
