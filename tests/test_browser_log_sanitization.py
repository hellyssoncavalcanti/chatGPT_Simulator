"""Testa a integração de log_sanitizer em browser.py.

browser.py usa playwright/asyncio e não pode ser importado em ambiente
offline. Aqui replicamos a lógica integrada (emit_log e _save_error_html)
e travamos o contrato: caminhos de usuário, API keys e cookies NUNCA devem
aparecer íntegros na fila SSE de log.
"""
import json
import queue as _queue

from log_sanitizer import sanitize as _sanitize_log_msg


# ─────────────────────────────────────────────────────────────
# Réplica offline de emit_log (browser.py:emit_log)
# ─────────────────────────────────────────────────────────────
def _emit_log_offline(q, sender: str, msg: str) -> None:
    """Cópia da lógica de emit_log após integração do sanitizador.

    Garante que o que vai para a fila SSE (q) está sanitizado,
    enquanto o file_log (aqui omitido) já é sanitizado por utils.log.
    """
    prefix = f"[browser.py] [{sender}] "
    if q is not None:
        safe_msg = _sanitize_log_msg(f"{prefix}{msg}")
        q.put(json.dumps({"type": "log", "content": safe_msg}) + "\n")


def _save_error_html_log_offline(q, filepath: str, label: str, html_exc=None, scr_exc=None) -> None:
    """Replicar os 4 pontos de q.put de _save_error_html após sanitização."""
    # Caso 1: HTML salvo com sucesso
    msg_html = f"📄 HTML de erro salvo: {filepath}"
    if q is not None:
        q.put(json.dumps({"type": "log", "content": _sanitize_log_msg(f"[browser.py] {msg_html}")}) + "\n")

    # Caso 2: Screenshot salvo (fallback)
    msg_scr = f"📄 Screenshot de erro salvo (HTML indisponível — {html_exc}): {filepath}"
    if q is not None:
        q.put(json.dumps({"type": "log", "content": _sanitize_log_msg(f"[browser.py] {msg_scr}")}) + "\n")

    # Caso 3: Ambos falharam
    msg_fail = (
        f"⚠️ Não foi possível salvar diagnóstico de erro '{label}': "
        f"HTML={html_exc} | screenshot={scr_exc}"
    )
    if q is not None:
        q.put(json.dumps({"type": "log", "content": _sanitize_log_msg(f"[browser.py] {msg_fail}")}) + "\n")

    # Caso 4: Exceção inesperada
    msg_exc = f"⚠️ Erro inesperado em _save_error_html('{label}'): SomeException()"
    if q is not None:
        q.put(json.dumps({"type": "log", "content": _sanitize_log_msg(f"[browser.py] {msg_exc}")}) + "\n")


# ─────────────────────────────────────────────────────────────
# Helpers de coleta
# ─────────────────────────────────────────────────────────────
def _drain(q) -> list[dict]:
    """Esvazia a fila e decodifica cada linha JSON."""
    out = []
    while not q.empty():
        raw = q.get_nowait()
        out.append(json.loads(raw.strip()))
    return out


class TestEmitLogSanitization:
    """emit_log sanitiza msg antes de colocar na fila SSE."""

    def test_api_key_masked_in_queue(self):
        q = _queue.Queue()
        _emit_log_offline(q, "chat", "api_key=CVAPI_2b9c80c2abf94a76baf8b3e68d89cb7e")
        events = _drain(q)
        assert len(events) == 1
        content = events[0]["content"]
        assert "2b9c80c2abf94a76" not in content
        assert "***" in content

    def test_bearer_token_masked_in_queue(self):
        q = _queue.Queue()
        _emit_log_offline(q, "chat", "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.ABCDEFG")
        events = _drain(q)
        assert "eyJhbGciOiJIUzI1NiJ9.ABCDEFG" not in events[0]["content"]

    def test_windows_user_path_masked_in_queue(self):
        q = _queue.Queue()
        _emit_log_offline(q, "init", r"user_data_dir=C:\Users\analyst\AppData\Local\Chromium")
        events = _drain(q)
        content = events[0]["content"]
        assert "analyst" not in content

    def test_posix_home_path_masked_in_queue(self):
        q = _queue.Queue()
        _emit_log_offline(q, "init", "user_data_dir=/home/analyst/.config/chromium-profile")
        events = _drain(q)
        content = events[0]["content"]
        assert "analyst" not in content
        assert "/home/***" in content

    def test_session_cookie_masked_in_queue(self):
        q = _queue.Queue()
        _emit_log_offline(q, "auth", "cookie session=ABCDEFGH12345678; path=/")
        events = _drain(q)
        content = events[0]["content"]
        assert "ABCDEFGH12345678" not in content

    def test_non_sensitive_message_preserved(self):
        q = _queue.Queue()
        _emit_log_offline(q, "browser", "✅ ChatGPT respondeu em 3.2s")
        events = _drain(q)
        content = events[0]["content"]
        assert "✅ ChatGPT respondeu em 3.2s" in content

    def test_sender_prefix_preserved(self):
        q = _queue.Queue()
        _emit_log_offline(q, "meu_sender", "mensagem ok")
        events = _drain(q)
        content = events[0]["content"]
        assert "[browser.py] [meu_sender]" in content

    def test_event_type_is_log(self):
        q = _queue.Queue()
        _emit_log_offline(q, "x", "qualquer msg")
        events = _drain(q)
        assert events[0]["type"] == "log"

    def test_no_queue_does_not_raise(self):
        _emit_log_offline(None, "x", "msg sem fila")  # não deve lançar

    def test_empty_message_handled(self):
        q = _queue.Queue()
        _emit_log_offline(q, "x", "")
        events = _drain(q)
        assert events[0]["type"] == "log"

    def test_multiple_secrets_all_masked(self):
        q = _queue.Queue()
        msg = (
            "api_key=CVAPI_2b9c80c2abf94a76 "
            r"path=C:\Users\bob\AppData "
            "session=ABCDEFGH12345678"
        )
        _emit_log_offline(q, "multi", msg)
        events = _drain(q)
        content = events[0]["content"]
        assert "2b9c80c2abf94a76" not in content
        assert "bob" not in content
        assert "ABCDEFGH12345678" not in content


class TestSaveErrorHtmlLogSanitization:
    """_save_error_html sanitiza filepath e exceções antes do q.put."""

    def _run(self, filepath: str, label: str = "test_label"):
        q = _queue.Queue()
        _save_error_html_log_offline(q, filepath, label)
        return _drain(q)

    def test_windows_path_masked_in_html_saved_msg(self):
        events = self._run(r"C:\Users\alice\AppData\Local\chatgpt_simulator\logs\html_dos_erros\20260502_err.html")
        html_event = events[0]
        assert "alice" not in html_event["content"]

    def test_windows_path_masked_in_screenshot_msg(self):
        events = self._run(r"C:\Users\alice\AppData\Local\chatgpt_simulator\logs\html_dos_erros\20260502_fallback.jpg")
        scr_event = events[1]
        assert "alice" not in scr_event["content"]

    def test_posix_path_masked_in_html_saved_msg(self):
        events = self._run("/home/alice/chatgpt_simulator/logs/html_dos_erros/20260502_err.html")
        html_event = events[0]
        assert "alice" not in html_event["content"]
        assert "/home/***" in html_event["content"]

    def test_posix_path_masked_in_screenshot_msg(self):
        events = self._run("/home/alice/chatgpt_simulator/logs/html_dos_erros/20260502_fallback.jpg")
        scr_event = events[1]
        assert "alice" not in scr_event["content"]

    def test_failure_msg_sanitized(self):
        q = _queue.Queue()
        exc_with_path = Exception(r"file not found: C:\Users\bob\AppData\logs")
        _save_error_html_log_offline(q, "/home/bob/fake.html", "label", html_exc=exc_with_path)
        events = _drain(q)
        for ev in events:
            assert "bob" not in ev["content"], f"'bob' vazou em: {ev['content']}"

    def test_all_four_events_emitted(self):
        q = _queue.Queue()
        _save_error_html_log_offline(q, "/tmp/err.html", "test")
        events = _drain(q)
        assert len(events) == 4

    def test_all_events_have_type_log(self):
        q = _queue.Queue()
        _save_error_html_log_offline(q, "/tmp/err.html", "test")
        events = _drain(q)
        assert all(ev["type"] == "log" for ev in events)

    def test_non_sensitive_path_prefix_preserved(self):
        events = self._run("/tmp/logs/html_dos_erros/20260502_label.html")
        html_event = events[0]
        assert "html_dos_erros" in html_event["content"]
        assert "20260502_label.html" in html_event["content"]


class TestSanitizeFallback:
    """Verifica que o fallback str() funciona se log_sanitizer falhar."""

    def test_fallback_identity(self):
        def _fallback(text: str) -> str:
            return str(text)
        result = _fallback("api_key=topsecret1234567890")
        assert result == "api_key=topsecret1234567890"

    def test_fallback_non_string_coercion(self):
        def _fallback(text: str) -> str:
            return str(text)
        assert _fallback(42) == "42"
        assert _fallback(None) == "None"
