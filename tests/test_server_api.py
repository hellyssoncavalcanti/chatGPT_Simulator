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
