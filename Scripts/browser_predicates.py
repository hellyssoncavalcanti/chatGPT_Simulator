"""Predicados puros extraídos de `Scripts/browser.py` (Lote P0 passo 3).

Inclui apenas funções SEM dependências assíncronas ou de Playwright, para
que possam ser testadas offline sem instalar `playwright` / `flask`.

Não moverá o loop do navegador nesta etapa — apenas predicados sobre
strings/JSON já recebidos. O `browser.py` passa a delegar via wrappers
finos mantendo os nomes internos originais.
"""

from __future__ import annotations

import re
from typing import Any


# ─────────────────────────────────────────────────────────────
# Resolução do sender da tarefa enfileirada no browser_queue
# ─────────────────────────────────────────────────────────────
def extract_task_sender(task: Any) -> str:
    """Resolve o rótulo do remetente da tarefa enfileirada.

    Ordem de resolução: `sender` → `request_source` → `remetente`
    (alias PT). Vazio/None → `"usuario_remoto"`.
    """
    if not isinstance(task, dict):
        return "usuario_remoto"
    sender = (
        task.get("sender")
        or task.get("request_source")
        or task.get("remetente")
        or ""
    )
    sender = str(sender or "").strip()
    return sender or "usuario_remoto"


# ─────────────────────────────────────────────────────────────
# Abas/tabs conhecidas que aparecem como popup e devem ser fechadas
# ─────────────────────────────────────────────────────────────
_KNOWN_ORPHAN_TAB_FRAGMENTS = (
    "residenciapediatrica.com.br/content/pdf/",
)


def is_known_orphan_tab_url(url: str) -> bool:
    """Detecta URLs de abas órfãs conhecidas (fecháveis sem prejuízo)."""
    if not url:
        return False
    u = str(url).strip().lower()
    return any(fragment in u for fragment in _KNOWN_ORPHAN_TAB_FRAGMENTS)


# ─────────────────────────────────────────────────────────────
# Validação estrutural de resposta JSON em markdown
# ─────────────────────────────────────────────────────────────
_FENCE_OPEN_RE = re.compile(r'^```(?:json)?\s*', re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r'\s*```$')


def _strip_code_fences(texto: str) -> str:
    if texto.startswith('```'):
        texto = _FENCE_OPEN_RE.sub('', texto)
        texto = _FENCE_CLOSE_RE.sub('', texto)
    return texto.strip()


def response_looks_incomplete_json(markdown_text: str) -> bool:
    """Verifica se um markdown com JSON aparenta estar truncado.

    Heurística (mantida fiel ao código original em `browser.py`):
      - se a resposta não começa com `{`, assume-se que não é JSON puro e
        retorna `False`;
      - caso contrário, conta chaves/colchetes (ignorando strings/escapes)
        e verifica se o fechamento final é `}`.
    """
    texto = (markdown_text or '').strip()
    if not texto:
        return False

    texto = _strip_code_fences(texto)
    if not texto.startswith('{'):
        return False

    depth_obj = 0
    depth_arr = 0
    in_string = False
    escape = False
    for ch in texto:
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

    return (
        in_string
        or depth_obj > 0
        or depth_arr > 0
        or not texto.rstrip().endswith('}')
    )


# ─────────────────────────────────────────────────────────────
# Detecta respostas intermediárias que exigem follow-up do modelo
# ─────────────────────────────────────────────────────────────
_FOLLOWUP_HINTS: tuple[str, ...] = (
    '"sql_queries"', "'sql_queries'", "sql_queries",
    '"search_queries"', "'search_queries'", "search_queries",
    '"queries_sql"', "'queries_sql'", "queries_sql",
    '"tool_name"', '"tool_calls"', '"function_call"',
)


def response_requests_followup_actions(markdown_text: str) -> bool:
    """True quando a resposta contém marcadores de tool-call/search intermediários."""
    texto = (markdown_text or "").strip().lower()
    if not texto:
        return False

    texto = _strip_code_fences(texto).lower() if texto.startswith("```") else texto
    return any(h in texto for h in _FOLLOWUP_HINTS)


# ─────────────────────────────────────────────────────────────
# Remoção de payloads base64 inline gigantes (logs/snapshots)
# ─────────────────────────────────────────────────────────────
_BASE64_DATA_URL_RE = re.compile(
    r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]{120,}",
    re.IGNORECASE,
)
_BASE64_JSON_FIELD_RE = re.compile(
    r'("(?:data_base64|image_base64|base64|image_data)"\s*:\s*")[A-Za-z0-9+/=\s]{120,}(")',
    re.IGNORECASE,
)
BASE64_REPLACEMENT = "[BASE64_IMAGE_REMOVIDA]"


def replace_inline_base64_payloads(text: str) -> tuple[str, int]:
    """Substitui blobs base64 inline (imagens) por placeholder curto.

    Retorna `(texto_saneado, quantidade_substituída)`.
    """
    if not text:
        return text, 0
    out = str(text)
    out, n1 = _BASE64_DATA_URL_RE.subn(BASE64_REPLACEMENT, out)
    out, n2 = _BASE64_JSON_FIELD_RE.subn(r'\1' + BASE64_REPLACEMENT + r'\2', out)
    return out, n1 + n2


# ─────────────────────────────────────────────────────────────
# Envelope [INICIO_TEXTO_COLADO]...[FIM_TEXTO_COLADO]
# ─────────────────────────────────────────────────────────────
PASTE_START_MARKER = "[INICIO_TEXTO_COLADO]"
PASTE_END_MARKER = "[FIM_TEXTO_COLADO]"


def ensure_paste_wrappers(text: str) -> tuple[str, bool]:
    """Retorna o texto sem modificação. Mantido por compatibilidade."""
    content = str(text or "")
    return content, False


__all__ = [
    "extract_task_sender",
    "is_known_orphan_tab_url",
    "response_looks_incomplete_json",
    "response_requests_followup_actions",
    "replace_inline_base64_payloads",
    "ensure_paste_wrappers",
    "BASE64_REPLACEMENT",
    "PASTE_START_MARKER",
    "PASTE_END_MARKER",
]
