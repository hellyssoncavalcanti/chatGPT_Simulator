"""Testes do módulo puro `analisador_parsers` (extração Lote P1 opção 1).

Cobertura-alvo: ≥3 casos por função pública.
"""

import json

import pytest

import analisador_parsers as ap


# ─────────────────────────────────────────────────────────
# detect_rate_limit_preview
# ─────────────────────────────────────────────────────────
class TestDetectRateLimitPreview:
    def test_none_when_text_is_empty(self):
        assert ap.detect_rate_limit_preview("", lambda t: True) is None

    def test_none_when_text_is_none(self):
        assert ap.detect_rate_limit_preview(None, lambda t: True) is None  # type: ignore[arg-type]

    def test_none_when_classifier_says_false(self):
        assert ap.detect_rate_limit_preview("conteúdo normal", lambda t: False) is None

    def test_returns_preview_when_classifier_says_true(self):
        text = "a" * 300
        preview = ap.detect_rate_limit_preview(text, lambda t: True)
        assert preview == "a" * 120  # _RATE_LIMIT_PREVIEW_CHARS

    def test_preview_shorter_than_limit_returned_inteiro(self):
        text = "erro curto"
        preview = ap.detect_rate_limit_preview(text, lambda t: True)
        assert preview == "erro curto"

    def test_classifier_receives_full_markdown(self):
        received = {}

        def spy(t):
            received["txt"] = t
            return True

        ap.detect_rate_limit_preview("linha 1\nlinha 2", spy)
        assert received["txt"] == "linha 1\nlinha 2"


# ─────────────────────────────────────────────────────────
# build_rate_limit_error_message
# ─────────────────────────────────────────────────────────
class TestBuildRateLimitErrorMessage:
    def test_empty_preview_renders_template(self):
        msg = ap.build_rate_limit_error_message("")
        assert msg.startswith("ChatGPT retornou rate limit")
        assert "Prévia: " in msg

    def test_preview_interpolated(self):
        msg = ap.build_rate_limit_error_message("excesso de solicitações")
        assert "Prévia: excesso de solicitações" in msg

    def test_none_is_safe(self):
        # Não levanta; trata como string vazia.
        msg = ap.build_rate_limit_error_message(None)  # type: ignore[arg-type]
        assert "Prévia: " in msg

    def test_message_prefix_is_stable_for_log_matchers(self):
        msg = ap.build_rate_limit_error_message("x")
        assert msg.startswith(
            "ChatGPT retornou rate limit (detectado no texto da resposta)."
        )


# ─────────────────────────────────────────────────────────
# strip_code_fences
# ─────────────────────────────────────────────────────────
class TestStripCodeFences:
    def test_empty_returns_empty(self):
        assert ap.strip_code_fences("") == ""
        assert ap.strip_code_fences(None) == ""  # type: ignore[arg-type]

    def test_no_fences_returns_stripped(self):
        assert ap.strip_code_fences("  {\"a\": 1}  ") == "{\"a\": 1}"

    def test_triple_backtick_plain(self):
        assert ap.strip_code_fences("```\n{\"a\":1}\n```") == "{\"a\":1}"

    def test_triple_backtick_with_json_tag(self):
        assert ap.strip_code_fences("```json\n{\"a\":1}\n```") == "{\"a\":1}"

    def test_triple_backtick_with_json_tag_uppercase(self):
        assert ap.strip_code_fences("```JSON\n{\"a\":1}\n```") == "{\"a\":1}"

    def test_fence_without_closing_is_stripped_partially(self):
        # Se só tiver abertura, ainda remove ela.
        out = ap.strip_code_fences("```json\n{\"a\":1}")
        assert out == "{\"a\":1}"


# ─────────────────────────────────────────────────────────
# extract_json_block
# ─────────────────────────────────────────────────────────
class TestExtractJsonBlock:
    def test_empty_returns_empty_string(self):
        assert ap.extract_json_block("") == ""

    def test_no_braces_returns_empty(self):
        assert ap.extract_json_block("nenhum JSON aqui") == ""

    def test_simple_object_extracted(self):
        assert ap.extract_json_block('prefix {"a":1} suffix') == '{"a":1}'

    def test_fenced_object_extracted(self):
        assert ap.extract_json_block('```json\n{"x": 2}\n```') == '{"x": 2}'

    def test_greedy_extracts_outermost(self):
        # A regex é gulosa: pega do primeiro `{` até o último `}`.
        raw = 'texto {"a":1} meio {"b":2} fim'
        assert ap.extract_json_block(raw) == '{"a":1} meio {"b":2}'


# ─────────────────────────────────────────────────────────
# normalize_llm_json
# ─────────────────────────────────────────────────────────
class TestNormalizeLlmJson:
    def test_empty_returns_empty(self):
        assert ap.normalize_llm_json("") == ""
        assert ap.normalize_llm_json(None) == ""  # type: ignore[arg-type]

    def test_typographic_quotes_replaced(self):
        raw = '{“a”: “b”}'
        out = ap.normalize_llm_json(raw)
        assert '“' not in out and '”' not in out
        assert '"a"' in out and '"b"' in out

    def test_trailing_comma_removed(self):
        raw = '{"a": 1,}'
        out = ap.normalize_llm_json(raw)
        assert out == '{"a": 1}'

    def test_backtick_replaced_with_double_quote(self):
        # Crase tipográfica usada por algumas LLMs como aspas.
        raw = '{`a`: 1}'
        out = ap.normalize_llm_json(raw)
        assert '`' not in out
        assert out.count('"') >= 2

    def test_missing_comma_before_next_key_after_close_brace(self):
        # Regex: `}` seguido de `"key":` ganha vírgula.
        raw = '{"a": {"x": 1} "b": 2}'
        out = ap.normalize_llm_json(raw)
        assert ', "b":' in out or ',"b":' in out

    def test_already_valid_survives(self):
        raw = '{"a": 1, "b": [2, 3]}'
        out = ap.normalize_llm_json(raw)
        assert json.loads(out) == {"a": 1, "b": [2, 3]}


# ─────────────────────────────────────────────────────────
# parse_json_block (pipeline completo)
# ─────────────────────────────────────────────────────────
class TestParseJsonBlock:
    def test_raises_when_no_block(self):
        with pytest.raises(ValueError):
            ap.parse_json_block("texto sem JSON algum")

    def test_strict_parse_happy_path(self):
        assert ap.parse_json_block('{"a": 1}') == {"a": 1}

    def test_fenced_json_parsed(self):
        raw = '```json\n{"k": "v"}\n```'
        assert ap.parse_json_block(raw) == {"k": "v"}

    def test_falls_back_to_normalized(self):
        # JSON inválido inicialmente (aspas tipográficas); normalizador arruma.
        raw = '{“a”: 1,}'
        assert ap.parse_json_block(raw) == {"a": 1}

    def test_raises_json_decode_when_normalization_also_fails(self):
        with pytest.raises(json.JSONDecodeError):
            ap.parse_json_block('{bad json without quotes}')


# ─────────────────────────────────────────────────────────
# json_looks_incomplete
# ─────────────────────────────────────────────────────────
class TestJsonLooksIncomplete:
    def test_empty_returns_false(self):
        assert ap.json_looks_incomplete("") is False
        assert ap.json_looks_incomplete(None) is False  # type: ignore[arg-type]

    def test_text_not_starting_with_brace_returns_false(self):
        assert ap.json_looks_incomplete("plain text without json") is False

    def test_balanced_closed_object_returns_false(self):
        assert ap.json_looks_incomplete('{"a": 1}') is False

    def test_unclosed_object_returns_true(self):
        assert ap.json_looks_incomplete('{"a": 1') is True

    def test_unclosed_string_returns_true(self):
        assert ap.json_looks_incomplete('{"a": "valor sem fechar') is True

    def test_unclosed_array_returns_true(self):
        assert ap.json_looks_incomplete('{"lst": [1, 2, 3') is True

    def test_escaped_quotes_inside_string_do_not_confuse(self):
        # String com aspas escapadas continua fechada corretamente.
        assert ap.json_looks_incomplete('{"a": "x\\"y"}') is False

    def test_strips_fences_before_evaluating(self):
        # Fenced JSON completo deve retornar False mesmo com cercas ```.
        assert ap.json_looks_incomplete('```json\n{"a": 1}\n```') is False

    def test_does_not_end_with_brace_returns_true(self):
        # Balanceado mas com lixo após o último `}`.
        assert ap.json_looks_incomplete('{"a": 1} trailing') is True


# ─────────────────────────────────────────────────────────
# decode_json_string_fragment
# ─────────────────────────────────────────────────────────
class TestDecodeJsonStringFragment:
    def test_simple_ascii_survives(self):
        assert ap.decode_json_string_fragment("abc") == "abc"

    def test_utf8_preserved(self):
        assert ap.decode_json_string_fragment("pediátrico") == "pediátrico"

    def test_escaped_newline_decoded(self):
        assert ap.decode_json_string_fragment("linha1\\nlinha2") == "linha1\nlinha2"

    def test_escaped_quote_decoded(self):
        assert ap.decode_json_string_fragment('ab\\"cd') == 'ab"cd'

    def test_escaped_tab_decoded(self):
        assert ap.decode_json_string_fragment("a\\tb") == "a\tb"

    def test_invalid_escape_falls_back_manually(self):
        # Backslash inválido para json.loads — fallback substitui só os
        # escapes conhecidos e mantém o resto como está.
        out = ap.decode_json_string_fragment('xyz\\q')
        assert out == 'xyz\\q'


# ─────────────────────────────────────────────────────────
# extract_visible_llm_markdown
# ─────────────────────────────────────────────────────────
class TestExtractVisibleLlmMarkdown:
    def test_empty_returns_empty(self):
        assert ap.extract_visible_llm_markdown("") == ""
        assert ap.extract_visible_llm_markdown("   \n\t ") == ""

    def test_none_returns_empty(self):
        assert ap.extract_visible_llm_markdown(None) == ""  # type: ignore[arg-type]

    def test_text_without_think_block_returned_stripped(self):
        assert ap.extract_visible_llm_markdown("  resposta final  ") == "resposta final"

    def test_open_think_without_close_returns_empty(self):
        # A LLM ainda está raciocinando — nada visível ainda.
        assert ap.extract_visible_llm_markdown("<think>estou pensando...") == ""

    def test_closed_think_block_removed(self):
        out = ap.extract_visible_llm_markdown(
            "<think>raciocínio interno</think>\nresposta visível"
        )
        assert "raciocínio" not in out
        assert "resposta visível" in out

    def test_multiple_think_blocks_all_removed(self):
        out = ap.extract_visible_llm_markdown(
            "<think>A</think>parte 1\n<think>B</think>parte 2"
        )
        assert "A" not in out and "B" not in out
        assert "parte 1" in out and "parte 2" in out

    def test_think_tag_case_insensitive(self):
        out = ap.extract_visible_llm_markdown(
            "<THINK>oculto</THINK>visível"
        )
        assert "oculto" not in out
        assert out == "visível"
