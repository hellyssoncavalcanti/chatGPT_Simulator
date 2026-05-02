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
import os
import time
from collections import deque
from typing import Callable, List, Mapping, MutableSequence, Optional, Tuple


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


def extract_rate_limit_details(error_payload, *, classify_fn=None, rate_limit_marker=None):
    """Extrai sinais de rate-limit de payloads vindos do browser.

    Contrato de retorno preservado de `server._extract_rate_limit_details`:
    `(is_rate_limited, message, retry_after_seconds|None)`.
    """
    code = ""
    message = ""
    retry_after = None

    if isinstance(error_payload, Mapping):
        code = str(error_payload.get("code") or "").strip().lower()
        message = str(error_payload.get("message") or error_payload.get("error") or "").strip()
        try:
            retry_after_raw = error_payload.get("retry_after_seconds")
            if retry_after_raw is not None:
                retry_after = max(1, int(float(retry_after_raw)))
        except Exception:
            retry_after = None
    else:
        message = str(error_payload or "").strip()

    if classify_fn is None or rate_limit_marker is None:
        import error_catalog as _error_catalog
        classify_fn = _error_catalog.classify_from_text
        rate_limit_marker = _error_catalog.RATE_LIMIT

    is_rate_limited = (
        code in {"rate_limit", "too_many_requests"}
        or classify_fn(f"{code} {message}") == rate_limit_marker
    )
    return is_rate_limited, message, retry_after


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


def resolve_chat_url(
    requested_url,
    stored_url,
    *,
    case_insensitive: bool = False,
) -> Optional[str]:
    """Decide qual URL usar para retomar a conversa.

    Aceita o sentinela histórico `"None"` (string literal vinda do JSON
    de clientes antigos) como ausência de URL. Devolve a primeira URL
    válida em ordem de prioridade ou `None` quando nenhuma serve.

    Pura: o chamador é responsável por buscar `stored_url` em
    `storage.load_chats()`.

    `case_insensitive=True` (padrão histórico de `api_sync`): além de
    `"None"`, também trata `"none"`, `"NONE"`, `"None "`, etc., como
    ausência. `chat_completions` usa `False` (preserva strict-equality
    com `"None"`) — mantido por backward-compat.
    """
    for candidate in (requested_url, stored_url):
        if not isinstance(candidate, str):
            continue
        trimmed = candidate.strip()
        if not trimmed:
            continue
        compare = trimmed.lower() if case_insensitive else trimmed
        sentinel = "none" if case_insensitive else "None"
        if compare == sentinel:
            continue
        return trimmed
    return None


def compute_python_request_interval(
    pausa_min,
    pausa_max,
    profile_count,
    *,
    rng=None,
) -> Tuple[float, float]:
    """Calcula o intervalo anti-rate-limit para pedidos Python.

    Retorna `(base_sec, target_sec)`:
      - `base = rng(max(0, pmin), max(pmin, pmax))` — sorteado uniformemente.
      - `target = max(0.0, base / max(1, profile_count))` — dividido pelo
        número de perfis Chromium ativos para distribuir a carga.

    Quando `pausa_min <= 0` E `pausa_max <= 0`, retorna `(0.0, 0.0)` —
    sinal que o caller deve pular o wait e apenas atualizar `last_ts`
    (preserva o curto-circuito histórico de
    `_wait_python_request_interval_if_needed` em server.py).

    `rng` é injetável para testes determinísticos; default é
    `random.uniform`.
    """
    pmin = float(pausa_min)
    pmax = float(pausa_max)
    if pmin <= 0 and pmax <= 0:
        return 0.0, 0.0
    if rng is None:
        import random
        rng = random.uniform
    base = float(rng(max(0.0, pmin), max(pmin, pmax)))
    target = max(0.0, base / float(max(1, int(profile_count))))
    return base, target


def format_origin_suffix(is_analyzer: bool, source_hint) -> str:
    """Sufixo `[origem: ...]` padronizado para logs de `chat_completions`.

    Regras (preservadas byte-a-byte):
      - `is_analyzer=True` → `" [origem: analisador_prontuarios.py]"`
        (sempre, independentemente de `source_hint`).
      - `is_analyzer=False` e `source_hint` truthy → `f" [origem: {source_hint}]"`.
      - Caso contrário → string vazia.

    Note o espaço inicial de cada caso truthy — facilita concatenar
    diretamente após o sufixo `_quem` na linha de log sem precisar
    ajustar separadores.
    """
    if is_analyzer:
        return " [origem: analisador_prontuarios.py]"
    if source_hint:
        return f" [origem: {source_hint}]"
    return ""


def format_requester_suffix(nome_membro, id_membro) -> str:
    """Sufixo padronizado para logs de requisição remota.

    Formato histórico (idêntico em `chat_completions` e `api_sync`):
        `, por "<nome>" (id_membro: "<id>")`  quando há nome OU id;
        string vazia quando ambos são falsy.

    Aceita `None`/strings vazias indiscriminadamente; non-strings são
    serializados via f-string (preserva o comportamento original que
    não validava tipos).
    """
    if not (nome_membro or id_membro):
        return ""
    return f', por "{nome_membro}" (id_membro: "{id_membro}")'


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


def extract_requester_identity(data) -> Tuple[Optional[str], Optional[str]]:
    """Extrai/normaliza nome/id do solicitante no payload HTTP.

    Chaves suportadas:
      - `nome_membro_solicitante`
      - `id_membro_solicitante`
    """
    payload_get = getattr(data, "get", None)
    if payload_get is None:
        return (None, None)
    return (
        normalize_optional_text(payload_get("nome_membro_solicitante")),
        normalize_optional_text(payload_get("id_membro_solicitante")),
    )


def resolve_lookup_origin_url(data) -> str:
    """Resolve `origin_url` para `/api/chat_lookup`.

    Prioridade histórica:
      1. `origin_url`
      2. `url_atual` (compatibilidade com clientes antigos)
      3. string vazia
    """
    payload_get = getattr(data, "get", None)
    if payload_get is None:
        return ""
    return (
        normalize_optional_text(payload_get("origin_url"))
        or normalize_optional_text(payload_get("url_atual"))
        or ""
    )


def extract_chat_delete_local_targets(data) -> Tuple[str, str]:
    """Extrai (`chat_id`, `origin_url`) para `/api/chat_delete_local`."""
    payload_get = getattr(data, "get", None)
    if payload_get is None:
        return ("", "")
    return (
        normalize_optional_text(payload_get("chat_id")) or "",
        normalize_optional_text(payload_get("origin_url")) or "",
    )


def extract_delete_request_targets(data) -> Tuple[Optional[str], Optional[str]]:
    """Extrai (`url`, `chat_id`) para `/api/delete`."""
    payload_get = getattr(data, "get", None)
    if payload_get is None:
        return (None, None)
    return (
        normalize_optional_text(payload_get("url")),
        normalize_optional_text(payload_get("chat_id")),
    )


def extract_menu_url(data) -> Optional[str]:
    """Extrai URL para `/api/menu/options`."""
    payload_get = getattr(data, "get", None)
    if payload_get is None:
        return None
    return normalize_optional_text(payload_get("url"))


def extract_menu_execute_payload(data) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extrai (`url`, `option`, `new_name`) para `/api/menu/execute`."""
    payload_get = getattr(data, "get", None)
    if payload_get is None:
        return (None, None, None)
    return (
        normalize_optional_text(payload_get("url")),
        normalize_optional_text(payload_get("option")),
        normalize_optional_text(payload_get("new_name")),
    )


def extract_web_search_test_params(data) -> Tuple[str, str]:
    """Extrai (`query`, `api_key`) para `/api/web_search/test` (query string)."""
    payload_get = getattr(data, "get", None)
    if payload_get is None:
        return ("", "")
    return (
        normalize_optional_text(payload_get("q")) or "",
        normalize_optional_text(payload_get("api_key")) or "",
    )


def build_web_search_test_task(query: str, stream_queue) -> dict:
    """Monta o payload de fila usado por `/api/web_search/test?q=...`."""
    return {
        "action": "SEARCH",
        "query": query,
        "stream_queue": stream_queue,
    }


def build_web_search_test_stream_response(raw_msg, query: str):
    """Interpreta uma mensagem de stream para `/api/web_search/test`.

    Retorna `(payload, status_code)` quando a mensagem exige resposta HTTP:
      - `searchresult` -> `(content_dict, 200)`
      - `error`        -> `({"success": False, "query": query, "error": ...}, 500)`

    Para tipos não-terminais/desconhecidos retorna `(None, None)`.
    """
    msg = json.loads(raw_msg)
    msg_type = msg.get("type")
    if msg_type == "searchresult":
        return msg.get("content", {}), 200
    if msg_type == "error":
        return build_web_search_test_error_payload(query, msg.get("content")), 500
    return None, None


def build_web_search_test_error_payload(query: str, error) -> dict:
    """Payload de erro padronizado para respostas JSON de `/api/web_search/test`."""
    return {
        "success": False,
        "query": query,
        "error": error,
    }


def build_web_search_test_timeout_payload(query: str) -> dict:
    """Payload padronizado para timeout (HTTP 504) em `/api/web_search/test`."""
    return build_web_search_test_error_payload(query, "Timeout (90s)")


def build_web_search_test_no_response_payload(query: str) -> dict:
    """Payload quando o browser não devolve mensagem terminal no stream."""
    return build_web_search_test_error_payload(query, "Sem resposta do browser")


def normalize_source_hint(value) -> str:
    """Normaliza um source-hint para a forma `lower-case` sem espaços nas pontas.

    Idiom canônico ``str(value).strip().lower()`` consumido por
    `is_analyzer_chat_request` / `is_python_chat_request` /
    `is_codex_chat_request` em `server.py`. Tratamento defensivo:
    `None` resulta em ``""`` (em vez do literal ``"none"`` produzido por
    `str(None)`).
    """
    if value is None:
        return ""
    try:
        return str(value).strip().lower()
    except Exception:
        return ""


def build_active_chat_meta(stream_queue, is_analyzer: bool, *, now: float) -> dict:
    """Monta o `meta` inicial inserido em `ACTIVE_CHATS[chat_id]`.

    Mantém as chaves históricas consumidas em `/api/metrics`,
    `/api/chat_lookup` e por `_cleanup_active_chats`:
      - ``queue``         → fila SSE associada ao chat;
      - ``status``        → texto curto exibido no UI (``"Iniciando..."``);
      - ``markdown``      → buffer parcial de markdown (vazio até o
        primeiro chunk);
      - ``finished``      → ``False`` enquanto o chat estiver vivo;
      - ``finished_at``   → ``None`` até o término;
      - ``last_event_at`` → relógio do último evento (controle de stale);
      - ``is_analyzer``   → flag boolean usada pelo classificador.

    Função pura — `now` injetável para testes determinísticos.
    """
    return {
        "queue": stream_queue,
        "status": "Iniciando...",
        "markdown": "",
        "finished": False,
        "finished_at": None,
        "last_event_at": float(now),
        "is_analyzer": bool(is_analyzer),
    }


def mark_chat_finished(active_chats, chat_id, *, now: float) -> bool:
    """Marca uma entrada de `ACTIVE_CHATS[chat_id]` como finalizada.

    Sets `finished=True`, `finished_at=now` e `last_event_at=now` em uma
    única passada — idiom canônico repetido em vários sites do dispatcher
    de chat (`chat_completions::generate`, `chat_completions::_drain_*`,
    timeouts, etc.).

    Tolera ausência de `chat_id` na coleção e entradas não-dict
    (retorna `False` sem mutar nada). Retorna `True` quando a meta foi
    efetivamente atualizada.

    Função pura no comportamento (sem IO/log) — caller fornece o
    relógio para testes determinísticos.
    """
    meta = active_chats.get(chat_id) if hasattr(active_chats, "get") else None
    if not isinstance(meta, dict):
        return False
    meta["finished"] = True
    meta["finished_at"] = now
    meta["last_event_at"] = now
    return True


def find_expired_chat_ids(active_chats, cutoff_ts: float) -> list:
    """Lista IDs de chats finalizados antes de `cutoff_ts`.

    Critério: ``meta.finished is truthy`` E
    ``meta.get("finished_at", 0) < cutoff_ts``. Função pura — caller
    fornece o snapshot e o `cutoff_ts` (suporta `now - TTL`).

    Retorna lista nova (caller pode iterar e deletar com segurança).
    Entradas não-dict são ignoradas defensivamente.
    """
    expired = []
    for k, meta in list(active_chats.items()):
        if not isinstance(meta, dict):
            continue
        if not meta.get("finished"):
            continue
        try:
            finished_at = float(meta.get("finished_at", 0) or 0)
        except (TypeError, ValueError):
            finished_at = 0.0
        if finished_at < cutoff_ts:
            expired.append(k)
    return expired


def count_unfinished_chats(active_chats) -> int:
    """Conta entradas em `active_chats` cuja `meta.finished` é falsa.

    Versão mínima do `count_active_chats` para call sites que precisam
    apenas do total (gauge Prometheus, audit hook). Itera sobre uma cópia
    da lista de items para tolerar mutação concorrente. Entradas não-dict
    (defensivamente) são ignoradas.
    """
    total = 0
    for _chat_id, meta in list(active_chats.items()):
        if not isinstance(meta, dict):
            continue
        if not meta.get("finished"):
            total += 1
    return total


def count_active_chats(active_chats, *, now: float, stale_threshold_sec: float) -> dict:
    """Conta chats ativos por categoria a partir do snapshot `active_chats`.

    Itera sobre o mapa `chat_id → meta` (ignora entradas com
    ``meta.get("finished") is truthy``), classifica `analyzer` vs `remote`
    pela flag `meta.get("is_analyzer")` e marca como `stale_candidate`
    quando `now - meta.last_event_at > stale_threshold_sec`
    (para `last_event_at > 0`).

    Retorna ``{"total", "analyzer", "remote", "stale_candidates"}``,
    mesmas chaves consumidas em `/api/metrics`. Função pura — caller
    fornece o snapshot e o relógio (permite teste determinístico).
    """
    total = 0
    analyzer = 0
    remote = 0
    stale = 0
    for _chat_id, meta in list(active_chats.items()):
        if not isinstance(meta, dict):
            continue
        if meta.get("finished"):
            continue
        total += 1
        if meta.get("is_analyzer"):
            analyzer += 1
        else:
            remote += 1
        try:
            last_event_at = float(meta.get("last_event_at") or 0.0)
        except (TypeError, ValueError):
            last_event_at = 0.0
        if last_event_at and (now - last_event_at) > stale_threshold_sec:
            stale += 1
    return {
        "total": total,
        "analyzer": analyzer,
        "remote": remote,
        "stale_candidates": stale,
    }


_VALID_AVATAR_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def resolve_avatar_filename(uploaded_filename, user) -> Tuple[Optional[str], Optional[str]]:
    """Resolve `(filename_to_save, error)` para `/api/user/upload_avatar`.

    Aceita apenas extensões em ``{.jpg, .jpeg, .png, .gif, .webp}`` (mesma
    whitelist histórica de `server.upload_avatar`). Caso a extensão seja
    inválida ou ausente, retorna ``(None, "Formato inválido")`` — string
    preservada byte-a-byte para não quebrar o JSON consumido pelo
    frontend.

    Quando válida, retorna ``(f"{user}{ext}", None)`` com `ext` em
    minúsculas (igual ao call site original).
    """
    name = uploaded_filename or ""
    import os as _os
    ext = _os.path.splitext(name)[1].lower()
    if ext not in _VALID_AVATAR_EXTENSIONS:
        return (None, "Formato inválido")
    user_str = "" if user is None else str(user)
    return (f"{user_str}{ext}", None)


_DOWNLOAD_MIME_BY_EXT = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv",
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "zip": "application/zip",
    "json": "application/json",
}


def resolve_download_content_type(content_type, display_name) -> str:
    """Resolve o `Content-Type` final para `/api/downloads/<file_id>`.

    Preserva o tipo informado pelo browser/ChatGPT quando ele é específico.
    Apenas quando o caller passa o fallback genérico
    ``"application/octet-stream"`` (ou ``None``) tenta inferir um tipo a
    partir da extensão do `display_name` usando um mapa estável de
    extensões para MIME (xlsx/xls/csv/pdf/png/jpg/jpeg/zip/json).

    Mapa idêntico ao usado historicamente em `server.serve_download`,
    extraído sem alteração de comportamento.
    """
    ct = content_type or "application/octet-stream"
    if ct != "application/octet-stream":
        return ct
    name = display_name or ""
    import os as _os
    ext = _os.path.splitext(name)[1].lower().lstrip(".")
    return _DOWNLOAD_MIME_BY_EXT.get(ext, ct)


def extract_manual_whatsapp_reply_targets(data):
    """Extrai (`phone`, `message`, `chat_id`, `id_paciente`, `id_atendimento`)
    do payload de `/api/send_manual_whatsapp_reply`.

    Normaliza `phone` e `message` para strings vazias quando ausentes.
    Para `chat_id`, `id_paciente` e `id_atendimento`: preserva o tipo
    original (int/None) mas aplica `.strip()` quando o valor é str
    (idiom defensivo para payloads com espaços acidentais).

    Aceita qualquer objeto com `.get()`; entradas inválidas (None/sem `.get`)
    retornam todos os campos como ``""``/``None``.
    """
    payload_get = getattr(data, "get", None)
    if payload_get is None:
        return ("", "", None, None, None)

    def _strip_if_str(v):
        return v.strip() if isinstance(v, str) else v

    phone = (payload_get("phone") or "").strip() if payload_get("phone") else ""
    message = (payload_get("message") or "").strip() if payload_get("message") else ""
    return (
        phone,
        message,
        _strip_if_str(payload_get("chat_id")),
        _strip_if_str(payload_get("id_paciente")),
        _strip_if_str(payload_get("id_atendimento")),
    )


def format_manual_whatsapp_requester_suffix(nome_membro, id_membro) -> str:
    """Sufixo `_quem` específico de `send_manual_whatsapp_reply`.

    Produz ``' por "<nome>" (id=<id>)'`` quando há nome OU id; vazia caso
    contrário. **Diferente** de `format_requester_suffix` (que emite
    ``(id_membro: "<id>")``): este idiom é histórico desta rota e
    consumido por log/observabilidade que dependem do formato exato.
    """
    nome = "" if nome_membro is None else str(nome_membro)
    ident = "" if id_membro is None else str(id_membro)
    if not nome and not ident:
        return ""
    return f' por "{nome}" (id={ident})'


def build_web_search_test_terminal_response(kind: str, query: str) -> Tuple[dict, int]:
    """Resolve `(payload, status_code)` para casos terminais de `/api/web_search/test`.

    `kind` aceita:
      - ``"timeout"``     → `(timeout_payload, 504)`.
      - ``"no_response"`` → `(no_response_payload, 200)`.

    O propósito é unificar o contrato `(dict, int)` já usado por
    `build_web_search_test_stream_response`, removendo o literal `504`
    duplicado no call site em `server.api_web_search_test`.
    """
    if kind == "timeout":
        return build_web_search_test_timeout_payload(query), 504
    if kind == "no_response":
        return build_web_search_test_no_response_payload(query), 200
    raise ValueError(f"unknown terminal kind: {kind!r}")


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


def extract_source_hint(data, headers) -> str:
    """Extrai a `source_hint` do payload + headers da requisição.

    Cadeia de fallback (idêntica em `chat_completions` e
    `_handle_browser_search_api`):
      1. `data["request_source"]`
      2. `headers["X-Request-Source"]`
      3. `headers["X-Client-Source"]`
      4. `""` (string vazia)

    Aceita qualquer objeto com `.get(name)` (`dict`, Flask `EnvironHeaders`
    ou similares); ausência de `.get` é tratada como dicionário vazio
    (preserva robustez se `data`/`headers` for `None`).
    """
    payload_get = getattr(data, 'get', None)
    headers_get = getattr(headers, 'get', None)
    candidate = ""
    if payload_get is not None:
        candidate = payload_get("request_source") or ""
    if not candidate and headers_get is not None:
        candidate = headers_get("X-Request-Source") or ""
    if not candidate and headers_get is not None:
        candidate = headers_get("X-Client-Source") or ""
    return candidate or ""



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


def build_chat_id_event(chat_id) -> str:
    """Evento NDJSON canônico para anunciar `chat_id` no stream."""
    return json.dumps({"type": "chat_id", "content": str(chat_id)}, ensure_ascii=False)


def build_chat_meta_event(chat_id, url: str, chromium_profile: str = "") -> str:
    """Evento NDJSON canônico para metadados iniciais do chat no stream."""
    return json.dumps(
        {
            "type": "chat_meta",
            "content": {
                "chat_id": chat_id,
                "url": str(url or ""),
                "chromium_profile": str(chromium_profile or ""),
            },
        },
        ensure_ascii=False,
    )


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


def build_markdown_event(content: str) -> str:
    """JSON do evento de markdown consumido pelo SSE / `stream_queue`.

    Formato: `{"type": "markdown", "content": "<texto>"}`. Espelha
    `build_error_event` (sem extras). `ensure_ascii=False` preserva
    acentos/emoji. Sem newline trailing — o chamador decide.
    """
    return json.dumps({"type": "markdown", "content": str(content)}, ensure_ascii=False)


def build_log_stream_line_sse(line, path) -> str:
    """Frame SSE para uma linha lida do log em `/api/logs/stream`.

    Formato canônico (`text/event-stream`):
    ``event: log\\ndata: {<json>}\\n\\n``. O payload JSON usa
    ``ensure_ascii=False`` para preservar acentos. `line` é
    `rstrip("\\n")`-ada para evitar quebras visuais duplas no consumer.
    """
    payload = json.dumps(
        {"line": (line or "").rstrip("\n"), "path": path},
        ensure_ascii=False,
    )
    return f"event: log\ndata: {payload}\n\n"


def build_log_stream_ping_sse() -> str:
    """Frame SSE de heartbeat para `/api/logs/stream` (mantém conexão viva)."""
    return "event: ping\ndata: {}\n\n"


def build_log_stream_error_sse(error, path) -> str:
    """Frame SSE de erro emitido quando `/api/logs/stream` falha durante leitura.

    Formato: ``event: error\\ndata: {"error": "<msg>", "path": "<p>"}\\n\\n``
    com ``ensure_ascii=False``. `error` é `str()`-coerced no caller
    histórico — preservamos esse contrato aqui também.
    """
    payload = json.dumps(
        {"error": str(error), "path": path},
        ensure_ascii=False,
    )
    return f"event: error\ndata: {payload}\n\n"


def build_search_result_event(content, **extras) -> str:
    """JSON do evento `searchresult` emitido por `_handle_browser_search_api`.

    Formato: `{"type": "searchresult", "content": <result>, **extras}`.
    Espelha `build_status_event` mas o `content` pode ser dict (resultado
    completo da query) — não há coerção via `str()`. Campos extras
    típicos: `query`, `index`, `total`, `source`. Sem newline trailing.
    """
    payload = {"type": "searchresult", "content": content}
    payload.update(extras)
    return json.dumps(payload, ensure_ascii=False)


def build_search_finish_event(results) -> str:
    """JSON do evento `finish` que encerra um stream de busca.

    Formato canônico:
    ``{"type": "finish", "content": {"success": True, "results": [...]}}``.
    `results` é repassado sem mutação para não acoplar o helper ao
    schema de cada item de resultado.
    """
    payload = {
        "type": "finish",
        "content": {"success": True, "results": results},
    }
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
    claude_project=None,
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
        'claude_project':   normalize_optional_text(claude_project),
        'browser_profile':  effective_browser_profile,
    }


def safe_int(value, default: int) -> int:
    """Converte ``value`` para ``int`` ou retorna ``default`` em qualquer
    falha (None, string vazia, string inválida, tipo incompatível).

    Usado em endpoints que aceitam parâmetros opcionais de query string ou
    JSON e precisam de fallback silencioso (ex.: `request.args.get("limit")`,
    `data.get("index")`). O contrato histórico equivale ao idiom:

    ```python
    try:
        x = int(value)
    except Exception:
        x = default
    ```

    Aceita `default` negativo (usado por `queue_failed_retry` com `-1` para
    sinalizar "ausente"). Mantém a semântica byte-equivalente (incluindo
    coerção de bool → int implícita do Python — `int(True) == 1`).
    """
    try:
        return int(value)
    except Exception:
        return int(default)


def resolve_logs_tail_lines_limit(
    raw_lines,
    *,
    default: int = 120,
    min_lines: int = 10,
    max_lines: int = 800,
) -> int:
    """Normaliza `?lines=` de `/api/logs/tail` com clamp estável."""
    requested = safe_int(raw_lines, default)
    return max(int(min_lines), min(int(max_lines), int(requested)))


def parse_from_end_flag(raw_value) -> bool:
    """Interpreta `?from_end=` de `/api/logs/stream` com o idiom legado."""
    return str(raw_value).strip().lower() not in {"0", "false", "no"}


def extract_queue_failed_limit(raw_limit, default: int = 100) -> int:
    """Normaliza `?limit=` de `/api/queue/failed`."""
    return safe_int(raw_limit, default)


def extract_queue_failed_retry_index(data, default: int = -1) -> int:
    """Extrai índice de retry da DLQ (`data["index"]`) com fallback estável."""
    if not isinstance(data, Mapping):
        return int(default)
    return safe_int(data.get("index", default), default)


def advance_health_ping_state(
    ping_count,
    last_log_time,
    now,
    *,
    interval_sec: int = 300,
) -> dict:
    """Avança o estado do `/health` preservando o contrato histórico.

    Regras:
      - Sempre incrementa `ping_count` em +1.
      - Se `now - last_log_time >= interval_sec`, sinaliza log, atualiza
        `last_log_time` para `now` e reseta `ping_count` para 0.
      - Caso contrário, mantém `last_log_time` e conserva o contador
        incrementado.
    """
    next_count = safe_int(ping_count, 0) + 1
    prev_last = float(last_log_time or 0.0)
    current = float(now or 0.0)
    should_log = (current - prev_last) >= int(interval_sec)
    if should_log:
        return {
            "should_log": True,
            "next_ping_count": 0,
            "next_last_log_time": current,
            "logged_ping_count": next_count,
        }
    return {
        "should_log": False,
        "next_ping_count": next_count,
        "next_last_log_time": prev_last,
        "logged_ping_count": next_count,
    }


def build_unauthorized_payload() -> dict:
    """Payload canônico para respostas HTTP 401 dos handlers protegidos."""
    return {"error": "Unauthorized"}


def build_search_progress_extras(
    query, idx: int, total: int, source: str, *, phase=None,
) -> dict:
    """Kwargs canônicos para eventos SSE de progresso de busca web/uptodate.

    Sites usuais: `_handle_browser_search_api.generate()` — usado em 3 yields
    (prepare, keepalive, result). Quando `phase` é fornecido, é inserido entre
    `total` e `source` para preservar a ordem histórica das chaves no JSON
    (`{query, index, total, phase, source}` para status; `{query, index, total,
    source}` para result).
    """
    extras = {
        "query": query,
        "index": idx,
        "total": total,
    }
    if phase is not None:
        extras["phase"] = phase
    extras["source"] = source
    return extras


def build_search_phase_label(route_label, kind) -> str:
    """Constrói o label canônico de `phase` para SSE de busca.

    `route_label` em maiúsculas (ex.: 'WEB_SEARCH'); `kind` é o sufixo
    (ex.: 'prepare', 'keepalive'). Retorna `web_search_prepare`.
    """
    if route_label is None:
        route_label = ""
    if kind is None:
        kind = ""
    return f"{str(route_label).lower()}_{str(kind)}"


def build_search_prepare_message(source_label, idx: int, total: int) -> str:
    """Mensagem canônica do status SSE 'Preparando busca'."""
    return f"📚 Preparando busca {source_label} {idx}/{total}."


def build_search_keepalive_message(source_label, query) -> str:
    """Mensagem canônica do status SSE 'busca em andamento'."""
    return f'⏳ Busca {source_label} por "{query}" ainda em andamento...'


def safe_snapshot_stats(queue_obj) -> dict:
    """Wrapper defensivo para ``queue_obj.snapshot_stats()`` que jamais
    levanta exceção.

    Retorna:
    - ``{}`` quando ``queue_obj`` não expõe ``snapshot_stats``;
    - resultado de ``snapshot_stats() or {}`` no caminho feliz;
    - ``{"error": "<repr>"}`` quando a chamada lança.

    Idiom historicamente duplicado em `queue_status` e `api_metrics` —
    contrato byte-equivalente preservado para ambos os endpoints.
    """
    try:
        if hasattr(queue_obj, "snapshot_stats"):
            return queue_obj.snapshot_stats() or {}
    except Exception as e:
        return {"error": str(e)}
    return {}


__all__ = [
    "format_wait_seconds",
    "queue_status_payload",
    "prune_old_attempts",
    "count_active_chatgpt_profiles",
    "combine_openai_messages",
    "build_sender_label",
    "wrap_paste_if_python_source",
    "coalesce_origin_url",
    "extract_source_hint",
    "decode_attachment",
    "resolve_chat_url",
    "resolve_browser_profile",
    "normalize_optional_text",
    "extract_requester_identity",
    "resolve_lookup_origin_url",
    "extract_chat_delete_local_targets",
    "extract_delete_request_targets",
    "extract_menu_url",
    "extract_menu_execute_payload",
    "extract_web_search_test_params",
    "build_web_search_test_task",
    "build_web_search_test_stream_response",
    "build_web_search_test_error_payload",
    "build_web_search_test_timeout_payload",
    "build_web_search_test_no_response_payload",
    "build_web_search_test_terminal_response",
    "extract_manual_whatsapp_reply_targets",
    "format_manual_whatsapp_requester_suffix",
    "resolve_download_content_type",
    "resolve_avatar_filename",
    "count_active_chats",
    "count_unfinished_chats",
    "find_expired_chat_ids",
    "mark_chat_finished",
    "build_active_chat_meta",
    "normalize_source_hint",
    "build_queue_key",
    "build_chat_task_payload",
    "build_chat_id_event",
    "build_chat_meta_event",
    "build_error_event",
    "build_status_event",
    "build_markdown_event",
    "build_chat_id_event",
    "build_chat_meta_event",
    "build_log_stream_line_sse",
    "build_log_stream_ping_sse",
    "build_log_stream_error_sse",
    "build_search_result_event",
    "build_search_finish_event",
    "normalize_source_hint",
    "build_active_chat_meta",
    "count_active_chats",
    "count_unfinished_chats",
    "find_expired_chat_ids",
    "extract_manual_whatsapp_reply_targets",
    "format_manual_whatsapp_requester_suffix",
    "resolve_download_content_type",
    "resolve_avatar_filename",
    "format_requester_suffix",
    "format_origin_suffix",
    "compute_python_request_interval",
    "safe_int",
    "resolve_logs_tail_lines_limit",
    "parse_from_end_flag",
    "extract_queue_failed_limit",
    "extract_queue_failed_retry_index",
    "advance_health_ping_state",
    "build_unauthorized_payload",
    "build_search_progress_extras",
    "build_search_phase_label",
    "build_search_prepare_message",
    "build_search_keepalive_message",
    "safe_snapshot_stats",
]


# Exportação auxiliar usada apenas para compatibilidade dos wrappers em
# server.py — evita ruído com o símbolo `deque` não importado em testes.
_deque_cls = deque
