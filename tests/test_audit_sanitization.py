"""Testa a integração de log_sanitizer em server._audit_event e utils.log.

Nenhum dos dois pode ser importado em ambiente offline (Flask para um,
cryptography+config para o outro). Aqui replicamos a lógica integrada
e travamos o contrato: segredos NUNCA devem aparecer íntegros na saída.
"""

import json

from log_sanitizer import sanitize as _sanitize
from log_sanitizer import sanitize_mapping as _sanitize_audit_payload


# ─────────────────────────────────────────────────────────────
# Réplica offline de server._audit_event (sem Flask/request)
# ─────────────────────────────────────────────────────────────
def _audit_event_offline(event_type: str, recorded=None, **extra):
    """Cópia da lógica em server._audit_event após integração do
    sanitizador. A lista `recorded` recebe a string final emitida
    pelo `log(...)` — equivalente ao que iria para disco.
    """
    payload = {
        "event": event_type,
        "ts": 1700000000,
        "ip": "10.0.0.1",
        "method": "GET",
        "path": "/api/foo",
    }
    payload.update(extra or {})
    safe_payload = _sanitize_audit_payload(payload)
    try:
        out = f"[SECURITY_AUDIT] {json.dumps(safe_payload, ensure_ascii=False)}"
    except Exception:
        out = f"[SECURITY_AUDIT] {safe_payload}"
    if recorded is not None:
        recorded.append(out)
    return out


# ─────────────────────────────────────────────────────────────
# Réplica offline de utils.log após integração
# ─────────────────────────────────────────────────────────────
def _utils_log_offline(source: str, msg):
    """Reproduz o que `utils.log(source, msg)` passa ao handler
    de logging após a integração."""
    safe_msg = _sanitize(str(msg))
    return f"[{source}] {safe_msg}"


class TestAuditEventSanitization:
    def test_api_key_in_extra_is_masked(self):
        out = _audit_event_offline(
            "login_attempt",
            api_key="CVAPI_2b9c80c2abf94a76baf8b3e68d89cb7e",
        )
        assert "2b9c80c2abf94a76" not in out
        assert "CVAPI_2b9c" in out

    def test_bearer_in_authorization_header_masked(self):
        out = _audit_event_offline(
            "upstream_call",
            headers="Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.ABCDEFG",
        )
        assert "eyJhbGciOiJIUzI1NiJ9.ABCDEFG" not in out

    def test_profile_path_user_masked(self):
        out = _audit_event_offline(
            "profile_open",
            path_env=r"C:\Users\analyst\AppData\Local\Chromium",
        )
        assert "analyst" not in out
        assert r"C:\\Users\\***" in out or "C:\\\\Users\\\\***" in out

    def test_session_cookie_in_headers_masked(self):
        out = _audit_event_offline(
            "health_check",
            cookie="session=ABCDEFGH12345678; other=ok",
        )
        assert "ABCDEFGH12345678" not in out
        assert "other=ok" in out

    def test_non_sensitive_fields_preserved(self):
        out = _audit_event_offline(
            "ok",
            duration_ms=42,
            queue_size=3,
        )
        # Valores numéricos e campos base seguem intactos.
        assert '"duration_ms": 42' in out
        assert '"queue_size": 3' in out
        assert '"event": "ok"' in out

    def test_output_is_valid_json_after_prefix(self):
        out = _audit_event_offline("check", api_key="CVAPI_exampletokenabc123")
        json_part = out.split("[SECURITY_AUDIT] ", 1)[1]
        parsed = json.loads(json_part)
        assert parsed["event"] == "check"
        assert "exampletokenabc123" not in parsed.get("api_key", "")


class TestUtilsLogSanitization:
    def test_api_key_masked_in_log_message(self):
        out = _utils_log_offline("server.py", "cliente usou api_key=topsecret1234567890")
        assert "topsecret1234567890" not in out
        assert "[server.py]" in out

    def test_bearer_masked_in_log_message(self):
        out = _utils_log_offline(
            "browser.py",
            "request com Authorization: Bearer abcdefghijklmnop",
        )
        assert "abcdefghijklmnop" not in out

    def test_home_path_masked(self):
        out = _utils_log_offline(
            "browser.py",
            "launching chromium user_data_dir=/home/analyst/.config/chromium-profile",
        )
        assert "analyst" not in out
        assert "/home/***" in out

    def test_non_sensitive_message_unchanged(self):
        msg = "queue depth now 3 items"
        out = _utils_log_offline("server.py", msg)
        assert msg in out

    def test_numeric_message_coerced_to_string(self):
        # Prova que a conversão str(msg) preserva o comportamento original.
        assert "[server.py] 42" == _utils_log_offline("server.py", 42)
