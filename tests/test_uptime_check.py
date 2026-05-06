import requests

from scripts import uptime_check


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, json_exc=None):
        self.status_code = status_code
        self._json_data = json_data
        self._json_exc = json_exc

    def json(self):
        if self._json_exc:
            raise self._json_exc
        return self._json_data


def test_all_endpoints_ok_returns_zero(monkeypatch):
    monkeypatch.setenv("UPTIME_BASE_URL", "https://xeanvi.com")
    monkeypatch.setenv("UPTIME_CHECK_PATHS", "/healthz,/readyz")

    def fake_get(url, timeout):
        return FakeResponse(status_code=200, json_data={"ok": True})

    monkeypatch.setattr(uptime_check.requests, "get", fake_get)
    assert uptime_check.main([]) == 0


def test_non_200_returns_one(monkeypatch):
    monkeypatch.setenv("UPTIME_CHECK_PATHS", "/healthz")
    monkeypatch.setattr(uptime_check.requests, "get", lambda *args, **kwargs: FakeResponse(status_code=503, json_data={"ok": False}))
    assert uptime_check.main([]) == 1


def test_timeout_or_connection_exception_returns_one(monkeypatch):
    monkeypatch.setenv("UPTIME_CHECK_PATHS", "/readyz")

    def raise_exc(*args, **kwargs):
        raise requests.Timeout("timeout")

    monkeypatch.setattr(uptime_check.requests, "get", raise_exc)
    assert uptime_check.main([]) == 1


def test_ok_false_returns_one(monkeypatch):
    monkeypatch.setenv("UPTIME_CHECK_PATHS", "/healthz")
    monkeypatch.setattr(uptime_check.requests, "get", lambda *args, **kwargs: FakeResponse(status_code=200, json_data={"ok": False}))
    assert uptime_check.main([]) == 1


def test_success_webhook_called_on_success(monkeypatch):
    monkeypatch.setenv("UPTIME_CHECK_PATHS", "/healthz")
    monkeypatch.setenv("UPTIME_SUCCESS_WEBHOOK_URL", "https://example.com/success")
    calls = []

    monkeypatch.setattr(uptime_check.requests, "get", lambda *args, **kwargs: FakeResponse(status_code=200, json_data={"ok": True}))
    monkeypatch.setattr(uptime_check.requests, "post", lambda url, json, timeout: calls.append((url, json)) or FakeResponse(status_code=200))

    assert uptime_check.main([]) == 0
    assert len(calls) == 1
    assert calls[0][0] == "https://example.com/success"


def test_failure_webhook_called_on_failure(monkeypatch):
    monkeypatch.setenv("UPTIME_CHECK_PATHS", "/healthz")
    monkeypatch.setenv("UPTIME_FAIL_WEBHOOK_URL", "https://example.com/fail")
    calls = []

    monkeypatch.setattr(uptime_check.requests, "get", lambda *args, **kwargs: FakeResponse(status_code=503, json_data={"ok": False}))
    monkeypatch.setattr(uptime_check.requests, "post", lambda url, json, timeout: calls.append((url, json)) or FakeResponse(status_code=200))

    assert uptime_check.main([]) == 1
    assert len(calls) == 1
    assert calls[0][0] == "https://example.com/fail"


def test_webhook_failure_does_not_change_result(monkeypatch):
    monkeypatch.setenv("UPTIME_CHECK_PATHS", "/healthz")
    monkeypatch.setenv("UPTIME_SUCCESS_WEBHOOK_URL", "https://example.com/success")

    monkeypatch.setattr(uptime_check.requests, "get", lambda *args, **kwargs: FakeResponse(status_code=200, json_data={"ok": True}))

    def post_fails(*args, **kwargs):
        raise requests.ConnectionError("failed")

    monkeypatch.setattr(uptime_check.requests, "post", post_fails)
    assert uptime_check.main([]) == 0


def test_uptime_check_paths_parsing_handles_spaces_and_commas():
    paths = uptime_check.parse_paths(" /healthz, /readyz , ,/ ")
    assert paths == ["/healthz", "/readyz", "/"]


def test_does_not_print_webhook_urls(monkeypatch, capsys):
    monkeypatch.setenv("UPTIME_CHECK_PATHS", "/healthz")
    monkeypatch.setenv("UPTIME_SUCCESS_WEBHOOK_URL", "https://secret.example.com/hook")
    monkeypatch.setattr(uptime_check.requests, "get", lambda *args, **kwargs: FakeResponse(status_code=200, json_data={"ok": True}))
    monkeypatch.setattr(uptime_check.requests, "post", lambda *args, **kwargs: FakeResponse(status_code=200))

    assert uptime_check.main([]) == 0
    out = capsys.readouterr().out
    assert "secret.example.com" not in out
