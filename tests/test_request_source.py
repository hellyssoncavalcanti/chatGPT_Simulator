from request_source import is_codex_chat_request, is_python_chat_request


class TestIsPythonChatRequest:
    def test_script_suffix(self):
        assert is_python_chat_request("analisador_prontuarios.py")
        assert is_python_chat_request("ACOMPANHAMENTO_WHATSAPP.PY")
        assert is_python_chat_request("auto_dev_agent.py")

    def test_script_with_worker_label(self):
        assert is_python_chat_request("analisador_prontuarios.py/worker-2")
        assert is_python_chat_request("script.py/lane-A")

    def test_explicit_python_prefix(self):
        assert is_python_chat_request("python:custom-integration")
        assert is_python_chat_request("PYTHON:batch-job")

    def test_human_and_frontend_sources_are_not_python(self):
        assert not is_python_chat_request("")
        assert not is_python_chat_request(None)
        assert not is_python_chat_request("chatgpt-ui")
        assert not is_python_chat_request("php-frontend")
        assert not is_python_chat_request("codex-cloud")

    def test_strips_and_lowercases(self):
        assert is_python_chat_request("   Script.PY   ")


class TestIsCodexChatRequest:
    def test_matches_on_source_hint(self):
        assert is_codex_chat_request("codex-cloud", "", "")
        assert is_codex_chat_request("python:codex-worker", "", "")

    def test_matches_on_url(self):
        assert is_codex_chat_request("", "https://chatgpt.com/codex/cloud/tasks/abc", "")
        assert is_codex_chat_request("", "https://chatgpt.com/codex/", "")

    def test_matches_on_origin_url(self):
        assert is_codex_chat_request("", "", "https://chatgpt.com/codex/cloud")

    def test_non_codex_traffic(self):
        assert not is_codex_chat_request("analisador_prontuarios.py", "https://chatgpt.com/", "")
        assert not is_codex_chat_request("", "https://chatgpt.com/c/abc", "https://chatgpt.com/")
        assert not is_codex_chat_request(None, None, None)

    def test_case_insensitive(self):
        assert is_codex_chat_request("CODEX", "", "")
        assert is_codex_chat_request("", "HTTPS://CHATGPT.COM/CODEX/CLOUD", "")
