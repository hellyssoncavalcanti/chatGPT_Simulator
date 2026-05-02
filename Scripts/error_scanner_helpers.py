"""Helpers puros para os endpoints de /api/errors/*.

Módulo isolado dos handlers Flask. Concentra:
- Filtragem canônica de snippets do log_scanner (`is_unwanted_snippet`).
- Conversão de snippets em entradas de erro para resposta HTTP
  (`build_scan_match_entry`).
- Conversão de exceções de scan_file em entrada de erro
  (`build_scan_error_entry`).
- Construção do prompt a ser enviado ao Claude Code para análise/correção
  (`build_claude_fix_prompt`).
- Construção do body a ser POSTado em /v1/chat/completions
  (`build_claude_fix_request_body`).
- Payloads padronizados de /api/errors/known
  (`build_known_errors_missing_payload`, `build_known_errors_loaded_payload`,
  `build_known_errors_error_payload`).
- Linhas NDJSON do stream de /api/errors/claude_fix
  (`build_claude_fix_empty_stream_lines`, `build_claude_fix_status_line`,
  `build_claude_fix_error_line`, `build_claude_fix_finish_line`).

Sem `flask`, `playwright`, `config` ou IO no import. Determinístico e testável
offline.
"""

from __future__ import annotations

import json
from typing import Iterable, Mapping, Optional


__all__ = [
    "UNWANTED_SNIPPET_KEYS",
    "is_unwanted_snippet",
    "build_scan_match_entry",
    "build_scan_error_entry",
    "build_claude_fix_prompt",
    "build_claude_fix_request_body",
    "build_known_errors_missing_payload",
    "build_known_errors_loaded_payload",
    "build_known_errors_error_payload",
    "build_claude_fix_empty_stream_lines",
    "build_claude_fix_status_line",
    "build_claude_fix_error_line",
    "build_claude_fix_finish_line",
]


# Conjunto canônico de keys usadas pelo log_scanner para sinalizar snippets que
# não devem virar "novo erro" no resultado da varredura. Mantido aqui para que
# qualquer handler use exatamente o mesmo critério.
UNWANTED_SNIPPET_KEYS = ("known_entry", "truncated", "read_error")


def is_unwanted_snippet(snippet) -> bool:
    """Retorna True se o snippet do log_scanner deve ser descartado.

    Critério: presença de qualquer flag truthy entre `known_entry`, `truncated`
    ou `read_error`. Tolera entradas que não suportam `.get` (retorna False —
    deixa o caller decidir como tratar valores anômalos sem quebrar o loop).
    """
    if not isinstance(snippet, Mapping):
        return False
    for key in UNWANTED_SNIPPET_KEYS:
        try:
            if snippet.get(key):
                return True
        except Exception:
            continue
    return False


def build_scan_match_entry(system, log_file_name, snippet) -> dict:
    """Constrói dict de erro a partir de um match do log_scanner.

    Aceita `snippet` como Mapping (uso típico). Para entradas anômalas, devolve
    placeholders vazios em vez de levantar — alinhado com `is_unwanted_snippet`.
    """
    line_num = None
    severity = None
    context = ""
    if isinstance(snippet, Mapping):
        try:
            line_num = snippet.get("line_num")
        except Exception:
            line_num = None
        try:
            severity = snippet.get("severity")
        except Exception:
            severity = None
        try:
            context = snippet.get("context", "") or ""
        except Exception:
            context = ""
    return {
        "system": system,
        "log_file": log_file_name,
        "line_num": line_num,
        "severity": severity,
        "context": context,
    }


def build_scan_error_entry(system, log_file_name, error) -> dict:
    """Constrói dict de erro para o caso de exceção em scan_file().

    Mantém shape compatível com `build_scan_match_entry` (mesmas chaves) para
    o frontend consumir sem ramificação extra.
    """
    return {
        "system": system,
        "log_file": log_file_name,
        "line_num": 0,
        "severity": "error",
        "context": f"[scan_file error] {error}",
    }


def build_claude_fix_prompt(new_errors) -> str:
    """Constrói o prompt enviado ao Claude Code para analisar+corrigir erros.

    Determinístico: mesma lista produz mesmo string byte-a-byte. Não depende de
    config nem ambiente — é seguro chamar offline.
    """
    if new_errors is None:
        new_errors = []
    if not isinstance(new_errors, list):
        try:
            new_errors = list(new_errors)
        except Exception:
            new_errors = []
    n = len(new_errors)
    head = [
        "Você é Claude Code, assistente de desenvolvimento autônomo do projeto chatGPT_Simulator.",
        "",
        f"Foram detectados {n} erro(s) novo(s) nos logs do projeto, ainda NÃO registrados em Scripts/erros_conhecidos.json.",
        "",
        "TAREFA (execute na ordem, sem perguntar):",
        "  1. Para cada erro abaixo, leia APENAS as linhas relevantes do código (use offset+limit). NUNCA leia arquivos .log inteiros.",
        "  2. Identifique a causa-raiz e aplique a CORREÇÃO MÍNIMA necessária diretamente nos arquivos.",
        "  3. Para cada erro tratado, registre no banco de erros conhecidos imediatamente após corrigir:",
        "       python Scripts/log_scanner.py --add-known \"<trecho do log>\" --status fixed --description \"<o que era>\" --fix \"<o que foi feito>\" --files \"<Scripts/arquivo.py>\"",
        "  4. Falsos positivos: registre com --status false_positive e --description \"<por que não é erro>\".",
        "  5. Ao final de TODAS as correções:",
        "       a. Crie UM commit consolidado com mensagem 'fix: corrige <N> erros detectados pelo log_scanner' (use HEREDOC para a mensagem).",
        "       b. Faça push do commit para o remote.",
        "       c. Abra um Pull Request no GitHub via `gh pr create` com:",
        "          - título curto e descritivo",
        "          - corpo listando cada correção (arquivo:linha → o que foi feito)",
        "  6. Reporte ao final desta resposta:",
        "       - URL do PR criado",
        "       - lista de correções aplicadas (arquivo:linha)",
        "       - erros sem correção (com motivo)",
        "",
        "REGRAS:",
        "  - Não pergunte por confirmação — execute autonomamente.",
        "  - Não modifique arquivos fora de Scripts/, frontend/ ou raiz do projeto.",
        "  - Se um erro for irreproduzível ou exigir contexto externo, registre-o como suppressed/monitoring com explicação.",
        "",
        f"=== {n} ERRO(S) NOVO(S) ===",
        "",
    ]
    for i, e in enumerate(new_errors, 1):
        if not isinstance(e, Mapping):
            severity = "?"
            system = ""
            line_num = "?"
            log_file = ""
            context = ""
        else:
            severity = e.get("severity", "?")
            system = e.get("system", "")
            line_num = e.get("line_num", "?")
            log_file = e.get("log_file", "")
            context = (e.get("context", "") or "")
        head.extend([
            f"--- ERRO #{i}: [{severity}] {system}:{line_num} ---",
            f"Arquivo de log: logs/{log_file}",
            "Trecho do log:",
            "```",
            context.rstrip(),
            "```",
            "",
        ])
    return "\n".join(head)


def build_claude_fix_request_body(
    api_key: str,
    prompt: str,
    target_url: str,
    claude_project: str,
    *,
    request_source: str = "errors_monitor.py/claude_fix",
    model: str = "Claude Code",
) -> dict:
    """Constrói o body POST para `/v1/chat/completions` do fluxo claude_fix.

    Receber api_key/target_url/claude_project explícitos mantém o módulo puro.
    """
    return {
        "api_key": api_key,
        "model": model,
        "message": prompt,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "url": target_url,
        "origin_url": target_url,
        "claude_project": claude_project,
        "request_source": request_source,
    }


def build_known_errors_missing_payload(json_path) -> dict:
    """Resposta padrão quando `erros_conhecidos.json` não existe."""
    return {
        "success": True,
        "entries": [],
        "count": 0,
        "path": str(json_path),
        "missing": True,
    }


def build_known_errors_loaded_payload(data) -> dict:
    """Resposta padrão quando o JSON foi carregado com sucesso.

    Aceita `data` como Mapping (caso típico). Para entradas anômalas, retorna
    payload vazio mas marcado como sucesso (não levanta).
    """
    if not isinstance(data, Mapping):
        return {"success": True, "entries": [], "count": 0, "version": None}
    try:
        entries = data.get("entries", []) or []
    except Exception:
        entries = []
    if not isinstance(entries, list):
        try:
            entries = list(entries)
        except Exception:
            entries = []
    try:
        version = data.get("version")
    except Exception:
        version = None
    return {
        "success": True,
        "entries": entries,
        "count": len(entries),
        "version": version,
    }


def build_known_errors_error_payload(error) -> dict:
    """Resposta padrão para erros de leitura/parse do JSON conhecido."""
    return {"success": False, "error": str(error)}


def build_claude_fix_empty_stream_lines(known_count: int) -> Iterable[str]:
    """Sequência NDJSON quando não há erros novos para enviar ao Claude.

    Retorna lista (ordenada e curta) com as 2 linhas terminadas em `\\n`:
    1. um markdown explicando que não há erros novos;
    2. o frame de finish padrão.
    """
    if not isinstance(known_count, int):
        try:
            known_count = int(known_count)
        except Exception:
            known_count = 0
    markdown = json.dumps(
        {
            "type": "markdown",
            "content": (
                "✅ Nenhum erro novo encontrado para análise. "
                f"({known_count} erro(s) conhecido(s) no banco)"
            ),
        }
    ) + "\n"
    return [markdown, build_claude_fix_finish_line()]


def build_claude_fix_status_line(new_errors_count: int) -> str:
    """Linha NDJSON inicial do stream do proxy /v1/chat/completions."""
    if not isinstance(new_errors_count, int):
        try:
            new_errors_count = int(new_errors_count)
        except Exception:
            new_errors_count = 0
    return json.dumps(
        {
            "type": "status",
            "content": f"Enviando {new_errors_count} erro(s) ao Claude Code...",
        }
    ) + "\n"


def build_claude_fix_error_line(error) -> str:
    """Linha NDJSON de erro do proxy quando falha a chamada ao Claude."""
    return json.dumps(
        {"type": "error", "content": f"Falha ao chamar Claude Code: {error}"}
    ) + "\n"


def build_claude_fix_finish_line() -> str:
    """Linha NDJSON final canônica do fluxo claude_fix."""
    return json.dumps({"type": "finish", "content": {}}) + "\n"
