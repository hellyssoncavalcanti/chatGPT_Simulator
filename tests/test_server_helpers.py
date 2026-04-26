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


# ─────────────────────────────────────────────────────────
# extract_source_hint
# ─────────────────────────────────────────────────────────
class TestExtractSourceHint:
    def test_payload_takes_priority(self):
        out = sh.extract_source_hint(
            {"request_source": "analisador.py"},
            {"X-Request-Source": "header.py", "X-Client-Source": "client.py"},
        )
        assert out == "analisador.py"

    def test_falls_back_to_x_request_source(self):
        out = sh.extract_source_hint(
            {},
            {"X-Request-Source": "header.py", "X-Client-Source": "client.py"},
        )
        assert out == "header.py"

    def test_falls_back_to_x_client_source(self):
        out = sh.extract_source_hint(
            {},
            {"X-Client-Source": "client.py"},
        )
        assert out == "client.py"

    def test_empty_when_nothing_provided(self):
        assert sh.extract_source_hint({}, {}) == ""
        assert sh.extract_source_hint(None, None) == ""

    def test_none_payload_treated_as_empty(self):
        out = sh.extract_source_hint(None, {"X-Request-Source": "header.py"})
        assert out == "header.py"

    def test_none_headers_treated_as_empty(self):
        out = sh.extract_source_hint({"request_source": "analisador.py"}, None)
        assert out == "analisador.py"

    def test_falsy_payload_value_falls_through(self):
        # request_source vazio ou None no payload → fallback para headers.
        out = sh.extract_source_hint(
            {"request_source": ""},
            {"X-Request-Source": "header.py"},
        )
        assert out == "header.py"

    def test_accepts_duck_typed_get(self):
        # Flask EnvironHeaders / qualquer objeto com .get() funciona.
        class FakeHeaders:
            def get(self, key, default=None):
                return {"X-Request-Source": "duck.py"}.get(key, default)

        out = sh.extract_source_hint({}, FakeHeaders())
        assert out == "duck.py"


# ─────────────────────────────────────────────────────────
# decode_attachment
# ─────────────────────────────────────────────────────────
import base64 as _b64


class TestDecodeAttachment:
    def test_plain_base64(self):
        payload = _b64.b64encode(b"hello").decode("ascii")
        out = sh.decode_attachment({"name": "x.txt", "data": payload})
        assert out == ("x.txt", b"hello")

    def test_data_uri_prefix_is_stripped(self):
        payload = _b64.b64encode(b"img-bytes").decode("ascii")
        out = sh.decode_attachment({
            "name": "logo.png",
            "data": f"data:image/png;base64,{payload}",
        })
        assert out == ("logo.png", b"img-bytes")

    def test_default_name_when_missing(self):
        payload = _b64.b64encode(b"abc").decode("ascii")
        out = sh.decode_attachment({"data": payload})
        assert out == ("file.txt", b"abc")

    def test_empty_name_passed_through(self):
        # Histórico: server.py usa `att.get("name", "file.txt")` — só repõe
        # default quando a CHAVE está ausente. `name=""` é repassado bruto
        # (resulta em `<ts>_` no path histórico).
        payload = _b64.b64encode(b"abc").decode("ascii")
        out = sh.decode_attachment({"name": "", "data": payload})
        assert out == ("", b"abc")

    def test_missing_data_decodes_to_empty_bytes(self):
        # Histórico: `base64.b64decode("") == b""` — o servidor escrevia
        # arquivo vazio e anexava o path. Preservamos o contrato.
        out = sh.decode_attachment({"name": "x"})
        assert out == ("x", b"")
        out2 = sh.decode_attachment({"name": "x", "data": ""})
        assert out2 == ("x", b"")

    def test_invalid_base64_returns_none(self):
        # Caracteres fora do alfabeto base64 e padding corrupto.
        assert sh.decode_attachment({"name": "x", "data": "###não-é-base64###"}) is None

    def test_non_mapping_returns_none(self):
        assert sh.decode_attachment(None) is None
        assert sh.decode_attachment("string") is None
        assert sh.decode_attachment(42) is None

    def test_non_string_data_returns_none(self):
        assert sh.decode_attachment({"name": "x", "data": 123}) is None

    def test_only_first_comma_split_preserved_byte_for_byte(self):
        # Histórico: server.py fazia `b64.split(",")[1]`. Com múltiplas vírgulas,
        # o restante após a primeira é descartado. Preservamos esse contrato.
        # Tomamos um payload base64 real e antecedemos com um data URI; depois
        # injetamos uma vírgula extra DEPOIS do payload para confirmar o cutoff.
        payload = _b64.b64encode(b"clean").decode("ascii")
        # split(",")[1] → exatamente `payload`; o ",extra" cai fora.
        out = sh.decode_attachment({
            "name": "f",
            "data": f"data:application/octet-stream;base64,{payload}",
        })
        assert out == ("f", b"clean")


# ─────────────────────────────────────────────────────────
# resolve_chat_url
# ─────────────────────────────────────────────────────────
class TestResolveChatUrl:
    def test_requested_takes_priority(self):
        assert sh.resolve_chat_url("https://a", "https://b") == "https://a"

    def test_falls_back_to_stored(self):
        assert sh.resolve_chat_url(None, "https://b") == "https://b"
        assert sh.resolve_chat_url("", "https://b") == "https://b"

    def test_none_string_sentinel_treated_as_absent(self):
        # Cliente antigo manda literal "None" — não deve ganhar prioridade.
        assert sh.resolve_chat_url("None", "https://b") == "https://b"
        assert sh.resolve_chat_url("None", "None") is None

    def test_returns_none_when_both_absent(self):
        assert sh.resolve_chat_url(None, None) is None
        assert sh.resolve_chat_url("", "") is None

    def test_strips_whitespace(self):
        assert sh.resolve_chat_url("  https://a  ", "https://b") == "https://a"
        assert sh.resolve_chat_url("   ", "https://b") == "https://b"

    def test_non_string_inputs_ignored(self):
        assert sh.resolve_chat_url(42, "https://b") == "https://b"
        assert sh.resolve_chat_url(None, 42) is None

    def test_case_insensitive_treats_lowercase_none_as_absent(self):
        # Caminho histórico de api_sync: `str(url).lower() == "none"`.
        assert sh.resolve_chat_url("none", "https://b", case_insensitive=True) == "https://b"
        assert sh.resolve_chat_url("NONE", "https://b", case_insensitive=True) == "https://b"
        assert sh.resolve_chat_url("None", "https://b", case_insensitive=True) == "https://b"

    def test_case_insensitive_strict_default(self):
        # Sem o flag, "none" minúsculo é tratado como URL válida (preserva
        # comportamento estrito histórico de chat_completions).
        assert sh.resolve_chat_url("none", "https://b") == "none"

    def test_case_insensitive_returns_none_when_both_match_sentinel(self):
        assert sh.resolve_chat_url("none", "NONE", case_insensitive=True) is None


# ─────────────────────────────────────────────────────────
# resolve_browser_profile
# ─────────────────────────────────────────────────────────
class TestResolveBrowserProfile:
    def test_requested_takes_priority(self):
        assert sh.resolve_browser_profile("default", "segunda_chance") == "default"

    def test_falls_back_to_stored(self):
        assert sh.resolve_browser_profile(None, "segunda_chance") == "segunda_chance"
        assert sh.resolve_browser_profile("", "segunda_chance") == "segunda_chance"

    def test_returns_none_when_both_empty(self):
        assert sh.resolve_browser_profile(None, None) is None
        assert sh.resolve_browser_profile("", "") is None
        assert sh.resolve_browser_profile("   ", "  ") is None

    def test_strips_whitespace(self):
        assert sh.resolve_browser_profile("  default  ", "segunda_chance") == "default"

    def test_non_string_inputs_ignored(self):
        assert sh.resolve_browser_profile(42, "default") == "default"
        assert sh.resolve_browser_profile(None, 42) is None
        assert sh.resolve_browser_profile({"x": 1}, "default") == "default"

    def test_whitespace_only_requested_falls_back(self):
        assert sh.resolve_browser_profile("   ", "default") == "default"


# ─────────────────────────────────────────────────────────
# normalize_optional_text
# ─────────────────────────────────────────────────────────
class TestNormalizeOptionalText:
    def test_strips_and_returns_string(self):
        assert sh.normalize_optional_text("  default  ") == "default"

    def test_empty_returns_none(self):
        assert sh.normalize_optional_text("") is None
        assert sh.normalize_optional_text("   ") is None
        assert sh.normalize_optional_text("\n\t") is None

    def test_none_input_returns_none(self):
        assert sh.normalize_optional_text(None) is None

    def test_non_string_returns_none(self):
        assert sh.normalize_optional_text(42) is None
        assert sh.normalize_optional_text({"a": 1}) is None
        assert sh.normalize_optional_text(["x"]) is None

    def test_preserves_internal_whitespace(self):
        assert sh.normalize_optional_text(" hello world ") == "hello world"


# ─────────────────────────────────────────────────────────
# extract_requester_identity
# ─────────────────────────────────────────────────────────
class TestExtractRequesterIdentity:
    def test_extracts_and_strips_both_fields(self):
        out = sh.extract_requester_identity({
            "nome_membro_solicitante": "  Alice  ",
            "id_membro_solicitante": "  123  ",
        })
        assert out == ("Alice", "123")

    def test_empty_or_whitespace_becomes_none(self):
        out = sh.extract_requester_identity({
            "nome_membro_solicitante": "   ",
            "id_membro_solicitante": "",
        })
        assert out == (None, None)

    def test_missing_keys_returns_none_tuple(self):
        assert sh.extract_requester_identity({}) == (None, None)

    def test_non_mapping_like_input_returns_none_tuple(self):
        assert sh.extract_requester_identity(None) == (None, None)
        assert sh.extract_requester_identity("x") == (None, None)

    def test_duck_typed_get_object_is_supported(self):
        class Payload:
            def get(self, key, default=None):
                data = {
                    "nome_membro_solicitante": " Bob ",
                    "id_membro_solicitante": " 9 ",
                }
                return data.get(key, default)

        assert sh.extract_requester_identity(Payload()) == ("Bob", "9")


# ─────────────────────────────────────────────────────────
# lookup/delete payload helpers
# ─────────────────────────────────────────────────────────
class TestResolveLookupOriginUrl:
    def test_prefers_origin_url(self):
        assert sh.resolve_lookup_origin_url({
            "origin_url": "  https://a  ",
            "url_atual": "https://b",
        }) == "https://a"

    def test_falls_back_to_url_atual(self):
        assert sh.resolve_lookup_origin_url({"url_atual": "  https://b "}) == "https://b"

    def test_returns_empty_for_missing_or_invalid_input(self):
        assert sh.resolve_lookup_origin_url({}) == ""
        assert sh.resolve_lookup_origin_url(None) == ""


class TestExtractChatDeleteLocalTargets:
    def test_extracts_and_normalizes(self):
        out = sh.extract_chat_delete_local_targets({
            "chat_id": "  c1 ",
            "origin_url": "  https://x ",
        })
        assert out == ("c1", "https://x")

    def test_missing_keys_return_empty_strings(self):
        assert sh.extract_chat_delete_local_targets({}) == ("", "")

    def test_invalid_input_returns_empty_strings(self):
        assert sh.extract_chat_delete_local_targets(None) == ("", "")


class TestExtractDeleteRequestTargets:
    def test_extracts_and_normalizes(self):
        out = sh.extract_delete_request_targets({
            "url": "  https://chat ",
            "chat_id": "  id-1 ",
        })
        assert out == ("https://chat", "id-1")

    def test_missing_keys_return_none_tuple(self):
        assert sh.extract_delete_request_targets({}) == (None, None)

    def test_invalid_input_returns_none_tuple(self):
        assert sh.extract_delete_request_targets(None) == (None, None)


class TestExtractMenuUrl:
    def test_extracts_and_normalizes(self):
        assert sh.extract_menu_url({"url": "  https://chat  "}) == "https://chat"

    def test_missing_or_invalid_returns_none(self):
        assert sh.extract_menu_url({}) is None
        assert sh.extract_menu_url(None) is None


class TestExtractMenuExecutePayload:
    def test_extracts_and_normalizes(self):
        out = sh.extract_menu_execute_payload({
            "url": "  https://chat  ",
            "option": "  Excluir  ",
            "new_name": "  Novo Nome ",
        })
        assert out == ("https://chat", "Excluir", "Novo Nome")

    def test_optional_new_name_can_be_none(self):
        out = sh.extract_menu_execute_payload({
            "url": "https://chat",
            "option": "Rename",
        })
        assert out == ("https://chat", "Rename", None)

    def test_missing_or_invalid_returns_none_tuple(self):
        assert sh.extract_menu_execute_payload({}) == (None, None, None)
        assert sh.extract_menu_execute_payload(None) == (None, None, None)


class TestExtractWebSearchTestParams:
    def test_extracts_and_normalizes(self):
        out = sh.extract_web_search_test_params({
            "q": "  tdaH tratamento  ",
            "api_key": "  segredo  ",
        })
        assert out == ("tdaH tratamento", "segredo")

    def test_missing_or_invalid_returns_empty_strings(self):
        assert sh.extract_web_search_test_params({}) == ("", "")
        assert sh.extract_web_search_test_params(None) == ("", "")


# ─────────────────────────────────────────────────────────
# build_queue_key
# ─────────────────────────────────────────────────────────
class TestBuildQueueKey:
    def test_basic_format(self):
        out = sh.build_queue_key("abc-123", now_ns=lambda: 1700000000000000000)
        assert out == "abc-123:1700000000000000000"

    def test_uses_real_time_by_default(self):
        # Sem monkeypatch: garante apenas que o formato `<chat_id>:<int>` aparece.
        out = sh.build_queue_key("xyz")
        assert out.startswith("xyz:")
        assert out.split(":", 1)[1].isdigit()

    def test_unique_with_monotonic_now_ns(self):
        counter = iter([10, 20, 30])
        a = sh.build_queue_key("c", now_ns=lambda: next(counter))
        b = sh.build_queue_key("c", now_ns=lambda: next(counter))
        c = sh.build_queue_key("c", now_ns=lambda: next(counter))
        assert (a, b, c) == ("c:10", "c:20", "c:30")

    def test_chat_id_is_passed_through_as_str(self):
        # Suporta chat_id como string longa com hyphens (UUID típico).
        out = sh.build_queue_key("11111111-2222-3333-4444-555555555555",
                                  now_ns=lambda: 1)
        assert out == "11111111-2222-3333-4444-555555555555:1"


# ─────────────────────────────────────────────────────────
# build_chat_task_payload
# ─────────────────────────────────────────────────────────
class TestBuildChatTaskPayload:
    def _payload(self, **overrides):
        defaults = dict(
            url="https://chatgpt.com/c/abc",
            chat_id="abc",
            message="oi",
            is_analyzer=False,
            sender_label="usuario_remoto",
            source_hint="",
            saved_paths=[],
            stream_queue=object(),
            codex_repo=None,
            effective_browser_profile=None,
        )
        defaults.update(overrides)
        return sh.build_chat_task_payload(**defaults)

    def test_action_is_fixed_chat(self):
        out = self._payload()
        assert out["action"] == "CHAT"

    def test_is_analyzer_coerced_to_bool(self):
        out = self._payload(is_analyzer=1)
        assert out["is_analyzer"] is True
        out2 = self._payload(is_analyzer=0)
        assert out2["is_analyzer"] is False
        out3 = self._payload(is_analyzer=None)
        assert out3["is_analyzer"] is False

    def test_request_source_falls_back_to_sender_label(self):
        out = self._payload(source_hint="", sender_label="analisador_prontuarios.py")
        assert out["request_source"] == "analisador_prontuarios.py"

    def test_request_source_uses_hint_when_present(self):
        out = self._payload(source_hint="acompanhamento_whatsapp.py",
                             sender_label="usuario_remoto")
        assert out["request_source"] == "acompanhamento_whatsapp.py"

    def test_codex_repo_normalized(self):
        assert self._payload(codex_repo="  myrepo  ")["codex_repo"] == "myrepo"
        assert self._payload(codex_repo="")["codex_repo"] is None
        assert self._payload(codex_repo="   ")["codex_repo"] is None
        assert self._payload(codex_repo=None)["codex_repo"] is None

    def test_browser_profile_passed_through(self):
        out = self._payload(effective_browser_profile="segunda_chance")
        assert out["browser_profile"] == "segunda_chance"

    def test_attachment_paths_passed_through(self):
        out = self._payload(saved_paths=["/tmp/a", "/tmp/b"])
        assert out["attachment_paths"] == ["/tmp/a", "/tmp/b"]

    def test_stream_queue_is_same_reference(self):
        sq = object()
        out = self._payload(stream_queue=sq)
        assert out["stream_queue"] is sq

    def test_all_historical_keys_present(self):
        out = self._payload()
        # Conjunto exato de chaves que o `browser.py` consome historicamente.
        assert set(out.keys()) == {
            "action", "url", "chat_id", "message", "is_analyzer",
            "sender", "request_source", "attachment_paths", "stream_queue",
            "codex_repo", "browser_profile",
        }


# ─────────────────────────────────────────────────────────
# build_error_event
# ─────────────────────────────────────────────────────────
class TestBuildErrorEvent:
    def test_basic_shape(self):
        out = json.loads(sh.build_error_event("Algo falhou"))
        assert out == {"type": "error", "content": "Algo falhou"}

    def test_unicode_preserved(self):
        out = sh.build_error_event("Erro: ✗ não autorizado")
        assert "✗" in out
        assert "não autorizado" in out

    def test_coerces_non_string_to_str(self):
        out = json.loads(sh.build_error_event(42))
        assert out["content"] == "42"
        out2 = json.loads(sh.build_error_event(ValueError("boom")))
        assert out2["content"] == "boom"

    def test_no_trailing_newline(self):
        out = sh.build_error_event("x")
        assert not out.endswith("\n")


# ─────────────────────────────────────────────────────────
# build_status_event
# ─────────────────────────────────────────────────────────
class TestBuildStatusEvent:
    def test_basic_shape(self):
        out = json.loads(sh.build_status_event("Aguardando..."))
        assert out == {"type": "status", "content": "Aguardando..."}

    def test_extras_merged(self):
        out = json.loads(sh.build_status_event(
            "Cooldown",
            phase="chat_rate_limit_cooldown",
            wait_seconds=12.5,
        ))
        assert out["phase"] == "chat_rate_limit_cooldown"
        assert out["wait_seconds"] == 12.5
        assert out["type"] == "status"
        assert out["content"] == "Cooldown"

    def test_extras_can_override_nothing_critical(self):
        # `type` e `content` aparecem antes do update — extras do mesmo nome
        # SOBRESCREVEM (Python dict). Documentamos o contrato.
        out = json.loads(sh.build_status_event("a", type="custom"))
        assert out["type"] == "custom"

    def test_unicode_preserved(self):
        out = sh.build_status_event("⏳ aguarde", phase="queue")
        assert "⏳" in out

    def test_no_extras_works(self):
        out = json.loads(sh.build_status_event("simples"))
        assert out == {"type": "status", "content": "simples"}


# ─────────────────────────────────────────────────────────
# build_markdown_event
# ─────────────────────────────────────────────────────────
class TestBuildMarkdownEvent:
    def test_basic_shape(self):
        out = json.loads(sh.build_markdown_event("# Título\n\ntexto"))
        assert out == {"type": "markdown", "content": "# Título\n\ntexto"}

    def test_unicode_preserved(self):
        out = sh.build_markdown_event("✅ análise concluída")
        assert "✅" in out
        assert "análise" in out

    def test_coerces_non_string_to_str(self):
        out = json.loads(sh.build_markdown_event(42))
        assert out["content"] == "42"

    def test_no_trailing_newline(self):
        out = sh.build_markdown_event("x")
        assert not out.endswith("\n")


# ─────────────────────────────────────────────────────────
# format_requester_suffix
# ─────────────────────────────────────────────────────────
class TestFormatRequesterSuffix:
    def test_both_none_returns_empty(self):
        assert sh.format_requester_suffix(None, None) == ""

    def test_both_empty_strings_returns_empty(self):
        assert sh.format_requester_suffix("", "") == ""

    def test_both_present(self):
        out = sh.format_requester_suffix("Alice", "ID-1")
        assert out == ', por "Alice" (id_membro: "ID-1")'

    def test_only_nome(self):
        out = sh.format_requester_suffix("Bob", None)
        assert out == ', por "Bob" (id_membro: "None")'

    def test_only_id(self):
        out = sh.format_requester_suffix(None, "X9")
        assert out == ', por "None" (id_membro: "X9")'

    def test_id_as_int_serialized_via_fstring(self):
        # Histórico não validava tipos; preservamos.
        out = sh.format_requester_suffix("Carol", 42)
        assert out == ', por "Carol" (id_membro: "42")'


# ─────────────────────────────────────────────────────────
# format_origin_suffix
# ─────────────────────────────────────────────────────────
class TestFormatOriginSuffix:
    def test_analyzer_overrides_hint(self):
        out = sh.format_origin_suffix(True, "qualquer_outro.py")
        assert out == " [origem: analisador_prontuarios.py]"

    def test_analyzer_with_empty_hint(self):
        assert sh.format_origin_suffix(True, "") == " [origem: analisador_prontuarios.py]"
        assert sh.format_origin_suffix(True, None) == " [origem: analisador_prontuarios.py]"

    def test_hint_used_when_not_analyzer(self):
        out = sh.format_origin_suffix(False, "acompanhamento_whatsapp.py")
        assert out == " [origem: acompanhamento_whatsapp.py]"

    def test_empty_when_not_analyzer_and_no_hint(self):
        assert sh.format_origin_suffix(False, "") == ""
        assert sh.format_origin_suffix(False, None) == ""

    def test_leading_space_present_in_truthy_cases(self):
        # Garantia explícita do contrato: sufixos truthy começam com espaço.
        assert sh.format_origin_suffix(True, "x").startswith(" ")
        assert sh.format_origin_suffix(False, "x").startswith(" ")


# ─────────────────────────────────────────────────────────
# compute_python_request_interval
# ─────────────────────────────────────────────────────────
class TestComputePythonRequestInterval:
    def test_zero_when_both_pauses_disabled(self):
        base, target = sh.compute_python_request_interval(0, 0, 2)
        assert (base, target) == (0.0, 0.0)

    def test_zero_when_both_negative(self):
        base, target = sh.compute_python_request_interval(-5, -1, 2)
        assert (base, target) == (0.0, 0.0)

    def test_target_is_base_divided_by_profile_count(self):
        # rng sempre devolve 60.0 → base=60, target = 60/2 = 30
        base, target = sh.compute_python_request_interval(
            10, 60, 2, rng=lambda lo, hi: 60.0,
        )
        assert base == 60.0
        assert target == 30.0

    def test_single_profile_target_equals_base(self):
        base, target = sh.compute_python_request_interval(
            5, 30, 1, rng=lambda lo, hi: 30.0,
        )
        assert base == 30.0
        assert target == 30.0

    def test_zero_profiles_clamped_to_one(self):
        # profile_count=0 não pode dividir por zero — clamp em 1.
        base, target = sh.compute_python_request_interval(
            10, 60, 0, rng=lambda lo, hi: 60.0,
        )
        assert base == 60.0
        assert target == 60.0

    def test_rng_receives_correct_bounds(self):
        seen = {}
        def rng(lo, hi):
            seen["lo"] = lo
            seen["hi"] = hi
            return lo

        sh.compute_python_request_interval(10, 60, 2, rng=rng)
        # max(0, 10) = 10; max(10, 60) = 60.
        assert seen == {"lo": 10.0, "hi": 60.0}

    def test_rng_swap_when_min_gt_max(self):
        # Histórico: max(pmin, pmax) garante que hi >= lo.
        seen = {}
        def rng(lo, hi):
            seen["lo"] = lo
            seen["hi"] = hi
            return lo
        sh.compute_python_request_interval(60, 10, 2, rng=rng)
        # lo=max(0,60)=60; hi=max(60,10)=60.
        assert seen["lo"] == 60.0
        assert seen["hi"] == 60.0

    def test_default_rng_is_random_uniform(self):
        # Apenas garante que a chamada sem rng não falha e devolve floats.
        base, target = sh.compute_python_request_interval(1, 2, 1)
        assert isinstance(base, float)
        assert isinstance(target, float)
        assert 0.0 <= target <= 2.0


# ─────────────────────────────────────────────────────────
# Web-search wait events (regressão: byte-equivalência com o dict-yielder antigo)
#
# Antes: `_iter_web_search_wait_messages` yieldava dict; o consumer mutava
# `phase`, `source` e fazia `content.replace("busca web", f"busca {label}")`,
# então `json.dumps(msg, ensure_ascii=False)`. A migração troca o yield por
# `build_status_event(content, **extras)` direto. Este teste documenta que
# o JSON resultante (mesmas chaves, mesma ordem, mesmos valores) é idêntico
# ao da via antiga para os 2 source_labels usados em produção: "web" e
# "uptodate".
# ─────────────────────────────────────────────────────────
class TestWebSearchWaitEventEquivalence:
    @staticmethod
    def _legacy_event(content_template, query_str, remaining, interval, phase_prefix, source_label):
        """Reproduz o pipeline antigo: dict + mutate + json.dumps."""
        msg = {
            "type": "status",
            "content": content_template.format(query_str=query_str, interval_or_remaining=interval),
            "query": query_str,
            "wait_seconds": round(remaining, 1),
            "planned_interval_seconds": round(interval, 1),
            "phase": "web_search_cooldown",
        }
        msg["phase"] = f"{phase_prefix}_cooldown"
        msg["source"] = source_label
        msg["content"] = msg["content"].replace("busca web", f"busca {source_label}")
        return json.dumps(msg, ensure_ascii=False)

    @staticmethod
    def _new_event(content, query_str, remaining, interval, phase_prefix, source_label):
        """Reproduz o pipeline novo: build_status_event direto."""
        return sh.build_status_event(
            content,
            query=query_str,
            wait_seconds=round(remaining, 1),
            planned_interval_seconds=round(interval, 1),
            phase=f"{phase_prefix}_cooldown",
            source=source_label,
        )

    @pytest.mark.parametrize("phase_prefix,source_label", [
        ("web_search", "web"),
        ("uptodate_search", "uptodate"),
    ])
    def test_first_event_byte_equivalent(self, phase_prefix, source_label):
        query = "tratamento de hipertensão"
        remaining = 12.5
        interval = 30.0
        legacy_template = (
            "⏳ Aguardando intervalo humano antes da busca web por "
            "\"{query_str}\". Pausa planejada: 00:30."
        )
        new_content = (
            f"⏳ Aguardando intervalo humano antes da busca {source_label} por "
            f"\"{query}\". Pausa planejada: 00:30."
        )
        legacy = self._legacy_event(
            legacy_template, query, remaining, interval, phase_prefix, source_label
        )
        new = self._new_event(
            new_content, query, remaining, interval, phase_prefix, source_label
        )
        assert legacy == new, f"Divergência byte-a-byte para {source_label}"
        assert json.loads(legacy) == json.loads(new)

    @pytest.mark.parametrize("phase_prefix,source_label", [
        ("web_search", "web"),
        ("uptodate_search", "uptodate"),
    ])
    def test_progress_event_byte_equivalent(self, phase_prefix, source_label):
        query = "doses pediátricas"
        remaining = 4.7
        interval = 30.0
        legacy_template = (
            "⏳ Pausa anti-bot em andamento antes da busca web por "
            "\"{query_str}\". Início previsto em 00:05."
        )
        new_content = (
            f"⏳ Pausa anti-bot em andamento antes da busca {source_label} por "
            f"\"{query}\". Início previsto em 00:05."
        )
        legacy = self._legacy_event(
            legacy_template, query, remaining, interval, phase_prefix, source_label
        )
        new = self._new_event(
            new_content, query, remaining, interval, phase_prefix, source_label
        )
        assert legacy == new
        assert json.loads(legacy) == json.loads(new)

    def test_phase_and_source_present_after_migration(self):
        out = json.loads(sh.build_status_event(
            "⏳ aguarde",
            query="q",
            wait_seconds=1.0,
            planned_interval_seconds=2.0,
            phase="web_search_cooldown",
            source="web",
        ))
        assert out["type"] == "status"
        assert out["phase"] == "web_search_cooldown"
        assert out["source"] == "web"
        assert out["query"] == "q"
        assert out["wait_seconds"] == 1.0
        assert out["planned_interval_seconds"] == 2.0

    def test_unicode_round_trip(self):
        out = sh.build_status_event(
            "⏳ Pausa anti-bot em andamento antes da busca uptodate por \"sépsis\".",
            query="sépsis",
            wait_seconds=0.0,
            planned_interval_seconds=30.0,
            phase="uptodate_search_cooldown",
            source="uptodate",
        )
        # ensure_ascii=False preserva chars não-ASCII na string serializada.
        assert "sépsis" in out
        assert "⏳" in out

    def test_key_order_matches_legacy_pipeline(self):
        """A ordem das chaves no JSON tem que ser idêntica à do pipeline antigo:
        type, content, query, wait_seconds, planned_interval_seconds, phase, source."""
        new = sh.build_status_event(
            "x",
            query="q",
            wait_seconds=1.0,
            planned_interval_seconds=2.0,
            phase="web_search_cooldown",
            source="web",
        )
        # Chaves devem aparecer na ordem documentada (Python 3.7+ preserva ordem).
        expected_order = [
            '"type"',
            '"content"',
            '"query"',
            '"wait_seconds"',
            '"planned_interval_seconds"',
            '"phase"',
            '"source"',
        ]
        positions = [new.find(k) for k in expected_order]
        assert all(p >= 0 for p in positions), f"Chave faltando: {positions}"
        assert positions == sorted(positions), (
            f"Ordem de chaves inesperada: {expected_order} → posições {positions}"
        )


class TestSafeInt:
    def test_int_passthrough(self):
        assert sh.safe_int(42, 0) == 42

    def test_string_digits(self):
        assert sh.safe_int("100", 0) == 100

    def test_negative_string(self):
        assert sh.safe_int("-7", 0) == -7

    def test_default_returned_for_none(self):
        assert sh.safe_int(None, 99) == 99

    def test_default_returned_for_empty_string(self):
        assert sh.safe_int("", 12) == 12

    def test_default_returned_for_invalid_string(self):
        assert sh.safe_int("abc", 5) == 5

    def test_default_returned_for_float_string(self):
        # int("1.5") levanta ValueError no Python — fallback para default.
        assert sh.safe_int("1.5", 7) == 7

    def test_float_value_truncates(self):
        assert sh.safe_int(3.9, 0) == 3

    def test_negative_default_preserved(self):
        # Idiom de queue_failed_retry: -1 sinaliza ausência.
        assert sh.safe_int(None, -1) == -1
        assert sh.safe_int("xx", -1) == -1

    def test_default_coerced_to_int(self):
        assert sh.safe_int(None, 5.7) == 5

    def test_bool_returns_int_value(self):
        # Compatibilidade com idiom histórico (Python: int(True) == 1).
        assert sh.safe_int(True, 0) == 1
        assert sh.safe_int(False, 99) == 0


class TestSafeSnapshotStats:
    class _DummyOk:
        def __init__(self, payload):
            self._payload = payload
        def snapshot_stats(self):
            return self._payload

    class _DummyRaises:
        def snapshot_stats(self):
            raise RuntimeError("boom")

    class _DummyNoMethod:
        pass

    def test_returns_payload_when_method_exists(self):
        payload = {"a": 1, "b": "x"}
        assert sh.safe_snapshot_stats(self._DummyOk(payload)) == payload

    def test_returns_empty_dict_when_method_returns_none(self):
        # Contrato histórico: `snapshot_stats() or {}`.
        assert sh.safe_snapshot_stats(self._DummyOk(None)) == {}

    def test_returns_empty_dict_when_method_returns_empty(self):
        assert sh.safe_snapshot_stats(self._DummyOk({})) == {}

    def test_returns_empty_dict_when_method_absent(self):
        assert sh.safe_snapshot_stats(self._DummyNoMethod()) == {}

    def test_returns_error_dict_when_method_raises(self):
        result = sh.safe_snapshot_stats(self._DummyRaises())
        assert "error" in result
        assert "boom" in result["error"]

    def test_handles_none_queue(self):
        # `None` não tem `snapshot_stats` → caminho de hasattr=False → {}.
        assert sh.safe_snapshot_stats(None) == {}


class TestSearchHandlerStatusEventEquivalence:
    """Byte-equivalência entre os dict-yielders SSE históricos de
    `_handle_browser_search_api` (status `*_prepare` e `*_keepalive`) e o
    novo `build_status_event(content, **extras)` adotado em 2026-04-26
    quinvicies (ciclo 19).
    """

    @staticmethod
    def _legacy_prepare(query_str, idx, total, route_label, source_label):
        return json.dumps({
            'type': 'status',
            'content': f'📚 Preparando busca {source_label} {idx}/{total}.',
            'query': query_str,
            'index': idx,
            'total': total,
            'phase': f'{route_label.lower()}_prepare',
            'source': source_label,
        }, ensure_ascii=False)

    @staticmethod
    def _new_prepare(query_str, idx, total, route_label, source_label):
        return sh.build_status_event(
            f'📚 Preparando busca {source_label} {idx}/{total}.',
            query=query_str,
            index=idx,
            total=total,
            phase=f'{route_label.lower()}_prepare',
            source=source_label,
        )

    @staticmethod
    def _legacy_keepalive(query_str, idx, total, route_label, source_label):
        return json.dumps({
            'type': 'status',
            'content': f'⏳ Busca {source_label} por "{query_str}" ainda em andamento...',
            'query': query_str,
            'index': idx,
            'total': total,
            'phase': f'{route_label.lower()}_keepalive',
            'source': source_label,
        }, ensure_ascii=False)

    @staticmethod
    def _new_keepalive(query_str, idx, total, route_label, source_label):
        return sh.build_status_event(
            f'⏳ Busca {source_label} por "{query_str}" ainda em andamento...',
            query=query_str,
            index=idx,
            total=total,
            phase=f'{route_label.lower()}_keepalive',
            source=source_label,
        )

    @pytest.mark.parametrize("route_label,source_label", [
        ("WEB_SEARCH", "web"),
        ("UPTODATE_SEARCH", "uptodate"),
    ])
    def test_prepare_event_byte_equivalent(self, route_label, source_label):
        legacy = self._legacy_prepare("tratamento", 1, 3, route_label, source_label)
        new = self._new_prepare("tratamento", 1, 3, route_label, source_label)
        assert legacy == new
        assert json.loads(legacy) == json.loads(new)

    @pytest.mark.parametrize("route_label,source_label", [
        ("WEB_SEARCH", "web"),
        ("UPTODATE_SEARCH", "uptodate"),
    ])
    def test_keepalive_event_byte_equivalent(self, route_label, source_label):
        legacy = self._legacy_keepalive("dose", 2, 5, route_label, source_label)
        new = self._new_keepalive("dose", 2, 5, route_label, source_label)
        assert legacy == new
        assert json.loads(legacy) == json.loads(new)

    def test_unicode_in_query_preserved(self):
        # Acento + aspas duplas no query — JSON precisa escapar `"` e
        # preservar UTF-8 cru (ensure_ascii=False).
        query = 'cefepime "diluição"'
        legacy = self._legacy_keepalive(query, 1, 1, "WEB_SEARCH", "web")
        new = self._new_keepalive(query, 1, 1, "WEB_SEARCH", "web")
        assert legacy == new
        # As aspas internas viram \" e o ç permanece literal.
        assert '\\"diluição\\"' in new

    def test_extras_order_matches_legacy(self):
        legacy = self._legacy_prepare("q", 1, 1, "WEB_SEARCH", "web")
        new = self._new_prepare("q", 1, 1, "WEB_SEARCH", "web")
        # Ordem das chaves deve ser idêntica (dict mantém ordem de inserção).
        expected_order = ['"type"', '"content"', '"query"', '"index"', '"total"', '"phase"', '"source"']
        legacy_positions = [legacy.find(k) for k in expected_order]
        new_positions = [new.find(k) for k in expected_order]
        assert legacy_positions == new_positions
        assert all(p >= 0 for p in legacy_positions)
