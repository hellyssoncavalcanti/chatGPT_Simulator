"""Parsers puros de resposta LLM extraídos de `analisador_prontuarios.py`.

Responsabilidades:
- Remoção de cercas de código markdown (```json ... ```).
- Extração do primeiro bloco JSON aparente.
- Normalização tolerante de JSON quase-válido (aspas tipográficas,
  vírgulas faltantes, trailing commas).
- Parse JSON com duas passagens (strict → normalize → retry).
- Detecção de rate-limit em texto da resposta via callback injetável.

Este módulo é **puro**:
  - sem `requests`, `openai`, `playwright`, `flask`;
  - sem `config`;
  - depende apenas de `json` e `re` da biblioteca padrão.

O matcher de rate-limit é passado por **injeção de dependência** — a
decisão fica com o chamador (hoje `_resposta_eh_rate_limit` em
`analisador_prontuarios.py`, que já delega para `error_catalog`).

Os wrappers em `analisador_prontuarios.py` preservam nomes/assinaturas
originais (padrão A já validado em `request_source`, `error_catalog`,
`server_helpers`, `browser_predicates`, `log_sanitizer`).
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

# Formato histórico da mensagem — alinhado à linha hoje em
# `_verificar_rate_limit_no_markdown`. Mudar isto quebra logs externos.
_RATE_LIMIT_PREVIEW_CHARS = 120
_RATE_LIMIT_MSG_TEMPLATE = (
    "ChatGPT retornou rate limit (detectado no texto da resposta). "
    "Prévia: {preview}"
)


# ─────────────────────────────────────────────────────────
# Detecção de rate-limit no texto da resposta
# ─────────────────────────────────────────────────────────
def detect_rate_limit_preview(
    markdown: str,
    is_rate_limit_fn: Callable[[str], bool],
) -> Optional[str]:
    """Se `is_rate_limit_fn(markdown)` for verdadeiro, retorna os primeiros
    `_RATE_LIMIT_PREVIEW_CHARS` caracteres de `markdown` (prévia usada na
    mensagem de erro). Caso contrário, retorna `None`.

    Não levanta. O caller transforma o preview em exceção específica
    (`ChatGPTRateLimitError`) mantendo a camada de exceções fora do
    módulo puro.
    """
    if not markdown:
        return None
    if is_rate_limit_fn(markdown):
        return markdown[:_RATE_LIMIT_PREVIEW_CHARS]
    return None


def build_rate_limit_error_message(preview: str) -> str:
    """Formata a mensagem padrão do erro de rate-limit detectado em texto.

    Contrato estável: dashboards/alertas podem casar o texto literal.
    """
    return _RATE_LIMIT_MSG_TEMPLATE.format(preview=preview or "")


# ─────────────────────────────────────────────────────────
# Extração e normalização de JSON tolerante
# ─────────────────────────────────────────────────────────
def strip_code_fences(texto: str) -> str:
    """Remove cercas Markdown ```...``` (opcionalmente com ```json)
    mantendo apenas o conteúdo interno. Preserva espaços internos; só
    faz trim nas bordas do bloco."""
    texto = (texto or "").strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```(?:json)?\s*", "", texto, flags=re.IGNORECASE)
        texto = re.sub(r"\s*```$", "", texto)
    return texto.strip()


def extract_json_block(texto: str) -> str:
    """Extrai o primeiro objeto JSON aparente (do primeiro `{` ao
    último `}` que casar, via regex gulosa). Retorna `""` se não houver
    candidato."""
    texto = strip_code_fences(texto)
    match = re.search(r'\{[\s\S]*\}', texto)
    return match.group().strip() if match else ""


def normalize_llm_json(raw_json: str) -> str:
    """Corrige problemas comuns de JSON quase-válido retornado por LLMs.

    Transformações aplicadas, em ordem:
      1. Troca aspas tipográficas e crases por aspas ASCII.
      2. Escapa aspas duplas INTERNAS em strings (heurística: se após
         a `"` o próximo caractere não-whitespace não é delimitador JSON,
         escapa).
      3. Insere vírgula ausente entre pares "valor" "chave": consecutivos.
      4. Insere vírgula entre `}` ou `]` e `{` consecutivos.
      5. Insere vírgula entre `}`/`]` e próxima chave.
      6. Remove trailing commas antes de `}` ou `]`.

    Saída pode ainda não ser JSON válido; o caller decide se levanta.
    """
    texto = (raw_json or "").strip()
    if not texto:
        return ""

    texto = (
        texto
        .replace("“", '"')
        .replace("”", '"')
        .replace("’", "'")
        .replace("‘", "'")
        .replace("`", '"')
    )

    chars: list[str] = []
    in_string = False
    escape = False
    n = len(texto)
    i = 0
    while i < n:
        ch = texto[i]
        if not in_string:
            chars.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue

        if escape:
            chars.append(ch)
            escape = False
            i += 1
            continue

        if ch == '\\':
            chars.append(ch)
            escape = True
            i += 1
            continue

        if ch == '"':
            j = i + 1
            while j < n and texto[j] in ' \t\r\n':
                j += 1
            next_ch = texto[j] if j < n else ''
            if next_ch in [',', '}', ']', ':', '']:
                chars.append('"')
                in_string = False
            else:
                chars.append('\\"')
            i += 1
            continue

        chars.append(ch)
        i += 1

    texto = ''.join(chars)

    texto = re.sub(r'("(?:(?:\\.|[^"\\])*)")(\s*)"([A-Za-z0-9_\-]+)"\s*:', r'\1,\2"\3":', texto)
    texto = re.sub(r'([}\]])(\s*)(\{)', r'\1,\2', texto)
    texto = re.sub(r'([}\]])(\s*)"([A-Za-z0-9_\-]+)"\s*:', r'\1,\2"\3":', texto)
    texto = re.sub(r',(\s*[}\]])', r'\1', texto)
    return texto


def parse_json_block(texto: str) -> dict:
    """Faz o parse JSON tolerante — SEM detecção de rate-limit.

    Pipeline:
      1. `extract_json_block` → candidato string.
      2. `json.loads(candidato)` direto.
      3. Se falhar, `normalize_llm_json(candidato)` e tenta de novo.

    Levanta:
      - `ValueError("LLM não retornou bloco JSON.")` se não houver `{...}`.
      - `json.JSONDecodeError` se normalização também falhar.
    """
    candidato = extract_json_block(texto)
    if not candidato:
        raise ValueError("LLM não retornou bloco JSON.")
    try:
        return json.loads(candidato)
    except json.JSONDecodeError:
        candidato_normalizado = normalize_llm_json(candidato)
        return json.loads(candidato_normalizado)


# ─────────────────────────────────────────────────────────
# Heurísticas e fragmentos auxiliares
# ─────────────────────────────────────────────────────────
def json_looks_incomplete(texto: str) -> bool:
    """Heurística para identificar respostas JSON possivelmente
    truncadas/incompletas.

    Conta chaves/colchetes abertos vs fechados, ignorando conteúdo
    dentro de strings (com escape). Retorna `True` quando:
      - a string iniciada não foi fechada;
      - `depth_obj > 0` (chaves ainda abertas);
      - `depth_arr > 0` (colchetes ainda abertos);
      - o texto não termina em `}`.

    Não levanta. Entrada vazia ou sem `{` inicial retorna `False`.
    """
    bruto = strip_code_fences(texto or "").strip()
    if not bruto or not bruto.startswith('{'):
        return False

    depth_obj = 0
    depth_arr = 0
    in_string = False
    escape = False
    for ch in bruto:
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == '{':
            depth_obj += 1
        elif ch == '}':
            depth_obj -= 1
        elif ch == '[':
            depth_arr += 1
        elif ch == ']':
            depth_arr -= 1

    return in_string or depth_obj > 0 or depth_arr > 0 or not bruto.rstrip().endswith('}')


def decode_json_string_fragment(value: str) -> str:
    """Decodifica um fragmento de string JSON (sem aspas externas)
    preservando UTF-8 e escapes comuns.

    Fallback: se `json.loads('"<value>"')` falhar, faz substituições
    manuais de `\\"`, `\\n`, `\\t` — mantendo o contrato histórico."""
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")


def extract_visible_llm_markdown(texto: str) -> str:
    """Remove blocos `<think>…</think>` para isolar a resposta visível
    entregue pela LLM.

    Regras:
      - Entrada vazia/whitespace retorna `""`.
      - Se há `<think>` mas sem `</think>` fechado ainda, devolve `""`
        (a resposta visível ainda não começou).
      - Caso contrário, remove todos os blocos `<think>…</think>` (case-
        insensitive, multiline) e faz `.strip()` no resultado.
    """
    bruto = texto or ""
    if not bruto.strip():
        return ""
    if "<think>" in bruto and "</think>" not in bruto:
        return ""
    sem_think = re.sub(r"<think>[\s\S]*?</think>", "", bruto, flags=re.IGNORECASE)
    return sem_think.strip()


# ─────────────────────────────────────────────────────────
# Fallback tolerante para queries de pesquisa em markdown LLM
# ─────────────────────────────────────────────────────────
_SEARCH_PAIR_PATTERN = re.compile(
    r'"query"\s*:\s*"(?P<query>(?:\\.|[^"\\])*)"\s*,?\s*"reason"\s*:\s*"(?P<reason>(?:\\.|[^"\\])*)"',
    re.IGNORECASE | re.DOTALL,
)
_SEARCH_LINE_PATTERN = re.compile(
    r'^\s*(?:[-*]|\d+[.)])\s*(?P<query>.+?)(?:\s+[—-]\s+|\s+\|\s+motivo:\s+)(?P<reason>.+?)\s*$',
    re.IGNORECASE | re.MULTILINE,
)


def extract_search_queries_fallback(markdown: str, max_queries: int = 16) -> list:
    """Extrai `[{"query", "reason"}]` quando a LLM não entrega JSON estrito.

    Dois passes tolerantes:
      1. `"query": "..."  "reason": "..."` (JSON truncado ou sem vírgulas).
      2. linhas formato `- query — motivo` ou `1) query | motivo: motivo`.

    Deduplica por `query.lower()`. Corta em `max_queries` itens no total.
    `max_queries` é injetado pelo chamador (em `analisador_prontuarios.py`,
    virá de `config.ANALISADOR_SEARCH_MAX_QUERIES`). Default `16` é
    defensivo — o caller real deve passar o valor configurado.
    """
    texto = strip_code_fences(markdown)
    if not texto:
        return []

    queries: list[dict] = []
    vistos: set[str] = set()
    limit = max(1, int(max_queries))

    for match in _SEARCH_PAIR_PATTERN.finditer(texto):
        query = re.sub(r"\s+", " ", decode_json_string_fragment(match.group("query"))).strip()
        reason = re.sub(r"\s+", " ", decode_json_string_fragment(match.group("reason"))).strip()
        if not query:
            continue
        chave = query.lower()
        if chave in vistos:
            continue
        vistos.add(chave)
        queries.append({"query": query, "reason": reason})
        if len(queries) >= limit:
            return queries

    for match in _SEARCH_LINE_PATTERN.finditer(texto):
        query = re.sub(r"\s+", " ", match.group("query")).strip(' "\'')
        reason = re.sub(r"\s+", " ", match.group("reason")).strip(' "\'')
        if not query:
            continue
        chave = query.lower()
        if chave in vistos:
            continue
        vistos.add(chave)
        queries.append({"query": query, "reason": reason})
        if len(queries) >= limit:
            break

    return queries


__all__ = [
    "detect_rate_limit_preview",
    "build_rate_limit_error_message",
    "strip_code_fences",
    "extract_json_block",
    "normalize_llm_json",
    "parse_json_block",
    "json_looks_incomplete",
    "decode_json_string_fragment",
    "extract_visible_llm_markdown",
    "extract_search_queries_fallback",
]
