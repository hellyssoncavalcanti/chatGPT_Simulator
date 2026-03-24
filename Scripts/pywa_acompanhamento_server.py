#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Servidor de acompanhamento WhatsApp SEM Meta Cloud API.

Funciona de forma isolada via automação do WhatsApp Web (https://web.whatsapp.com/)
usando Playwright (mesma linha do browser.py do projeto).

Fluxo:
1) Busca mensagens de acompanhamento no PHP (execute_sql).
2) Envia mensagens no WhatsApp Web para os pacientes.
3) Monitora respostas no WhatsApp Web para os pacientes mapeados.
4) Encaminha a resposta para a URL específica do ChatGPT daquele paciente
   no endpoint local do Simulator (/v1/chat/completions).
5) Envia a resposta gerada de volta ao paciente no WhatsApp.
"""

import hashlib
import json
import logging
import os
import queue
import random
import re
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
from flask import Flask, jsonify
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
PHP_URL = os.getenv(
    "PYWA_PHP_URL",
    "https://conexaovida.org/scripts/js/chatgpt_integracao_criado_pelo_gemini.js.php",
)
PHP_API_KEY = os.getenv("PYWA_PHP_API_KEY", "CVAPI_2b9c80c2abf94a76baf8b3e68d89cb7e")

SIMULATOR_URL = os.getenv("PYWA_SIMULATOR_URL", "http://127.0.0.1:3003/v1/chat/completions")
SIMULATOR_API_KEY = os.getenv("PYWA_SIMULATOR_API_KEY", "CVAPI_2b9c80c2abf94a76baf8b3e68d89cb7e")

WHATSAPP_WEB_URL = os.getenv("WHATSAPP_WEB_URL", "https://web.whatsapp.com/")
TEST_DESTINATION_PHONE_RAW = os.getenv("PYWA_TEST_DESTINATION_PHONE", "81981487277")
POLL_INTERVAL_SEC = int(os.getenv("PYWA_POLL_INTERVAL_SEC", "120"))
REPLY_POLL_INTERVAL_SEC = int(os.getenv("PYWA_REPLY_POLL_INTERVAL_SEC", "20"))
REQUEST_TIMEOUT_SEC = int(os.getenv("PYWA_REQUEST_TIMEOUT_SEC", "45"))

HOST = os.getenv("PYWA_HOST", "0.0.0.0")
PORT = int(os.getenv("PYWA_PORT", "3011"))

BASE_DIR = Path(__file__).resolve().parents[1]
DB_DIR = BASE_DIR / "db"
DB_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DB_DIR / "pywa_followup_state.json"
WHATSAPP_PROFILE_DIR = BASE_DIR / "chrome_profile_whatsapp"
WHATSAPP_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_FETCH_SQL = """
SELECT
  caa.id AS id_analise,
  caa.id_atendimento,
  caa.id_paciente,
  COALESCE(m.telefone1,m.telefone2,m.telefone1pais,m.telefone2pais) AS telefone,
  m.nome AS nome_paciente,
  caa.mensagens_acompanhamento,
  caa.chat_url,
  cc.url_chatgpt
FROM chatgpt_atendimentos_analise caa
JOIN membros m ON m.id = caa.id_paciente
LEFT JOIN chatgpt_chats cc ON cc.id_atendimento = caa.id_atendimento
WHERE caa.mensagens_acompanhamento IS NOT NULL
  AND caa.mensagens_acompanhamento <> ''
ORDER BY caa.id_atendimento DESC
LIMIT 100
""".strip()
FETCH_SQL = os.getenv("PYWA_FETCH_SQL", DEFAULT_FETCH_SQL)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("whatsapp_web_acompanhamento")

app = Flask(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.state = self._load()

    def _base(self) -> Dict[str, Any]:
        return {
            "sent_questions": {},
            "phone_context": {},
            "forwarded_messages": {},
            "last_seen_inbound": {},
            "updated_at": utc_now_iso(),
        }

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return self._base()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for k, v in self._base().items():
                data.setdefault(k, v if not isinstance(v, dict) else {})
            return data
        except Exception:
            return self._base()

    def save(self) -> None:
        with self.lock:
            self.state["updated_at"] = utc_now_iso()
            self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def is_sent(self, key: str) -> bool:
        with self.lock:
            return key in self.state["sent_questions"]

    def mark_sent(self, key: str, payload: Dict[str, Any]) -> None:
        with self.lock:
            self.state["sent_questions"][key] = payload
        self.save()

    def set_phone_context(self, phone: str, ctx: Dict[str, Any]) -> None:
        with self.lock:
            self.state["phone_context"][phone] = ctx
        self.save()

    def all_phone_contexts(self) -> Dict[str, Dict[str, Any]]:
        with self.lock:
            return dict(self.state["phone_context"])

    def get_phone_context_field(self, phone: str, field: str) -> Any:
        with self.lock:
            ctx = self.state["phone_context"].get(phone)
            if ctx and isinstance(ctx, dict):
                return ctx.get(field)
            return None

    def mark_forwarded(self, dedupe_key: str, payload: Dict[str, Any]) -> None:
        with self.lock:
            self.state["forwarded_messages"][dedupe_key] = payload
        self.save()

    def was_forwarded(self, dedupe_key: str) -> bool:
        with self.lock:
            return dedupe_key in self.state["forwarded_messages"]

    def get_last_seen_inbound(self, phone: str) -> str:
        with self.lock:
            return str(self.state["last_seen_inbound"].get(phone, ""))

    def set_last_seen_inbound(self, phone: str, msg_key: str) -> None:
        with self.lock:
            self.state["last_seen_inbound"][phone] = msg_key
        self.save()


state = StateStore(STATE_FILE)


def _php_post(action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload)
    data["api_key"] = PHP_API_KEY
    r = requests.post(f"{PHP_URL}?action={action}", json=data, timeout=REQUEST_TIMEOUT_SEC)
    r.raise_for_status()
    return r.json()


def run_sql(query: str) -> List[Dict[str, Any]]:
    res = _php_post("execute_sql", {"query": query})
    if not res.get("success"):
        raise RuntimeError(f"execute_sql falhou: {res}")
    return res.get("data") or []


def insert_whatsapp_chat(
    phone: str,
    id_paciente: Any,
    id_atendimento: Any,
    id_analise: Any,
    chat_url: str,
    first_message: str,
) -> Optional[int]:
    """Insert a chatgpt_chats record for a WhatsApp follow-up conversation.

    Called when the first follow-up message is sent to a patient.
    Returns the inserted row id, or None on failure.
    """
    safe_phone = (phone or "").replace("'", "")
    safe_chat_url = (chat_url or "").replace("'", "''")
    safe_msg = first_message.replace("'", "''")

    initial_mensagens = json.dumps(
        [
            {
                "role": "system",
                "content": first_message,
                "timestamp": utc_now_iso(),
                "source": "whatsapp",
            }
        ],
        ensure_ascii=False,
    )
    safe_mensagens = initial_mensagens.replace("'", "''")

    # id_chatgpt and url_chatgpt may be empty at insert time (populated
    # later when the ChatGPT Simulator returns a conversation URL).
    query = (
        "INSERT INTO chatgpt_chats "
        "(id_criador, id_paciente, id_atendimento, id_chatgpt_atendimentos_analise, "
        " url_atual, titulo, id_chatgpt, url_chatgpt, chat_mode, whatsapp_paciente, mensagens) "
        "VALUES ("
        f"NULL, "
        f"{int(id_paciente) if id_paciente else 'NULL'}, "
        f"{int(id_atendimento) if id_atendimento else 'NULL'}, "
        f"{int(id_analise) if id_analise else 'NULL'}, "
        f"'whatsapp://acompanhamento', "
        f"'Acompanhamento WhatsApp', "
        f"'', "
        f"'{safe_chat_url}', "
        f"'whatsapp', "
        f"'{safe_phone}', "
        f"'{safe_mensagens}'"
        ")"
    )
    try:
        run_sql(query)
        # Retrieve the inserted id
        rows = run_sql(
            f"SELECT id FROM chatgpt_chats "
            f"WHERE whatsapp_paciente = '{safe_phone}' AND chat_mode = 'whatsapp' "
            f"ORDER BY id DESC LIMIT 1"
        )
        chat_id = int(rows[0]["id"]) if rows else None
        log.info(
            "chatgpt_chats inserido (WhatsApp) | id=%s phone=%s id_paciente=%s id_atendimento=%s",
            chat_id, phone, id_paciente, id_atendimento,
        )
        return chat_id
    except Exception:
        log.exception("Falha ao inserir chatgpt_chats (WhatsApp) para phone=%s", phone)
        return None


def append_whatsapp_message(
    phone: str,
    role: str,
    content: str,
    source: str = "whatsapp",
) -> None:
    """Append a message to the mensagens JSON array of the WhatsApp chat record."""
    safe_phone = (phone or "").replace("'", "")
    new_msg = json.dumps(
        {"role": role, "content": content, "timestamp": utc_now_iso(), "source": source},
        ensure_ascii=False,
    ).replace("'", "''")

    # Use JSON_ARRAY_APPEND if mensagens already exists, otherwise set a new array
    query = (
        "UPDATE chatgpt_chats SET mensagens = "
        f"CASE WHEN mensagens IS NULL OR mensagens = '' "
        f"  THEN CONCAT('[', '{new_msg}', ']') "
        f"  ELSE JSON_ARRAY_APPEND(mensagens, '$', CAST('{new_msg}' AS JSON)) "
        f"END "
        f"WHERE whatsapp_paciente = '{safe_phone}' AND chat_mode = 'whatsapp' "
        f"ORDER BY id DESC LIMIT 1"
    )
    try:
        run_sql(query)
    except Exception:
        log.exception("Falha ao atualizar mensagens do chat WhatsApp para phone=%s", phone)


def lookup_whatsapp_chat(phone: str) -> Optional[Dict[str, Any]]:
    """Find the most recent chatgpt_chats record for a WhatsApp phone.

    Returns dict with keys: id, id_paciente, id_atendimento,
    id_chatgpt_atendimentos_analise, url_chatgpt, whatsapp_paciente.
    """
    if not phone:
        return None
    safe_phone = (phone or "").replace("'", "")
    suffix = safe_phone[-9:] if len(safe_phone) >= 9 else safe_phone
    query = (
        "SELECT id, id_paciente, id_atendimento, id_chatgpt_atendimentos_analise, "
        "       url_chatgpt, whatsapp_paciente "
        "FROM chatgpt_chats "
        f"WHERE chat_mode = 'whatsapp' AND whatsapp_paciente LIKE '%{suffix}' "
        "ORDER BY id DESC LIMIT 1"
    )
    try:
        rows = run_sql(query)
        if rows:
            return rows[0]
    except Exception:
        log.exception("Falha ao buscar chatgpt_chats WhatsApp para phone=%s", phone)
    return None


def send_to_chatgpt(url_chatgpt: str, text: str, id_paciente: Any, id_atendimento: Any) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {SIMULATOR_API_KEY}"}
    payload = {
        "model": "ChatGPT Simulator",
        "message": text,
        "url": url_chatgpt,
        "stream": False,
        "id_paciente": id_paciente,
        "id_atendimento": id_atendimento,
    }
    r = requests.post(SIMULATOR_URL, headers=headers, json=payload, timeout=600)
    r.raise_for_status()
    return r.json()


def normalize_phone(raw: Any) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    if digits.startswith("0"):
        digits = digits.lstrip("0")
    if len(digits) < 10:
        return None
    if not digits.startswith("55"):
        digits = "55" + digits
    return digits


def is_valid_br_mobile_phone(phone: Optional[str]) -> bool:
    """
    Valida número celular BR em formato normalizado:
    - 55 + DDD(2) + número(9), total 13 dígitos
    - primeiro dígito do número local deve ser 9
    """
    if not phone:
        return False
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) != 13 or not digits.startswith("55"):
        return False
    return digits[4] == "9"


def resolve_phone_with_member_fallback(raw_phone: Any, id_paciente: Any) -> Tuple[Optional[str], str]:
    direct = normalize_phone(raw_phone)
    if is_valid_br_mobile_phone(direct):
        return direct, "analises"

    try:
        id_int = int(id_paciente)
    except (TypeError, ValueError):
        return (direct if direct else None), ("analises" if direct else "indisponivel")

    try:
        rows = run_sql(
            f"SELECT telefone1, telefone2 FROM membros WHERE id = {id_int} LIMIT 1"
        )
    except Exception:
        log.exception("Falha ao buscar telefone fallback em membros para id_paciente=%s", id_paciente)
        return (direct if direct else None), ("analises" if direct else "indisponivel")

    if not rows:
        return (direct if direct else None), ("analises" if direct else "indisponivel")

    row = rows[0] or {}
    candidates = [row.get("telefone1"), row.get("telefone2"), raw_phone]
    for candidate in candidates:
        normalized = normalize_phone(candidate)
        if is_valid_br_mobile_phone(normalized):
            source = "membros" if candidate != raw_phone else "analises"
            return normalized, source

    return (direct if direct else None), ("analises" if direct else "indisponivel")


def derive_age_from_row(row: Dict[str, Any]) -> str:
    idade = row.get("idade")
    if idade is not None and str(idade).strip():
        return str(idade).strip()

    birth_raw = row.get("data_nascimento")
    if not birth_raw:
        return "N/D"
    text = str(birth_raw).strip()
    if not text:
        return "N/D"
    date_part = text.split("T")[0].split(" ")[0]
    try:
        dt = datetime.fromisoformat(date_part)
        today = datetime.now().date()
        years = today.year - dt.date().year - ((today.month, today.day) < (dt.date().month, dt.date().day))
        return str(max(years, 0))
    except Exception:
        return "N/D"


def derive_age_from_birthdate(birth_raw: Any) -> str:
    if not birth_raw:
        return "N/D"
    text = str(birth_raw).strip()
    if not text:
        return "N/D"
    date_part = text.split("T")[0].split(" ")[0]
    try:
        dt = datetime.fromisoformat(date_part)
        today = datetime.now().date()
        years = today.year - dt.date().year - ((today.month, today.day) < (dt.date().month, dt.date().day))
        return str(max(years, 0))
    except Exception:
        return "N/D"


def derive_start_datetime_from_row(row: Dict[str, Any]) -> str:
    candidates = [
        "data_hora_inicio_atendimento",
        "inicio_atendimento",
        "data_inicio_atendimento",
        "created_at",
        "data_atendimento",
    ]
    for key in candidates:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return "N/D"


def build_preview_with_ellipsis(text: str, max_len: int = 120) -> str:
    clean = (text or "").strip().replace("\n", " ")
    if len(clean) <= max_len:
        return clean
    return clean[:max_len].rstrip() + "..."


def fetch_patient_metadata(id_paciente: Any, id_atendimento: Any) -> Dict[str, Any]:
    result = {"data_nascimento": None, "datetime_atendimento_inicio": None}
    try:
        id_p = int(id_paciente)
    except (TypeError, ValueError):
        return result
    try:
        id_a = int(id_atendimento)
    except (TypeError, ValueError):
        id_a = None

    query = (
        "SELECT m.data_nascimento, caa.datetime_atendimento_inicio "
        "FROM membros m "
        "LEFT JOIN chatgpt_atendimentos_analise caa ON caa.id_paciente = m.id "
        f"WHERE m.id = {id_p} "
    )
    if id_a is not None:
        query += f"AND caa.id_atendimento = {id_a} "
    query += "ORDER BY caa.id_atendimento DESC LIMIT 1"

    try:
        rows = run_sql(query)
        if rows:
            row = rows[0] or {}
            result["data_nascimento"] = row.get("data_nascimento")
            result["datetime_atendimento_inicio"] = row.get("datetime_atendimento_inicio")
    except Exception:
        log.exception("Falha ao buscar metadados do paciente id_paciente=%s id_atendimento=%s", id_paciente, id_atendimento)
    return result


TEST_DESTINATION_PHONE = normalize_phone(TEST_DESTINATION_PHONE_RAW) or "5581981487277"


def extract_followup_items(mensagens_acompanhamento: Any) -> List[Tuple[str, str]]:
    if mensagens_acompanhamento is None:
        return []

    payload = mensagens_acompanhamento
    if isinstance(payload, str):
        payload = payload.strip()
        if not payload:
            return []
        if payload.startswith("{") or payload.startswith("["):
            try:
                payload = json.loads(payload)
            except Exception:
                return [("mensagem", mensagens_acompanhamento.strip())]
        else:
            return [("mensagem", mensagens_acompanhamento.strip())]

    if isinstance(payload, dict):
        ordered_keys = ["mensagem_1_semana", "mensagem_1_mes", "mensagem_pre_retorno"]
        items: List[Tuple[str, str]] = []
        for k in ordered_keys:
            v = str(payload.get(k, "")).strip()
            if v:
                items.append((k, v))
        for k, v in payload.items():
            if k in ordered_keys:
                continue
            v2 = str(v).strip()
            if v2:
                items.append((str(k), v2))
        return items

    if isinstance(payload, list):
        return [(f"mensagem_{i}", str(item).strip()) for i, item in enumerate(payload, start=1) if str(item).strip()]

    text = str(payload).strip()
    return [("mensagem", text)] if text else []


# ---------------------------------------------------------------------------
# Time-based follow-up selection
# ---------------------------------------------------------------------------
# Maps each message key to the (min_days, max_days) window in which it should
# be sent, counted from the consultation date.
FOLLOWUP_TIME_WINDOWS: Dict[str, Tuple[int, int]] = {
    "mensagem_1_semana":    (5, 21),
    "mensagem_1_mes":       (25, 50),
    "mensagem_pre_retorno": (50, 90),
}


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Try to parse a datetime string from the database."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw == "N/D":
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def select_followup_for_timing(
    items: List[Tuple[str, str]],
    inicio_atendimento: Any,
) -> List[Tuple[str, str]]:
    """Return only the items whose time window matches the elapsed days since
    the consultation.  If the consultation date is unknown, return nothing
    (skip the row) to avoid sending the wrong message."""
    dt_inicio = _parse_datetime(inicio_atendimento)
    if dt_inicio is None:
        log.warning(
            "Data do atendimento indisponível — nenhuma mensagem selecionada "
            "por falta de referência temporal."
        )
        return []

    now = datetime.now(timezone.utc)
    elapsed_days = (now - dt_inicio).days

    selected: List[Tuple[str, str]] = []
    for key, text in items:
        window = FOLLOWUP_TIME_WINDOWS.get(key)
        if window is None:
            # Unknown key — treat as eligible immediately
            selected.append((key, text))
            continue
        min_d, max_d = window
        if min_d <= elapsed_days <= max_d:
            selected.append((key, text))

    if not selected:
        log.info(
            "Nenhuma mensagem elegível para envio — dias desde atendimento: %s "
            "(janelas configuradas: %s)",
            elapsed_days,
            {k: f"{v[0]}-{v[1]}d" for k, v in FOLLOWUP_TIME_WINDOWS.items()},
        )

    return selected


def build_forward_prompt(ctx: Dict[str, Any], patient_text: str) -> str:
    pergunta = ctx.get("pergunta") or "(não identificada)"
    nome = ctx.get("nome_paciente") or "Paciente"
    atendimento = ctx.get("id_atendimento")
    return (
        "[RESPOSTA WHATSAPP DE ACOMPANHAMENTO]\n"
        f"Paciente: {nome}\n"
        f"ID atendimento: {atendimento}\n"
        f"Pergunta/mensagem de acompanhamento: {pergunta}\n"
        f"Resposta do paciente: {patient_text}\n\n"
        "Com base nessa resposta, forneça orientação clínica de continuidade, "
        "objetiva e segura para envio ao paciente."
    )


def lookup_atendimento_by_phone(phone_digits: str) -> Optional[Dict[str, Any]]:
    """Find the most recent chatgpt_atendimentos_analise record for a phone.

    Returns dict with keys: id_analise, id_atendimento, id_paciente,
    chat_url, nome_paciente — or None if not found.
    """
    if not phone_digits:
        return None
    # Match against the last 8-9 digits to handle country-code variations
    suffix = phone_digits[-9:] if len(phone_digits) >= 9 else phone_digits
    query = (
        "SELECT caa.id AS id_analise, caa.id_atendimento, caa.id_paciente, "
        "       caa.chat_url, m.nome AS nome_paciente "
        "FROM chatgpt_atendimentos_analise caa "
        "JOIN membros m ON m.id = caa.id_paciente "
        "WHERE caa.chat_url IS NOT NULL AND caa.chat_url <> '' "
        "  AND caa.status = 'concluido' "
        f"  AND (REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(m.telefone1,''),' ',''),'-',''),'(',''),')','') LIKE '%{suffix}' "
        f"    OR REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(m.telefone2,''),' ',''),'-',''),'(',''),')','') LIKE '%{suffix}' "
        f"    OR REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(m.telefone1pais,''),' ',''),'-',''),'(',''),')','') LIKE '%{suffix}' "
        f"    OR REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(m.telefone2pais,''),' ',''),'-',''),'(',''),')','') LIKE '%{suffix}') "
        "ORDER BY caa.id DESC LIMIT 1"
    )
    try:
        rows = run_sql(query)
        if rows:
            row = rows[0]
            return {
                "id_analise": row.get("id_analise"),
                "id_atendimento": row.get("id_atendimento"),
                "id_paciente": row.get("id_paciente"),
                "chat_url": (row.get("chat_url") or "").strip(),
                "nome_paciente": row.get("nome_paciente"),
            }
    except Exception:
        log.exception("Falha ao buscar atendimento por telefone %s", phone_digits)
    return None


def lookup_atendimento_by_name(name: str) -> Optional[Dict[str, Any]]:
    """Fallback: find atendimento by patient name (for saved WhatsApp contacts)."""
    if not name or len(name) < 3:
        return None
    # Escape single quotes for SQL
    safe_name = name.replace("'", "''")
    query = (
        "SELECT caa.id AS id_analise, caa.id_atendimento, caa.id_paciente, "
        "       caa.chat_url, m.nome AS nome_paciente, "
        "       COALESCE(m.telefone1, m.telefone2, m.telefone1pais, m.telefone2pais) AS telefone "
        "FROM chatgpt_atendimentos_analise caa "
        "JOIN membros m ON m.id = caa.id_paciente "
        "WHERE caa.chat_url IS NOT NULL AND caa.chat_url <> '' "
        "  AND caa.status = 'concluido' "
        f"  AND m.nome LIKE '%{safe_name}%' "
        "ORDER BY caa.id DESC LIMIT 1"
    )
    try:
        rows = run_sql(query)
        if rows:
            row = rows[0]
            return {
                "id_analise": row.get("id_analise"),
                "id_atendimento": row.get("id_atendimento"),
                "id_paciente": row.get("id_paciente"),
                "chat_url": (row.get("chat_url") or "").strip(),
                "nome_paciente": row.get("nome_paciente"),
                "telefone": normalize_phone(row.get("telefone")),
            }
    except Exception:
        log.exception("Falha ao buscar atendimento por nome '%s'", name)
    return None


class WhatsAppWebClient:
    """Wraps Playwright browser in a dedicated thread.

    All Playwright calls are dispatched to the browser-owner thread via a task
    queue, avoiding ``greenlet.error: Cannot switch to a different thread``.
    """

    def __init__(self) -> None:
        self._task_queue: queue.Queue = queue.Queue()
        self._playwright = None
        self._browser = None
        self._page = None
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- public entry point ---------------------------------------------------

    def start(self) -> None:
        """Spawn the browser thread (idempotent) and block until ready."""
        if self._thread is not None and self._thread.is_alive():
            self._ready.wait()
            return
        self._thread = threading.Thread(target=self._browser_loop, daemon=True)
        self._thread.start()
        self._ready.wait()

    # -- internal: browser-owner thread main loop -----------------------------

    def _browser_loop(self) -> None:
        """Runs on the dedicated browser thread. Creates Playwright, then
        processes tasks from the queue forever."""
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(WHATSAPP_PROFILE_DIR),
                headless=False,
                args=["--start-maximized"],
            )
            self._page = self._browser.new_page()
            self._page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded")
            self._wait_ready()
            self._log_chat_list_snapshot("startup")
            log.info("WhatsApp Web pronto.")
        except Exception:
            log.exception("Falha ao iniciar WhatsApp Web browser thread")
            self._ready.set()  # unblock waiters so they see the error
            return

        self._ready.set()

        while True:
            func, future = self._task_queue.get()
            try:
                result = func()
                future.set_result(result)
            except Exception as exc:
                future.set_exception(exc)

    # -- helper: dispatch a callable to the browser thread --------------------

    def _run_on_browser_thread(self, func):
        """Submit *func* to the browser thread and block until it finishes."""
        fut: Future = Future()
        self._task_queue.put((func, fut))
        return fut.result()  # blocks caller until browser thread completes

    def _wait_ready(self, timeout_ms: int = 180000) -> None:
        assert self._page is not None
        try:
            self._page.wait_for_selector('div[aria-label="Chat list"], #pane-side', timeout=timeout_ms)
            log.info("WhatsApp Web autenticado: lista de chats visível no browser.")
        except PlaywrightTimeoutError:
            log.error(
                "WhatsApp Web não autenticado. Abra a janela e faça login via QR Code em %s",
                WHATSAPP_WEB_URL,
            )
            raise

    def _log_chat_list_snapshot(self, reason: str, limit: int = 12) -> None:
        assert self._page is not None
        try:
            chats = self._page.evaluate(
                """(maxItems) => {
                    const root = document.querySelector('#pane-side');
                    if (!root) return [];
                    const rows = root.querySelectorAll('div[role="row"]');
                    const out = [];

                    for (const row of rows) {
                        // Only look in the chat name/header area (div._ak8q),
                        // NOT in the message preview area (div._ak8j / div._ak8k)
                        const nameArea = row.querySelector('div._ak8q');
                        if (!nameArea) continue;
                        const nameSpan = nameArea.querySelector('span[title]');
                        if (!nameSpan) continue;
                        const name = (nameSpan.getAttribute('title') || '').trim();
                        if (!name) continue;
                        if (out.includes(name)) continue;
                        out.push(name);
                        if (out.length >= maxItems) break;
                    }
                    return out;
                }""",
                limit,
            )
            if chats:
                formatted = " | ".join(f"[{c}]" for c in chats)
                log.info("Lista de chats visíveis (%s): %s", reason, formatted)
            else:
                log.warning("Não foi possível capturar a lista de chats visíveis (%s).", reason)
        except Exception:
            log.exception("Falha ao capturar lista de chats visíveis (%s).", reason)

    def _find_existing_chat_in_sidebar(self, phone: str) -> bool:
        assert self._page is not None
        phone_digits = re.sub(r"\D", "", phone or "")
        if not phone_digits:
            return False
        try:
            found = self._page.evaluate(
                """(needle) => {
                    const root = document.querySelector('#pane-side');
                    if (!root) return false;
                    const texts = root.querySelectorAll('span[dir="auto"], div[title]');
                    for (const el of texts) {
                        const txt = ((el.getAttribute && el.getAttribute('title')) || el.textContent || '').trim();
                        if (!txt) continue;
                        const digits = txt.replace(/\\D/g, '');
                        if (!digits) continue;
                        if (digits.endsWith(needle.slice(-8)) || digits.endsWith(needle.slice(-9)) || digits === needle) {
                            return true;
                        }
                    }
                    return false;
                }""",
                phone_digits,
            )
            return bool(found)
        except Exception:
            log.exception("Falha ao buscar chat existente na barra lateral para telefone=%s", phone)
            return False

    def _open_chat(self, phone: str) -> None:
        assert self._page is not None
        existing = self._find_existing_chat_in_sidebar(phone)
        if existing:
            log.info("Abrindo chat com histórico identificado na lista lateral para %s", phone)
        else:
            log.info("Abrindo novo chat (sem histórico identificado na lista lateral) para %s", phone)
        self._log_chat_list_snapshot(f"antes_open_chat:{phone}", limit=8)
        log.info("Abrindo chat EXCLUSIVAMENTE via URL send para %s", phone)
        url = f"https://web.whatsapp.com/send?phone={phone}&text={quote('')}&app_absent=0"
        self._page.goto(url, wait_until="domcontentloaded")
        self._page.wait_for_selector("p._aupe, footer div[contenteditable='true']", timeout=15000)
        inbound_count = self._page.locator("div.message-in").count()
        outbound_count = self._page.locator("div.message-out").count()
        log.info(
            "Chat aberto via URL send para %s | mensagens_recebidas=%s | mensagens_enviadas=%s",
            phone,
            inbound_count,
            outbound_count,
        )

    def send_message(self, phone: str, text: str) -> None:
        self.start()

        def _do():
            log.info("Iniciando fluxo de envio WhatsApp para %s", phone)
            self._open_chat(phone)
            box = self._page.locator("p._aupe, footer div[contenteditable='true']").first
            before_out = self._page.locator("div.message-out").count()
            box.click()
            self._page.keyboard.type(text, delay=5)
            send_icon = self._page.locator('span[data-icon="wds-ic-send-filled"]').first
            if send_icon.count() > 0:
                send_icon.click(timeout=10000)
            else:
                log.warning("Ícone de envio não encontrado; tentando Enter no campo de mensagem.")
                self._page.keyboard.press("Enter")
            self._page.wait_for_timeout(1200)
            after_out = self._page.locator("div.message-out").count()
            if after_out > before_out:
                log.info(
                    "Envio confirmado no browser para %s | out_antes=%s out_depois=%s",
                    phone,
                    before_out,
                    after_out,
                )
            else:
                log.warning(
                    "Sem confirmação visual de envio no browser para %s | out_antes=%s out_depois=%s",
                    phone,
                    before_out,
                    after_out,
                )

        self._run_on_browser_thread(_do)

    def read_last_inbound(self, phone: str) -> Optional[Dict[str, str]]:
        self.start()

        def _do():
            self._open_chat(phone)
            return self._page.evaluate(
                """() => {
                    const msgs = Array.from(document.querySelectorAll('div.message-in'));
                    if (!msgs.length) return null;
                    const last = msgs[msgs.length - 1];
                    const textNode = last.querySelector('span.selectable-text.copyable-text span') ||
                                     last.querySelector('span.selectable-text span');
                    const text = textNode ? textNode.textContent.trim() : '';
                    const msgId = last.getAttribute('data-id') || last.id || '';
                    if (!text) return null;
                    return { id: msgId || text, text };
                }"""
            )

        return self._run_on_browser_thread(_do)

    # -- New methods for listening to ANY incoming message ---------------------

    def scan_unread_chats(self) -> List[Dict[str, Any]]:
        """Scan sidebar for chats with unread message badges.
        Returns [{title, unread_count}]."""
        self.start()

        def _do():
            return self._page.evaluate(
                """() => {
                    const root = document.querySelector('#pane-side');
                    if (!root) return [];
                    const rows = root.querySelectorAll('div[role="row"]');
                    const results = [];

                    for (const row of rows) {
                        // Strategy 1: aria-label with unread info
                        let unread = 0;
                        const ariaEls = row.querySelectorAll('[aria-label]');
                        for (const el of ariaEls) {
                            const label = (el.getAttribute('aria-label') || '').toLowerCase();
                            const m = label.match(/(\\d+)\\s*(unread|não lida|nova|new)/);
                            if (m) { unread = parseInt(m[1]); break; }
                        }

                        // Strategy 2: look for a small span with a colored background
                        // containing just a number (the unread badge)
                        if (!unread) {
                            const spans = row.querySelectorAll('span');
                            for (const span of spans) {
                                const text = span.textContent.trim();
                                if (!/^\\d{1,4}$/.test(text)) continue;
                                const style = window.getComputedStyle(span);
                                const bg = style.backgroundColor;
                                // Badge has a visible background (green or accent color)
                                if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent'
                                    && bg !== 'rgb(255, 255, 255)' && bg !== 'rgb(0, 0, 0)') {
                                    unread = parseInt(text);
                                    break;
                                }
                            }
                        }

                        if (!unread) continue;

                        const nameArea = row.querySelector('div._ak8q');
                        if (!nameArea) continue;
                        const nameSpan = nameArea.querySelector('span[title]');
                        if (!nameSpan) continue;
                        const title = (nameSpan.getAttribute('title') || '').trim();
                        if (!title) continue;

                        results.push({ title: title, unread_count: unread });
                    }
                    return results;
                }"""
            )

        return self._run_on_browser_thread(_do)

    def open_chat_by_sidebar_click(self, title: str) -> bool:
        """Click on a chat in the sidebar by its title. Returns True if opened."""
        self.start()

        def _do():
            clicked = self._page.evaluate(
                """(targetTitle) => {
                    const root = document.querySelector('#pane-side');
                    if (!root) return false;
                    const rows = root.querySelectorAll('div[role="row"]');
                    for (const row of rows) {
                        const nameArea = row.querySelector('div._ak8q');
                        if (!nameArea) continue;
                        const nameSpan = nameArea.querySelector('span[title]');
                        if (!nameSpan) continue;
                        const t = (nameSpan.getAttribute('title') || '').trim();
                        if (t === targetTitle) {
                            row.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                title,
            )
            if clicked:
                # Wait for chat to load
                try:
                    self._page.wait_for_selector(
                        "#main header, p._aupe, footer div[contenteditable='true']",
                        timeout=8000,
                    )
                    self._page.wait_for_timeout(500)
                except Exception:
                    log.warning("Timeout aguardando chat abrir para título: %s", title)
            return bool(clicked)

        return self._run_on_browser_thread(_do)

    def extract_phone_from_open_chat(self) -> Optional[str]:
        """Try to extract the phone number from the currently open chat header."""
        self.start()

        def _do():
            return self._page.evaluate(
                """() => {
                    const header = document.querySelector('#main header');
                    if (!header) return null;

                    // Collect all text content from header spans
                    const spans = header.querySelectorAll('span[title], span[dir="auto"], span');
                    for (const span of spans) {
                        const text = (span.getAttribute('title') || span.textContent || '').trim();
                        if (!text) continue;
                        const digits = text.replace(/\\D/g, '');
                        // A phone number has 10-15 digits
                        if (digits.length >= 10 && digits.length <= 15) {
                            return digits;
                        }
                    }
                    return null;
                }"""
            )

        return self._run_on_browser_thread(_do)

    def read_all_inbound_from_open_chat(self) -> List[Dict[str, str]]:
        """Read ALL inbound messages from the currently open chat."""
        self.start()

        def _do():
            return self._page.evaluate(
                """() => {
                    const msgs = Array.from(document.querySelectorAll('div.message-in'));
                    const results = [];
                    for (const msg of msgs) {
                        const textNode = msg.querySelector('span.selectable-text.copyable-text span') ||
                                         msg.querySelector('span.selectable-text span');
                        const text = textNode ? textNode.textContent.trim() : '';
                        if (!text) continue;
                        const msgId = msg.getAttribute('data-id') || msg.id || '';
                        results.push({ id: msgId || text, text: text });
                    }
                    return results;
                }"""
            )

        return self._run_on_browser_thread(_do)


wa_web = WhatsAppWebClient()


def send_pending_followups_once() -> Dict[str, Any]:
    rows = run_sql(FETCH_SQL)
    total_rows = len(rows)
    total_followup_items = 0
    sent = 0
    skipped = 0
    skipped_missing_phone = 0
    skipped_empty_followup = 0
    skipped_already_sent = 0
    skipped_not_due = 0
    errors = 0
    recovered_member_phone = 0

    for row in rows:
        id_atendimento = row.get("id_atendimento")
        id_paciente = row.get("id_paciente")
        id_analise = row.get("id_analise")
        nome_paciente = row.get("nome_paciente")
        chat_url = (row.get("chat_url") or "").strip()
        url_chatgpt = (row.get("url_chatgpt") or "").strip()
        original_phone = normalize_phone(row.get("telefone"))
        phone, phone_source = resolve_phone_with_member_fallback(row.get("telefone"), id_paciente)
        if (not original_phone or not is_valid_br_mobile_phone(original_phone)) and phone:
            recovered_member_phone += 1

        if not phone:
            skipped += 1
            skipped_missing_phone += 1
            continue

        all_itens = extract_followup_items(row.get("mensagens_acompanhamento"))
        if not all_itens:
            skipped += 1
            skipped_empty_followup += 1
            continue
        total_followup_items += len(all_itens)

        # Fetch consultation date once per row to determine which message
        # is appropriate based on elapsed time.
        metadata = fetch_patient_metadata(id_paciente=id_paciente, id_atendimento=id_atendimento)
        idade = derive_age_from_birthdate(metadata.get("data_nascimento"))
        if idade == "N/D":
            idade = derive_age_from_row(row)
        inicio_atendimento = metadata.get("datetime_atendimento_inicio") or derive_start_datetime_from_row(row)

        # Filter: only send the message(s) whose time window matches now.
        itens = select_followup_for_timing(all_itens, inicio_atendimento)
        skipped_not_due += len(all_itens) - len(itens)

        for key, pergunta in itens:
            dedupe_key = f"{id_atendimento}:{key}:{hashlib.sha1(pergunta.encode('utf-8')).hexdigest()}"
            if state.is_sent(dedupe_key):
                skipped += 1
                skipped_already_sent += 1
                continue

            try:
                preview = build_preview_with_ellipsis(pergunta, max_len=140)
                log.info(
                    "Pré-envio | Paciente: [%s] | idade: [%s] | Telefone de contato: [%s] (origem=%s) "
                    "| Telefone para testes: [%s] | Id do atendimento: [%s] | Data do atendimento: [%s] "
                    "| Tipo: [%s] | Mensagem: [%s]",
                    (nome_paciente or "N/D"),
                    (idade or "N/D"),
                    (phone or "N/D"),
                    phone_source,
                    TEST_DESTINATION_PHONE,
                    id_atendimento if id_atendimento is not None else "N/D",
                    inicio_atendimento if inicio_atendimento is not None else "N/D",
                    key,
                    preview or "N/D",
                )

                # Random delay between sends to simulate human behaviour.
                if sent > 0:
                    delay = random.uniform(10, 45)
                    log.info("Aguardando %.1fs antes do próximo envio...", delay)
                    time.sleep(delay)

                wa_web.send_message(
                    TEST_DESTINATION_PHONE,
                    f"{pergunta}\n\n"
                    "Pode me responder por aqui?",
                )

                state.mark_sent(
                    dedupe_key,
                    {
                        "id_atendimento": id_atendimento,
                        "id_paciente": id_paciente,
                        "phone": phone,
                        "question_key": key,
                        "pergunta": pergunta,
                        "sent_at": utc_now_iso(),
                    },
                )
                state.set_phone_context(
                    phone,
                    {
                        "id_atendimento": id_atendimento,
                        "id_paciente": id_paciente,
                        "nome_paciente": nome_paciente,
                        "pergunta": pergunta,
                        "question_key": key,
                        "url_chatgpt": url_chatgpt,
                    },
                )

                # Persist in chatgpt_chats with chat_mode='whatsapp' for later lookup
                full_msg = f"{pergunta}\n\nPode me responder por aqui?"
                insert_whatsapp_chat(
                    phone=phone,
                    id_paciente=id_paciente,
                    id_atendimento=id_atendimento,
                    id_analise=id_analise,
                    chat_url=chat_url or url_chatgpt,
                    first_message=full_msg,
                )

                sent += 1
            except Exception:
                errors += 1
                log.exception("Falha no envio para %s (atendimento=%s)", phone, id_atendimento)

    return {
        "total": total_rows,
        "total_followup_items": total_followup_items,
        "sent": sent,
        "skipped": skipped,
        "skipped_missing_phone": skipped_missing_phone,
        "skipped_empty_followup": skipped_empty_followup,
        "skipped_already_sent": skipped_already_sent,
        "skipped_not_due": skipped_not_due,
        "errors": errors,
        "recovered_member_phone": recovered_member_phone,
    }


def _build_skip_reason_summary(stats: Dict[str, Any]) -> str:
    reasons: List[str] = []
    if stats.get("skipped_missing_phone", 0):
        reasons.append(f"sem telefone válido={stats['skipped_missing_phone']}")
    if stats.get("skipped_empty_followup", 0):
        reasons.append(f"sem mensagem de acompanhamento={stats['skipped_empty_followup']}")
    if stats.get("skipped_already_sent", 0):
        reasons.append(f"já enviado anteriormente={stats['skipped_already_sent']}")
    if stats.get("skipped_not_due", 0):
        reasons.append(f"fora da janela temporal={stats['skipped_not_due']}")
    if stats.get("errors", 0):
        reasons.append(f"falha ao enviar={stats['errors']}")
    return "; ".join(reasons) if reasons else "nenhum motivo classificado"


def _phone_from_title(title: str) -> Optional[str]:
    """Try to extract a phone number from a WhatsApp chat sidebar title."""
    digits = re.sub(r"\D", "", title or "")
    if len(digits) >= 10:
        return normalize_phone(digits)
    return None


def _resolve_chat_to_atendimento(
    title: str, phone_hint: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Given a chat title (and optional phone), find the matching atendimento.

    Tries (in order):
      1. chatgpt_chats.whatsapp_paciente (fastest — direct phone lookup)
      2. chatgpt_atendimentos_analise via membros phone columns
      3. chatgpt_atendimentos_analise via patient name fallback

    Returns dict with id_analise, id_atendimento, id_paciente, chat_url,
    nome_paciente, telefone.
    """
    # Try phone extracted from title first
    phone = _phone_from_title(title)

    # 1) Fast path: lookup by whatsapp_paciente in chatgpt_chats
    for candidate_phone in [phone, normalize_phone(phone_hint) if phone_hint else None]:
        if not candidate_phone:
            continue
        wa_chat = lookup_whatsapp_chat(candidate_phone)
        if wa_chat and (wa_chat.get("url_chatgpt") or "").strip():
            return {
                "id_analise": wa_chat.get("id_chatgpt_atendimentos_analise"),
                "id_atendimento": wa_chat.get("id_atendimento"),
                "id_paciente": wa_chat.get("id_paciente"),
                "chat_url": (wa_chat.get("url_chatgpt") or "").strip(),
                "nome_paciente": None,
                "telefone": candidate_phone,
            }

    # 2) Lookup via chatgpt_atendimentos_analise + membros phone columns
    if phone:
        result = lookup_atendimento_by_phone(phone)
        if result:
            result.setdefault("telefone", phone)
            return result

    if phone_hint:
        norm = normalize_phone(phone_hint)
        if norm:
            result = lookup_atendimento_by_phone(norm)
            if result:
                result.setdefault("telefone", norm)
                return result

    # 3) Fallback: try matching by name
    result = lookup_atendimento_by_name(title)
    return result


def process_incoming_replies_once() -> Dict[str, int]:
    """Scan WhatsApp sidebar for unread chats, resolve each to a
    chatgpt_atendimentos_analise record, forward the patient reply to
    the ChatGPT simulator via chat_url, and reply back."""
    processed = 0
    skipped = 0
    no_match = 0

    # 1) Scan sidebar for chats with unread messages
    unread_chats = wa_web.scan_unread_chats()
    if not unread_chats:
        return {"processed": 0, "skipped": 0, "no_match": 0}

    log.info(
        "Chats com mensagens não lidas: %s",
        " | ".join(f"[{c['title']}]({c['unread_count']})" for c in unread_chats),
    )

    for chat in unread_chats:
        title = chat["title"]
        try:
            # 2) Open the chat by clicking in the sidebar
            if not wa_web.open_chat_by_sidebar_click(title):
                log.warning("Não foi possível abrir chat '%s' pela sidebar", title)
                skipped += 1
                continue

            # 3) Try to extract phone from the open chat header
            phone_hint = wa_web.extract_phone_from_open_chat()

            # 4) Resolve to an atendimento record (phone or name lookup)
            atendimento = _resolve_chat_to_atendimento(title, phone_hint)
            if not atendimento or not atendimento.get("chat_url"):
                log.info(
                    "Chat '%s' não corresponde a nenhum atendimento com chat_url "
                    "(phone_hint=%s) — ignorando.",
                    title,
                    phone_hint,
                )
                no_match += 1
                continue

            phone = atendimento.get("telefone") or phone_hint or _phone_from_title(title)
            chat_url = atendimento["chat_url"]
            id_atendimento = atendimento.get("id_atendimento")
            id_paciente = atendimento.get("id_paciente")
            nome_paciente = atendimento.get("nome_paciente") or title

            log.info(
                "Chat '%s' → atendimento id=%s | paciente=%s | phone=%s | chat_url=%s",
                title,
                id_atendimento,
                nome_paciente,
                phone,
                build_preview_with_ellipsis(chat_url, 60),
            )

            # 5) Read inbound messages from the open chat
            inbound_msgs = wa_web.read_all_inbound_from_open_chat()
            if not inbound_msgs:
                skipped += 1
                continue

            # Process the last inbound message
            last = inbound_msgs[-1]
            msg_key = last.get("id") or hashlib.sha1(last["text"].encode("utf-8")).hexdigest()
            phone_key = phone or title
            if msg_key == state.get_last_seen_inbound(phone_key):
                skipped += 1
                continue

            dedupe_key = f"{phone_key}:{msg_key}"
            if state.was_forwarded(dedupe_key):
                state.set_last_seen_inbound(phone_key, msg_key)
                skipped += 1
                continue

            # 6) Forward to ChatGPT simulator
            ctx = {
                "id_atendimento": id_atendimento,
                "id_paciente": id_paciente,
                "nome_paciente": nome_paciente,
                "pergunta": state.get_phone_context_field(phone_key, "pergunta") or "(acompanhamento)",
            }
            prompt = build_forward_prompt(ctx, last["text"])
            log.info(
                "Encaminhando resposta do paciente '%s' ao ChatGPT simulator | msg: [%s]",
                nome_paciente,
                build_preview_with_ellipsis(last["text"], 120),
            )
            res = send_to_chatgpt(
                url_chatgpt=chat_url,
                text=prompt,
                id_paciente=id_paciente,
                id_atendimento=id_atendimento,
            )
            answer = (res.get("html") or "").strip() or "Recebido. A equipe entrará em contato se necessário."

            # 7) Log patient message and simulator response in chatgpt_chats.mensagens
            if phone:
                append_whatsapp_message(phone, role="user", content=last["text"], source="whatsapp")
                append_whatsapp_message(phone, role="assistant", content=answer, source="chatgpt_simulator")

            # 8) Reply to the patient
            dest_phone = TEST_DESTINATION_PHONE if TEST_DESTINATION_PHONE else phone
            if dest_phone:
                wa_web.send_message(dest_phone, answer)
            else:
                log.warning("Sem telefone para responder ao chat '%s'", title)

            state.mark_forwarded(
                dedupe_key,
                {
                    "phone": phone_key,
                    "at": utc_now_iso(),
                    "id_atendimento": id_atendimento,
                    "id_paciente": id_paciente,
                    "patient_text": last["text"],
                    "inbound_key": msg_key,
                    "chat_url": chat_url,
                },
            )
            state.set_last_seen_inbound(phone_key, msg_key)
            processed += 1

        except Exception:
            log.exception("Falha ao processar resposta do chat '%s'", title)

    return {"processed": processed, "skipped": skipped, "no_match": no_match}


def scheduler_loop() -> None:
    log.info("Scheduler de envios iniciado. Intervalo: %ss", POLL_INTERVAL_SEC)
    while True:
        try:
            stats = send_pending_followups_once()
            motivos = _build_skip_reason_summary(stats)
            log.info(
                "Envio acompanhamento | total=%s itens=%s enviados=%s ignorados=%s "
                "(sem_telefone=%s, sem_mensagem=%s, ja_enviado=%s, fora_janela=%s, erros=%s, recuperado_membros=%s, motivos=%s)",
                stats["total"],
                stats["total_followup_items"],
                stats["sent"],
                stats["skipped"],
                stats["skipped_missing_phone"],
                stats["skipped_empty_followup"],
                stats["skipped_already_sent"],
                stats["skipped_not_due"],
                stats["errors"],
                stats["recovered_member_phone"],
                motivos,
            )
            if stats["sent"] == 0 and stats["total"] > 0:
                log.warning("Nenhum envio novo nesta varredura. Motivos: %s", motivos)
        except Exception:
            log.exception("Falha no ciclo de envio")
        time.sleep(POLL_INTERVAL_SEC)


def replies_loop() -> None:
    log.info("Monitor de respostas iniciado (scan de sidebar). Intervalo: %ss", REPLY_POLL_INTERVAL_SEC)
    while True:
        try:
            stats = process_incoming_replies_once()
            if stats.get("processed", 0) > 0 or stats.get("no_match", 0) > 0:
                log.info(
                    "Monitor respostas | processadas=%s ignoradas=%s sem_match=%s",
                    stats.get("processed", 0),
                    stats.get("skipped", 0),
                    stats.get("no_match", 0),
                )
        except Exception:
            log.exception("Falha no monitor de respostas")
        time.sleep(REPLY_POLL_INTERVAL_SEC)


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "service": "whatsapp_web_acompanhamento_server",
            "state_file": str(STATE_FILE),
            "whatsapp_web_url": WHATSAPP_WEB_URL,
            "simulator_url": SIMULATOR_URL,
            "php_url": PHP_URL,
            "test_destination_phone": TEST_DESTINATION_PHONE,
            "poll_interval_sec": POLL_INTERVAL_SEC,
            "reply_poll_interval_sec": REPLY_POLL_INTERVAL_SEC,
        }
    )


@app.post("/send-now")
def send_now():
    return jsonify({"ok": True, **send_pending_followups_once()})


@app.post("/process-replies-now")
def process_replies_now():
    return jsonify({"ok": True, **process_incoming_replies_once()})


if __name__ == "__main__":
    log.info("Modo isolado ativo (sem Meta Cloud API).")
    log.info("WhatsApp Web: %s", WHATSAPP_WEB_URL)
    log.info("Simulator local: %s", SIMULATOR_URL)
    log.info("PHP remoto: %s", PHP_URL)
    log.info("Modo teste ativo: todos os envios serão direcionados para %s", TEST_DESTINATION_PHONE)

    log.info("Iniciando browser WhatsApp. Se necessário, faça login via QR Code...")
    wa_web.start()

    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=replies_loop, daemon=True).start()

    log.info("Servidor de acompanhamento WhatsApp Web em %s:%s", HOST, PORT)
    app.run(host=HOST, port=PORT)
