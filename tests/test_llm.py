from fastapi.testclient import TestClient

from planner import llm, webapi


def test_no_key_means_no_provider(monkeypatch):
    for k in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm.db, "get_secret", lambda n, d=None: None)
    assert llm.provider() is None
    assert llm.ask({"cover_weeks": 3.4}, "why?")["ok"] is False


def test_gemini_preferred_and_answer_must_use_engine_numbers(monkeypatch):
    monkeypatch.setattr(llm.db, "get_secret", lambda n, d=None: "k" if n in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY") else None)
    assert llm.provider() == "gemini"
    facts = {"cover_weeks": 3.35, "min_weeks": 6, "recommended_qty": 1500}
    monkeypatch.setattr(llm, "complete", lambda s, u, m=500: "Cover is 3.4 weeks against a 6-week minimum, so the engine suggests 1,500 units.")
    assert llm.ask(facts, "why?")["ok"] is True
    monkeypatch.setattr(llm, "complete", lambda s, u, m=500: "Order 2000 units, cover will be 9 weeks.")
    r = llm.ask(facts, "why?")
    assert r["ok"] is False and r["source"] == "blocked"
    monkeypatch.setattr(llm, "complete", lambda s, u, m=500: None)
    assert llm.ask(facts, "why?")["source"] == "error"


def test_ask_endpoint_sends_only_engine_facts(monkeypatch):
    monkeypatch.setenv("STORAGE", "csv")
    monkeypatch.setattr(llm.db, "get_secret", lambda n, d=None: "csv" if n == "STORAGE" else ("k" if n == "GEMINI_API_KEY" else None))
    seen = {}
    monkeypatch.setattr(llm, "complete", lambda s, u, m=500: seen.update(system=s, user=u) or "Cover is 3.4 weeks.")
    webapi.bust()
    r = TestClient(webapi.app).post("/api/ask", json={"sku": "SKU001", "question": "Why is this flagged?"}).json()
    assert r["ok"] is True
    assert "STOCKOUT_RISK" in seen["user"]
    assert "must not change" in seen["system"]


def test_openrouter_request_is_google_only_and_never_logs_key(monkeypatch):
    import io, json
    seen = {}

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        seen["url"], seen["headers"] = req.full_url, dict(req.header_items())
        if req.full_url.endswith("/models"):
            return R(json.dumps({"data": [{"id": "google/gemini-3.7-flash"}]}).encode())
        seen["body"] = json.loads(req.data)
        return R(json.dumps({"choices": [{"message": {"content": "Cover is 3.4 weeks."}}]}).encode())

    monkeypatch.setattr(llm.db, "get_secret", lambda n, d=None: {"OPENROUTER_API_KEY": "sk-or-v1-test", "OPENROUTER_MODEL": "openai/gpt-4o"}.get(n, d))
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    llm._model_cache.clear()
    assert llm.provider() == "openrouter"
    assert llm.complete("sys", "user") == "Cover is 3.4 weeks."
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["body"]["model"].startswith("google/")  # a non-Google model setting is ignored
    assert seen["body"]["provider"] == {"only": ["google-vertex", "google-ai-studio"], "allow_fallbacks": False}
    assert "sk-or-v1-test" not in json.dumps(seen["body"])


def test_missing_model_is_swapped_for_closest_google_flash(monkeypatch):
    import io, json

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(llm.db, "get_secret", lambda n, d=None: {"OPENROUTER_API_KEY": "k"}.get(n, d))
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout=0: R(json.dumps({"data": [
        {"id": "openai/gpt-5"}, {"id": "google/gemini-3.8-flash"}, {"id": "google/gemini-3.1-flash-image"}, {"id": "google/gemma-4-31b-it:free"}]}).encode()))
    llm._model_cache.clear()
    assert llm.openrouter_model() == "google/gemini-3.8-flash"
