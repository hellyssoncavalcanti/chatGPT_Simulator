"""Testes offline para Scripts/correlation.py."""
import re
import pytest
import correlation as cor


class TestGenerateCorrelationId:
    def test_returns_nonempty_string(self):
        cid = cor.generate_correlation_id()
        assert isinstance(cid, str) and cid

    def test_length_is_8(self):
        assert len(cor.generate_correlation_id()) == 8

    def test_is_hexadecimal(self):
        cid = cor.generate_correlation_id()
        assert re.fullmatch(r"[0-9a-f]{8}", cid), f"not hex: {cid!r}"

    def test_unique_across_calls(self):
        ids = {cor.generate_correlation_id() for _ in range(100)}
        # Com 100 amostras de 8-hex, chance de colisão < 0.0001 %.
        assert len(ids) > 90


class TestExtractCorrelationId:
    def test_reads_header_value(self):
        cid = cor.extract_correlation_id({"X-Correlation-Id": "abc123"})
        assert cid == "abc123"

    def test_case_insensitive_via_dict_exact(self):
        # dict normal é case-sensitive; Flask headers não são.
        cid = cor.extract_correlation_id({"X-Correlation-Id": "myid"})
        assert cid == "myid"

    def test_absent_header_generates_new(self):
        cid = cor.extract_correlation_id({})
        assert isinstance(cid, str) and len(cid) == 8

    def test_absent_header_uses_fallback(self):
        cid = cor.extract_correlation_id({}, fallback="fallback-id")
        assert cid == "fallback-id"

    def test_empty_header_generates_new(self):
        cid = cor.extract_correlation_id({"X-Correlation-Id": "   "})
        assert len(cid) == 8

    def test_header_too_long_generates_new(self):
        long_id = "a" * (cor.MAX_CORRELATION_ID_LEN + 1)
        cid = cor.extract_correlation_id({"X-Correlation-Id": long_id})
        assert len(cid) == 8  # gerado novo

    def test_unsafe_chars_stripped(self):
        # Mantém alphanum + -_. e descarta ';', '<', '>' e similares.
        cid = cor.extract_correlation_id({"X-Correlation-Id": "ok-id;<script>"})
        assert ";" not in cid
        assert "<" not in cid
        assert "ok-id" in cid

    def test_all_unsafe_falls_through_to_generate(self):
        cid = cor.extract_correlation_id({"X-Correlation-Id": ";;;!!!"})
        assert len(cid) == 8

    def test_valid_uuid_format_accepted(self):
        uid = "550e8400-e29b-41d4"
        cid = cor.extract_correlation_id({"X-Correlation-Id": uid})
        assert cid == uid


class TestFormatLogPrefix:
    def test_format_includes_cid(self):
        prefix = cor.format_log_prefix("abc123")
        assert "abc123" in prefix
        assert prefix.startswith("[cid:")

    def test_format_question_mark_for_empty(self):
        prefix = cor.format_log_prefix("")
        assert "?" in prefix

    def test_format_output_stable(self):
        assert cor.format_log_prefix("x") == "[cid:x]"


class TestInjectIntoPayload:
    def test_adds_correlation_id_key(self):
        result = cor.inject_into_payload({"a": 1}, "cid123")
        assert result["correlation_id"] == "cid123"

    def test_does_not_mutate_original(self):
        original = {"a": 1}
        cor.inject_into_payload(original, "cid")
        assert "correlation_id" not in original

    def test_preserves_existing_keys(self):
        result = cor.inject_into_payload({"url": "https://a.com", "msg": "hi"}, "x")
        assert result["url"] == "https://a.com"
        assert result["msg"] == "hi"

    def test_overwrites_existing_correlation_id(self):
        result = cor.inject_into_payload({"correlation_id": "old"}, "new")
        assert result["correlation_id"] == "new"
