"""Testes offline de Scripts/error_scanner_helpers.py."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Scripts"))

from error_scanner_helpers import (  # noqa: E402
    UNWANTED_SNIPPET_KEYS,
    build_claude_fix_empty_stream_lines,
    build_claude_fix_error_line,
    build_claude_fix_finish_line,
    build_claude_fix_prompt,
    build_claude_fix_request_body,
    build_claude_fix_status_line,
    build_known_errors_error_payload,
    build_known_errors_loaded_payload,
    build_known_errors_missing_payload,
    build_scan_error_entry,
    build_scan_match_entry,
    is_unwanted_snippet,
)


class TestUnwantedSnippetKeys(unittest.TestCase):
    def test_constant_shape(self):
        self.assertEqual(
            UNWANTED_SNIPPET_KEYS,
            ("known_entry", "truncated", "read_error"),
        )


class TestIsUnwantedSnippet(unittest.TestCase):
    def test_known_entry_true(self):
        self.assertTrue(is_unwanted_snippet({"known_entry": True}))

    def test_truncated_true(self):
        self.assertTrue(is_unwanted_snippet({"truncated": True}))

    def test_read_error_true(self):
        self.assertTrue(is_unwanted_snippet({"read_error": True}))

    def test_known_entry_truthy_dict(self):
        self.assertTrue(is_unwanted_snippet({"known_entry": {"id": 1}}))

    def test_clean_snippet(self):
        self.assertFalse(is_unwanted_snippet({"line_num": 5, "context": "x"}))

    def test_all_falsy(self):
        self.assertFalse(is_unwanted_snippet({
            "known_entry": False,
            "truncated": None,
            "read_error": 0,
        }))

    def test_empty_dict(self):
        self.assertFalse(is_unwanted_snippet({}))

    def test_non_mapping_returns_false(self):
        self.assertFalse(is_unwanted_snippet(None))
        self.assertFalse(is_unwanted_snippet("known_entry"))
        self.assertFalse(is_unwanted_snippet(42))
        self.assertFalse(is_unwanted_snippet(["known_entry"]))


class TestBuildScanMatchEntry(unittest.TestCase):
    def test_basic(self):
        snippet = {"line_num": 12, "severity": "error", "context": "Traceback"}
        entry = build_scan_match_entry("server", "server.log", snippet)
        self.assertEqual(entry, {
            "system": "server",
            "log_file": "server.log",
            "line_num": 12,
            "severity": "error",
            "context": "Traceback",
        })

    def test_missing_keys_default_to_none_or_empty(self):
        entry = build_scan_match_entry("s", "f.log", {})
        self.assertEqual(entry["system"], "s")
        self.assertEqual(entry["log_file"], "f.log")
        self.assertIsNone(entry["line_num"])
        self.assertIsNone(entry["severity"])
        self.assertEqual(entry["context"], "")

    def test_context_none_coerces_to_empty_string(self):
        entry = build_scan_match_entry("s", "f.log", {"context": None})
        self.assertEqual(entry["context"], "")

    def test_non_mapping_snippet_falls_back_to_placeholders(self):
        entry = build_scan_match_entry("s", "f.log", "string-snippet")
        self.assertIsNone(entry["line_num"])
        self.assertIsNone(entry["severity"])
        self.assertEqual(entry["context"], "")

    def test_preserves_unicode(self):
        snippet = {"context": "Erro: símbolo inválido — α"}
        entry = build_scan_match_entry("s", "f.log", snippet)
        self.assertEqual(entry["context"], "Erro: símbolo inválido — α")


class TestBuildScanErrorEntry(unittest.TestCase):
    def test_exception_object(self):
        entry = build_scan_error_entry("s", "f.log", RuntimeError("boom"))
        self.assertEqual(entry["system"], "s")
        self.assertEqual(entry["log_file"], "f.log")
        self.assertEqual(entry["line_num"], 0)
        self.assertEqual(entry["severity"], "error")
        self.assertEqual(entry["context"], "[scan_file error] boom")

    def test_string_error(self):
        entry = build_scan_error_entry("s", "f.log", "permission denied")
        self.assertEqual(entry["context"], "[scan_file error] permission denied")

    def test_shape_matches_match_entry_keys(self):
        match_entry = build_scan_match_entry("s", "f.log", {"line_num": 1})
        error_entry = build_scan_error_entry("s", "f.log", "x")
        self.assertEqual(set(match_entry.keys()), set(error_entry.keys()))


class TestBuildClaudeFixPrompt(unittest.TestCase):
    def test_empty_list(self):
        prompt = build_claude_fix_prompt([])
        self.assertIn("0 erro(s) novo(s)", prompt)
        self.assertIn("=== 0 ERRO(S) NOVO(S) ===", prompt)
        # Sem detalhes de erros
        self.assertNotIn("--- ERRO #", prompt)

    def test_none_input_treated_as_empty(self):
        prompt = build_claude_fix_prompt(None)
        self.assertIn("0 erro(s) novo(s)", prompt)

    def test_invalid_input_treated_as_empty(self):
        prompt = build_claude_fix_prompt(12345)
        self.assertIn("0 erro(s) novo(s)", prompt)

    def test_single_error_full_shape(self):
        err = {
            "severity": "error",
            "system": "browser",
            "line_num": 42,
            "log_file": "browser.log",
            "context": "Traceback line\n  more\n",
        }
        prompt = build_claude_fix_prompt([err])
        self.assertIn("1 erro(s) novo(s)", prompt)
        self.assertIn("--- ERRO #1: [error] browser:42 ---", prompt)
        self.assertIn("Arquivo de log: logs/browser.log", prompt)
        self.assertIn("Traceback line", prompt)
        # Stripping trailing whitespace na coleta — não termina com `\n\n```` (apenas um `\n` antes do fence)
        self.assertNotIn("more\n\n```", prompt)

    def test_multiple_errors_numbered_sequentially(self):
        errs = [
            {"severity": "warn", "system": "a", "line_num": 1, "log_file": "a.log", "context": "x"},
            {"severity": "error", "system": "b", "line_num": 2, "log_file": "b.log", "context": "y"},
            {"severity": "info", "system": "c", "line_num": 3, "log_file": "c.log", "context": "z"},
        ]
        prompt = build_claude_fix_prompt(errs)
        self.assertIn("--- ERRO #1: [warn] a:1 ---", prompt)
        self.assertIn("--- ERRO #2: [error] b:2 ---", prompt)
        self.assertIn("--- ERRO #3: [info] c:3 ---", prompt)
        self.assertIn("3 erro(s) novo(s)", prompt)

    def test_missing_fields_use_placeholders(self):
        prompt = build_claude_fix_prompt([{}])
        self.assertIn("--- ERRO #1: [?] :? ---", prompt)
        self.assertIn("Arquivo de log: logs/", prompt)

    def test_non_mapping_error_uses_placeholders(self):
        prompt = build_claude_fix_prompt(["not-a-dict"])
        self.assertIn("--- ERRO #1: [?] :? ---", prompt)

    def test_deterministic(self):
        err = {"severity": "error", "system": "x", "line_num": 10, "log_file": "x.log", "context": "ctx"}
        a = build_claude_fix_prompt([err, err])
        b = build_claude_fix_prompt([err, err])
        self.assertEqual(a, b)

    def test_iterator_input(self):
        gen = (
            {"severity": "error", "system": "g", "line_num": i, "log_file": "g.log", "context": "k"}
            for i in range(2)
        )
        prompt = build_claude_fix_prompt(gen)
        self.assertIn("--- ERRO #1: [error] g:0 ---", prompt)
        self.assertIn("--- ERRO #2: [error] g:1 ---", prompt)

    def test_context_none_renders_empty_block(self):
        err = {"severity": "x", "system": "s", "line_num": 1, "log_file": "f.log", "context": None}
        prompt = build_claude_fix_prompt([err])
        # Bloco ```...``` presente, mas sem conteúdo
        self.assertIn("```\n\n```", prompt)


class TestBuildClaudeFixRequestBody(unittest.TestCase):
    def test_basic_shape(self):
        body = build_claude_fix_request_body(
            api_key="k123",
            prompt="P",
            target_url="https://claude.ai/code",
            claude_project="chatGPT_Simulator",
        )
        self.assertEqual(body["api_key"], "k123")
        self.assertEqual(body["model"], "Claude Code")
        self.assertEqual(body["message"], "P")
        self.assertEqual(body["messages"], [{"role": "user", "content": "P"}])
        self.assertTrue(body["stream"])
        self.assertEqual(body["url"], "https://claude.ai/code")
        self.assertEqual(body["origin_url"], "https://claude.ai/code")
        self.assertEqual(body["claude_project"], "chatGPT_Simulator")
        self.assertEqual(body["request_source"], "errors_monitor.py/claude_fix")

    def test_custom_request_source(self):
        body = build_claude_fix_request_body(
            api_key="k",
            prompt="x",
            target_url="u",
            claude_project="p",
            request_source="custom/source",
        )
        self.assertEqual(body["request_source"], "custom/source")

    def test_custom_model(self):
        body = build_claude_fix_request_body(
            api_key="k",
            prompt="x",
            target_url="u",
            claude_project="p",
            model="gpt-5",
        )
        self.assertEqual(body["model"], "gpt-5")

    def test_message_and_messages_are_consistent(self):
        body = build_claude_fix_request_body(
            api_key="k",
            prompt="hello",
            target_url="u",
            claude_project="p",
        )
        # message duplicado em messages para compat com fluxos antigos
        self.assertEqual(body["message"], body["messages"][0]["content"])

    def test_each_call_returns_new_dict(self):
        a = build_claude_fix_request_body(
            api_key="k", prompt="P", target_url="u", claude_project="p",
        )
        b = build_claude_fix_request_body(
            api_key="k", prompt="P", target_url="u", claude_project="p",
        )
        self.assertIsNot(a, b)
        self.assertIsNot(a["messages"], b["messages"])


class TestBuildKnownErrorsMissingPayload(unittest.TestCase):
    def test_basic_shape(self):
        payload = build_known_errors_missing_payload("/some/path.json")
        self.assertEqual(payload, {
            "success": True,
            "entries": [],
            "count": 0,
            "path": "/some/path.json",
            "missing": True,
        })

    def test_path_object_coerces_to_string(self):
        from pathlib import Path as P
        payload = build_known_errors_missing_payload(P("/x/y/z.json"))
        self.assertIsInstance(payload["path"], str)
        self.assertTrue(payload["path"].endswith("z.json"))

    def test_each_call_returns_new_dict(self):
        a = build_known_errors_missing_payload("p")
        b = build_known_errors_missing_payload("p")
        self.assertIsNot(a, b)


class TestBuildKnownErrorsLoadedPayload(unittest.TestCase):
    def test_full_data(self):
        data = {"entries": [{"id": 1}, {"id": 2}], "version": "v3"}
        payload = build_known_errors_loaded_payload(data)
        self.assertEqual(payload, {
            "success": True,
            "entries": [{"id": 1}, {"id": 2}],
            "count": 2,
            "version": "v3",
        })

    def test_missing_entries(self):
        payload = build_known_errors_loaded_payload({"version": "v1"})
        self.assertEqual(payload["entries"], [])
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["version"], "v1")

    def test_entries_is_none(self):
        payload = build_known_errors_loaded_payload({"entries": None})
        self.assertEqual(payload["entries"], [])
        self.assertEqual(payload["count"], 0)

    def test_non_mapping_input(self):
        payload = build_known_errors_loaded_payload("not-a-dict")
        self.assertEqual(payload, {
            "success": True, "entries": [], "count": 0, "version": None,
        })

    def test_entries_iterable_coerces_to_list(self):
        payload = build_known_errors_loaded_payload({"entries": iter([1, 2, 3])})
        self.assertEqual(payload["entries"], [1, 2, 3])
        self.assertEqual(payload["count"], 3)


class TestBuildKnownErrorsErrorPayload(unittest.TestCase):
    def test_exception_object(self):
        payload = build_known_errors_error_payload(RuntimeError("boom"))
        self.assertEqual(payload, {"success": False, "error": "boom"})

    def test_string_input(self):
        payload = build_known_errors_error_payload("custom message")
        self.assertEqual(payload["error"], "custom message")


class TestBuildClaudeFixEmptyStreamLines(unittest.TestCase):
    def test_count_zero(self):
        lines = list(build_claude_fix_empty_stream_lines(0))
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0].rstrip("\n"))
        second = json.loads(lines[1].rstrip("\n"))
        self.assertEqual(first["type"], "markdown")
        self.assertIn("0 erro(s) conhecido(s)", first["content"])
        self.assertEqual(second, {"type": "finish", "content": {}})

    def test_count_positive(self):
        lines = list(build_claude_fix_empty_stream_lines(42))
        first = json.loads(lines[0].rstrip("\n"))
        self.assertIn("42 erro(s) conhecido(s)", first["content"])

    def test_invalid_count_falls_back_to_zero(self):
        lines = list(build_claude_fix_empty_stream_lines("not-a-number"))
        first = json.loads(lines[0].rstrip("\n"))
        self.assertIn("0 erro(s) conhecido(s)", first["content"])

    def test_lines_terminated_with_newline(self):
        lines = list(build_claude_fix_empty_stream_lines(5))
        for ln in lines:
            self.assertTrue(ln.endswith("\n"))


class TestBuildClaudeFixStatusLine(unittest.TestCase):
    def test_basic(self):
        line = build_claude_fix_status_line(3)
        self.assertTrue(line.endswith("\n"))
        decoded = json.loads(line.rstrip("\n"))
        self.assertEqual(decoded["type"], "status")
        self.assertIn("Enviando 3 erro(s)", decoded["content"])

    def test_invalid_input_zero_fallback(self):
        line = build_claude_fix_status_line(None)
        decoded = json.loads(line.rstrip("\n"))
        self.assertIn("Enviando 0 erro(s)", decoded["content"])

    def test_string_count_coerced(self):
        line = build_claude_fix_status_line("7")
        decoded = json.loads(line.rstrip("\n"))
        self.assertIn("Enviando 7 erro(s)", decoded["content"])


class TestBuildClaudeFixErrorLine(unittest.TestCase):
    def test_exception(self):
        line = build_claude_fix_error_line(RuntimeError("kaboom"))
        decoded = json.loads(line.rstrip("\n"))
        self.assertEqual(decoded["type"], "error")
        self.assertIn("kaboom", decoded["content"])
        self.assertIn("Falha ao chamar Claude Code", decoded["content"])

    def test_string_error(self):
        line = build_claude_fix_error_line("network down")
        decoded = json.loads(line.rstrip("\n"))
        self.assertIn("network down", decoded["content"])


class TestBuildClaudeFixFinishLine(unittest.TestCase):
    def test_canonical_finish(self):
        line = build_claude_fix_finish_line()
        self.assertTrue(line.endswith("\n"))
        decoded = json.loads(line.rstrip("\n"))
        self.assertEqual(decoded, {"type": "finish", "content": {}})

    def test_idempotent(self):
        # Mesma string a cada chamada (sem timestamps/randoms).
        a = build_claude_fix_finish_line()
        b = build_claude_fix_finish_line()
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
