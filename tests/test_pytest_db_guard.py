import os
import subprocess
import sys
from pathlib import Path


def test_pytest_guard_rejects_production_db_path(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(repo_root),
            "DB_PATH": "/var/www/stock/trader/stock/veteran_trades.db",
            "XEANVI_TEST_DB_PATH": "/var/www/stock/trader/stock/veteran_trades.db",
            "FLASK_ENV": "testing",
            "TESTING": "1",
            "SECRET_KEY": "test",
            "TOKEN_ENCRYPTION_KEY": "test",
            "ALPACA_CLIENT_ID": "test",
            "ALPACA_CLIENT_SECRET": "test",
            "ALPACA_REDIRECT_URI": "https://example.com/callback",
            "FINNHUB_API_KEY": "test",
            "GEMINI_API_KEY": "test",
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_sitemap.py"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Refusing to run tests against production DB." in (result.stdout + result.stderr)
