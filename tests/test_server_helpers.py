import json
from collections import deque

import pytest

import server_helpers as sh


class TestFormatWaitSeconds:
    def test_zero_returns_00_00(self):
        assert sh.format_wait_seconds(0) == "00:00"

    def test_under_minute(self):
        assert sh.format_wait_seconds(45) == "00:45"

    def test_exact_minute_boundary(self):
        assert sh.format_wait_seconds(60) == "01:00"

    def test_multi_minute(self):
        assert sh.format_wait_seconds(125) == "02:05"

    def test_negative_clamps_to_zero(self):
        assert sh.format_wait_seconds(-10) == "00:00"

    def test_float_is_rounded(self):
        # 2.6 arredonda para 3s → "00:03"
        assert sh.format_wait_seconds(2.6) == "00:03"
        assert sh.format_wait_seconds(2.4) == "00:02"

    def test_invalid_input_returns_zero(self):
        assert sh.format_wait_seconds(None) == "00:00"
        assert sh.format_wait_seconds("abc") == "00:00"

    def test_large_values(self):
        # 3725s = 62m05s → "62:05" (sem rolling para horas; o cliente mostra minutos).
        assert sh.format_wait_seconds(3725) == "62:05"


class TestQueueStatusPayload:
    def _loads(self, s):
        return json.loads(s)

    def test_basic_shape(self):
        out = self._loads(sh.queue_status_payload(30, 2, 5, "analisador"))
        assert out["type"] == "status"
        assert out["phase"] == "server_python_queue_wait"
        assert out["queue_position"] == 2
        assert out["queue_size"] == 5
        assert out["sender"] == "analisador"

    def test_content_includes_formatted_wait_and_position(self):
        out = self._loads(sh.queue_status_payload(125, 2, 5, "analisador"))
        assert "02:05" in out["content"]
        assert "2/5" in out["content"]

    def test_wait_seconds_rounded_to_one_decimal(self):
        out = self._loads(sh.queue_status_payload(12.345, 1, 3, "x"))
        assert out["wait_seconds"] == 12.3

    def test_negative_wait_clamped_to_zero(self):
        out = self._loads(sh.queue_status_payload(-5, 1, 1, "x"))
        assert out["wait_seconds"] == 0.0
        assert "00:00" in out["content"]

    def test_invalid_wait_becomes_zero(self):
        out = self._loads(sh.queue_status_payload("NaN", 1, 2, "x"))
        assert out["wait_seconds"] == 0.0

    def test_total_zero_is_displayed_as_at_least_one(self):
        # max(1, total) impede divisão por zero no texto; queue_size original permanece 0.
        out = self._loads(sh.queue_status_payload(10, 0, 0, "x"))
        assert out["queue_size"] == 0
        assert "0/1" in out["content"]

    def test_utf8_preserved(self):
        # ensure_ascii=False; o emoji ⏳ precisa sobreviver sem escape.
        s = sh.queue_status_payload(0, 1, 1, "x")
        assert "⏳" in s

    def test_json_is_valid(self):
        s = sh.queue_status_payload(10, 3, 7, "analisador")
        assert isinstance(json.loads(s), dict)


class TestPruneOldAttempts:
    def test_removes_entries_outside_window(self):
        dq = deque([100.0, 105.0, 110.0])
        removed = sh.prune_old_attempts(dq, window_sec=10, now=120.0)
        # window = [110, 120]; 100 e 105 saem.
        assert removed == 2
        assert list(dq) == [110.0]

    def test_keeps_all_when_inside_window(self):
        dq = deque([115.0, 116.0, 118.0])
        removed = sh.prune_old_attempts(dq, window_sec=10, now=120.0)
        assert removed == 0
        assert list(dq) == [115.0, 116.0, 118.0]

    def test_empty_deque_is_noop(self):
        dq: deque[float] = deque()
        assert sh.prune_old_attempts(dq, window_sec=10, now=100.0) == 0
        assert list(dq) == []

    def test_removes_all_when_all_are_stale(self):
        dq = deque([1.0, 2.0, 3.0])
        removed = sh.prune_old_attempts(dq, window_sec=5, now=1000.0)
        assert removed == 3
        assert list(dq) == []

    def test_now_func_hook_is_used_when_now_absent(self):
        dq = deque([90.0, 100.0])
        sh.prune_old_attempts(dq, window_sec=5, now_func=lambda: 110.0)
        # cutoff = 105; ambos saem.
        assert list(dq) == []

    def test_accepts_list_as_fallback(self):
        # Aceitar lista facilita testes; prod usa deque.
        lst = [0.0, 1.0, 100.0]
        removed = sh.prune_old_attempts(lst, window_sec=10, now=105.0)
        assert removed == 2
        assert lst == [100.0]


class TestCountActiveChatgptProfiles:
    def test_none_returns_one(self):
        assert sh.count_active_chatgpt_profiles(None) == 1

    def test_empty_dict_returns_one(self):
        assert sh.count_active_chatgpt_profiles({}) == 1

    def test_single_profile(self):
        assert sh.count_active_chatgpt_profiles({"default": "/some/path"}) == 1

    def test_two_profiles(self):
        assert sh.count_active_chatgpt_profiles({
            "default": "/a", "segunda_chance": "/b"
        }) == 2

    def test_many_profiles(self):
        m = {f"p{i}": f"/path{i}" for i in range(5)}
        assert sh.count_active_chatgpt_profiles(m) == 5

    def test_non_mapping_without_len_returns_one(self):
        class NoLen:
            pass
        # Nosso contrato: se o argumento não tem len(), voltar ao default seguro.
        assert sh.count_active_chatgpt_profiles(NoLen()) == 1

    def test_list_is_treated_like_mapping_length(self):
        # O código histórico só chamava len(); aceitamos qualquer sized.
        assert sh.count_active_chatgpt_profiles(["default", "segunda_chance"]) == 2


# ─────────────────────────────────────────────────────────
# combine_openai_messages
# ─────────────────────────────────────────────────────────
class TestCombineOpenaiMessages:
    def test_non_list_returns_empty(self):
        assert sh.combine_openai_messages(None) == ""
        assert sh.combine_openai_messages("oi") == ""
        assert sh.combine_openai_messages({"role": "user", "content": "x"}) == ""

    def test_empty_list_returns_empty(self):
        assert sh.combine_openai_messages([]) == ""

    def test_single_user_message(self):
        assert sh.combine_openai_messages([
            {"role": "user", "content": "olá"}
        ]) == "olá"

    def test_system_prepended_with_double_newline(self):
        out = sh.combine_openai_messages([
            {"role": "user", "content": "pergunta"},
            {"role": "system", "content": "contexto"},
        ])
        assert out.startswith("contexto")
        assert "pergunta" in out
        assert "\n\n" in out

    def test_assistant_message_ignored(self):
        out = sh.combine_openai_messages([
            {"role": "user", "content": "pergunta"},
            {"role": "assistant", "content": "resposta anterior"},
        ])
        assert "resposta anterior" not in out
        assert out == "pergunta"

    def test_non_string_content_skipped(self):
        out = sh.combine_openai_messages([
            {"role": "user", "content": 42},
            {"role": "user", "content": "texto"},
        ])
        assert out == "texto"

    def test_non_mapping_items_ignored(self):
        out = sh.combine_openai_messages([
            "lixo",
            None,
            {"role": "user", "content": "válido"},
        ])
        assert out == "válido"


# ─────────────────────────────────────────────────────────
# build_sender_label
# ─────────────────────────────────────────────────────────
class TestBuildSenderLabel:
    def test_analyzer_always_returns_canonical_label(self):
        assert sh.build_sender_label("anything", True) == "analisador_prontuarios.py"
        assert sh.build_sender_label("", True) == "analisador_prontuarios.py"

    def test_hint_preserved_when_not_analyzer(self):
        assert sh.build_sender_label("acompanhamento_whatsapp.py", False) == "acompanhamento_whatsapp.py"

    def test_empty_hint_falls_back_to_default(self):
        assert sh.build_sender_label("", False) == "usuario_remoto"
        assert sh.build_sender_label(None, False) == "usuario_remoto"  # type: ignore[arg-type]
        assert sh.build_sender_label("   ", False) == "usuario_remoto"


# ─────────────────────────────────────────────────────────
# wrap_paste_if_python_source
# ─────────────────────────────────────────────────────────
class TestWrapPasteIfPythonSource:
    def test_not_python_source_returns_unchanged(self):
        assert sh.wrap_paste_if_python_source("texto qualquer", False) == "texto qualquer"
        assert sh.wrap_paste_if_python_source("", False) == ""

    def test_python_source_wraps_plain_text(self):
        out = sh.wrap_paste_if_python_source("analise isto", True)
        assert out == "[INICIO_TEXTO_COLADO]analise isto[FIM_TEXTO_COLADO]"

    def test_already_wrapped_not_double_wrapped(self):
        already = "[INICIO_TEXTO_COLADO]x[FIM_TEXTO_COLADO]"
        assert sh.wrap_paste_if_python_source(already, True) == already

    def test_whitespace_only_is_not_wrapped(self):
        assert sh.wrap_paste_if_python_source("", True) == ""
        assert sh.wrap_paste_if_python_source("   \n", True) == "   \n"

    def test_non_string_returns_empty(self):
        assert sh.wrap_paste_if_python_source(None, True) == ""
        assert sh.wrap_paste_if_python_source(42, True) == ""


# ─────────────────────────────────────────────────────────
# coalesce_origin_url
# ─────────────────────────────────────────────────────────
class TestCoalesceOriginUrl:
    def test_origin_url_takes_priority(self):
        out = sh.coalesce_origin_url(
            {"origin_url": "https://a", "url_atual": "https://b"},
            header_value="https://c",
        )
        assert out == "https://a"

    def test_falls_back_to_url_atual(self):
        out = sh.coalesce_origin_url(
            {"url_atual": "https://b"},
            header_value="https://c",
        )
        assert out == "https://b"

    def test_falls_back_to_header(self):
        assert sh.coalesce_origin_url({}, header_value="https://c") == "https://c"

    def test_empty_when_nothing_provided(self):
        assert sh.coalesce_origin_url({}) == ""
        assert sh.coalesce_origin_url(None) == ""

    def test_strips_whitespace(self):
        assert sh.coalesce_origin_url({"origin_url": "  https://a  "}) == "https://a"

    def test_non_mapping_data_treated_as_empty(self):
        assert sh.coalesce_origin_url("lixo", header_value="https://fb") == "https://fb"
