import browser_predicates as bp


class TestExtractTaskSender:
    def test_uses_sender_key(self):
        assert bp.extract_task_sender({"sender": "analisador"}) == "analisador"

    def test_falls_back_to_request_source(self):
        assert bp.extract_task_sender({"request_source": "script.py"}) == "script.py"

    def test_falls_back_to_remetente(self):
        assert bp.extract_task_sender({"remetente": "whatsapp"}) == "whatsapp"

    def test_priority_order_sender_over_request_source(self):
        assert bp.extract_task_sender(
            {"sender": "a", "request_source": "b", "remetente": "c"}
        ) == "a"

    def test_empty_values_fall_through(self):
        assert bp.extract_task_sender({"sender": "", "request_source": "x"}) == "x"

    def test_none_or_non_dict_default(self):
        assert bp.extract_task_sender(None) == "usuario_remoto"
        assert bp.extract_task_sender("string") == "usuario_remoto"
        assert bp.extract_task_sender({}) == "usuario_remoto"

    def test_strips_whitespace(self):
        assert bp.extract_task_sender({"sender": "  analisador  "}) == "analisador"


class TestIsKnownOrphanTabUrl:
    def test_matches_pediatrica_pdf(self):
        assert bp.is_known_orphan_tab_url(
            "https://residenciapediatrica.com.br/content/pdf/paper-42.pdf"
        )

    def test_case_insensitive(self):
        assert bp.is_known_orphan_tab_url(
            "HTTPS://RESIDENCIAPEDIATRICA.COM.BR/CONTENT/PDF/X"
        )

    def test_non_matching_url(self):
        assert not bp.is_known_orphan_tab_url("https://chatgpt.com/c/abc")

    def test_empty_and_none(self):
        assert not bp.is_known_orphan_tab_url("")
        assert not bp.is_known_orphan_tab_url(None)

    def test_strips_whitespace(self):
        assert bp.is_known_orphan_tab_url(
            "   https://residenciapediatrica.com.br/content/pdf/x   "
        )


class TestResponseLooksIncompleteJson:
    def test_complete_object_is_not_incomplete(self):
        assert not bp.response_looks_incomplete_json('{"k":"v"}')

    def test_missing_closing_brace(self):
        assert bp.response_looks_incomplete_json('{"k":"v"')

    def test_fenced_complete_object(self):
        assert not bp.response_looks_incomplete_json('```json\n{"k":"v"}\n```')

    def test_fenced_incomplete_object(self):
        assert bp.response_looks_incomplete_json('```json\n{"k":"v"\n```')

    def test_open_string_flag(self):
        # String sem fechamento final.
        assert bp.response_looks_incomplete_json('{"k":"value')

    def test_non_json_text_is_not_incomplete(self):
        # Não começa com `{` → heurística assume que não é JSON puro.
        assert not bp.response_looks_incomplete_json("Texto livre com } e }.")

    def test_empty_input(self):
        assert not bp.response_looks_incomplete_json("")
        assert not bp.response_looks_incomplete_json(None)

    def test_nested_array_incomplete(self):
        assert bp.response_looks_incomplete_json('{"arr":[1,2,3')


class TestResponseRequestsFollowupActions:
    def test_sql_queries_triggers(self):
        assert bp.response_requests_followup_actions('{"sql_queries": ["SELECT 1"]}')

    def test_search_queries_triggers(self):
        assert bp.response_requests_followup_actions('{"search_queries": ["abc"]}')

    def test_tool_calls_triggers(self):
        assert bp.response_requests_followup_actions('{"tool_calls": [{}]}')

    def test_queries_sql_triggers(self):
        assert bp.response_requests_followup_actions('{"queries_sql": []}')

    def test_fenced_payload_triggers(self):
        assert bp.response_requests_followup_actions(
            '```json\n{"sql_queries": ["SELECT 1"]}\n```'
        )

    def test_final_markdown_does_not_trigger(self):
        assert not bp.response_requests_followup_actions(
            "# Resposta final\n\nNenhuma ferramenta necessária."
        )

    def test_empty_input(self):
        assert not bp.response_requests_followup_actions("")
        assert not bp.response_requests_followup_actions(None)


class TestReplaceInlineBase64Payloads:
    def test_data_url_image_is_replaced(self):
        text = "prefix data:image/png;base64," + ("A" * 200) + " suffix"
        out, count = bp.replace_inline_base64_payloads(text)
        assert count == 1
        assert "[BASE64_IMAGE_REMOVIDA]" in out
        assert "A" * 120 not in out

    def test_short_base64_not_replaced(self):
        text = "data:image/png;base64,AAAA"
        out, count = bp.replace_inline_base64_payloads(text)
        assert count == 0
        assert out == text

    def test_json_field_base64_is_replaced(self):
        text = '{"data_base64":"' + ("B" * 200) + '"}'
        out, count = bp.replace_inline_base64_payloads(text)
        assert count == 1
        assert '"data_base64":"[BASE64_IMAGE_REMOVIDA]"' in out

    def test_empty_input(self):
        out, count = bp.replace_inline_base64_payloads("")
        assert out == ""
        assert count == 0

    def test_none_input(self):
        out, count = bp.replace_inline_base64_payloads(None)
        assert out is None
        assert count == 0

    def test_data_url_and_json_field_both_replaced(self):
        big = "A" * 200
        text = (
            f'prefix data:image/png;base64,{big}|'
            f'{{"data_base64":"{big}"}}'
        )
        out, count = bp.replace_inline_base64_payloads(text)
        # Uma substituição via data-URL regex + uma via JSON-field regex.
        assert count == 2
        assert out.count("[BASE64_IMAGE_REMOVIDA]") == 2


class TestEnsurePasteWrappers:
    def test_wraps_unmarked_text(self):
        out, wrapped = bp.ensure_paste_wrappers("hello")
        assert wrapped is True
        assert out.startswith(bp.PASTE_START_MARKER)
        assert out.endswith(bp.PASTE_END_MARKER)

    def test_already_wrapped_untouched(self):
        already = f"{bp.PASTE_START_MARKER}x{bp.PASTE_END_MARKER}"
        out, wrapped = bp.ensure_paste_wrappers(already)
        assert wrapped is False
        assert out == already

    def test_blank_input_not_wrapped(self):
        out, wrapped = bp.ensure_paste_wrappers("   \n  ")
        assert wrapped is False
        # conteúdo original preservado (apenas str() aplicado).
        assert out == "   \n  "

    def test_none_input_treated_as_empty(self):
        out, wrapped = bp.ensure_paste_wrappers(None)
        assert wrapped is False
        assert out == ""

    def test_partial_marker_is_wrapped(self):
        # Apenas o marcador de início não conta como "já envolvido".
        out, wrapped = bp.ensure_paste_wrappers(f"{bp.PASTE_START_MARKER}abc")
        assert wrapped is True
        assert out.count(bp.PASTE_START_MARKER) == 2

    def test_preserves_unicode(self):
        out, _ = bp.ensure_paste_wrappers("olá 😀")
        assert "olá 😀" in out
