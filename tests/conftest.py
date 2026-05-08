import os
import socket
from pathlib import Path

import pytest

PROD_DB_PATH = "/var/www/stock/trader/stock/veteran_trades.db"


def _is_prod_db_path(path_value: str | None) -> bool:
    if not path_value:
        return False
    return Path(path_value).resolve() == Path(PROD_DB_PATH).resolve()


if _is_prod_db_path(os.getenv("DB_PATH")) or _is_prod_db_path(os.getenv("XEANVI_TEST_DB_PATH")):
    raise RuntimeError("Refusing to run tests against production DB.")

TEST_DB_DEFAULT = os.getenv("XEANVI_TEST_DB_PATH", "/tmp/xeanvi-test.db")
os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DB_PATH", TEST_DB_DEFAULT)
os.environ.setdefault("XEANVI_TEST_DB_PATH", TEST_DB_DEFAULT)
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "test-token-encryption-key")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("RATELIMIT_STORAGE_URI", "memory://")
os.environ.setdefault("STRIPE_PUBLIC_KEY", "pk_test_dummy")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")
os.environ.setdefault("STRIPE_PRICE_ID_MONTHLY", "price_test_monthly")
os.environ.setdefault("STRIPE_PRICE_ID_ANNUAL", "price_test_annual")
os.environ.setdefault("BREVO_API_KEY", "brevo_test_dummy")
os.environ.setdefault("BREVO_RESET_PASSWORD_TEMPLATE_ID", "1")
os.environ.setdefault("BREVO_SIGNUP_LIST_ID", "1")
os.environ.setdefault("ALPACA_CLIENT_ID", "alpaca_test_id")
os.environ.setdefault("ALPACA_CLIENT_SECRET", "alpaca_test_secret")
os.environ.setdefault("ALPACA_REDIRECT_URI", "https://example.com/alpaca/callback")
os.environ.setdefault("ALPACA_API_KEY", "alpaca_api_key")
os.environ.setdefault("ALPACA_API_SECRET", "alpaca_api_secret")
os.environ.setdefault("FINNHUB_API_KEY", "finnhub_test_key")
os.environ.setdefault("GEMINI_API_KEY", "gemini_test_key")

@pytest.fixture(scope="session", autouse=True)
def _test_safety_env(tmp_path_factory: pytest.TempPathFactory) -> None:
    tmp_db = tmp_path_factory.mktemp("db") / "xeanvi-test.db"
    os.environ["FLASK_ENV"] = "testing"
    os.environ["TESTING"] = "1"
    os.environ["DB_PATH"] = str(tmp_db)
    os.environ["XEANVI_TEST_DB_PATH"] = str(tmp_db)



@pytest.fixture(scope="session", autouse=True)
def _block_network_calls() -> None:
    def _deny_network(*args, **kwargs):
        raise RuntimeError("External network calls are disabled during tests.")

    mp = pytest.MonkeyPatch()
    mp.setattr(socket, "create_connection", _deny_network)
    yield
    mp.undo()


@pytest.fixture(scope="session", autouse=True)
def _runtime_production_db_guard() -> None:
    import config
    if _is_prod_db_path(getattr(config, "DB_PATH", None)):
        raise RuntimeError("Refusing to run tests against production DB.")

    import app as app_module
    uri = str(app_module.app.config.get("SQLALCHEMY_DATABASE_URI", ""))
    if PROD_DB_PATH in uri:
        raise RuntimeError("Refusing to run tests against production DB.")
