import json
import os

import pytest

import server


def _reset_security_state():
    with server._security_lock:
        server._rate_limit_hits.clear()
        server._failed_login_attempts.clear()
        server._blocked_ips.clear()


def test_health_and_metrics_with_api_key():
    _reset_security_state()
    c = server.app.test_client()

    r1 = c.get("/health")
    assert r1.status_code == 200

    r2 = c.get("/api/metrics", query_string={"api_key": server.config.API_KEY})
    assert r2.status_code == 200
    payload = r2.get_json()
    assert payload["success"] is True
    assert "metrics" in payload


def test_login_bruteforce_blocks_ip(monkeypatch):
    _reset_security_state()
    c = server.app.test_client()

    monkeypatch.setattr(server, "LOGIN_MAX_FAILS", 2, raising=True)
    monkeypatch.setattr(server, "LOGIN_BLOCK_SEC", 120, raising=True)
    monkeypatch.setattr(server.auth, "verify_login", lambda u, p: None, raising=True)

    assert c.post("/login", json={"username": "x", "password": "y"}).status_code == 401
    assert c.post("/login", json={"username": "x", "password": "y"}).status_code == 401
    blocked = c.post("/login", json={"username": "x", "password": "y"})
    assert blocked.status_code == 429


def test_logs_tail_endpoint(monkeypatch, tmp_path):
    _reset_security_state()
    c = server.app.test_client()

    log_file = tmp_path / "simulator.log"
    log_file.write_text("line1\nline2\nline3\n", encoding="utf-8")
    monkeypatch.setattr(server.config, "LOG_PATH", str(log_file), raising=True)

    r = c.get("/api/logs/tail", query_string={"api_key": server.config.API_KEY, "lines": 2})
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["success"] is True
    assert payload["lines"][-2:] == ["line2", "line3"]


# ---------------------------------------------------------------------------
# MockProvider tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=False)
def mock_provider_env(monkeypatch):
    monkeypatch.setenv("SIMULATOR_LLM_PROVIDER", "mock")


def _iter_ndjson(data: bytes):
    for line in data.splitlines():
        line = line.strip()
        if line:
            yield json.loads(line)


def test_chat_completions_streaming_mock(monkeypatch, mock_provider_env):
    """MockProvider deve emitir status → markdown → finish sem precisar de browser."""
    _reset_security_state()
    c = server.app.test_client()

    resp = c.post(
        "/v1/chat/completions",
        json={"message": "olá", "stream": True},
        query_string={"api_key": server.config.API_KEY},
    )
    assert resp.status_code == 200

    events = list(_iter_ndjson(resp.data))
    types = [e.get("type") for e in events]

    assert "markdown" in types, f"esperado evento 'markdown', recebido: {types}"
    assert "finish" in types, f"esperado evento 'finish', recebido: {types}"

    markdown_evt = next(e for e in events if e.get("type") == "markdown")
    assert markdown_evt["content"] == "MOCK"


def test_chat_completions_block_mock(monkeypatch, mock_provider_env):
    """Modo bloco deve retornar JSON com success=True e html=MOCK."""
    _reset_security_state()
    c = server.app.test_client()

    resp = c.post(
        "/v1/chat/completions",
        json={"message": "olá", "stream": False},
        query_string={"api_key": server.config.API_KEY},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload.get("success") is True
    assert payload.get("html") == "MOCK"
