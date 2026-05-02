"""Testes offline para Scripts/analisador_helpers.py."""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Scripts"))
import analisador_helpers as h


# ─── stringify_compact ───────────────────────────────────────────────────────

class TestStringifyCompact:
    def test_list_joins_with_semicolons(self):
        assert h.stringify_compact(["a", "b", "c"]) == "a; b; c"

    def test_list_skips_none_and_empty(self):
        assert h.stringify_compact([None, "", [], {}, "ok"]) == "ok"

    def test_list_dicts_json_encoded(self):
        result = h.stringify_compact([{"x": 1}])
        assert '"x"' in result

    def test_dict_json_encoded(self):
        result = h.stringify_compact({"k": "v"})
        assert '"k"' in result and '"v"' in result

    def test_plain_string_stripped(self):
        assert h.stringify_compact("  hello  ") == "hello"

    def test_none_returns_empty(self):
        assert h.stringify_compact(None) == ""

    def test_json_string_list_input(self):
        assert h.stringify_compact('["x", "y"]') == "x; y"

    def test_empty_list_returns_empty(self):
        assert h.stringify_compact([]) == ""


# ─── format_compiled_value_for_prompt ────────────────────────────────────────

class TestFormatCompiledValueForPrompt:
    def test_string_normalized(self):
        result = h.format_compiled_value_for_prompt("  a  b  ")
        assert result == "a b"

    def test_string_truncated(self):
        long_str = "x" * 2000
        result = h.format_compiled_value_for_prompt(long_str, max_chars=10)
        assert len(result) == 10

    def test_list_returned_as_is(self):
        val = [1, 2, 3]
        assert h.format_compiled_value_for_prompt(val) is val

    def test_json_string_list_returned_as_list(self):
        result = h.format_compiled_value_for_prompt('["a"]')
        assert result == ["a"]

    def test_dict_returned_as_is(self):
        val = {"k": "v"}
        assert h.format_compiled_value_for_prompt(val) is val


# ─── normalize_esgotado_reason ────────────────────────────────────────────────

class TestNormalizeEsgotadoReason:
    def test_empty_returns_default(self):
        assert h.normalize_esgotado_reason("") == "Sem mensagem de erro registrada"

    def test_none_returns_default(self):
        assert h.normalize_esgotado_reason(None) == "Sem mensagem de erro registrada"

    def test_simple_message_returned(self):
        result = h.normalize_esgotado_reason("falha de conexão")
        assert result == "falha de conexão"

    def test_pipe_separated_last_valid(self):
        result = h.normalize_esgotado_reason("msg1 | msg2 | msg3")
        assert result == "msg3"

    def test_auto_reset_filtered(self):
        result = h.normalize_esgotado_reason("[AUTO-RESET tentativa 1] | falha real")
        assert result == "falha real"
        assert "[AUTO-RESET" not in result

    def test_texto_insuficiente_canonical(self):
        result = h.normalize_esgotado_reason("texto insuficiente após remoção de html")
        assert "insuficiente" in result.lower()
        assert result == "Prontuário ficou insuficiente após limpeza/remoção de HTML."

    def test_llm_json_invalido_canonical(self):
        for pattern in [
            "llm não retornou json válido",
            "expecting ',' delimiter",
            "unterminated string",
        ]:
            result = h.normalize_esgotado_reason(pattern)
            assert "JSON" in result

    def test_simulador_retornou_erro_canonical(self):
        result = h.normalize_esgotado_reason("simulador retornou erro 500")
        assert "ChatGPT Simulator" in result

    def test_long_message_truncated(self):
        long = "x" * 200
        result = h.normalize_esgotado_reason(long)
        assert len(result) <= 183  # 180 + "..."
        assert result.endswith("...")

    def test_markdown_no_retornou_canonical(self):
        result = h.normalize_esgotado_reason("simulador não retornou conteúdo markdown")
        assert "markdown" in result.lower()


# ─── group_esgotado_reasons ───────────────────────────────────────────────────

class TestGroupEsgotadoReasons:
    def test_empty_rows_returns_empty(self):
        assert h.group_esgotado_reasons([]) == []

    def test_none_rows_returns_empty(self):
        assert h.group_esgotado_reasons(None) == []

    def test_groups_and_counts(self):
        rows = [
            {"erro_msg": "falha de conexão"},
            {"erro_msg": "falha de conexão"},
            {"erro_msg": "outro erro"},
        ]
        result = h.group_esgotado_reasons(rows)
        motivos = {r["motivo"]: r["total"] for r in result}
        assert motivos.get("falha de conexão") == 2
        assert motivos.get("outro erro") == 1

    def test_max_5_results(self):
        rows = [{"erro_msg": f"erro {i}"} for i in range(20)]
        result = h.group_esgotado_reasons(rows)
        assert len(result) <= 5

    def test_result_shape(self):
        rows = [{"erro_msg": "x"}]
        result = h.group_esgotado_reasons(rows)
        assert "motivo" in result[0]
        assert "total" in result[0]


# ─── montar_resumo_fallback ───────────────────────────────────────────────────

class TestMontarResumoFallback:
    def test_empty_resumo_returns_sufixo(self):
        result = h.montar_resumo_fallback("", "2024-01-01", "Consulta inicial")
        assert result == "Consulta de 2024-01-01: Consulta inicial"

    def test_none_resumo_returns_sufixo(self):
        result = h.montar_resumo_fallback(None, "2024-01-01", "texto")
        assert "Consulta de 2024-01-01" in result

    def test_no_dt_concatenates(self):
        result = h.montar_resumo_fallback("resumo anterior", "", "nova consulta")
        assert "resumo anterior" in result
        assert "nova consulta" in result

    def test_same_content_unchanged(self):
        resumo = "Consulta de 2024-01-01: Texto exato"
        result = h.montar_resumo_fallback(resumo, "2024-01-01", "Texto exato")
        assert result == resumo

    def test_different_content_replaces_line(self):
        resumo = "Consulta de 2024-01-01: conteúdo antigo"
        result = h.montar_resumo_fallback(resumo, "2024-01-01", "conteúdo novo")
        assert "conteúdo novo" in result
        assert "conteúdo antigo" not in result

    def test_new_date_appended(self):
        resumo = "Consulta de 2024-01-01: original"
        result = h.montar_resumo_fallback(resumo, "2024-02-01", "nova")
        assert "2024-01-01" in result
        assert "2024-02-01" in result


# ─── normalizar_node ─────────────────────────────────────────────────────────

class TestNormalizarNode:
    def test_tipo_alias_diagnosis(self):
        node = h.normalizar_node({"type": "diagnosis", "value": "Hipertensão"})
        assert node["tipo"] == "diagnostico"

    def test_tipo_alias_patient(self):
        node = h.normalizar_node({"tipo": "patient", "valor": "João"})
        assert node["tipo"] == "paciente"

    def test_valor_from_label(self):
        node = h.normalizar_node({"tipo": "sintoma", "label": "dor"})
        assert node["valor"] == "dor"

    def test_normalizado_generated(self):
        node = h.normalizar_node({"tipo": "sintoma", "valor": "Dor Aguda"})
        assert node["normalizado"] == "dor_aguda"

    def test_id_generated_from_tipo_normalizado(self):
        node = h.normalizar_node({"tipo": "sintoma", "valor": "febre"})
        assert node["id"] == "sintoma_febre"

    def test_explicit_id_preserved(self):
        node = h.normalizar_node({"id": "myid", "tipo": "exame", "valor": "x"})
        assert node["id"] == "myid"

    def test_output_keys(self):
        node = h.normalizar_node({"tipo": "risco", "valor": "queda"})
        assert set(node.keys()) == {"id", "tipo", "valor", "normalizado", "contexto"}

    def test_unknown_tipo_lowercased(self):
        node = h.normalizar_node({"tipo": "CustomType", "valor": "x"})
        assert node["tipo"] == "customtype"


# ─── normalizar_edge ─────────────────────────────────────────────────────────

class TestNormalizarEdge:
    def test_source_target_mapped(self):
        edge = h.normalizar_edge({"source": "a", "target": "b", "relation": "tem"})
        assert edge["node_origem"] == "a"
        assert edge["node_destino"] == "b"
        assert edge["relacao_tipo"] == "tem"

    def test_from_to_mapped(self):
        edge = h.normalizar_edge({"from": "x", "to": "y"})
        assert edge["node_origem"] == "x"
        assert edge["node_destino"] == "y"

    def test_empty_dict_all_empty(self):
        edge = h.normalizar_edge({})
        assert edge == {
            "node_origem": "",
            "node_destino": "",
            "relacao_tipo": "",
            "relacao_contexto": "",
        }

    def test_output_keys(self):
        edge = h.normalizar_edge({})
        assert set(edge.keys()) == {
            "node_origem", "node_destino", "relacao_tipo", "relacao_contexto"
        }


# ─── deduplicar_nodes_grafo ───────────────────────────────────────────────────

class TestDeduplicarNodesGrafo:
    def test_no_duplicates_unchanged(self):
        nodes = [
            {"tipo": "sintoma", "normalizado": "febre"},
            {"tipo": "diagnostico", "normalizado": "hipertensao"},
        ]
        result = h.deduplicar_nodes_grafo(nodes)
        assert len(result) == 2

    def test_duplicates_merged(self):
        nodes = [
            {"tipo": "sintoma", "normalizado": "febre", "contexto": "ctx1"},
            {"tipo": "sintoma", "normalizado": "febre", "contexto": "ctx2"},
        ]
        result = h.deduplicar_nodes_grafo(nodes)
        assert len(result) == 1
        assert "ctx1" in result[0]["contexto"]
        assert "ctx2" in result[0]["contexto"]

    def test_skips_non_dict(self):
        result = h.deduplicar_nodes_grafo(["not_a_dict", {"tipo": "sintoma", "normalizado": "x"}])
        assert len(result) == 1

    def test_skips_nodes_without_key(self):
        nodes = [{"tipo": "sintoma"}]  # no normalizado or valor
        result = h.deduplicar_nodes_grafo(nodes)
        assert result == []

    def test_id_merged_from_duplicate(self):
        nodes = [
            {"tipo": "sintoma", "normalizado": "febre"},
            {"tipo": "sintoma", "normalizado": "febre", "id": "s1"},
        ]
        result = h.deduplicar_nodes_grafo(nodes)
        assert result[0].get("id") == "s1"


# ─── primeiro_node_representativo ────────────────────────────────────────────

class TestPrimeiroNodeRepresentativo:
    def test_diagnostico_first(self):
        nodes = [
            {"tipo": "sintoma"},
            {"tipo": "diagnostico"},
            {"tipo": "medicamento"},
        ]
        result = h.primeiro_node_representativo(nodes)
        assert result["tipo"] == "diagnostico"

    def test_fallback_to_first(self):
        nodes = [{"tipo": "paciente"}, {"tipo": "conduta"}]
        result = h.primeiro_node_representativo(nodes)
        assert result is nodes[0]

    def test_empty_returns_none(self):
        assert h.primeiro_node_representativo([]) is None


# ─── ensure_list ─────────────────────────────────────────────────────────────

class TestEnsureList:
    def test_none_returns_empty(self):
        assert h.ensure_list(None) == []

    def test_list_returned_as_is(self):
        val = [1, 2, 3]
        assert h.ensure_list(val) is val

    def test_json_string_list(self):
        assert h.ensure_list('["a", "b"]') == ["a", "b"]

    def test_json_string_dict_returns_empty(self):
        assert h.ensure_list('{"k": "v"}') == []

    def test_invalid_json_returns_empty(self):
        assert h.ensure_list("not json") == []

    def test_non_list_non_string_returns_empty(self):
        assert h.ensure_list(42) == []


# ─── is_grafo_generico ────────────────────────────────────────────────────────

class TestIsGrafoGenerico:
    def test_empty_result_is_generic(self):
        assert h.is_grafo_generico({}) is True

    def test_no_nodes_is_generic(self):
        assert h.is_grafo_generico({"grafo_clinico_nodes": []}) is True

    def test_one_relevant_node_is_generic(self):
        resultado = {
            "grafo_clinico_nodes": [{"tipo": "diagnostico", "valor": "x"}]
        }
        assert h.is_grafo_generico(resultado) is True

    def test_two_relevant_nodes_not_generic(self):
        resultado = {
            "grafo_clinico_nodes": [
                {"tipo": "diagnostico", "valor": "x"},
                {"tipo": "sintoma", "valor": "y"},
            ]
        }
        assert h.is_grafo_generico(resultado) is False

    def test_irrelevant_tipos_are_generic(self):
        resultado = {
            "grafo_clinico_nodes": [
                {"tipo": "paciente", "valor": "João"},
                {"tipo": "conduta", "valor": "repouso"},
            ]
        }
        assert h.is_grafo_generico(resultado) is True

    def test_nodes_without_valor_are_generic(self):
        resultado = {
            "grafo_clinico_nodes": [
                {"tipo": "diagnostico"},
                {"tipo": "sintoma"},
            ]
        }
        assert h.is_grafo_generico(resultado) is True


# ─── strip_html ───────────────────────────────────────────────────────────────

class TestStripHtml:
    def test_plain_text_unchanged(self):
        assert h.strip_html("hello world") == "hello world"

    def test_tags_removed(self):
        result = h.strip_html("<b>texto</b>")
        assert result == "texto"

    def test_entities_decoded(self):
        result = h.strip_html("hello &amp; world")
        assert "&" in result

    def test_empty_string(self):
        assert h.strip_html("") == ""

    def test_none_safe(self):
        assert h.strip_html(None) == ""

    def test_multiple_spaces_collapsed(self):
        result = h.strip_html("<p>a</p>   <p>b</p>")
        assert "   " not in result


# ─── is_llm_connection_error ─────────────────────────────────────────────────

class TestIsLlmConnectionError:
    def test_connection_reset_error(self):
        assert h.is_llm_connection_error(ConnectionResetError()) is True

    def test_broken_pipe(self):
        assert h.is_llm_connection_error(BrokenPipeError()) is True

    def test_timeout_error(self):
        assert h.is_llm_connection_error(TimeoutError()) is True

    def test_regular_exception_false(self):
        assert h.is_llm_connection_error(ValueError("bad")) is False

    def test_max_retries_in_message(self):
        exc = Exception("Max retries exceeded for URL")
        assert h.is_llm_connection_error(exc) is True

    def test_connection_reset_in_message(self):
        exc = Exception("Connection reset by peer")
        assert h.is_llm_connection_error(exc) is True

    def test_failed_to_establish_in_message(self):
        exc = Exception("Failed to establish a new connection")
        assert h.is_llm_connection_error(exc) is True
