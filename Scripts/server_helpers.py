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

import base64
import binascii
import json
import time
from collections import deque
from typing import Callable, Mapping, MutableSequence, Optional, Tuple


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


_DEFAULT_ATTACHMENT_NAME = "file.txt"


def decode_attachment(att) -> Optional[Tuple[str, bytes]]:
    """Decodifica um anexo `{"name": ..., "data": "<base64 ou data URI>"}`.

    Retorna `(nome, bytes_decodificados)` ou `None` quando o anexo é
    malformado a ponto do código histórico ter caído no `except Exception`
    (não-mapping, `data` não-string, ou base64 inválido).

    Semântica preservada byte-a-byte de `server.chat_completions`:
      - Nome ausente → `"file.txt"` (default do `dict.get`). Nome com
        valor `None` é repassado para o chamador, que historicamente
        produzia o caminho `<ts>_None`.
      - `data` ausente ou vazio → `base64.b64decode("") == b""`. O
        chamador histórico cria o arquivo vazio e anexa o path; este
        comportamento é preservado (helper retorna `(name, b"")`).
      - Se `data` contém vírgula (ex.: `data:image/png;base64,iVBOR...`),
        usa **apenas** o trecho imediatamente após a primeira vírgula
        (`s.split(",")[1]`), ignorando vírgulas posteriores no payload —
        comportamento histórico que evita reconstruir prefixos de
        `data:` URIs e é seguro para os clientes atuais.

    Sem IO: a gravação em disco continua sendo responsabilidade do
    chamador (preserva o caminho `config.DIRS["uploads"]` em server.py).
    """
    if not isinstance(att, Mapping):
        return None
    raw = att.get("data", "")
    if not isinstance(raw, str):
        return None
    name = att.get("name", _DEFAULT_ATTACHMENT_NAME)
    payload = raw.split(",")[1] if "," in raw else raw
    try:
        decoded = base64.b64decode(payload)
    except (binascii.Error, ValueError):
        return None
    return name, decoded


def resolve_chat_url(requested_url, stored_url) -> Optional[str]:
    """Decide qual URL usar para retomar a conversa.

    Aceita o sentinela histórico `"None"` (string literal vinda do JSON
    de clientes antigos) como ausência de URL. Devolve a primeira URL
    válida em ordem de prioridade ou `None` quando nenhuma serve.

    Pura: o chamador é responsável por buscar `stored_url` em
    `storage.load_chats()`.
    """
    for candidate in (requested_url, stored_url):
        if not isinstance(candidate, str):
            continue
        trimmed = candidate.strip()
        if trimmed and trimmed != "None":
            return trimmed
    return None


def normalize_optional_text(value) -> Optional[str]:
    """Colapsa o idiom `(value or '').strip() or None`.

    - String não-vazia após strip → string strip-ada.
    - String vazia, whitespace ou tipos não-string → `None`.

    Usado em campos opcionais de payload (`codex_repo`, `browser_profile`,
    `request_source`, etc.) onde "" e None são equivalentes.
    """
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def resolve_browser_profile(requested_profile, stored_profile) -> Optional[str]:
    """Resolve o `browser_profile` efetivo para a tarefa do browser.

    Prioridade: `requested_profile` (vindo do payload da request) →
    `stored_profile` (vindo do snapshot persistido) → `None`.

    Strings são `.strip()`-adas; vazias após strip são ignoradas. Tipos
    não-string são tratados como ausência. Mantém o contrato histórico
    de `chat_completions` em que `None` significa "deixar `browser.py`
    cair no fallback `default`".
    """
    for candidate in (requested_profile, stored_profile):
        if not isinstance(candidate, str):
            continue
        trimmed = candidate.strip()
        if trimmed:
            return trimmed
    return None


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


def build_error_event(content: str) -> str:
    """JSON do evento de erro consumido pelo SSE / `stream_queue`.

    Formato estável: `{"type": "error", "content": "<texto>"}`. Usa
    `ensure_ascii=False` para preservar UTF-8 (mesmo idiom de
    `queue_status_payload`).

    Não anexa newline — o chamador decide entre `stream_q.put(...)` e
    `yield ... + "\\n"` para SSE direto.
    """
    return json.dumps({"type": "error", "content": str(content)}, ensure_ascii=False)


def build_status_event(content: str, **extras) -> str:
    """JSON de evento de status genérico, com campos extras opcionais.

    Formato: `{"type": "status", "content": "...", **extras}`. Os campos
    extras são mesclados ao topo do payload (ordem de inserção do dict
    Python preservada). Para o caso específico da fila Python, prefira
    `queue_status_payload` (formato canônico há mais tempo).

    `ensure_ascii=False` preserva acentos/emoji.
    """
    payload = {"type": "status", "content": str(content)}
    payload.update(extras)
    return json.dumps(payload, ensure_ascii=False)


def build_queue_key(chat_id, *, now_ns: Callable[[], int] = time.time_ns) -> str:
    """Gera a chave única usada para identificar um slot de fila Python.

    Formato histórico preservado: `f"{chat_id}:{time.time_ns()}"`.
    `now_ns` é injetável para testes determinísticos.
    """
    return f"{chat_id}:{now_ns()}"


def build_chat_task_payload(
    *,
    url,
    chat_id,
    message,
    is_analyzer,
    sender_label,
    source_hint,
    saved_paths,
    stream_queue,
    codex_repo,
    effective_browser_profile,
) -> dict:
    """Monta o dicionário enviado ao `browser_queue` em `chat_completions`.

    Campos preservados byte-a-byte (ordem, defaults, normalizações):
      - `action` fixo em `"CHAT"`.
      - `is_analyzer` coagido para `bool` (compat com clientes que mandam
        truthy não-bool).
      - `request_source` cai para `sender_label` quando `source_hint`
        está vazio/falsy.
      - `attachment_paths` é a lista pronta de paths gravados em disco.
      - `codex_repo` é normalizado (`normalize_optional_text`) — `""`
        ou whitespace viram `None`, sinalizando ao `browser.py` que deve
        usar a seleção atual do dropdown.
      - `browser_profile` repassado como-está (já resolvido por
        `resolve_browser_profile` no chamador).
      - `stream_queue` referência viva — o consumidor do payload no
        `browser.py` envia eventos SSE de volta por ela.

    `sender` aparece duplicado no dict histórico (uma chave após
    `is_analyzer`, outra após `stream_queue`); como ambos mapeiam para
    `sender_label`, a duplicação é semântica-no-op (Python preserva o
    último valor). Reproduzimos o dict numa única atribuição para evitar
    qualquer divergência observável.
    """
    return {
        'action':           'CHAT',
        'url':              url,
        'chat_id':          chat_id,
        'message':          message,
        'is_analyzer':      bool(is_analyzer),
        'sender':           sender_label,
        'request_source':   source_hint or sender_label,
        'attachment_paths': saved_paths,
        'stream_queue':     stream_queue,
        'codex_repo':       normalize_optional_text(codex_repo),
        'browser_profile':  effective_browser_profile,
    }


__all__ = [
    "format_wait_seconds",
    "queue_status_payload",
    "prune_old_attempts",
    "count_active_chatgpt_profiles",
    "combine_openai_messages",
    "build_sender_label",
    "wrap_paste_if_python_source",
    "coalesce_origin_url",
    "decode_attachment",
    "resolve_chat_url",
    "resolve_browser_profile",
    "normalize_optional_text",
    "build_queue_key",
    "build_chat_task_payload",
    "build_error_event",
    "build_status_event",
]


# Exportação auxiliar usada apenas para compatibilidade dos wrappers em
# server.py — evita ruído com o símbolo `deque` não importado em testes.
_deque_cls = deque
