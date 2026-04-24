"""Helpers puros extraídos de `server.py` (Lote P0 passo 2 do refactor).

Funções aqui NÃO podem depender de:
  - Flask / Werkzeug (nada de `request`, `jsonify`, decoradores HTTP).
  - `config` (tudo que precisaria de config deve ser recebido como parâmetro).
  - Estado global de `server.py` (locks, dicts compartilhados).

Motivo: estas funções são caminho quente da fila e do feedback de espera
(rate-limit, fila Python). Mantê-las puras permite testá-las offline sem
instalar `flask` / `cryptography` e sem montar o app completo.
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Callable, Mapping, MutableSequence, Optional


def format_wait_seconds(seconds) -> str:
    """Formata um número de segundos como MM:SS (clamp em 0).

    Exemplo: `format_wait_seconds(125) == "02:05"`.
    Valores negativos ou `NaN`/`None` resultam em `"00:00"`.
    """
    try:
        remaining = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        remaining = 0
    mins, secs = divmod(remaining, 60)
    return f"{mins:02d}:{secs:02d}"


def queue_status_payload(wait_seconds, position: int, total: int, sender_label: str) -> str:
    """Produz o JSON (string) de status de fila Python no stream SSE.

    Formato estável consumido pelo frontend/analisador; manter chaves
    (`type`, `content`, `phase`, `wait_seconds`, `queue_position`,
    `queue_size`, `sender`) para não quebrar consumidores.
    """
    try:
        wait_f = float(wait_seconds)
    except (TypeError, ValueError):
        wait_f = 0.0
    wait_f = max(0.0, wait_f)
    pos = int(position)
    tot = int(total)
    return json.dumps({
        "type": "status",
        "content": (
            f"⏳ Fila interna do servidor: posição {pos}/{max(1, tot)}. "
            f"Tempo restante estimado para liberação: {format_wait_seconds(wait_f)}."
        ),
        "phase": "server_python_queue_wait",
        "wait_seconds": round(wait_f, 1),
        "queue_position": pos,
        "queue_size": tot,
        "sender": sender_label,
    }, ensure_ascii=False)


def prune_old_attempts(
    dq: MutableSequence[float],
    window_sec: int,
    *,
    now: Optional[float] = None,
    now_func: Callable[[], float] = time.time,
) -> int:
    """Remove timestamps antigos (mais velhos que `window_sec`) de `dq`.

    Retorna o número de entradas removidas. Aceita qualquer sequência
    mutável que implemente `popleft()` (normalmente `collections.deque`).

    `now`/`now_func`: ganchos para testes determinísticos — em produção,
    chamar apenas com `dq` e `window_sec` reproduz o comportamento
    histórico de `server._prune_old_attempts`.
    """
    current = float(now) if now is not None else float(now_func())
    cutoff = current - int(window_sec)
    removed = 0
    while dq and dq[0] < cutoff:
        # Usa popleft se disponível (deque); fallback para pop(0) em listas.
        if hasattr(dq, "popleft"):
            dq.popleft()  # type: ignore[attr-defined]
        else:
            dq.pop(0)
        removed += 1
    return removed


def count_active_chatgpt_profiles(profiles_map: Optional[Mapping]) -> int:
    """Quantidade de perfis Chromium/ChatGPT acionáveis em paralelo.

    Recebe o mapa `chave → diretório` (tipicamente `config.CHROMIUM_PROFILES`).
    Sempre retorna ao menos 1 para evitar divisão por zero no cálculo do
    intervalo anti-rate-limit.
    """
    if not profiles_map:
        return 1
    try:
        return max(1, len(profiles_map))
    except TypeError:
        return 1


# ─────────────────────────────────────────────────────────
# Payloads de `/v1/chat/completions`
# ─────────────────────────────────────────────────────────
_PASTE_WRAPPER_OPEN = "[INICIO_TEXTO_COLADO]"
_PASTE_WRAPPER_CLOSE = "[FIM_TEXTO_COLADO]"


def combine_openai_messages(messages) -> str:
    """Concatena um array OpenAI-style (`[{"role": "...", "content": "..."}]`)
    no texto único esperado pela pipeline interna.

    Regras históricas preservadas:
      - `role == "system"` é **prependado** ao texto acumulado (com duas
        quebras de linha).
      - `role == "user"` é concatenado ao final (com quebra de linha).
      - Outros papéis (`assistant`, ferramentas etc.) são ignorados.
      - Resultado é `.strip()`-ado ao final.

    Aceita `messages=None`/não-lista devolvendo `""`.
    """
    if not isinstance(messages, list):
        return ""
    combined = ""
    for msg in messages:
        if not isinstance(msg, Mapping):
            continue
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue
        if role == "system":
            combined = content + "\n\n" + combined
        elif role == "user":
            combined += f"{content}\n"
    return combined.strip()


def build_sender_label(source_hint: str, is_analyzer: bool) -> str:
    """Rótulo canônico do remetente para logs e mensagens de fila.

    - `is_analyzer=True` → `"analisador_prontuarios.py"` (sempre).
    - `source_hint` truthy → devolve `source_hint` como recebido.
    - Caso contrário → `"usuario_remoto"`.
    """
    if is_analyzer:
        return "analisador_prontuarios.py"
    hint = (source_hint or "").strip()
    return hint or "usuario_remoto"


def wrap_paste_if_python_source(message, is_python_source: bool) -> str:
    """Garante os wrappers `[INICIO_TEXTO_COLADO]...[FIM_TEXTO_COLADO]`
    em mensagens oriundas de scripts Python.

    Não envolve (devolve como veio) quando:
      - `is_python_source` é falso;
      - `message` não é string, é vazia ou só whitespace;
      - já possui AMBOS os marcadores em algum lugar do texto.

    Caso contrário, retorna `f"{OPEN}{message}{CLOSE}"`.
    """
    if not is_python_source:
        return message if isinstance(message, str) else ""
    if not isinstance(message, str) or not message.strip():
        return message if isinstance(message, str) else ""
    if _PASTE_WRAPPER_OPEN in message and _PASTE_WRAPPER_CLOSE in message:
        return message
    return f"{_PASTE_WRAPPER_OPEN}{message}{_PASTE_WRAPPER_CLOSE}"


def coalesce_origin_url(data, header_value: str = "") -> str:
    """Resolve a URL de origem efetiva do pedido `/v1/chat/completions`.

    Ordem de precedência (histórica):
      1. `data["origin_url"]`
      2. `data["url_atual"]` (compat com clientes antigos)
      3. `header_value` (tipicamente `request.headers["X-Origin-URL"]`)
      4. `""` (string vazia)

    Entradas não-mapping/`None` são tratadas como payload vazio.
    """
    payload = data if isinstance(data, Mapping) else {}
    candidate = (
        payload.get("origin_url")
        or payload.get("url_atual")
        or header_value
        or ""
    )
    return str(candidate or "").strip()


__all__ = [
    "format_wait_seconds",
    "queue_status_payload",
    "prune_old_attempts",
    "count_active_chatgpt_profiles",
    "combine_openai_messages",
    "build_sender_label",
    "wrap_paste_if_python_source",
    "coalesce_origin_url",
]


# Exportação auxiliar usada apenas para compatibilidade dos wrappers em
# server.py — evita ruído com o símbolo `deque` não importado em testes.
_deque_cls = deque
