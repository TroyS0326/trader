import argparse
import os
import re
import sys
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
PROD_ENV_PATH = '/etc/xeanvi/xeanvi.env'
if os.path.exists(PROD_ENV_PATH):
    load_dotenv(PROD_ENV_PATH)
else:
    load_dotenv(BASE_DIR / '.env')

_PLACEHOLDER_PATTERNS = [
    'example', 'placeholder', 'replace', 'change', 'your_', 'test_key',
    'pk_live_or_test_key', 'sk_live_or_test_key', 'price_monthly_id',
    'price_annual_id', 'whsec_your', 'secure-value',
]


def _value(name: str, default: str = '') -> str:
    return (os.getenv(name, default) or '').strip()


def _is_placeholder(value: str) -> bool:
    if not value:
        return True
    lowered = value.lower()
    return any(token in lowered for token in _PLACEHOLDER_PATTERNS)


def validate_required_production_config(strict: bool = False) -> list[str]:
    if not strict:
        return []
    errors: list[str] = []
    required = [
        'SECRET_KEY','TOKEN_ENCRYPTION_KEY','REDIS_URL','RATELIMIT_STORAGE_URI','STRIPE_PUBLIC_KEY',
        'STRIPE_SECRET_KEY','STRIPE_WEBHOOK_SECRET','STRIPE_PRICE_ID_MONTHLY','STRIPE_PRICE_ID_ANNUAL',
        'BREVO_API_KEY','BREVO_SENDER_EMAIL','ALPACA_CLIENT_ID','ALPACA_CLIENT_SECRET','FINNHUB_API_KEY','GEMINI_API_KEY'
    ]
    for key in required:
        if _is_placeholder(_value(key)):
            errors.append(f'{key} is missing or placeholder.')

    if _value('FLASK_DEBUG', '0') != '0':
        errors.append('FLASK_DEBUG must be 0 in production.')
    if _value('FLASK_ENV').lower() != 'production':
        errors.append('FLASK_ENV must be production.')
    if not _value('APP_BASE_URL', 'https://xeanvi.com').startswith('https://'):
        errors.append('APP_BASE_URL must start with https://.')
    if _value('SESSION_COOKIE_SECURE', '1') != '1':
        errors.append('SESSION_COOKIE_SECURE must be 1.')
    origins = [o.strip() for o in _value('WTF_CSRF_TRUSTED_ORIGINS', 'https://xeanvi.com,https://www.xeanvi.com').split(',') if o.strip()]
    if 'https://xeanvi.com' not in origins or 'https://www.xeanvi.com' not in origins:
        errors.append('WTF_CSRF_TRUSTED_ORIGINS must include https://xeanvi.com and https://www.xeanvi.com.')
    if not _value('ALPACA_REDIRECT_URI').startswith('https://'):
        errors.append('ALPACA_REDIRECT_URI must use https.')

    template_id = _value('BREVO_RESET_PASSWORD_TEMPLATE_ID')
    if not template_id or not template_id.isdigit():
        errors.append('BREVO_RESET_PASSWORD_TEMPLATE_ID is missing or must be numeric.')

    signup_optional = _value('BREVO_SIGNUP_SYNC_OPTIONAL', '0') == '1'
    signup_list = _value('BREVO_SIGNUP_LIST_ID')
    if not signup_optional:
        if not signup_list.isdigit() or int(signup_list) <= 0:
            errors.append('BREVO_SIGNUP_LIST_ID must be a positive integer unless BREVO_SIGNUP_SYNC_OPTIONAL=1.')

    return errors


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args()
    errors = validate_required_production_config(strict=args.strict)
    if errors:
        for e in errors:
            print(f'ERROR: {e}')
        sys.exit(1)
    print('Production config validation passed.')
