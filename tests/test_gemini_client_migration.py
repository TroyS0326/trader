import sys
import types


def test_module_import_does_not_init_client():
    import gemini_client

    gemini_client._client = None
    assert gemini_client._client is None


def test_generate_text_uses_google_genai_client(monkeypatch):
    import gemini_client

    gemini_client._client = None

    calls = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            calls["kwargs"] = kwargs
            return types.SimpleNamespace(text="hello")

    class FakeClient:
        def __init__(self, api_key=None):
            calls["api_key"] = api_key
            self.models = FakeModels()

    fake_genai = types.SimpleNamespace(Client=FakeClient)
    monkeypatch.setitem(sys.modules, "google", types.SimpleNamespace(genai=fake_genai))
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")

    out = gemini_client.generate_text("prompt", temperature=0.2, max_output_tokens=42)

    assert out == "hello"
    assert calls["api_key"] == "k"
    assert calls["kwargs"]["model"] == "gemini-2.5-flash"
    assert calls["kwargs"]["contents"] == "prompt"
    assert calls["kwargs"]["config"]["temperature"] == 0.2
    assert calls["kwargs"]["config"]["max_output_tokens"] == 42


def test_generate_text_does_not_pass_unsupported_timeout(monkeypatch):
    import gemini_client

    gemini_client._client = None
    gemini_client._generate_content_supports_timeout = None

    calls = {}

    class FakeModels:
        def generate_content(self, model, contents, config=None):
            calls["model"] = model
            calls["contents"] = contents
            calls["config"] = config
            return types.SimpleNamespace(text="ok")

    class FakeClient:
        def __init__(self, api_key=None):
            self.models = FakeModels()

    fake_genai = types.SimpleNamespace(Client=FakeClient)
    monkeypatch.setitem(sys.modules, "google", types.SimpleNamespace(genai=fake_genai))

    out = gemini_client.generate_text(
        "prompt",
        model="gemini-2.0-flash",
        temperature=0.2,
        response_mime_type="application/json",
        system_instruction="system",
        timeout=10.0,
    )

    assert out == "ok"
    assert calls["model"] == "gemini-2.0-flash"
    assert calls["contents"] == "prompt"
    assert calls["config"]["temperature"] == 0.2
    assert calls["config"]["response_mime_type"] == "application/json"
    assert calls["config"]["system_instruction"] == "system"


def test_wrapper_has_no_google_generativeai_imports():
    with open("gemini_client.py", "r", encoding="utf-8") as f:
        assert "google.generativeai" not in f.read()
