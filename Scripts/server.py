# =============================================================================
# server.py — Servidor Flask do ChatGPT Simulator (portas 3002 HTTPS / 3003 HTTP)
# =============================================================================
#
# RESPONSABILIDADE:
#   Expõe a API REST consumida pelo frontend PHP (chatgpt_integracao_criado_
#   pelo_gemini_js.php) e pelo analisador_prontuarios.py. Recebe requisições,
#   enfileira tarefas para o browser.py via browser_queue e retorna respostas
#   em streaming (SSE) ou JSON.
#
# RELAÇÕES:
#   • Importa: config, shared (browser_queue), storage, auth, utils
#   • Chamado por: main.py (em duas threads: HTTPS 3002 e HTTP 3003)
#   • Consome de: browser.py (via browser_queue + ACTIVE_CHATS)
#   • Serve: chatgpt_integracao_criado_pelo_gemini_js.php, analisador_prontuarios.py
#
# ROTAS PRINCIPAIS:
#   POST /login                     — autenticação
#   POST /logout                    — encerra sessão
#   GET  /api/user/info             — dados do usuário logado
#   POST /api/menu/options          — lista opções de menu do ChatGPT
#   POST /api/menu/execute          — executa opção de menu (ex: Excluir)
#   POST /api/sync                  — sincroniza histórico de mensagens
#   POST /api/delete                — exclui chat no ChatGPT e no histórico local
#   POST /v1/chat/completions       — endpoint principal: envia mensagem ao ChatGPT
#                                     e retorna resposta em streaming ou bloco
#   GET  /api/history               — histórico local de chats
#   POST /api/web_search            — pesquisa web via browser.py (Google)
#   POST /api/uptodate_search       — pesquisa no UpToDate via browser.py
# =============================================================================
import uuid
import json
import queue
import base64
import os
import random
import shutil
import time
import copy
import logging
import sys
from collections import deque
from flask import Flask, request, jsonify, Response, send_from_directory, stream_with_context, make_response
from flask_cors import CORS
import config
from shared import browser_queue, get_file_info
import storage
import auth
from utils import log as file_log
import threading
try:
    from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
except Exception:
    Counter = Gauge = None
    generate_latest = None
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

# ─────────────────────────────────────────────────────────────
# CAPTURA CONFIGURAÇÃO DE DEBUG (que é estabelecida no arquivo "config.py").
# ─────────────────────────────────────────────────────────────
# Verifica se config já foi importado; se não, importa
if 'config' not in sys.modules:
    import config

# Tenta importar DEBUG_LOG do módulo config já carregado
try:
    DEBUG_LOG = config.DEBUG_LOG
except AttributeError:
    DEBUG_LOG = False  # fallback se a variável não existir no config
    logging.warning("⚠️ DEBUG_LOG não encontrado no config.py. Usando False como padrão.")

ACTIVE_CHATS = {}
ACTIVE_SYNCS = {}
ACTIVE_SYNCS_LOCK = threading.Lock()
WEB_SEARCH_MIN_INTERVAL_SEC = 8
WEB_SEARCH_MAX_INTERVAL_SEC = 22
WEB_SEARCH_PROGRESS_TICK_SEC = 1.0
_web_search_timing_lock = threading.Lock()
_web_search_last_started_at = 0.0
_web_search_last_interval_sec = 0.0
CHAT_RATE_LIMIT_DEFAULT_COOLDOWN_SEC = 240
CHAT_RATE_LIMIT_PROGRESS_TICK_SEC = 1.0
_chat_rate_limit_lock = threading.Lock()
_chat_rate_limit_until = 0.0
_chat_rate_limit_strikes = 0
ACTIVE_CHAT_STALE_SEC = 900
SERVER_STARTED_AT = time.time()
PYTHON_CHAT_QUEUE_TICK_SEC = 1.0
PYTHON_CHAT_QUEUE_TIMEOUT_SEC = max(
    30,
    int(
        getattr(
            config,
            "REQUEST_TIMEOUT_SEC",
            getattr(config, "AUTODEV_AGENT_REQUEST_TIMEOUT", 900)
        ) or os.getenv(
            "REQUEST_TIMEOUT_SEC",
            os.getenv("AUTODEV_AGENT_REQUEST_TIMEOUT", "900")
        )
    )
)
_python_chat_queue_lock = threading.Lock()
_python_chat_queue_cond = threading.Condition(_python_chat_queue_lock)
_python_chat_queue_waiting = []
_python_chat_queue_active = None
PROM_QUEUE_SIZE = Gauge("simulator_queue_size", "Tamanho atual da fila") if Gauge else None
PROM_ACTIVE_CHATS = Gauge("simulator_active_chats", "Chats ativos em processamento") if Gauge else None
PROM_HTTP_ERRORS = Counter("simulator_http_errors_total", "Total de erros HTTP por status", ["status"]) if Counter else None


def _cleanup_active_chats():
    while True:
        time.sleep(300)
        cutoff = time.time() - 600
        to_delete = [
            k for k, v in list(ACTIVE_CHATS.items())
            if v.get('finished') and v.get('finished_at', 0) < cutoff
        ]
        for k in to_delete:
            del ACTIVE_CHATS[k]
        if to_delete:
            log(f"[ACTIVE_CHATS] {len(to_delete)} entradas expiradas removidas.")

threading.Thread(target=_cleanup_active_chats, daemon=True).start()

# --- FILTRO DE LOG ---
class No401AuthLog(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        # Suprime log genérico de 401 em /api/user/info
        if "GET /api/user/info" in msg and " 401 " in msg:
            return False
        # Suprime log repetitivo de GET /health (ping do analisador)
        if "GET /health" in msg and " 200 " in msg:
            return False
        return True
logging.getLogger("werkzeug").addFilter(No401AuthLog())

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

app = Flask(__name__, static_folder=config.DIRS["frontend"])
# CORS: apenas origens explicitamente configuradas em config.CORS_ALLOWED_ORIGINS.
# Vazio = nenhuma origem cross-site (mas chamadas same-origin/API-key funcionam).
CORS(app, resources={r"/*": {"origins": list(getattr(config, "CORS_ALLOWED_ORIGINS", []))}},
     supports_credentials=True)

_security_lock = threading.Lock()
_rate_limit_hits: dict[str, deque[float]] = {}
_blocked_ips: dict[str, dict] = {}
_failed_login_attempts: dict[str, deque[float]] = {}
RATE_LIMIT_WINDOW_SEC = 60
RATE_LIMIT_PER_MIN = max(20, int(getattr(config, "SECURITY_RATE_LIMIT_PER_MIN", 120)))
LOGIN_MAX_FAILS = max(3, int(getattr(config, "SECURITY_LOGIN_MAX_FAILS", 8)))
LOGIN_BLOCK_SEC = max(60, int(getattr(config, "SECURITY_LOGIN_BLOCK_SEC", 900)))
SENSITIVE_AUDIT_ENDPOINTS = {
    "/login",
    "/logout",
    "/api/user/update_password",
    "/api/menu/execute",
    "/api/delete",
    "/v1/chat/completions",
    "/api/queue/status",
    "/api/queue/failed",
    "/api/queue/failed/retry",
    "/api/logs/tail",
    "/api/logs/stream",
    "/api/metrics",
    "/metrics",
}


def log(msg):
    file_log("server.py", msg)


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "").strip()
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.remote_addr or "unknown").strip()


def _audit_event(event_type: str, **extra):
    payload = {
        "event": event_type,
        "ts": int(time.time()),
        "ip": _client_ip() if request else "unknown",
        "method": getattr(request, "method", ""),
        "path": getattr(request, "path", ""),
    }
    payload.update(extra or {})
    try:
        log(f"[SECURITY_AUDIT] {json.dumps(payload, ensure_ascii=False)}")
    except Exception:
        log(f"[SECURITY_AUDIT] {payload}")


def _prune_old_attempts(dq: deque[float], window_sec: int):
    cutoff = time.time() - window_sec
    while dq and dq[0] < cutoff:
        dq.popleft()


def _is_ip_blocked(ip: str) -> tuple[bool, float, str]:
    with _security_lock:
        rec = _blocked_ips.get(ip)
        if not rec:
            return False, 0.0, ""
        until = float(rec.get("until", 0.0))
        if until <= time.time():
            _blocked_ips.pop(ip, None)
            return False, 0.0, ""
        return True, max(0.0, until - time.time()), str(rec.get("reason", "blocked"))


def _register_rate_limit_hit(ip: str, key: str) -> tuple[bool, float]:
    map_key = f"{ip}:{key}"
    with _security_lock:
        dq = _rate_limit_hits.setdefault(map_key, deque())
        now = time.time()
        dq.append(now)
        _prune_old_attempts(dq, RATE_LIMIT_WINDOW_SEC)
        if len(dq) > RATE_LIMIT_PER_MIN:
            retry_after = max(1.0, RATE_LIMIT_WINDOW_SEC - (now - dq[0]))
            return True, retry_after
        return False, 0.0


def _register_login_failure(ip: str):
    now = time.time()
    with _security_lock:
        dq = _failed_login_attempts.setdefault(ip, deque())
        dq.append(now)
        _prune_old_attempts(dq, LOGIN_BLOCK_SEC)
        if len(dq) >= LOGIN_MAX_FAILS:
            _blocked_ips[ip] = {
                "until": now + LOGIN_BLOCK_SEC,
                "reason": "bruteforce_login",
            }


def _clear_login_failures(ip: str):
    with _security_lock:
        _failed_login_attempts.pop(ip, None)


def _generate_csrf_token() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _validate_csrf_for_session() -> bool:
    # Exige CSRF apenas para autenticação por sessão/cookie em métodos mutáveis.
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return True
    if request.path in {"/login", "/health"}:
        return True
    if request.path.startswith("/static"):
        return True

    # Se request autenticou por API key/Bearer, não depende de cookie de sessão.
    auth_header = request.headers.get("Authorization") or ""
    data = request.get_json(silent=True) or {}
    if auth_header.startswith("Bearer ") or data.get("api_key") == config.API_KEY or request.args.get("api_key") == config.API_KEY:
        return True

    session_token = request.cookies.get("session_token")
    if not session_token:
        return True

    origin = (request.headers.get("Origin") or "").strip()
    referer = (request.headers.get("Referer") or "").strip()
    host_url = (request.host_url or "").rstrip("/")
    if origin.startswith(host_url) or referer.startswith(host_url):
        return True

    csrf_cookie = request.cookies.get("csrf_token", "")
    csrf_header = request.headers.get("X-CSRF-Token", "")
    csrf_body = str((data or {}).get("csrf_token") or "")
    provided = csrf_header or csrf_body
    return bool(csrf_cookie and provided and csrf_cookie == provided)


def _format_wait_seconds(seconds):
    remaining = max(0, int(round(seconds)))
    mins, secs = divmod(remaining, 60)
    return f"{mins:02d}:{secs:02d}"


def _extract_rate_limit_details(error_payload):
    """
    Identifica sinalização de rate-limit enviada pelo browser.py.
    Aceita payload string ou dict.
    """
    code = ""
    message = ""
    retry_after = None

    if isinstance(error_payload, dict):
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

    lowered = f"{code} {message}".lower()
    is_rate_limited = (
        code in {"rate_limit", "too_many_requests"}
        or "excesso de solicita" in lowered
        or "too many request" in lowered
        or "too many requests" in lowered
    )
    return is_rate_limited, message, retry_after


def _register_chat_rate_limit(retry_after_seconds=None, reason=""):
    global _chat_rate_limit_until, _chat_rate_limit_strikes
    cooldown = retry_after_seconds or CHAT_RATE_LIMIT_DEFAULT_COOLDOWN_SEC
    cooldown = max(1, int(cooldown))
    now = time.time()
    with _chat_rate_limit_lock:
        remaining = max(0.0, _chat_rate_limit_until - now)
        if remaining > 0:
            _chat_rate_limit_strikes = min(6, _chat_rate_limit_strikes + 1)
        else:
            _chat_rate_limit_strikes = 0
        backoff_multiplier = 2 ** _chat_rate_limit_strikes
        adjusted_cooldown = min(1800, int(cooldown * backoff_multiplier))
        until_ts = now + adjusted_cooldown
        _chat_rate_limit_until = max(_chat_rate_limit_until, until_ts)
    cooldown = adjusted_cooldown
    if reason:
        log(f"[CHAT_RATE_LIMIT] cooldown de {cooldown}s registrado. Motivo: {reason}")
    else:
        log(f"[CHAT_RATE_LIMIT] cooldown de {cooldown}s registrado.")


def _get_chat_rate_limit_remaining_seconds():
    with _chat_rate_limit_lock:
        return max(0.0, _chat_rate_limit_until - time.time())


def _wait_chat_rate_limit_if_needed(stream_queue=None):
    inline_open = False
    while True:
        remaining = _get_chat_rate_limit_remaining_seconds()
        if remaining <= 0:
            if inline_open:
                try:
                    width = max(80, shutil.get_terminal_size((160, 20)).columns - 1)
                except Exception:
                    width = 120
                sys.stdout.write("\r" + (" " * width) + "\r\n")
                sys.stdout.flush()
            return
        status_text = (
            "⏳ Aguardando cooldown por excesso de solicitações no ChatGPT. "
            f"Nova tentativa em {_format_wait_seconds(remaining)}."
        )
        if stream_queue is not None:
            stream_queue.put(json.dumps({
                "type": "status",
                "content": status_text,
                "phase": "chat_rate_limit_cooldown",
                "wait_seconds": round(remaining, 1),
            }, ensure_ascii=False))
        # No CMD do próprio ChatGPT Simulator: atualizar cooldown inline.
        try:
            width = max(80, shutil.get_terminal_size((160, 20)).columns - 1)
        except Exception:
            width = 120
        line = status_text if len(status_text) <= width else (status_text[: width - 3].rstrip() + "...")
        sys.stdout.write("\r" + line.ljust(width))
        sys.stdout.flush()
        inline_open = True
        time.sleep(min(CHAT_RATE_LIMIT_PROGRESS_TICK_SEC, remaining))


def _has_active_remote_user_chat():
    now = time.time()
    for _chat_id, meta in list(ACTIVE_CHATS.items()):
        if meta.get('finished'):
            continue
        last_event_at = float(meta.get('last_event_at') or 0.0)
        if last_event_at and (now - last_event_at) > ACTIVE_CHAT_STALE_SEC:
            meta['finished'] = True
            meta['finished_at'] = now
            log(f"[ACTIVE_CHATS] chat {_chat_id} marcado como finalizado por inatividade "
                f"({int(now - last_event_at)}s).")
            continue
        if meta.get('is_analyzer'):
            continue
        return True
    return False


def _wait_remote_user_priority_if_needed(is_analyzer: bool, stream_queue=None):
    """
    Se a origem for o analisador, aguarda chats remotos em andamento finalizarem.
    """
    if not is_analyzer:
        return
    while _has_active_remote_user_chat():
        if stream_queue is not None:
            stream_queue.put(json.dumps({
                "type": "status",
                "content": (
                    "⏳ Aguardando finalização de pedido remoto prioritário em andamento "
                    "antes de iniciar a análise automática."
                ),
                "phase": "analyzer_waiting_remote_priority",
            }, ensure_ascii=False))
        time.sleep(1.0)


def _is_python_chat_request(source_hint_norm: str) -> bool:
    src = (source_hint_norm or "").strip().lower()
    return src.endswith(".py") or ".py/" in src or src.startswith("python:")


def _is_codex_chat_request(source_hint_norm: str, url: str, origin_url: str) -> bool:
    hay = " ".join([
        str(source_hint_norm or "").lower(),
        str(url or "").lower(),
        str(origin_url or "").lower(),
    ])
    return ("codex" in hay) or ("/codex/cloud" in hay) or ("/codex/" in hay)


def _queue_status_payload(wait_seconds: float, position: int, total: int, sender_label: str) -> str:
    return json.dumps({
        "type": "status",
        "content": (
            f"⏳ Fila interna do servidor: posição {position}/{max(1, total)}. "
            f"Tempo restante estimado para liberação: {_format_wait_seconds(wait_seconds)}."
        ),
        "phase": "server_python_queue_wait",
        "wait_seconds": round(max(0.0, wait_seconds), 1),
        "queue_position": int(position),
        "queue_size": int(total),
        "sender": sender_label,
    }, ensure_ascii=False)


def _acquire_python_chat_slot(request_key: str,
                              stream_queue=None,
                              sender_label: str = "") -> None:
    """FIFO para pedidos Python (ChatGPT), com timeout e status progressivo."""
    global _python_chat_queue_active
    joined_at = time.time()
    with _python_chat_queue_cond:
        _python_chat_queue_waiting.append(request_key)
        while True:
            elapsed = time.time() - joined_at
            remaining = max(0.0, PYTHON_CHAT_QUEUE_TIMEOUT_SEC - elapsed)

            # Timeout na fila antes de obter slot
            if remaining <= 0:
                if request_key in _python_chat_queue_waiting:
                    _python_chat_queue_waiting.remove(request_key)
                _python_chat_queue_cond.notify_all()
                raise TimeoutError(
                    f"Timeout de fila ({PYTHON_CHAT_QUEUE_TIMEOUT_SEC}s) aguardando slot interno."
                )

            is_head = (
                len(_python_chat_queue_waiting) > 0
                and _python_chat_queue_waiting[0] == request_key
            )
            if is_head and _python_chat_queue_active is None:
                _python_chat_queue_waiting.pop(0)
                _python_chat_queue_active = request_key
                _python_chat_queue_cond.notify_all()
                return

            if stream_queue is not None:
                try:
                    pos = (_python_chat_queue_waiting.index(request_key) + 1)
                except ValueError:
                    pos = 1
                total = len(_python_chat_queue_waiting) + (1 if _python_chat_queue_active else 0)
                stream_queue.put(_queue_status_payload(remaining, pos, total, sender_label))

            _python_chat_queue_cond.wait(timeout=min(PYTHON_CHAT_QUEUE_TICK_SEC, remaining))


def _release_python_chat_slot(request_key: str) -> None:
    global _python_chat_queue_active
    with _python_chat_queue_cond:
        if _python_chat_queue_active == request_key:
            _python_chat_queue_active = None
        else:
            try:
                _python_chat_queue_waiting.remove(request_key)
            except ValueError:
                pass
        _python_chat_queue_cond.notify_all()


def _reserve_web_search_slot():
    """
    Reserva a próxima janela permitida para busca web com espaçamento humano.
    O lock garante que buscas concorrentes respeitem o mesmo relógio global.
    """
    global _web_search_last_started_at, _web_search_last_interval_sec

    now = time.time()
    interval = random.uniform(WEB_SEARCH_MIN_INTERVAL_SEC, WEB_SEARCH_MAX_INTERVAL_SEC)

    with _web_search_timing_lock:
        earliest_start = now
        if _web_search_last_started_at > 0:
            earliest_start = max(earliest_start, _web_search_last_started_at + interval)

        wait_seconds = max(0.0, earliest_start - now)
        _web_search_last_started_at = earliest_start
        _web_search_last_interval_sec = interval

    return {
        "interval_sec": interval,
        "scheduled_start_at": earliest_start,
        "wait_seconds": wait_seconds,
        "requested_at": now,
    }


def _iter_web_search_wait_messages(wait_ctx, query_str):
    remaining = wait_ctx["wait_seconds"]
    interval = wait_ctx["interval_sec"]

    if remaining <= 0:
        return

    yield {
        "type": "status",
        "content": (
            f"⏳ Aguardando intervalo humano antes da busca web por "
            f"\"{query_str}\". Pausa planejada: {_format_wait_seconds(interval)}."
        ),
        "query": query_str,
        "wait_seconds": round(remaining, 1),
        "planned_interval_seconds": round(interval, 1),
        "phase": "web_search_cooldown",
    }

    while remaining > 0:
        chunk = min(WEB_SEARCH_PROGRESS_TICK_SEC, remaining)
        time.sleep(chunk)
        remaining = max(0.0, wait_ctx["scheduled_start_at"] - time.time())
        yield {
            "type": "status",
            "content": (
                f"⏳ Pausa anti-bot em andamento antes da busca web por "
                f"\"{query_str}\". Início previsto em {_format_wait_seconds(remaining)}."
            ),
            "query": query_str,
            "wait_seconds": round(remaining, 1),
            "planned_interval_seconds": round(interval, 1),
            "phase": "web_search_cooldown",
        }


def _execute_single_browser_search(query_str, browser_action, source_label, phase_prefix, stream_queue=None, sender_label=None):
    wait_ctx = _reserve_web_search_slot()

    if wait_ctx["wait_seconds"] > 0:
        log(
            f"[{phase_prefix.upper()}] cooldown de {wait_ctx['wait_seconds']:.1f}s "
            f"(intervalo alvo {wait_ctx['interval_sec']:.1f}s) antes da query: {query_str}"
        )

    for msg in _iter_web_search_wait_messages(wait_ctx, query_str):
        msg["phase"] = f"{phase_prefix}_cooldown"
        msg["source"] = source_label
        msg["content"] = msg["content"].replace("busca web", f"busca {source_label}")
        if stream_queue is not None:
            stream_queue.put(json.dumps(msg, ensure_ascii=False))

    if stream_queue is not None:
        stream_queue.put(json.dumps({
            "type": "status",
            "content": f"🔎 Iniciando busca {source_label} por \"{query_str}\".",
            "query": query_str,
            "wait_seconds": 0,
            "phase": f"{phase_prefix}_start",
            "source": source_label,
        }, ensure_ascii=False))

    q = queue.Queue()
    browser_queue.put({
        'action':       browser_action,
        'query':        query_str,
        'stream_queue': q,
        'sender':       sender_label or source_label
    })

    try:
        while True:
            raw_msg = q.get(timeout=60)
            if raw_msg is None:
                break
            msg = json.loads(raw_msg)
            if msg.get('type') == 'searchresult':
                content = msg.get('content', {}) or {}
                content.setdefault('source', source_label)
                return content
            if msg.get('type') == 'error':
                return {'success': False, 'query': query_str, 'error': msg.get('content'), 'source': source_label}
    except queue.Empty:
        return {'success': False, 'query': query_str, 'error': f'Timeout na busca {source_label}', 'source': source_label}
    except Exception as e:
        return {'success': False, 'query': query_str, 'error': str(e), 'source': source_label}

    return {'success': False, 'query': query_str, 'error': f'Busca {source_label} encerrada sem resultado', 'source': source_label}


def _execute_single_web_search(query_str, stream_queue=None, sender_label=None):
    return _execute_single_browser_search(
        query_str=query_str,
        browser_action='SEARCH',
        source_label='web',
        phase_prefix='web_search',
        stream_queue=stream_queue,
        sender_label=sender_label,
    )


def _execute_single_uptodate_search(query_str, stream_queue=None, sender_label=None):
    return _execute_single_browser_search(
        query_str=query_str,
        browser_action='UPTODATE_SEARCH',
        source_label='uptodate',
        phase_prefix='uptodate_search',
        stream_queue=stream_queue,
        sender_label=sender_label,
    )

# --- POLÍTICA DE ACESSO ---
# Ordem de verificação (primeira que aprovar, libera):
#   1. Rotas públicas (página inicial, health-check, login, estáticos).
#   2. API key válida (Bearer Authorization, body.api_key ou query.api_key).
#      → Esta é a autenticação PRIMÁRIA para integrações externas. O IP do
#        solicitante pode mudar (residencial, móvel, proxies), então a chave
#        é a fonte de verdade.
#   3. Sessão de usuário válida (cookie session_token).
#   4. Somente quando as três acima falham, verificamos origem/IP como
#      camada extra de defesa contra bots não autenticados.
@app.before_request
def enforce_access_policy():
    # 1. Rotas totalmente públicas
    if request.path in ['/', '/health', '/robots.txt', '/favicon.ico', '/login'] or request.path.startswith('/static'):
        return

    # 2. API key (Bearer / body / query) — autenticação primária
    auth_header = request.headers.get("Authorization") or ""
    if auth_header.startswith("Bearer ") and auth_header.split(" ", 1)[1] == config.API_KEY:
        return
    data = request.get_json(silent=True) or {}
    if data.get("api_key") == config.API_KEY:
        return
    if request.args.get("api_key") == config.API_KEY:
        return

    # 3. Sessão de usuário válida
    if auth.check_session(request):
        return

    # 4. Fallback: origem/IP explicitamente na allowlist (defesa em profundidade)
    origin = request.headers.get('Origin') or ''
    referer = request.headers.get('Referer') or ''
    allowed_domains = getattr(config, 'CORS_ALLOWED_ORIGINS', []) or []
    allowed_ips = getattr(config, 'ALLOWED_IPS', ['127.0.0.1']) or []

    if origin and any(origin.startswith(domain) for domain in allowed_domains):
        return
    if referer and any(referer.startswith(domain) for domain in allowed_domains):
        return
    if request.remote_addr in allowed_ips:
        return

    return jsonify({"error": "Acesso negado. Autenticação ausente ou origem não autorizada."}), 403


# --- MIDDLEWARE ---
def check_auth():
    # 1. Tenta pegar pelo Header padrão (Bearer Token) - Estilo OpenAI/Ollama
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        key = auth_header.split(" ")[1]
        if key == config.API_KEY:
            return True
    # 2. Tenta pegar pelo corpo do JSON (POST)
    data = request.get_json(silent=True) or {}
    if data.get("api_key") == config.API_KEY:
        return True
    # 3. Tenta pegar pela Query String da URL (GET)
    if request.args.get("api_key") == config.API_KEY:
        return True
    # 4. Verificação de sessão de usuário (Browser)
    user = auth.check_session(request)
    if user: return True
    return False

@app.before_request
def before_request():
    public_routes = ['/', '/health', '/login', '/favicon.ico']
    # /api/web_search/test gerencia autenticação internamente (mostra HTML de login se não autenticado)
    self_auth_routes = ['/api/web_search/test']
    if request.path in public_routes or request.path in self_auth_routes or request.path.startswith('/static'):
        return
    if request.method == "OPTIONS": return

    ip = _client_ip()
    blocked, remaining_block, block_reason = _is_ip_blocked(ip)
    if blocked:
        _audit_event(
            "blocked_ip_request",
            reason=block_reason,
            retry_after_sec=round(remaining_block, 1),
        )
        return jsonify({
            "error": "Too many suspicious attempts. IP temporarily blocked.",
            "retry_after_sec": int(max(1, remaining_block))
        }), 429

    rl_key = request.path if request.path.startswith("/api/") or request.path.startswith("/v1/") else "__other__"
    limited, retry_after = _register_rate_limit_hit(ip, rl_key)
    if limited:
        _audit_event(
            "rate_limit_exceeded",
            key=rl_key,
            retry_after_sec=round(retry_after, 1),
        )
        return jsonify({
            "error": "Rate limit exceeded",
            "retry_after_sec": int(max(1, retry_after))
        }), 429

    if not _validate_csrf_for_session():
        _audit_event("csrf_validation_failed")
        return jsonify({"error": "CSRF validation failed"}), 403

    if not check_auth():
        _audit_event("unauthorized_request")
        return jsonify({"error": "Unauthorized", "auth_required": True}), 401


@app.after_request
def after_request_audit(response):
    try:
        if PROM_QUEUE_SIZE is not None:
            PROM_QUEUE_SIZE.set(float(browser_queue.qsize()))
        if PROM_ACTIVE_CHATS is not None:
            active = sum(1 for _k, meta in list(ACTIVE_CHATS.items()) if not meta.get("finished"))
            PROM_ACTIVE_CHATS.set(float(active))
        if PROM_HTTP_ERRORS is not None and int(getattr(response, "status_code", 0)) >= 400:
            PROM_HTTP_ERRORS.labels(status=str(int(response.status_code))).inc()
        if request.path in SENSITIVE_AUDIT_ENDPOINTS:
            _audit_event(
                "endpoint_access",
                status_code=int(getattr(response, "status_code", 0)),
                user_agent=(request.headers.get("User-Agent", "")[:180]),
            )
    except Exception:
        pass
    return response

# --- ROTAS AUTH ---
@app.route("/login", methods=["POST"])
def login_route():
    data = request.get_json() or {}
    ip = _client_ip()
    blocked, remaining_block, block_reason = _is_ip_blocked(ip)
    if blocked:
        _audit_event(
            "login_blocked",
            reason=block_reason,
            retry_after_sec=round(remaining_block, 1),
            username=(data.get("username") or "")[:80]
        )
        return jsonify({"success": False, "error": "IP temporariamente bloqueado", "retry_after_sec": int(max(1, remaining_block))}), 429

    token = auth.verify_login(data.get("username"), data.get("password"))
    if token:
        _clear_login_failures(ip)
        csrf_token = _generate_csrf_token()
        resp = jsonify({"success": True, "token": token})
        resp.set_cookie(
            'session_token', token, max_age=60*60*24*30,
            httponly=True,
            samesite=getattr(config, "SESSION_COOKIE_SAMESITE", "Lax"),
            secure=bool(getattr(config, "SESSION_COOKIE_SECURE", False)),
        )
        resp.set_cookie(
            'csrf_token', csrf_token, max_age=60*60*24*30,
            httponly=False,
            samesite=getattr(config, "SESSION_COOKIE_SAMESITE", "Lax"),
            secure=bool(getattr(config, "SESSION_COOKIE_SECURE", False)),
        )
        _audit_event("login_success", username=(data.get("username") or "")[:80])
        return resp
    _register_login_failure(ip)
    _audit_event("login_failed", username=(data.get("username") or "")[:80])
    return jsonify({"success": False, "error": "Credenciais inválidas"}), 401

@app.route("/logout", methods=["POST"])
def logout_route():
    token = request.cookies.get('session_token')
    auth.logout(token)
    resp = jsonify({"success": True})
    resp.set_cookie('session_token', '', expires=0)
    return resp

@app.route("/api/user/info", methods=["GET"])
def user_info():
    token = request.cookies.get('session_token')
    info = auth.get_user_info(token)
    if info: return jsonify(info)
    return jsonify({"error": "No session"}), 401

@app.route("/api/user/update_password", methods=["POST"])
def update_pass():
    data = request.get_json() or {}
    user = auth.check_session(request)
    if user and auth.change_password(user, data.get("new_password")):
        return jsonify({"success": True})
    return jsonify({"success": False})

@app.route("/api/user/upload_avatar", methods=["POST"])
def upload_avatar():
    user = auth.check_session(request)
    if not user: return jsonify({"success": False}), 401
    if 'file' not in request.files: return jsonify({"success": False})
    file = request.files['file']
    if file.filename == '': return jsonify({"success": False})
    if file:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ['.jpg', '.jpeg', '.png', '.gif', '.webp']: return jsonify({"success": False, "error": "Formato inválido"})
        filename = f"{user}{ext}"
        save_path = os.path.join(config.DIRS["users"], filename)
        try:
            if HAS_PIL:
                img = Image.open(file)
                img.thumbnail((150, 150))
                if img.mode in ("RGBA", "P"): img = img.convert("RGB")
                img.save(save_path, quality=85, optimize=True)
            else:
                file.save(save_path)
            auth.update_avatar(user, filename)
            return jsonify({"success": True, "avatar": filename})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

@app.route("/api/user/avatar/<filename>")
def get_avatar(filename):
    return send_from_directory(config.DIRS["users"], filename)

@app.route("/api/downloads/<file_id>")
def serve_download(file_id):
    """
    Proxy sob demanda: busca o arquivo do ChatGPT via browser.py
    (usando cookies/auth do Playwright) e faz streaming para o cliente.
    Nenhum arquivo é armazenado permanentemente em disco.
    """
    info = get_file_info(file_id)
    if not info:
        return jsonify({"error": "Arquivo não registrado. O link pode ter expirado."}), 404

    # Atalho: payload já capturado em memória pelo browser.py (sem roundtrip ao ChatGPT).
    if info.get("payload_b64"):
        raw_bytes = base64.b64decode(info["payload_b64"])
        display_name = info.get("name") or file_id
        content_type = info.get("content_type") or "application/octet-stream"

        ext = os.path.splitext(display_name)[1].lower().lstrip('.')
        mime_map = {
            'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'xls': 'application/vnd.ms-excel', 'csv': 'text/csv',
            'pdf': 'application/pdf', 'png': 'image/png',
            'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
            'zip': 'application/zip', 'json': 'application/json',
        }
        if content_type == 'application/octet-stream' and ext in mime_map:
            content_type = mime_map[ext]

        resp = make_response(raw_bytes)
        resp.headers['Content-Type'] = content_type
        resp.headers['Content-Disposition'] = f'attachment; filename="{display_name}"'
        resp.headers['Content-Length'] = len(raw_bytes)
        resp.headers['Cache-Control'] = 'no-cache'
        return resp

    file_url = info["url"]
    file_name = info["name"]

    # Cria fila de resposta para esta requisição
    response_queue = queue.Queue()

    # Envia tarefa de download para browser.py
    browser_queue.put({
        "action": "DOWNLOAD_FILE",
        "file_url": file_url,
        "file_name": file_name,
        "stream_queue": response_queue,
    })

    # Aguarda resposta do browser.py (timeout: 60s)
    result_data = None
    error_msg = None
    deadline = time.time() + 60

    while time.time() < deadline:
        try:
            raw = response_queue.get(timeout=2)
            if raw is None:
                break  # Sentinel: browser.py terminou
            evt = json.loads(raw) if isinstance(raw, str) else raw
            evt_type = evt.get("type", "")
            content = evt.get("content", "")

            if evt_type == "file_data":
                result_data = content
                break
            elif evt_type == "error":
                error_msg = content
                break
        except queue.Empty:
            continue

    if error_msg:
        return jsonify({"error": error_msg}), 502

    if not result_data:
        return jsonify({"error": "Timeout ao baixar arquivo do ChatGPT."}), 504

    # Decodifica dados base64 e envia ao cliente
    raw_bytes = base64.b64decode(result_data["data_b64"])
    content_type = result_data.get("content_type", "application/octet-stream")
    display_name = result_data.get("name", file_name)

    # Extensão → mime fallback
    ext = os.path.splitext(display_name)[1].lower().lstrip('.')
    mime_map = {
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'xls': 'application/vnd.ms-excel', 'csv': 'text/csv',
        'pdf': 'application/pdf', 'png': 'image/png',
        'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
        'zip': 'application/zip', 'json': 'application/json',
    }
    if content_type == 'application/octet-stream' and ext in mime_map:
        content_type = mime_map[ext]

    resp = make_response(raw_bytes)
    resp.headers['Content-Type'] = content_type
    resp.headers['Content-Disposition'] = f'attachment; filename="{display_name}"'
    resp.headers['Content-Length'] = len(raw_bytes)
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

# --- ROTAS GERAIS ---

# Endpoint de saúde para o Analisador de Prontuários (e outros serviços)
_health_ping_count = 0
_health_last_log_time = 0

@app.route("/health", methods=["GET"])
def health_check():
    global _health_ping_count, _health_last_log_time

    _health_ping_count += 1
    now = time.time()
    # Loga apenas 1x a cada 5 minutos para não poluir
    if now - _health_last_log_time >= 300:
        caller = request.headers.get("User-Agent", "desconhecido")
        log(f"🏥 Health check #{_health_ping_count} (origem: {caller})")
        _health_last_log_time = now
        _health_ping_count = 0  # reseta contador após logar

    return jsonify({"status": "ok", "service": "ChatGPT Simulator"}), 200


@app.route("/api/queue/status", methods=["GET"])
def queue_status():
    """
    Observabilidade da fila interna server → browser.
    Requer autenticação padrão (before_request/check_auth).
    """
    stats = {}
    try:
        if hasattr(browser_queue, "snapshot_stats"):
            stats = browser_queue.snapshot_stats() or {}
    except Exception as e:
        stats = {"error": str(e)}

    return jsonify({
        "success": True,
        "queue": {
            "qsize": int(browser_queue.qsize()),
            **stats
        }
    }), 200


@app.route("/api/queue/failed", methods=["GET"])
def queue_failed():
    """DLQ: lista tarefas que falharam no browser loop."""
    try:
        limit = int(request.args.get("limit", 100))
    except Exception:
        limit = 100
    items = browser_queue.list_failed(limit=limit) if hasattr(browser_queue, "list_failed") else []
    return jsonify({"success": True, "failed": items, "count": len(items)}), 200


@app.route("/api/queue/failed/retry", methods=["POST"])
def queue_failed_retry():
    """Reinsere item da DLQ na fila principal por índice."""
    data = request.get_json(silent=True) or {}
    try:
        idx = int(data.get("index", -1))
    except Exception:
        idx = -1
    if not hasattr(browser_queue, "retry_failed"):
        return jsonify({"success": False, "error": "dlq_not_supported"}), 400
    retried = browser_queue.retry_failed(idx)
    if not retried:
        return jsonify({"success": False, "error": "invalid_index"}), 404
    return jsonify({"success": True, "task": retried}), 200


@app.route("/api/logs/tail", methods=["GET"])
def logs_tail():
    """
    Retorna as últimas linhas do log atual do simulator.
    Ideal para polling leve no frontend (toast de observabilidade).
    """
    try:
        requested = int(request.args.get("lines", 120))
    except Exception:
        requested = 120
    lines_limit = max(10, min(800, requested))

    path = getattr(config, "LOG_PATH", "")
    if not path or not os.path.exists(path):
        return jsonify({"success": False, "error": "log_not_found", "path": path}), 404

    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            chunk_size = 4096
            data = b""
            pos = file_size
            while pos > 0 and data.count(b"\n") <= lines_limit:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                data = f.read(read_size) + data
            text = data.decode("utf-8", errors="replace")
            tail_lines = text.splitlines()[-lines_limit:]
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "path": path}), 500

    return jsonify({
        "success": True,
        "path": path,
        "lines": tail_lines,
        "line_count": len(tail_lines),
    }), 200


@app.route("/api/logs/stream", methods=["GET"])
def logs_stream():
    """
    Stream SSE de logs para reduzir polling no frontend.
    query:
      - from_end=1|0 (default 1): inicia no fim do arquivo
    """
    path = getattr(config, "LOG_PATH", "")
    if not path or not os.path.exists(path):
        return jsonify({"success": False, "error": "log_not_found", "path": path}), 404

    from_end = str(request.args.get("from_end", "1")).strip().lower() not in {"0", "false", "no"}

    @stream_with_context
    def generate():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                if from_end:
                    f.seek(0, os.SEEK_END)
                while True:
                    line = f.readline()
                    if line:
                        payload = json.dumps({"line": line.rstrip("\n"), "path": path}, ensure_ascii=False)
                        yield f"event: log\ndata: {payload}\n\n"
                    else:
                        yield "event: ping\ndata: {}\n\n"
                        time.sleep(1.0)
        except GeneratorExit:
            return
        except Exception as e:
            payload = json.dumps({"error": str(e), "path": path}, ensure_ascii=False)
            yield f"event: error\ndata: {payload}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/metrics", methods=["GET"])
def api_metrics():
    """
    Métricas operacionais leves para observabilidade em tempo real.
    """
    now = time.time()
    active_total = 0
    active_analyzer = 0
    active_remote = 0
    stale_candidates = 0
    for _chat_id, meta in list(ACTIVE_CHATS.items()):
        if meta.get("finished"):
            continue
        active_total += 1
        if meta.get("is_analyzer"):
            active_analyzer += 1
        else:
            active_remote += 1
        last_event_at = float(meta.get("last_event_at") or 0.0)
        if last_event_at and (now - last_event_at) > ACTIVE_CHAT_STALE_SEC:
            stale_candidates += 1

    queue_stats = {}
    try:
        if hasattr(browser_queue, "snapshot_stats"):
            queue_stats = browser_queue.snapshot_stats() or {}
    except Exception as e:
        queue_stats = {"error": str(e)}

    metrics = {
        "timestamp": int(now),
        "uptime_sec": int(max(0, now - SERVER_STARTED_AT)),
        "queue_qsize": int(browser_queue.qsize()),
        "queue": queue_stats,
        "active_chats_total": active_total,
        "active_chats_remote": active_remote,
        "active_chats_analyzer": active_analyzer,
        "active_chats_stale_candidates": stale_candidates,
        "syncs_in_progress": len(ACTIVE_SYNCS),
        "rate_limit_remaining_sec": round(_get_chat_rate_limit_remaining_seconds(), 1),
        "request_timeout_sec": int(PYTHON_CHAT_QUEUE_TIMEOUT_SEC),
    }
    return jsonify({"success": True, "metrics": metrics}), 200


@app.route("/metrics", methods=["GET"])
def prometheus_metrics():
    """Endpoint Prometheus text exposition."""
    if generate_latest is None:
        return Response("# prometheus_client not installed\n", mimetype=CONTENT_TYPE_LATEST)
    if PROM_QUEUE_SIZE is not None:
        PROM_QUEUE_SIZE.set(float(browser_queue.qsize()))
    if PROM_ACTIVE_CHATS is not None:
        active = sum(1 for _k, meta in list(ACTIVE_CHATS.items()) if not meta.get("finished"))
        PROM_ACTIVE_CHATS.set(float(active))
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

@app.route("/", methods=["GET", "POST"])
def index(): 
    # Força o mime-type correto para evitar erro de texto no navegador
    response = make_response(send_from_directory(config.DIRS["frontend"], "index.html"))
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response

@app.route('/api/history', methods=['GET'])
def get_history():
    # Valida a autenticação (via Cookie na UI ou via Bearer Token remotamente)
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    
    # Carrega e devolve o histórico em formato JSON
    history = storage.load_chats()
    return jsonify(history)

@app.route('/api/chat_lookup', methods=['POST'])
def api_chat_lookup():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    origin_url = data.get('origin_url') or data.get('url_atual') or ''
    if not origin_url:
        return jsonify({"success": False, "error": "Missing origin_url"}), 400

    chat = storage.find_chat_by_origin(origin_url)
    if not chat:
        return jsonify({"success": False, "error": "chat_not_found"}), 404

    return jsonify({"success": True, "chat": chat})


@app.route('/api/chat_delete_local', methods=['POST'])
def api_chat_delete_local():
    """Remove chat(s) do histórico local (history.json) por chat_id e/ou origin_url.
    Não exclui do ChatGPT — apenas do storage local do servidor Python."""
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    chat_id = data.get('chat_id') or ''
    origin_url = data.get('origin_url') or ''

    deleted_count = 0
    if chat_id:
        if storage.delete_chat(chat_id):
            deleted_count += 1
    if origin_url:
        deleted_count += storage.delete_chats_by_origin(origin_url)

    return jsonify({"success": True, "deleted_count": deleted_count})


@app.route("/api/menu/options", methods=["POST"])
def menu_options():
    data = request.get_json() or {}
    url = data.get("url")
    if not url: return jsonify([])
    q = queue.Queue()
    browser_queue.put({'action': 'GET_MENU', 'url': url, 'stream_queue': q})
    try:
        res = q.get(timeout=30)
        msg = json.loads(res)
        if msg.get('type') == 'menu_result' and msg.get('content', {}).get('success'):
            return jsonify(msg.get('content', {}).get('options'))
    except: pass
    return jsonify([])

@app.route("/api/menu/execute", methods=["POST"])
def menu_execute():
    data = request.get_json() or {}
    url = data.get("url")
    option = data.get("option")
    new_name = data.get("new_name")
    if not url or not option: return jsonify({"success": False})
    q = queue.Queue()
    browser_queue.put({'action': 'EXEC_MENU', 'url': url, 'option': option, 'new_name': new_name, 'stream_queue': q})
    def generate():
        while True:
            try:
                res = q.get(timeout=45)
                if res is None: break           # ← sentinela do browser.py finally
                yield res
                msg = json.loads(res)
                if msg.get('type') == 'exec_result' and msg.get('content', {}).get('success'):
                    try:
                        all_chats = storage.load_chats()
                        target_id = None
                        for cid, chat in all_chats.items():
                            if chat.get('url') == url: target_id = cid; break
                        if target_id:
                            if new_name and ("Renomear" in option or "Rename" in option):
                                storage.save_chat(target_id, new_name, url, all_chats[target_id].get('messages', []))
                            if "Excluir" in option or "Delete" in option:
                                if target_id in all_chats:
                                    del all_chats[target_id]
                                    with open(config.CHATS_FILE, "w", encoding="utf-8") as f:
                                        json.dump(all_chats, f, indent=4, ensure_ascii=False)
                    except: pass
                if msg.get('type') in ['exec_result', 'error']: break
            except Exception as e:
                yield json.dumps({"type": "error", "content": str(e)}) + "\n"; break
    return Response(stream_with_context(generate()), mimetype='application/x-ndjson')

@app.route("/api/sync", methods=["POST"])
def api_sync():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    url     = data.get("url")
    chat_id = data.get("chat_id")
    stream  = data.get("stream", False)

    # --- Identificação do solicitante (opcional) ---
    nome_membro = data.get("nome_membro_solicitante") or None
    id_membro   = data.get("id_membro_solicitante")   or None
    _quem = f', por "{nome_membro}" (id_membro: "{id_membro}")' if (nome_membro or id_membro) else ""
    _url_info  = f' | url: {url}'     if url     else ''
    _cid_info  = f' | chat_id: {chat_id}' if chat_id else ''
    print(f"\n[🔄 SYNC] Pedido de sincronização recebido{_quem}{_cid_info}{_url_info}")

    if not chat_id and not url:
        return jsonify({"success": False, "error": "Missing chat_id and url"}), 400

    sync_key = chat_id or url
    with ACTIVE_SYNCS_LOCK:
        started_at = ACTIVE_SYNCS.get(sync_key)
        if started_at and (time.time() - started_at) < 120:
            return jsonify({
                "success": False,
                "error": "sync_in_progress",
                "message": "Já existe sincronização ativa para este chat."
            }), 409
        ACTIVE_SYNCS[sync_key] = time.time()

    if chat_id in ACTIVE_CHATS and not ACTIVE_CHATS[chat_id].get('finished'):
        target_q = ACTIVE_CHATS[chat_id]['queue']
        
        if stream:
            def sync_generate():
                yield json.dumps({"type": "status", "content": "Reconectado ao processo ativo..."}) + "\n"
                if ACTIVE_CHATS[chat_id]['status']: yield json.dumps({"type": "status", "content": ACTIVE_CHATS[chat_id]['status']}) + "\n"
                if ACTIVE_CHATS[chat_id]['markdown']: yield json.dumps({"type": "markdown", "content": ACTIVE_CHATS[chat_id]['markdown']}) + "\n"
                try:
                    while not ACTIVE_CHATS[chat_id].get('finished'):
                        try:
                            raw_msg = target_q.get(timeout=2)
                            if raw_msg is None: 
                                ACTIVE_CHATS[chat_id]['finished'] = True
                                ACTIVE_CHATS[chat_id]['finished_at'] = time.time()
                                break
                            try:
                                msg_obj = json.loads(raw_msg)
                                if msg_obj.get('type') == 'status': ACTIVE_CHATS[chat_id]['status'] = msg_obj['content']
                                elif msg_obj.get('type') == 'markdown': ACTIVE_CHATS[chat_id]['markdown'] = msg_obj['content']
                                elif msg_obj.get('type') == 'finish': ACTIVE_CHATS[chat_id]['finished'] = True
                                ACTIVE_CHATS[chat_id]['finished_at'] = time.time()
                            except: pass
                            yield raw_msg + "\n"
                        except queue.Empty:
                            pass
                except GeneratorExit: pass
            return Response(sync_generate(), mimetype="application/x-ndjson")
        else:
            while not ACTIVE_CHATS[chat_id].get('finished'):
                try:
                    raw_msg = target_q.get(timeout=2)
                    if raw_msg is None: 
                        ACTIVE_CHATS[chat_id]['finished'] = True
                        ACTIVE_CHATS[chat_id]['finished_at'] = time.time()
                        break
                    try:
                        msg_obj = json.loads(raw_msg)
                        if msg_obj.get('type') == 'status': ACTIVE_CHATS[chat_id]['status'] = msg_obj['content']
                        elif msg_obj.get('type') == 'markdown': ACTIVE_CHATS[chat_id]['markdown'] = msg_obj['content']
                        elif msg_obj.get('type') == 'finish': ACTIVE_CHATS[chat_id]['finished'] = True
                    except: pass
                except queue.Empty:
                    pass

            fresh_markdown = ACTIVE_CHATS[chat_id].get('markdown', '')
            all_chats      = storage.load_chats()
            chat_data      = all_chats.get(chat_id, {})
            msgs           = list(chat_data.get('messages', []))
            if fresh_markdown and (not msgs or msgs[-1].get('content') != fresh_markdown):
                msgs.append({"role": "assistant", "content": fresh_markdown})
                storage.save_chat(chat_id, chat_data.get('title', 'Chat'), chat_data.get('url', url or ''), msgs, origin_url=chat_data.get('origin_url') or '')
            return jsonify({
                "success": True, "updated": True,
                "chat": {
                    "title":    chat_data.get('title', 'Chat'),
                    "url":      chat_data.get('url', url or ''),
                    "messages": msgs
                }
            })

    if not url: return jsonify({"success": False, "error": "Missing url"}), 400
    
    sync_q = queue.Queue()
    browser_queue.put({'action': 'SYNC', 'url': url, 'chat_id': chat_id, 'stream_queue': sync_q})
    
    try:
        while True:
            raw_msg = sync_q.get(timeout=180)
            if raw_msg is None: break
            msg = json.loads(raw_msg)
            
            if msg.get('type') == 'syncresult':  # ✅ era 'sync_result' — corrigido
                content = msg.get('content', {})
                if content.get("success"):
                    fresh_messages = content.get('messages', [])
                    fresh_title    = content.get("title", "")
                    fresh_url      = content.get("url", "") or url
                    was_updated    = storage.update_full_history(chat_id, fresh_messages, title=fresh_title, url=fresh_url)
                    storage.save_chat(chat_id, fresh_title or 'Chat', fresh_url, [], origin_url=(storage.load_chats().get(chat_id, {}) or {}).get('origin_url', ''))
                    return jsonify({
                        "success": True, "updated": was_updated,
                        "chat": {
                            "chat_id": chat_id, "url": url,
                            "title": fresh_title, "messages": fresh_messages
                        }
                    })
                else:
                    if content.get("error") == "chat_not_found":
                        all_chats = storage.load_chats()
                        if chat_id in all_chats:
                            del all_chats[chat_id]
                            with open(config.CHATS_FILE, "w", encoding="utf-8") as f:
                                json.dump(all_chats, f, indent=4, ensure_ascii=False)
                        return jsonify({"success": False, "error": "chat_not_found", "deleted": True})
                    return jsonify({"success": False, "error": content.get("error")})
            
            elif msg.get('type') == 'error': 
                return jsonify({"success": False, "error": msg.get('content')})

        return jsonify({"success": False, "error": "Sync finalizado sem resultado do navegador."})
                
    except queue.Empty:
        return jsonify({"success": False, "error": "Timeout ao sincronizar o chat."})
    except Exception as e: 
        return jsonify({"success": False, "error": str(e)})
    finally:
        with ACTIVE_SYNCS_LOCK:
            ACTIVE_SYNCS.pop(sync_key, None)


@app.route("/api/delete", methods=["POST"])
def api_delete():
    # Validação de Segurança
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    url = data.get("url")
    chat_id = data.get("chat_id")
    
    if not url or not chat_id: 
        return jsonify({"success": False, "error": "Missing url or chat_id"}), 400
        
    del_q = queue.Queue()
    
    # Envia o comando para o browser abrir o menu e clicar em "Excluir"
    # O seu browser.py já suporta "Excluir" ou "Delete" nativamente
    browser_queue.put({
        'action': 'EXEC_MENU', 
        'url': url, 
        'chat_id': chat_id, 
        'option': 'Excluir', 
        'stream_queue': del_q
    })
    
    try:
        while True:
            raw_msg = del_q.get(timeout=60)
            if raw_msg is None: break
            msg = json.loads(raw_msg)
            
            # Quando a ação do menu terminar, avaliamos o resultado
            if msg.get('type') == 'exec_result':
                content = msg.get('content', {})
                if content.get("success"):
                    # Se excluiu com sucesso na OpenAI, excluímos do nosso histórico local
                    all_chats = storage.load_chats()
                    if chat_id in all_chats:
                        del all_chats[chat_id]
                        with open(config.CHATS_FILE, "w", encoding="utf-8") as f:
                            json.dump(all_chats, f, indent=4, ensure_ascii=False)
                            
                    return jsonify({"success": True, "deleted": True})
                else:
                    return jsonify({"success": False, "error": content.get("error", "Falha ao excluir o chat no navegador.")})
            
            elif msg.get('type') == 'error': 
                return jsonify({"success": False, "error": msg.get('content')})
                
    except queue.Empty:
        return jsonify({"success": False, "error": "Timeout ao excluir o chat."})
    except Exception as e: 
        return jsonify({"success": False, "error": str(e)})

# Retorna as regras bloqueando todos os robôs
def _handle_browser_search_api(execute_fn, *, route_label, source_label):
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401

    data    = request.get_json() or {}
    queries = data.get('queries', [])  # lista de strings
    stream  = bool(data.get('stream', False))
    nome_membro = data.get("nome_membro_solicitante") or None
    id_membro   = data.get("id_membro_solicitante") or None
    _quem = f', por "{nome_membro}" (id_membro: "{id_membro}")' if (nome_membro or id_membro) else ""
    source_hint = (
        data.get("request_source")
        or request.headers.get("X-Request-Source")
        or request.headers.get("X-Client-Source")
        or ""
    )
    source_hint_norm = str(source_hint).strip().lower()
    sender_label = "analisador_prontuarios.py" if (
        'analisador_prontuarios' in source_hint_norm
        or 'analisador-prontuarios' in source_hint_norm
        or source_hint_norm == 'analyzer'
    ) else (source_hint or "usuario_remoto")

    if not queries or not isinstance(queries, list):
        return jsonify({'success': False, 'error': 'Missing queries array'}), 400

    print(f"\n[🌐 {route_label}] Pedido recebido{_quem} | queries={len(queries)}")

    if stream:
        def generate():
            all_results = []

            for idx, query_str in enumerate(queries, start=1):
                yield json.dumps({
                    'type': 'status',
                    'content': f'📚 Preparando busca {source_label} {idx}/{len(queries)}.',
                    'query': query_str,
                    'index': idx,
                    'total': len(queries),
                    'phase': f'{route_label.lower()}_prepare',
                    'source': source_label,
                }, ensure_ascii=False) + "\n"

                progress_q = queue.Queue()
                worker = threading.Thread(
                    target=lambda q_str=query_str, out_q=progress_q, snd=sender_label: out_q.put(execute_fn(q_str, stream_queue=out_q, sender_label=snd)),
                    daemon=True,
                )
                worker.start()

                result = None
                while True:
                    try:
                        item = progress_q.get(timeout=15)
                    except queue.Empty:
                        yield json.dumps({
                            'type': 'status',
                            'content': f'⏳ Busca {source_label} por "{query_str}" ainda em andamento...',
                            'query': query_str,
                            'index': idx,
                            'total': len(queries),
                            'phase': f'{route_label.lower()}_keepalive',
                            'source': source_label,
                        }, ensure_ascii=False) + "\n"
                        continue

                    if isinstance(item, dict) and ('success' in item or 'error' in item):
                        result = item
                        all_results.append(result)
                        yield json.dumps({
                            'type': 'searchresult',
                            'content': result,
                            'query': query_str,
                            'index': idx,
                            'total': len(queries),
                            'source': source_label,
                        }, ensure_ascii=False) + "\n"
                        break

                    if isinstance(item, str):
                        yield item + "\n"

                worker.join(timeout=0.1)

            yield json.dumps({
                'type': 'finish',
                'content': {
                    'success': True,
                    'results': all_results,
                }
            }, ensure_ascii=False) + "\n"

        return Response(stream_with_context(generate()), mimetype='application/x-ndjson')

    all_results = []
    for query_str in queries:
        all_results.append(execute_fn(query_str, sender_label=sender_label))

    return jsonify({'success': True, 'results': all_results})


@app.route('/api/web_search', methods=['POST'])
def api_web_search():
    return _handle_browser_search_api(
        _execute_single_web_search,
        route_label='WEB_SEARCH',
        source_label='web',
    )


@app.route('/api/uptodate_search', methods=['POST'])
def api_uptodate_search():
    return _handle_browser_search_api(
        _execute_single_uptodate_search,
        route_label='UPTODATE_SEARCH',
        source_label='uptodate',
    )


# --- ROTA DE DOCUMENTAÇÃO + TESTE DA PESQUISA WEB (PROTEGIDA) ---
# Requer login (session cookie) OU api_key na URL.
# Não está em public_routes — o before_request exige autenticação.
@app.route('/api/web_search/test', methods=['GET'])
def api_web_search_test():
    # check_auth() já valida session cookie OU api_key na query string
    # Se não autenticado, o middleware before_request já retornou 401
    # Mas por segurança, verificamos novamente aqui
    if not check_auth():
        return Response("""<!DOCTYPE html><html><head><meta charset="utf-8"><title>🔐 Acesso Negado</title>
        <style>body{font-family:system-ui;display:flex;justify-content:center;align-items:center;min-height:100vh;background:#1a1a2e;color:#e0e0e0;margin:0}
        .box{text-align:center;padding:40px;background:#16213e;border-radius:12px;max-width:500px}
        h1{color:#ff6b6b}a{color:#00d4ff}code{background:#0f3460;padding:2px 8px;border-radius:4px;font-size:13px}</style></head>
        <body><div class="box"><h1>🔐 Acesso Negado</h1>
        <p>Esta página requer autenticação.</p>
        <p><b>Opção 1:</b> Faça login em <a href="/">https://localhost:3002</a> e acesse novamente.</p>
        <p><b>Opção 2:</b> Adicione a API key na URL:<br><code>/api/web_search/test?api_key=SUA_CHAVE</code></p>
        </div></body></html>""", mimetype='text/html', status=401)

    query   = request.args.get('q', '').strip()
    api_key = request.args.get('api_key', '')

    # Se não tem query, retorna a página de documentação + teste
    if not query:
        return Response(f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>🔍 Pesquisa Web — ChatGPT Simulator</title>
<style>
  *{{box-sizing:border-box}}
  body{{font-family:system-ui,-apple-system,sans-serif;max-width:960px;margin:0 auto;padding:20px;background:#1a1a2e;color:#e0e0e0}}
  h1{{color:#00d4ff;margin-bottom:5px}}
  .subtitle{{color:#888;font-size:14px;margin-bottom:25px}}
  .tabs{{display:flex;gap:0;margin-bottom:0;border-bottom:2px solid #0f3460}}
  .tab{{padding:12px 24px;cursor:pointer;background:#16213e;color:#888;border:none;font-size:14px;font-weight:500;border-radius:8px 8px 0 0;transition:all .2s}}
  .tab:hover{{color:#ccc}}
  .tab.active{{background:#0f3460;color:#00d4ff;border-bottom:2px solid #00d4ff;margin-bottom:-2px}}
  .panel{{display:none;background:#0f3460;padding:24px;border-radius:0 0 8px 8px}}
  .panel.active{{display:block}}
  input[type=text]{{width:100%;padding:12px;font-size:16px;border:1px solid #333;border-radius:8px;background:#16213e;color:#fff}}
  button{{padding:12px 24px;font-size:16px;background:#00d4ff;color:#000;border:none;border-radius:8px;cursor:pointer;margin-top:10px;font-weight:600}}
  button:hover{{background:#00b8d4}}
  button:disabled{{background:#555;cursor:wait}}
  #resultado{{margin-top:20px;max-height:500px;overflow-y:auto}}
  .item{{margin:10px 0;padding:12px;background:#16213e;border-radius:6px;border-left:3px solid #00d4ff}}
  .item a{{color:#00d4ff;text-decoration:none}}
  .item a:hover{{text-decoration:underline}}
  .snippet{{color:#aaa;font-size:13px;margin-top:6px}}
  .status{{color:#ffd700;font-style:italic;padding:10px}}
  code{{background:#16213e;padding:2px 6px;border-radius:4px;font-size:13px;color:#00d4ff}}
  pre{{background:#16213e;padding:16px;border-radius:8px;overflow-x:auto;font-size:13px;line-height:1.5;border:1px solid #333}}
  pre code{{background:none;padding:0;color:#e0e0e0}}
  .key{{color:#ff9800}}
  .str{{color:#4caf50}}
  .comment{{color:#666}}
  h2{{color:#00d4ff;margin-top:30px;font-size:18px}}
  h3{{color:#ccc;margin-top:20px;font-size:15px}}
  p{{line-height:1.7}}
  .warn{{background:rgba(255,152,0,0.1);border-left:3px solid #ff9800;padding:12px;border-radius:4px;margin:15px 0}}
  .ok{{background:rgba(76,175,80,0.1);border-left:3px solid #4caf50;padding:12px;border-radius:4px;margin:15px 0}}
  table{{width:100%;border-collapse:collapse;margin:15px 0}}
  th,td{{text-align:left;padding:8px 12px;border-bottom:1px solid #333}}
  th{{color:#00d4ff;font-size:13px}}
  td{{font-size:13px}}
  td code{{font-size:12px}}
</style></head><body>
<h1>🔍 Pesquisa Web — ChatGPT Simulator</h1>
<div class="subtitle">Documentação da API de busca no Google via Playwright + Teste interativo</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab('docs')">📖 Documentação</div>
  <div class="tab" onclick="switchTab('test')">🧪 Testar Busca</div>
  <div class="tab" onclick="switchTab('integration')">🔌 Integração</div>
  <div class="tab" onclick="switchTab('llm')">🤖 Modo LLM</div>
</div>

<!-- ═══ ABA 1: DOCUMENTAÇÃO ═══ -->
<div class="panel active" id="panel-docs">
<h2>Como funciona</h2>
<p>O sistema abre o Google no navegador Chromium via Playwright, digita a query com timing humano realista,
aguarda os resultados carregarem e extrai título, URL e snippet de cada resultado orgânico.</p>

<div class="ok">✅ Resultados reais do Google — não usa APIs pagas, nem scraping headless detectável.</div>

<h2>Endpoint</h2>
<table>
  <tr><th>Método</th><th>URL</th><th>Autenticação</th></tr>
  <tr><td><code>POST</code></td><td><code>/api/web_search</code></td><td>Bearer Token ou api_key</td></tr>
  <tr><td><code>GET</code></td><td><code>/api/web_search/test?q=...</code></td><td>Session cookie ou api_key</td></tr>
</table>

<h2>Request (POST)</h2>
<pre><code>POST /api/web_search
Content-Type: application/json
Authorization: Bearer <span class="key">SUA_API_KEY</span>

{{
  <span class="str">"queries"</span>: [
    <span class="str">"metilfenidato efeitos adversos crianças"</span>,
    <span class="str">"risperidone autism pediatric guidelines site:pubmed.ncbi.nlm.nih.gov"</span>
  ]
}}</code></pre>

<h2>Response</h2>
<pre><code>{{
  <span class="str">"success"</span>: true,
  <span class="str">"results"</span>: [
    {{
      <span class="str">"success"</span>: true,
      <span class="str">"query"</span>: <span class="str">"metilfenidato efeitos adversos crianças"</span>,
      <span class="str">"count"</span>: 10,
      <span class="str">"results"</span>: [
        {{
          <span class="str">"position"</span>: 1,
          <span class="str">"title"</span>: <span class="str">"Methylphenidate for children and adolescents..."</span>,
          <span class="str">"url"</span>: <span class="str">"https://pubmed.ncbi.nlm.nih.gov/36971690/"</span>,
          <span class="str">"snippet"</span>: <span class="str">"Our updated meta-analyses suggest that..."</span>,
          <span class="str">"type"</span>: <span class="str">"organic"</span>
        }}
      ]
    }}
  ]
}}</code></pre>

<h2>Tipos de resultado</h2>
<table>
  <tr><th>type</th><th>Descrição</th></tr>
  <tr><td><code>organic</code></td><td>Resultado orgânico do Google (título + URL + snippet)</td></tr>
  <tr><td><code>featured_snippet</code></td><td>Resposta em destaque (caixa de resposta direta do Google)</td></tr>
  <tr><td><code>people_also_ask</code></td><td>Perguntas relacionadas ("As pessoas também perguntam")</td></tr>
</table>

<h2>Limites</h2>
<table>
  <tr><th>Parâmetro</th><th>Valor</th></tr>
  <tr><td>Máx. queries por request</td><td>5 (recomendado: 1-3)</td></tr>
  <tr><td>Máx. resultados por query</td><td>10</td></tr>
  <tr><td>Timeout por query</td><td>~60s (o browser precisa digitar)</td></tr>
  <tr><td>Concorrência</td><td>1 aba por query (sequencial)</td></tr>
</table>

<div class="warn">⚠️ Cada busca abre uma aba real no Chromium. Evite buscas desnecessárias para não sobrecarregar o browser.</div>
</div>

<!-- ═══ ABA 2: TESTAR BUSCA ═══ -->
<div class="panel" id="panel-test">
<h2>Teste interativo</h2>
<p>Digite uma busca e veja os resultados em tempo real. O Chromium vai abrir uma aba do Google, digitar e scrapear.</p>
<input type="text" id="q" placeholder="Ex: metilfenidato efeitos adversos crianças site:pubmed.ncbi.nlm.nih.gov" autofocus>
<button id="btn-buscar" onclick="buscar()">🔎 Buscar no Google</button>
<div id="resultado"></div>
</div>

<!-- ═══ ABA 3: INTEGRAÇÃO ═══ -->
<div class="panel" id="panel-integration">
<h2>Exemplo Python</h2>
<pre><code><span class="comment"># Busca simples</span>
import requests

resp = requests.post(
    <span class="str">"http://127.0.0.1:3003/api/web_search"</span>,
    json={{<span class="str">"queries"</span>: [<span class="str">"TDAH tratamento crianças guidelines"</span>]}},
    headers={{
        <span class="str">"Content-Type"</span>: <span class="str">"application/json"</span>,
        <span class="str">"Authorization"</span>: <span class="str">"Bearer <span class="key">SUA_API_KEY</span>"</span>
    }},
    timeout=90
)
data = resp.json()
for res in data[<span class="str">"results"</span>]:
    for item in res.get(<span class="str">"results"</span>, []):
        print(f"{{item[<span class="str">'title'</span>]}} — {{item[<span class="str">'url'</span>]}}")</code></pre>

<h2>Exemplo JavaScript (fetch)</h2>
<pre><code><span class="comment">// Busca via fetch (frontend)</span>
const resp = await fetch(<span class="str">'/api/web_search'</span>, {{
    method: <span class="str">'POST'</span>,
    headers: {{
        <span class="str">'Content-Type'</span>: <span class="str">'application/json'</span>,
        <span class="str">'Authorization'</span>: <span class="str">'Bearer SUA_API_KEY'</span>
    }},
    body: JSON.stringify({{
        queries: [<span class="str">'risperidona crianças autismo posologia'</span>]
    }})
}});
const data = await resp.json();
console.log(data.results);</code></pre>

<h2>Exemplo cURL</h2>
<pre><code>curl -X POST http://127.0.0.1:3003/api/web_search \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer <span class="key">SUA_API_KEY</span>" \\
  -d '{{"queries": ["metilfenidato site:pubmed.ncbi.nlm.nih.gov"]}}'</code></pre>
</div>

<!-- ═══ ABA 4: MODO LLM ═══ -->
<div class="panel" id="panel-llm">
<h2>Como a LLM solicita pesquisa web</h2>
<p>Quando a LLM precisa de informações externas (bulas, guidelines, artigos), ela retorna
um JSON especial em vez de texto. O sistema detecta automaticamente e executa a busca.</p>

<h3>Formato que a LLM retorna:</h3>
<pre><code>{{
  <span class="str">"search_queries"</span>: [
    {{
      <span class="str">"query"</span>: <span class="str">"methylphenidate children adverse effects systematic review site:pubmed.ncbi.nlm.nih.gov"</span>,
      <span class="str">"reason"</span>: <span class="str">"buscar revisão sistemática sobre efeitos adversos do metilfenidato em crianças"</span>
    }}
  ]
}}</code></pre>

<h3>Fluxo completo:</h3>
<div class="ok">
1️⃣ Usuário pergunta → LLM decide que precisa buscar na web<br>
2️⃣ LLM retorna JSON com <code>search_queries</code><br>
3️⃣ Frontend detecta o JSON automaticamente<br>
4️⃣ Frontend chama <code>POST /api/web_search</code><br>
5️⃣ Browser abre Google, digita, scrapa resultados<br>
6️⃣ Resultados são formatados e enviados de volta à LLM<br>
7️⃣ LLM responde ao usuário usando os resultados reais
</div>

<h3>Boas práticas para queries médicas:</h3>
<table>
  <tr><th>Objetivo</th><th>Exemplo de query</th></tr>
  <tr><td>Artigos PubMed</td><td><code>methylphenidate ADHD children site:pubmed.ncbi.nlm.nih.gov</code></td></tr>
  <tr><td>Guidelines pediátricas</td><td><code>ADHD pediatric treatment guidelines AAP 2024</code></td></tr>
  <tr><td>Bula ANVISA</td><td><code>clonidina bula profissional anvisa posologia pediátrica</code></td></tr>
  <tr><td>Interações</td><td><code>risperidone valproate interaction children</code></td></tr>
  <tr><td>Revisão sistemática</td><td><code>melatonin autism sleep systematic review</code></td></tr>
</table>

<h3>Regras importantes:</h3>
<div class="warn">
• <b>SQL e pesquisa web NÃO se misturam</b> — nunca <code>sql_queries</code> e <code>search_queries</code> juntos<br>
• Quando retornar <code>search_queries</code>, não escrever NENHUM texto fora do JSON<br>
• Máximo recomendado: 3 queries por solicitação<br>
• Após receber os resultados, a LLM deve citar as fontes ao responder
</div>
</div>

<script>
function switchTab(id) {{
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('panel-' + id).classList.add('active');
}}

async function buscar() {{
  const q = document.getElementById('q').value.trim();
  if (!q) return;
  const r = document.getElementById('resultado');
  const btn = document.getElementById('btn-buscar');
  btn.disabled = true;
  btn.textContent = '⏳ Buscando...';
  r.innerHTML = '<div class="status">⏳ Buscando no Google via Playwright... (o browser vai abrir uma aba, digitar e scrapear)</div>';
  try {{
    const resp = await fetch('/api/web_search', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{queries: [q], api_key: '{api_key or config.API_KEY}'}})
    }});
    const data = await resp.json();
    if (!data.success) {{ r.innerHTML = '<div class="status">❌ ' + JSON.stringify(data) + '</div>'; return; }}
    let html = '';
    for (const res of data.results || []) {{
      if (!res.success && res.error) {{ html += '<div class="status">❌ ' + res.error + '</div>'; continue; }}
      html += '<div class="status">✅ Query: "' + res.query + '" — ' + (res.count || 0) + ' resultado(s)</div>';
      for (const item of res.results || []) {{
        if (item.type === 'people_also_ask') {{
          html += '<div class="item">❓ <b>Perguntas relacionadas</b><div class="snippet">' + item.snippet + '</div></div>';
        }} else if (item.type === 'featured_snippet') {{
          html += '<div class="item">★ <b>Destaque</b><div class="snippet">' + item.snippet + '</div></div>';
        }} else {{
          html += '<div class="item">[' + item.position + '] <a href="' + item.url + '" target="_blank">' + item.title + '</a>';
          if (item.snippet) html += '<div class="snippet">' + item.snippet + '</div>';
          html += '</div>';
        }}
      }}
    }}
    r.innerHTML = html || '<div class="status">Nenhum resultado.</div>';
  }} catch(e) {{ r.innerHTML = '<div class="status">❌ Erro: ' + e.message + '</div>'; }}
  finally {{ btn.disabled = false; btn.textContent = '🔎 Buscar no Google'; }}
}}
document.getElementById('q')?.addEventListener('keydown', e => {{ if (e.key === 'Enter') buscar(); }});
</script></body></html>""", mimetype='text/html')

    # Se recebeu ?q=..., executa a busca diretamente (retorna JSON)
    q = queue.Queue()
    browser_queue.put({
        'action':       'SEARCH',
        'query':        query,
        'stream_queue': q
    })

    try:
        while True:
            raw_msg = q.get(timeout=90)
            if raw_msg is None:
                break
            msg = json.loads(raw_msg)
            if msg.get('type') == 'searchresult':
                return jsonify(msg.get('content', {}))
            elif msg.get('type') == 'error':
                return jsonify({'success': False, 'query': query, 'error': msg.get('content')}), 500
    except queue.Empty:
        return jsonify({'success': False, 'query': query, 'error': 'Timeout (90s)'}), 504

    return jsonify({'success': False, 'query': query, 'error': 'Sem resposta do browser'})


@app.route('/robots.txt')
def robots_txt():
    # O mimetype "text/plain" garante que o navegador leia como texto puro
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")

# --- ROTA: ENVIAR RESPOSTA MANUAL AO PACIENTE VIA WhatsApp ---
# Recebe mensagem do profissional/secretária e repassa ao
# acompanhamento_whatsapp.py (porta 3011) para enviar via WhatsApp Web.
@app.route("/api/send_manual_whatsapp_reply", methods=["POST"])
def send_manual_whatsapp_reply():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    phone   = (data.get("phone") or "").strip()
    message = (data.get("message") or "").strip()
    chat_id = data.get("chat_id")
    id_paciente      = data.get("id_paciente")
    id_atendimento   = data.get("id_atendimento")
    id_membro        = data.get("id_membro_solicitante")
    nome_membro      = data.get("nome_membro_solicitante")

    if not phone or not message:
        return jsonify({"success": False, "error": "phone e message são obrigatórios"}), 400

    _quem = f' por "{nome_membro}" (id={id_membro})' if (nome_membro or id_membro) else ""
    print(f"\n[📨 MANUAL REPLY] Resposta manual{_quem} para phone={phone} | chat_id={chat_id}")

    # Repassa ao acompanhamento_whatsapp.py (porta 3011)
    import requests as http_requests
    pywa_url = os.getenv("PYWA_URL", "http://127.0.0.1:3011")
    try:
        payload = {
            "phone": phone,
            "message": message,
            "chat_id": chat_id,
            "id_paciente": id_paciente,
            "id_atendimento": id_atendimento,
            "id_membro_solicitante": id_membro,
            "nome_membro_solicitante": nome_membro,
        }
        resp = http_requests.post(
            f"{pywa_url}/send-manual-reply",
            json=payload,
            timeout=30,
        )
        result = resp.json()
        if resp.ok and result.get("ok"):
            print(f"[📨 MANUAL REPLY] Enviado com sucesso para {phone}")
            return jsonify({"success": True, "whatsapp_response": result})
        else:
            err = result.get("error") or f"HTTP {resp.status_code}"
            print(f"[📨 MANUAL REPLY] Falha: {err}")
            return jsonify({"success": False, "error": err}), 502
    except Exception as e:
        print(f"[📨 MANUAL REPLY] Erro ao contactar acompanhamento_whatsapp: {e}")
        return jsonify({"success": False, "error": str(e)}), 502


# --- ROTA DE CHAT (ATUALIZADA) ---
@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}

    # --- 1. CAPTURA DO CHAT_ID ---
    chat_id = data.get("chat_id")
    if not chat_id:
        chat_id = request.args.get("chat_id")

    # --- Identificação do solicitante (opcional) ---
    nome_membro = data.get("nome_membro_solicitante") or None
    id_membro   = data.get("id_membro_solicitante")   or None
    _quem = f', por "{nome_membro}" (id_membro: "{id_membro}")' if (nome_membro or id_membro) else ""
    source_hint = (
        data.get("request_source")
        or request.headers.get("X-Request-Source")
        or request.headers.get("X-Client-Source")
        or ""
    )
    source_hint_norm = str(source_hint).strip().lower()
    is_analyzer = (
        'analisador_prontuarios' in source_hint_norm
        or 'analisador-prontuarios' in source_hint_norm
        or source_hint_norm == 'analyzer'
    )
    sender_label = "analisador_prontuarios.py" if is_analyzer else (source_hint or "usuario_remoto")
    _origem = " [origem: analisador_prontuarios.py]" if is_analyzer else (f" [origem: {source_hint}]" if source_hint else "")

    if chat_id:
        print(f"\n[📡 SERVIDOR] Requisição remota recebida{_quem}{_origem}! Continuando Chat ID: {chat_id}")
    else:
        chat_id = str(uuid.uuid4())
        print(f"\n[📡 SERVIDOR] Novo pedido remoto{_quem}{_origem}. Gerando Chat ID: {chat_id}")

    # Tenta pegar a mensagem única (string)
    message = data.get("message", "")

    # Se a mensagem vier vazia, mas houver um array 'messages', concatena tudo
    if not message and "messages" in data and isinstance(data["messages"], list):
        combined_text = ""
        for msg in data["messages"]:
            role    = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                combined_text = content + "\n\n" + combined_text
            elif role == "user":
                combined_text += f"{content}\n"
        message = combined_text.strip()
        print(f"[📡 SERVIDOR] Array de mensagens concatenado. Tamanho do texto: {len(message)} caracteres.")

    stream      = data.get("stream", False)
    attachments = data.get("attachments", [])
    origin_url  = data.get("origin_url") or data.get("url_atual") or request.headers.get("X-Origin-URL") or ""
    is_python_source = _is_python_chat_request(source_hint_norm)
    is_codex_request = _is_codex_chat_request(source_hint_norm, data.get("url"), origin_url)
    use_python_queue = bool(is_python_source and not is_codex_request)

    # Todos os pedidos oriundos de scripts Python devem usar encapsulamento de
    # texto colado para evitar typing realista (lento) no browser.py.
    if is_python_source and isinstance(message, str) and message.strip():
        if "[INICIO_TEXTO_COLADO]" not in message or "[FIM_TEXTO_COLADO]" not in message:
            message = f"[INICIO_TEXTO_COLADO]{message}[FIM_TEXTO_COLADO]"

    # --- 2. PROCESSAMENTO DE ANEXOS ---
    saved_paths = []
    if attachments:
        for att in attachments:
            try:
                name  = att.get("name", "file.txt")
                b64   = att.get("data", "")
                if "," in b64: b64 = b64.split(",")[1]
                bdata = base64.b64decode(b64)
                path  = os.path.join(config.DIRS["uploads"], f"{int(time.time())}_{name}")
                with open(path, "wb") as f:
                    f.write(bdata)
                saved_paths.append(path)
            except Exception as e:
                print(f"Erro no anexo: {e}")

    # --- 3. CAPTURA DA URL ---
    url = data.get("url")
    if not url or url == "None":
        all_chats = storage.load_chats()
        url = all_chats.get(chat_id, {}).get("url")

    if url and url != "None":
        print(f"[📡 SERVIDOR] URL detectada. Retomando conversa em: {url}")
    else:
        print("[📡 SERVIDOR] Nenhuma URL detectada. Iniciando um novo chat do zero.")

    # Persiste imediatamente o pedido remoto para sobreviver ao fechamento precoce da aba.
    chat_snapshot = storage.load_chats().get(chat_id, {})
    storage.save_chat(
        chat_id,
        chat_snapshot.get('title') or 'Novo Chat',
        url or chat_snapshot.get('url', ''),
        [],
        origin_url=origin_url or chat_snapshot.get('origin_url', '')
    )
    if message:
        storage.append_message(chat_id, "user", message)

    # --- 4. PREPARAÇÃO DA FILA ---
    stream_q = queue.Queue()

    ACTIVE_CHATS[chat_id] = {
        'queue':       stream_q,
        'status':      'Iniciando...',
        'markdown':    '',
        'finished':    False,
        'finished_at': None,
        'last_event_at': time.time(),
        'is_analyzer': bool(is_analyzer)
    }

    chat_task_payload = {
        'action':           'CHAT',
        'url':              url,
        'chat_id':          chat_id,
        'message':          message,
        'is_analyzer':      bool(is_analyzer),
        'sender':           sender_label,
        'request_source':   source_hint or sender_label,
        'attachment_paths': saved_paths,
        'stream_queue':     stream_q,
        'sender':           sender_label,
        # Codex: repositório/ambiente a ser selecionado no dropdown de
        # https://chatgpt.com/codex/cloud antes do paste da mensagem.
        # Opcional — quando ausente, browser.py usa a seleção atual do UI.
        'codex_repo':       (data.get('codex_repo') or '').strip() or None,
        # Perfil Chromium alvo (ex.: "default", "analisador"). Opcional.
        # browser.py resolve contra config.CHROMIUM_PROFILES; valor ausente
        # ou chave inválida → fallback para "default" (perfil compartilhado).
        'browser_profile':  (data.get('browser_profile') or '').strip() or None,
    }

    def _dispatch_chat_task():
        queue_key = f"{chat_id}:{time.time_ns()}"
        slot_acquired = False
        try:
            if use_python_queue:
                _acquire_python_chat_slot(queue_key, stream_q if stream else None, sender_label)
                slot_acquired = True
            _wait_remote_user_priority_if_needed(is_analyzer, stream_q if stream else None)
            _wait_chat_rate_limit_if_needed(stream_q if stream else None)
            browser_queue.put(chat_task_payload)
        except TimeoutError as queue_timeout:
            stream_q.put(json.dumps({
                "type": "error",
                "content": f"Timeout aguardando fila interna do servidor: {queue_timeout}"
            }, ensure_ascii=False))
            stream_q.put(None)
        except Exception as dispatch_err:
            stream_q.put(json.dumps({
                "type": "error",
                "content": f"Falha ao enfileirar tarefa no browser: {dispatch_err}"
            }, ensure_ascii=False))
            stream_q.put(None)
        finally:
            if slot_acquired:
                _release_python_chat_slot(queue_key)

    if stream:
        threading.Thread(target=_dispatch_chat_task, daemon=True).start()
    else:
        _dispatch_chat_task()

    # --- 5. RESPOSTA STREAMING OU BLOCO ---
    if stream:
        def _drain_stream_queue_after_disconnect():
            """
            Quando o cliente de stream desconecta, continua consumindo a fila em
            background para que ACTIVE_CHATS reflita término real da tarefa e não
            bloqueie filas prioritárias até o timeout de stale.
            """
            try:
                while True:
                    try:
                        raw_msg = stream_q.get(timeout=900)
                    except queue.Empty:
                        break

                    if raw_msg is None:
                        ACTIVE_CHATS[chat_id]['finished'] = True
                        ACTIVE_CHATS[chat_id]['finished_at'] = time.time()
                        ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                        break

                    try:
                        msg_obj = json.loads(raw_msg)
                    except Exception:
                        ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                        continue

                    t = msg_obj.get('type')
                    if t == 'status':
                        ACTIVE_CHATS[chat_id]['status'] = msg_obj.get('content', '')
                        ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                    elif t == 'markdown':
                        ACTIVE_CHATS[chat_id]['markdown'] = msg_obj.get('content', '')
                        ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                    elif t == 'finish':
                        fin = msg_obj.get('content', {}) or {}
                        ACTIVE_CHATS[chat_id]['finished'] = True
                        ACTIVE_CHATS[chat_id]['finished_at'] = time.time()
                        ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                        try:
                            storage.append_message(chat_id, "user", message)
                            storage.append_message(chat_id, "assistant", ACTIVE_CHATS[chat_id]['markdown'])
                            storage.save_chat(chat_id, fin.get('title', ''), fin.get('url', '') or url or '', [], origin_url=origin_url)
                        except Exception as e:
                            log(f"[WARN] Falha ao persistir finish (drain pós-disconnect): {e}")
                        break
                    elif t == 'error':
                        ACTIVE_CHATS[chat_id]['finished'] = True
                        ACTIVE_CHATS[chat_id]['finished_at'] = time.time()
                        ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                        break
                    else:
                        # log/chat_meta/etc.
                        ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
            except Exception as e:
                log(f"[WARN] Falha no dreno de stream pós-disconnect para chat {chat_id}: {e}")

        def generate():
            yield json.dumps({"type": "chat_id", "content": chat_id}) + "\n"
            if url and url != "None":
                yield json.dumps({"type": "chat_meta", "content": {"chat_id": chat_id, "url": url}}) + "\n"

            try:
                while True:
                    try:
                        raw_msg = stream_q.get(timeout=600)
                    except queue.Empty:
                        # Browser não respondeu em 600s — avisa o cliente e encerra
                        ACTIVE_CHATS[chat_id]['finished']    = True
                        ACTIVE_CHATS[chat_id]['finished_at'] = time.time()
                        yield json.dumps({"type": "error", "content": "Timeout: browser não respondeu em 600s."}) + "\n"
                        break

                    if raw_msg is None:
                        ACTIVE_CHATS[chat_id]['finished']    = True
                        ACTIVE_CHATS[chat_id]['finished_at'] = time.time()
                        ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                        break

                    try:
                        msg_obj = json.loads(raw_msg)
                        t = msg_obj.get('type')
                        if t in ('log', 'status', 'error'):
                            content_text = msg_obj.get('content')
                            if isinstance(content_text, str) and content_text and not content_text.startswith("Remetente: "):
                                msg_obj['content'] = f"Remetente: {sender_label} | {content_text}"
                                raw_msg = json.dumps(msg_obj, ensure_ascii=False)
                        if t == 'log':
                            ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                        if t == 'status':
                            ACTIVE_CHATS[chat_id]['status'] = msg_obj['content']
                            ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                        elif t == 'markdown':
                            ACTIVE_CHATS[chat_id]['markdown'] = msg_obj['content']
                            ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                        elif t == 'chat_meta':
                            fin = msg_obj.get('content', {}) or {}
                            early_url = fin.get('url') or ''
                            early_chat_id = fin.get('chat_id') or chat_id
                            if early_url:
                                try:
                                    snapshot = storage.load_chats().get(early_chat_id, {})
                                    storage.save_chat(
                                        early_chat_id,
                                        snapshot.get('title') or 'Novo Chat',
                                        early_url,
                                        [],
                                        origin_url=origin_url or snapshot.get('origin_url', '')
                                    )
                                except Exception as e:
                                    log(f"[WARN] Falha ao persistir chat_meta antecipado: {e}")
                        elif t == 'finish':
                            ACTIVE_CHATS[chat_id]['finished']    = True
                            ACTIVE_CHATS[chat_id]['finished_at'] = time.time()
                            ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                            # [FIX Bug 1] Persiste no storage ao terminar (stream nunca escrevia)
                            try:
                                fin = msg_obj.get('content', {})
                                storage.append_message(chat_id, "user", message)
                                storage.append_message(chat_id, "assistant", ACTIVE_CHATS[chat_id]['markdown'])
                                storage.save_chat(chat_id, fin.get('title', ''), fin.get('url', '') or url or '', [], origin_url=origin_url)
                            except Exception as e:
                                log(f"[WARN] Falha ao persistir stream finish: {e}")
                        elif t == 'error':
                            is_rate_limited, err_msg, retry_after = _extract_rate_limit_details(msg_obj.get('content'))
                            if is_rate_limited:
                                _register_chat_rate_limit(retry_after, reason=err_msg)
                            ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                    except Exception:
                        pass

                    yield raw_msg + "\n"
                    if ACTIVE_CHATS[chat_id]['finished']:
                        break

            except GeneratorExit:
                # Conexão de stream foi interrompida pelo lado cliente/proxy.
                # NÃO marca como finalizado aqui: a tarefa no browser pode
                # continuar em progresso e será recuperável via /api/sync.
                ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                log(
                    f"[ACTIVE_CHATS] stream interrompido (cliente/proxy) para chat {chat_id}; "
                    "mantendo tarefa ativa para retomada."
                )
                if not ACTIVE_CHATS.get(chat_id, {}).get('finished'):
                    threading.Thread(target=_drain_stream_queue_after_disconnect, daemon=True).start()

        return Response(generate(), mimetype="application/x-ndjson")

    else:
        # --- MODO BLOCO ---
        final_html  = ""
        final_url   = url
        final_title = "Chat"

        try:
            while True:
                try:
                    raw_msg = stream_q.get(timeout=600)
                except queue.Empty:
                    # [FIX SV1] queue.Empty era exceção não tratada → HTTP 500
                    ACTIVE_CHATS[chat_id]['finished']    = True
                    ACTIVE_CHATS[chat_id]['finished_at'] = time.time()
                    return jsonify({
                        "success": False,
                        "error":   "Timeout: browser não respondeu em 600s.",
                        "chat_id": chat_id
                    })

                if raw_msg is None:
                    ACTIVE_CHATS[chat_id]['finished'] = True
                    ACTIVE_CHATS[chat_id]['finished_at'] = time.time()
                    ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                    break

                try:
                    msg = json.loads(raw_msg)
                except Exception:
                    continue

                t = msg.get('type')
                if t == 'status':
                    ACTIVE_CHATS[chat_id]['status'] = msg['content']
                    ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                elif t == 'markdown':
                    final_html = msg['content']
                    ACTIVE_CHATS[chat_id]['markdown'] = msg['content']
                    ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                elif t == 'finish':
                    final_url   = msg['content'].get('url',   final_url)
                    final_title = msg['content'].get('title', final_title)
                    ACTIVE_CHATS[chat_id]['finished']    = True
                    ACTIVE_CHATS[chat_id]['finished_at'] = time.time()
                    ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                elif t == 'error':
                    is_rate_limited, err_msg, retry_after = _extract_rate_limit_details(msg.get('content'))
                    if is_rate_limited:
                        _register_chat_rate_limit(retry_after, reason=err_msg)
                    ACTIVE_CHATS[chat_id]['finished']    = True
                    ACTIVE_CHATS[chat_id]['finished_at'] = time.time()
                    ACTIVE_CHATS[chat_id]['last_event_at'] = time.time()
                    return jsonify({"success": False, "error": msg['content'], "chat_id": chat_id})

        except Exception as e:
            log(f"[ERRO] Modo block inesperado: {e}")
            return jsonify({"success": False, "error": str(e), "chat_id": chat_id})

        # Persiste no storage
        storage.append_message(chat_id, "user",      message)
        storage.append_message(chat_id, "assistant", final_html)
        storage.save_chat(chat_id, final_title, final_url, [], origin_url=origin_url)  # [FIX S3] passa [] — save_chat carrega e mescla internamente

        return jsonify({
            "success": True,
            "chat_id": chat_id,
            "html":    final_html,
            "url":     final_url,
            "title":   final_title
        })
