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

try:
    from playwright._impl._errors import TargetClosedError
except ImportError:
    # Fallback: define a placeholder so isinstance() checks still work.
    # Actual TargetClosedError will be caught by the generic str-check.
    class TargetClosedError(Exception):  # type: ignore[no-redef]
        pass

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
# Modo de teste por padrão: restringe varredura para um único paciente.
# Pode ser sobrescrito por variável de ambiente.
TEST_ONLY_ID_PACIENTE_RAW = os.getenv("PYWA_TEST_ONLY_ID_PACIENTE", "1712836976").strip()

HOST = os.getenv("PYWA_HOST", "0.0.0.0")
PORT = int(os.getenv("PYWA_PORT", "3011"))

BASE_DIR = Path(__file__).resolve().parents[1]
DB_DIR = BASE_DIR / "db"
DB_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DB_DIR / "pywa_followup_state.json"
WHATSAPP_PROFILE_DIR = BASE_DIR / "chrome_profile_whatsapp"
WHATSAPP_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# SQL: busca apenas registros elegíveis com base na data do atendimento
# ---------------------------------------------------------------------------
# Janela global: 5–90 dias desde datetime_atendimento_inicio (cobre
# mensagem_1_semana 5-21d, mensagem_1_mes 25-50d, mensagem_pre_retorno 50-90d).
# Ordenado por datetime_atendimento_inicio ASC → pacientes mais antigos primeiro
# (mais perto do retorno = maior prioridade).
DEFAULT_FETCH_SQL = """
SELECT
  caa.id                              AS id_analise,
  caa.id_atendimento,
  caa.id_paciente,
  caa.datetime_atendimento_inicio,
  DATEDIFF(CURDATE(), DATE(caa.datetime_atendimento_inicio)) AS dias_desde_atendimento,
  caa.status,
  COALESCE(m.telefone1, m.telefone2, m.telefone1pais, m.telefone2pais) AS telefone,
  m.nome                              AS nome_paciente,
  m.data_nascimento,
  caa.mensagens_acompanhamento,
  caa.chat_url,
  cc.url_chatgpt
FROM chatgpt_atendimentos_analise caa
JOIN membros m ON m.id = caa.id_paciente
LEFT JOIN chatgpt_chats cc
       ON cc.id_chatgpt_atendimentos_analise = caa.id
      AND cc.chat_mode = 'whatsapp'
WHERE caa.mensagens_acompanhamento IS NOT NULL
  AND caa.mensagens_acompanhamento <> ''
  AND caa.status = 'concluido'
  AND caa.datetime_atendimento_inicio IS NOT NULL
  AND DATEDIFF(CURDATE(), DATE(caa.datetime_atendimento_inicio)) BETWEEN 5 AND 90
ORDER BY caa.datetime_atendimento_inicio ASC
LIMIT 200
""".strip()
FETCH_SQL = os.getenv("PYWA_FETCH_SQL", DEFAULT_FETCH_SQL)

# SQL de resumo: contagem por janela temporal
SUMMARY_SQL = """
SELECT
  COUNT(*)                                                              AS total_elegiveis,
  SUM(DATEDIFF(CURDATE(), DATE(caa.datetime_atendimento_inicio)) BETWEEN  5 AND 21) AS faixa_1_semana,
  SUM(DATEDIFF(CURDATE(), DATE(caa.datetime_atendimento_inicio)) BETWEEN 25 AND 50) AS faixa_1_mes,
  SUM(DATEDIFF(CURDATE(), DATE(caa.datetime_atendimento_inicio)) BETWEEN 50 AND 90) AS faixa_pre_retorno
FROM chatgpt_atendimentos_analise caa
JOIN membros m ON m.id = caa.id_paciente
WHERE caa.mensagens_acompanhamento IS NOT NULL
  AND caa.mensagens_acompanhamento <> ''
  AND caa.status = 'concluido'
  AND caa.datetime_atendimento_inicio IS NOT NULL
  AND DATEDIFF(CURDATE(), DATE(caa.datetime_atendimento_inicio)) BETWEEN 5 AND 90
""".strip()

try:
    TEST_ONLY_ID_PACIENTE = int(TEST_ONLY_ID_PACIENTE_RAW) if TEST_ONLY_ID_PACIENTE_RAW else None
except ValueError:
    TEST_ONLY_ID_PACIENTE = None

if TEST_ONLY_ID_PACIENTE is not None:
    FETCH_SQL = FETCH_SQL.replace(
        "ORDER BY caa.datetime_atendimento_inicio ASC",
        f"  AND caa.id_paciente = {TEST_ONLY_ID_PACIENTE}\nORDER BY caa.datetime_atendimento_inicio ASC",
    )
    SUMMARY_SQL = SUMMARY_SQL + f"\n  AND caa.id_paciente = {TEST_ONLY_ID_PACIENTE}"

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
            "contact_aliases": {},
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

    def unmark_sent(self, key: str) -> None:
        with self.lock:
            if key in self.state["sent_questions"]:
                del self.state["sent_questions"][key]
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

    def set_contact_alias(self, title: str, phone: str) -> None:
        t = re.sub(r"\s+", " ", (title or "").strip().lower())
        p = normalize_phone(phone)
        if not t or not p:
            return
        with self.lock:
            self.state["contact_aliases"][t] = p
        self.save()

    def get_contact_aliases(self) -> Dict[str, str]:
        with self.lock:
            aliases = self.state.get("contact_aliases") or {}
            return dict(aliases if isinstance(aliases, dict) else {})

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


def _sql_escape(value: Any) -> str:
    """Escape value for string interpolation in SQL built by this service."""
    return str(value or "").replace("\\", "\\\\").replace("'", "''")


def _upsert_whatsapp_contact_profile(
    *,
    phone: Optional[str],
    display_name: str,
    profile_name: str,
    source: str,
    wa_chat_title: str = "",
    id_paciente: Any = None,
    id_atendimento: Any = None,
) -> None:
    """Persist WhatsApp contact metadata in chatgpt_whatsapp table."""
    safe_phone = _sql_escape(normalize_phone(phone) or "")
    safe_display_name = _sql_escape(display_name)
    safe_profile_name = _sql_escape(profile_name)
    safe_chat_title = _sql_escape(wa_chat_title)
    safe_source = _sql_escape(source)
    profile_json = json.dumps(
        {
            "captured_at_utc": utc_now_iso(),
            "display_name": display_name or "",
            "profile_name": profile_name or "",
            "wa_chat_title": wa_chat_title or "",
            "source": source or "",
        },
        ensure_ascii=False,
    )
    safe_profile_json = _sql_escape(profile_json)
    id_paciente_sql = str(int(id_paciente)) if str(id_paciente).strip().isdigit() else "NULL"
    id_atendimento_sql = str(int(id_atendimento)) if str(id_atendimento).strip().isdigit() else "NULL"
    is_named_contact = 0
    has_display_name = bool((display_name or "").strip())
    has_phone_title = bool(_phone_from_title(display_name))
    if has_display_name and not has_phone_title:
        is_named_contact = 1

    query = (
        "INSERT INTO chatgpt_whatsapp "
        "(whatsapp_phone, wa_display_name, wa_profile_name, wa_chat_title, "
        " id_paciente, id_atendimento, is_named_contact, profile_payload_json, "
        " source, first_seen_at, last_seen_at, updated_at) "
        "VALUES ("
        f"'{safe_phone}', "
        f"'{safe_display_name}', "
        f"'{safe_profile_name}', "
        f"'{safe_chat_title}', "
        f"{id_paciente_sql}, "
        f"{id_atendimento_sql}, "
        f"{is_named_contact}, "
        f"'{safe_profile_json}', "
        f"'{safe_source}', "
        "UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP()"
        ") "
        "ON DUPLICATE KEY UPDATE "
        "wa_display_name = VALUES(wa_display_name), "
        "wa_profile_name = VALUES(wa_profile_name), "
        "wa_chat_title = VALUES(wa_chat_title), "
        "is_named_contact = VALUES(is_named_contact), "
        "id_paciente = COALESCE(VALUES(id_paciente), id_paciente), "
        "id_atendimento = COALESCE(VALUES(id_atendimento), id_atendimento), "
        "profile_payload_json = VALUES(profile_payload_json), "
        "source = VALUES(source), "
        "last_seen_at = UTC_TIMESTAMP(), "
        "updated_at = UTC_TIMESTAMP()"
    )
    try:
        run_sql(query)
    except Exception:
        log.exception(
            "Falha ao persistir perfil do contato WhatsApp | phone=%s display='%s'",
            phone,
            display_name,
        )


def lookup_whatsapp_contact_profile(phone: str) -> Optional[Dict[str, Any]]:
    """Fetch a normalized WhatsApp contact profile from chatgpt_whatsapp."""
    norm = normalize_phone(phone)
    if not norm:
        return None
    safe_phone = _sql_escape(norm)
    query = (
        "SELECT whatsapp_phone, wa_display_name, wa_profile_name, wa_chat_title, "
        "       id_paciente, id_atendimento, is_named_contact, last_seen_at "
        "FROM chatgpt_whatsapp "
        f"WHERE whatsapp_phone = '{safe_phone}' "
        "ORDER BY id DESC LIMIT 1"
    )
    try:
        rows = run_sql(query)
        if rows:
            return rows[0]
    except Exception:
        log.exception("Falha ao buscar perfil de contato WhatsApp para phone=%s", phone)
    return None


def lookup_whatsapp_contact_by_display_name(display_name: str) -> Optional[Dict[str, Any]]:
    """Find the latest WhatsApp contact profile by saved display name."""
    norm_name = re.sub(r"\s+", " ", str(display_name or "").strip())
    if len(norm_name) < 2:
        return None
    safe_name = _sql_escape(norm_name)
    query = (
        "SELECT whatsapp_phone, wa_display_name, wa_profile_name, wa_chat_title, "
        "       id_paciente, id_atendimento, is_named_contact, last_seen_at "
        "FROM chatgpt_whatsapp "
        f"WHERE wa_display_name = '{safe_name}' "
        "ORDER BY id DESC LIMIT 1"
    )
    try:
        rows = run_sql(query)
        if rows:
            return rows[0]
    except Exception:
        log.exception("Falha ao buscar perfil WhatsApp por nome='%s'", norm_name)
    return None


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
        "       url_chatgpt, whatsapp_paciente, mensagens "
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


def was_message_already_sent_for_analise(id_analise: Any, message_text: str) -> bool:
    """Check if a follow-up message was already sent for a given analysis.

    Looks up chatgpt_chats by id_chatgpt_atendimentos_analise and checks
    whether the message text already exists in the mensagens JSON column.
    Returns True if the message is found (i.e. already sent), False otherwise.
    """
    if not id_analise:
        return False
    query = (
        "SELECT mensagens FROM chatgpt_chats "
        f"WHERE chat_mode = 'whatsapp' "
        f"  AND id_chatgpt_atendimentos_analise = {int(id_analise)} "
        "ORDER BY id DESC LIMIT 1"
    )
    try:
        rows = run_sql(query)
        if not rows:
            return False
        raw = rows[0].get("mensagens") or ""
        if not raw:
            return False
        mensagens = json.loads(raw, strict=False) if isinstance(raw, str) else raw
        if not isinstance(mensagens, list):
            return False
        for msg in mensagens:
            if not isinstance(msg, dict):
                continue
            content = (msg.get("content") or "").strip()
            if content == message_text.strip():
                return True
    except Exception:
        log.exception(
            "Falha ao verificar duplicidade de mensagem para id_analise=%s",
            id_analise,
        )
    return False


def preload_sent_messages_for_analises(id_analises: List[Any]) -> Dict[int, set]:
    """
    Carrega em lote as mensagens já registradas em chatgpt_chats.mensagens
    para os id_chatgpt_atendimentos_analise informados.

    Retorna:
      { id_analise: {conteudo_msg_1, conteudo_msg_2, ...}, ... }
    """
    normalized_ids: List[int] = []
    for raw in id_analises:
        try:
            normalized_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not normalized_ids:
        return {}

    unique_ids = sorted(set(normalized_ids))
    id_list = ",".join(str(i) for i in unique_ids)
    query = (
        "SELECT id_chatgpt_atendimentos_analise, mensagens "
        "FROM chatgpt_chats "
        "WHERE chat_mode = 'whatsapp' "
        f"  AND id_chatgpt_atendimentos_analise IN ({id_list})"
    )

    out: Dict[int, set] = {i: set() for i in unique_ids}
    try:
        rows = run_sql(query)
        for row in rows:
            try:
                aid = int(row.get("id_chatgpt_atendimentos_analise"))
            except (TypeError, ValueError):
                continue
            raw = row.get("mensagens") or ""
            if not raw:
                continue
            try:
                mensagens = json.loads(raw, strict=False) if isinstance(raw, str) else raw
            except Exception:
                continue
            if not isinstance(mensagens, list):
                continue
            bucket = out.setdefault(aid, set())
            for msg in mensagens:
                if not isinstance(msg, dict):
                    continue
                content = (msg.get("content") or "").strip()
                if content:
                    bucket.add(content)
    except Exception:
        log.exception("Falha ao pré-carregar mensagens enviadas em lote para dedupe")

    return out


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
TEST_MODE_STRICT_SINGLE_PATIENT = os.getenv("PYWA_TEST_STRICT_SINGLE_PATIENT", "1").strip().lower() not in ("0", "false", "no")


def phones_match(a: Optional[str], b: Optional[str]) -> bool:
    """Compares phones using normalized digits, tolerating country-code variants."""
    na = normalize_phone(a)
    nb = normalize_phone(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # fallback by suffix (DDD+numero) when one side came with different prefix
    return na[-10:] == nb[-10:] or na[-9:] == nb[-9:]


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
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
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

    _MAX_RECOVERY_ATTEMPTS = 3

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
        self._ready.clear()
        self._thread = threading.Thread(target=self._browser_loop, daemon=True)
        self._thread.start()
        self._ready.wait()

    # -- internal: browser launch / recovery ----------------------------------

    def _launch_browser(self) -> None:
        """Create or recreate the Playwright browser + page and navigate to
        WhatsApp Web.  Called on first start and on auto-recovery."""
        # Clean up previous instances if any
        self._close_browser_quietly()

        if self._playwright is None:
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

    def _close_browser_quietly(self) -> None:
        """Best-effort close of page / browser context (ignore errors)."""
        for resource_name, resource in [("page", self._page), ("browser", self._browser)]:
            if resource is None:
                continue
            try:
                resource.close()
            except Exception:
                log.debug("Ignorando erro ao fechar %s durante cleanup.", resource_name)
        self._page = None
        self._browser = None

    def _is_page_alive(self) -> bool:
        """Quick check whether the page is still usable."""
        if self._page is None:
            return False
        try:
            self._page.evaluate("() => true")
            return True
        except Exception:
            return False

    def _recover_browser(self) -> None:
        """Attempt to relaunch the browser after it was closed / crashed."""
        for attempt in range(1, self._MAX_RECOVERY_ATTEMPTS + 1):
            log.warning(
                "Browser/página fechado(a). Tentativa de recuperação %s/%s...",
                attempt,
                self._MAX_RECOVERY_ATTEMPTS,
            )
            try:
                self._launch_browser()
                log.info("Browser recuperado com sucesso na tentativa %s.", attempt)
                return
            except Exception:
                log.exception(
                    "Recuperação do browser falhou (tentativa %s/%s).",
                    attempt,
                    self._MAX_RECOVERY_ATTEMPTS,
                )
                backoff = min(5 * attempt, 15)
                time.sleep(backoff)
        raise RuntimeError(
            f"Não foi possível recuperar o browser após "
            f"{self._MAX_RECOVERY_ATTEMPTS} tentativas."
        )

    # -- internal: browser-owner thread main loop -----------------------------

    def _browser_loop(self) -> None:
        """Runs on the dedicated browser thread. Creates Playwright, then
        processes tasks from the queue forever."""
        try:
            self._launch_browser()
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
            except (TargetClosedError, Exception) as exc:
                is_closed = isinstance(exc, TargetClosedError) or (
                    "Target page, context or browser has been closed" in str(exc)
                    or "target page" in str(exc).lower()
                    or not self._is_page_alive()
                )
                if is_closed:
                    log.warning(
                        "Detectado browser/página fechado(a) durante operação: %s",
                        exc,
                    )
                    try:
                        self._recover_browser()
                        # Retry the failed operation once after recovery
                        try:
                            result = func()
                            future.set_result(result)
                        except Exception as retry_exc:
                            future.set_exception(retry_exc)
                    except Exception as recovery_exc:
                        future.set_exception(recovery_exc)
                else:
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
            # Type text line by line, using Shift+Enter for newlines
            # to avoid triggering message send on each line break.
            lines = text.split("\n")
            for i, line in enumerate(lines):
                if line:
                    self._page.keyboard.type(line, delay=5)
                if i < len(lines) - 1:
                    self._page.keyboard.press("Shift+Enter")
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

    def scan_chat_list_rows(self) -> List[Dict[str, Any]]:
        """Scan sidebar and return lightweight chat rows metadata.
        Returns [{title, unread_count, time_text, preview_text}]."""
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

                        const nameArea = row.querySelector('div._ak8q');
                        if (!nameArea) continue;
                        const nameSpan = nameArea.querySelector('span[title]');
                        if (!nameSpan) continue;
                        const title = (nameSpan.getAttribute('title') || '').trim();
                        if (!title) continue;

                        let timeText = '';
                        const timeCandidates = row.querySelectorAll('div._ak8i span, div[role="gridcell"] span');
                        for (const t of timeCandidates) {
                            const txt = (t.textContent || '').trim();
                            if (!txt) continue;
                            if (/^\\d{1,2}:\\d{2}$/.test(txt) || /^\\d{2}\\/\\d{2}\\/\\d{4}$/.test(txt) || /^\\d{2}\\/\\d{2}\\/\\d{2}$/.test(txt)
                                || /ontem|yesterday|hoje|today/i.test(txt)) {
                                timeText = txt;
                                break;
                            }
                        }

                        let previewText = '';
                        const previewNode = row.querySelector('div._ak8k span[title], div._ak8k span[dir="ltr"], div._ak8k span[dir="auto"]');
                        if (previewNode) {
                            previewText = (previewNode.getAttribute('title') || previewNode.textContent || '').trim();
                        }

                        results.push({
                            title: title,
                            unread_count: unread,
                            time_text: timeText,
                            preview_text: previewText
                        });
                    }
                    return results;
                }"""
            )

        return self._run_on_browser_thread(_do)

    # open_chat_by_sidebar_click definido abaixo (versão única)

    def extract_phone_from_open_chat(self) -> Optional[str]:
        """Try to extract the phone number from the currently open chat header."""
        self.start()

        def _do():
            if not getattr(self, '_page', None):
                return None

            return self._page.evaluate(
                """() => {
                    const header = document.querySelector('#main header');
                    if (!header) return null;
                    const spans = header.querySelectorAll('span[title], span[dir="auto"], span');
                    for (const span of spans) {
                        const text = (span.getAttribute('title') || span.textContent || '').trim();
                        if (!text) continue;
                        const digits = text.replace(/\\D/g, '');
                        if (digits.length >= 10 && digits.length <= 15) {
                            return digits;
                        }
                    }
                    return null;
                }"""
            )

        return self._run_on_browser_thread(_do)

    def open_chat_by_sidebar_click(self, target_title: str) -> bool:
        """
        Busca o contato na sidebar do WhatsApp Web pelo título exato e clica nele.
        Usa Playwright locator nativo para clique confiável no React.
        Retorna True se conseguiu abrir o chat, False caso contrário.
        """
        self.start()

        def _do():
            if not getattr(self, '_page', None):
                return False

            title = " ".join(target_title.split())

            # 1) Localiza o índice da row via JS (mais rápido que iterar locators)
            row_index = self._page.evaluate("""(target) => {
                const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
                const rows = document.querySelectorAll('#pane-side div[role="row"]');
                for (let i = 0; i < rows.length; i++) {
                    const span = rows[i].querySelector('span[title]');
                    if (!span) continue;
                    const txt = norm(span.getAttribute('title') || span.textContent || '');
                    if (txt === target) return i;
                }
                return -1;
            }""", title)

            if row_index < 0:
                log.warning("open_chat_by_sidebar_click: título '%s' não encontrado na sidebar", title)
                return False

            # 2) Scroll para visibilidade e clique nativo via Playwright locator
            row_locator = self._page.locator('#pane-side div[role="row"]').nth(row_index)
            try:
                row_locator.scroll_into_view_if_needed(timeout=2000)
                self._page.wait_for_timeout(200)
            except Exception:
                pass

            # Tenta clicar no gridcell ou na row inteira
            cell = row_locator.locator('div[role="gridcell"]').first
            try:
                if cell.count() > 0:
                    cell.click(timeout=3000)
                else:
                    row_locator.click(timeout=3000)
            except Exception as e:
                log.warning("open_chat_by_sidebar_click: falha no clique para '%s': %s", title, e)
                return False

            # 3) Aguarda o header do chat renderizar
            try:
                self._page.wait_for_selector('#main header', timeout=5000)
                self._page.wait_for_timeout(300)
                return True
            except Exception:
                log.warning("open_chat_by_sidebar_click: header não apareceu após clicar '%s'", title)
                return False

        return self._run_on_browser_thread(_do)

    def extract_phone_from_open_chat(self) -> Optional[str]:
        self.start()

        def _do():
            if not getattr(self, '_page', None):
                return None

            return self._page.evaluate(
                """() => {
                    const header = document.querySelector('#main header');
                    if (!header) return null;
                    const spans = header.querySelectorAll('span[title], span[dir="auto"], span');
                    for (const span of spans) {
                        const text = (span.getAttribute('title') || span.textContent || '').trim();
                        if (!text) continue;
                        const digits = text.replace(/\\D/g, '');
                        if (digits.length >= 10 && digits.length <= 15) {
                            return digits;
                        }
                    }
                    return null;
                }"""
            )

        return self._run_on_browser_thread(_do)

import re

def extract_phone_from_contact_panel(page) -> str:
    """
    Abre o painel 'Dados do contato' clicando no header do chat atual,
    lê o número de telefone contido nele, e fecha o painel em seguida.
    """
    try:
        # 1. Clica no header para abrir o painel da direita
        page.locator('#main header').click(timeout=2000)
        
        # 2. Aguarda a animação do painel abrir (qualquer div/aside que carregue a info)
        page.wait_for_timeout(1500)
        
        # 3. Extrai o telefone injetando o avaliador no browser
        phone = page.evaluate("""() => {
            const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
            const onlyDigits = (s) => String(s || '').replace(/\\D/g, '');
            const maybePhone = (s) => {
                const m = String(s || '').match(/\\+?\\d[\\d\\s()-]{7,}/);
                if (!m) return '';
                const d = onlyDigits(m[0]);
                return d.length >= 10 && d.length <= 15 ? d : '';
            };

            // Busca nós que contêm texto selecionável, spans ou parágrafos
            const nodes = document.querySelectorAll('[data-testid="selectable-text"], span[dir="auto"], span, div, p');
            for (const n of nodes) {
                // Pega apenas elementos dentro de um container lateral (aside/section)
                const candidate = n.closest('section, aside, div[role="dialog"], div[role="region"], div[data-testid="contact-info-drawer"]');
                if (candidate) {
                    const txt = norm(n.textContent || '');
                    const p = maybePhone(txt);
                    if (p) return p;
                }
            }
            return '';
        }""")
        
        # 4. Fecha o painel (ESC) para manter a UI limpa
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        
        return phone or ""
    except Exception as e:
        print(f"[WARNING] Erro ao tentar ler painel de contato: {e}")
        return ""


def open_chat_by_sidebar_click(page, target_title: str) -> bool:
    """
    Busca o contato na sidebar do WhatsApp Web pelo título exato e clica nele.
    Retorna True se conseguiu clicar, False caso contrário.
    """
    target_title = " ".join(target_title.split())  # normaliza espaços
    
    clicked = page.evaluate("""(target) => {
        const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
        const rows = Array.from(document.querySelectorAll('#pane-side div[role="row"]'));
        
        for (const row of rows) {
            const span = row.querySelector('div._ak8q span[title], span[title]');
            const txt = norm(span?.getAttribute?.('title') || span?.textContent || '');
            
            if (txt && txt === target) {
                row.scrollIntoView({ block: 'center' });
                // Encontra a área clicável exata dentro da linha
                const clickable = row.querySelector('div[role="gridcell"]') || row;
                
                // Dispara mousedown e click nativo para o React entender
                clickable.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                clickable.click();
                return true;
            }
        }
        return false;
    }""", target_title)
    
    if clicked:
        # Aguarda o header do chat principal renderizar para confirmar abertura
        try:
            page.wait_for_selector('#main header', timeout=3000)
            return True
        except:
            return False
            
    return False

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

    def get_open_chat_identity(self) -> Dict[str, str]:
        """Return current open chat header identity {title, phone} when possible."""
        self.start()

        def _do():
            return self._page.evaluate(
                """() => {
                    const header = document.querySelector('#main header');
                    if (!header) return { title: '', phone: '' };

                    let title = '';
                    const titleEl = header.querySelector('span[title]');
                    if (titleEl) title = (titleEl.getAttribute('title') || '').trim();
                    if (!title) {
                        const auto = header.querySelector('span[dir="auto"]');
                        if (auto) title = (auto.textContent || '').trim();
                    }

                    let phone = '';
                    const spans = header.querySelectorAll('span[title], span[dir="auto"], span');
                    for (const s of spans) {
                        const txt = (s.getAttribute('title') || s.textContent || '').trim();
                        if (!txt) continue;
                        const digits = txt.replace(/\\D/g, '');
                        if (digits.length >= 10 && digits.length <= 15) {
                            phone = digits;
                            break;
                        }
                    }
                    return { title, phone };
                }"""
            )

        result = self._run_on_browser_thread(_do) or {}
        return {
            "title": str(result.get("title") or "").strip(),
            "phone": normalize_phone(result.get("phone")) or "",
        }

    def get_open_contact_details(self) -> Dict[str, str]:
        """Open contact details panel and extract visible profile information.

        Returns:
          {title, phone, profile_name, profile_phone}
        """
        self.start()

        def _do():
            # Fecha possível painel já aberto e volta ao chat.
            try:
                self._page.keyboard.press("Escape")
                self._page.wait_for_timeout(150)
            except Exception:
                pass

            header = self._page.locator("#main header").first
            if header.count() == 0:
                return {"title": "", "phone": "", "profile_name": "", "profile_phone": ""}

            # Captura identidade visível no header ANTES de abrir o painel.
            base = self._page.evaluate(
                """() => {
                    const pickPhone = (txt) => {
                        if (!txt) return '';
                        const m = String(txt).match(/\\+?\\d[\\d\\s\\-()]{7,}/);
                        return m ? m[0].replace(/\\D/g, '') : '';
                    };
                    const header = document.querySelector('#main header');
                    if (!header) return { title: '', phone: '' };
                    const titleEl = header.querySelector('span[title]') || header.querySelector('span[dir="auto"]');
                    const title = (titleEl?.getAttribute?.('title') || titleEl?.textContent || '').trim();
                    let phone = '';
                    for (const s of header.querySelectorAll('span[title], span[dir=\"auto\"], span')) {
                        const txt = (s.getAttribute?.('title') || s.textContent || '').trim();
                        const p = pickPhone(txt);
                        if (p.length >= 10) { phone = p; break; }
                    }
                    return { title, phone };
                }"""
            ) or {}

            try:
                header.click(timeout=3000)
            except Exception:
                return {
                    "title": str(base.get("title") or "").strip(),
                    "phone": str(base.get("phone") or "").strip(),
                    "profile_name": "",
                    "profile_phone": "",
                }

            # Aguarda o painel abrir — tenta múltiplos seletores
            panel_visible = False
            panel_selectors = [
                '[data-testid="contact-info-drawer"]',
                'div[aria-label="Dados do contato"]',
                'div[aria-label="Contact info"]',
                'aside[aria-label="Dados do contato"]',
                'aside[aria-label="Contact info"]',
            ]
            for sel in panel_selectors:
                try:
                    self._page.wait_for_selector(sel, timeout=2000)
                    panel_visible = True
                    break
                except Exception:
                    continue
            if not panel_visible:
                try:
                    self._page.wait_for_selector("text=/Dados do contato|Contact info|Informações do contato/i", timeout=3000)
                    panel_visible = True
                except Exception:
                    panel_visible = False

            # Pausa extra para o painel carregar seus dados
            self._page.wait_for_timeout(800)

            data = self._page.evaluate(
                """() => {
                    const pickPhone = (txt) => {
                        if (!txt) return '';
                        const m = String(txt).match(/\\+?\\d[\\d\\s\\-()]{7,}/);
                        if (!m) return '';
                        const digits = m[0].replace(/\\D/g, '');
                        return (digits.length >= 10 && digits.length <= 15) ? digits : '';
                    };
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        if (!style) return false;
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };
                    const out = { profile_name: '', profile_phone: '', _debug: '' };
                    const isNoiseText = (txt) => {
                        const lower = String(txt || '').trim().toLowerCase();
                        if (!lower) return true;
                        return (
                            lower.includes('dados do contato')
                            || lower.includes('contact info')
                            || lower.includes('informações do contato')
                            || lower.includes('mídia')
                            || lower.includes('media')
                            || lower.includes('mensagens favoritas')
                            || lower.includes('silenciar')
                            || lower.includes('wa-wordmark')
                            || lower.includes('meta ai')
                        );
                    };

                    // === Estratégia 1: data-testid do painel de contato ===
                    let panelRoot = document.querySelector('[data-testid="contact-info-drawer"]');

                    // === Estratégia 2: aria-label ===
                    if (!panelRoot) {
                        panelRoot =
                            document.querySelector('div[aria-label="Dados do contato"]')
                            || document.querySelector('div[aria-label="Contact info"]')
                            || document.querySelector('aside[aria-label="Dados do contato"]')
                            || document.querySelector('aside[aria-label="Contact info"]');
                    }

                    // === Estratégia 3: heading textual ===
                    if (!panelRoot) {
                        const heading = Array.from(
                            document.querySelectorAll('h1, h2, div[role="heading"], span[dir="auto"], span')
                        ).find((n) => /dados do contato|contact info|informações do contato/i.test((n.textContent || '').trim()));
                        if (heading) {
                            const candidate = heading.closest('section, aside, div[role="dialog"], div[role="region"], div[class]');
                            if (candidate && isVisible(candidate)) {
                                panelRoot = candidate;
                            }
                        }
                    }

                    // === Estratégia 4: qualquer painel à direita com texto de telefone ===
                    if (!panelRoot) {
                        const phoneNodes = Array.from(
                            document.querySelectorAll('[data-testid="selectable-text"], span[dir="auto"], span')
                        ).filter((n) => !!pickPhone((n.textContent || '').trim()));
                        for (const n of phoneNodes) {
                            const candidate = n.closest('section, aside, div[role="dialog"], div[role="region"], div[class]');
                            if (!candidate || !isVisible(candidate)) continue;
                            const rect = candidate.getBoundingClientRect();
                            if (rect.width < 150 || rect.height < 100) continue;
                            // Aceita qualquer posição (removido filtro 0.45)
                            panelRoot = candidate;
                            break;
                        }
                    }

                    if (!panelRoot) {
                        out._debug = 'panelRoot not found';
                        return out;
                    }
                    out._debug = 'panelRoot=' + (panelRoot.tagName || '?') +
                        ' testid=' + (panelRoot.getAttribute('data-testid') || '') +
                        ' aria=' + (panelRoot.getAttribute('aria-label') || '') +
                        ' size=' + panelRoot.getBoundingClientRect().width + 'x' + panelRoot.getBoundingClientRect().height;

                    // --- Nome do perfil ---
                    const preferredName = Array.from(
                        panelRoot.querySelectorAll('h1, h2, div[role="heading"], span[dir="auto"], span[title]')
                    ).find((n) => {
                        const txt = (n.getAttribute?.('title') || n.textContent || '').trim();
                        if (!txt) return false;
                        if (!isVisible(n)) return false;
                        if (isNoiseText(txt)) return false;
                        return txt.length >= 3;
                    });
                    if (preferredName) out.profile_name = (preferredName.getAttribute?.('title') || preferredName.textContent || '').trim();

                    if (!out.profile_name) {
                        const candidates = panelRoot.querySelectorAll('span[dir="auto"], div[role="heading"]');
                        for (const c of candidates) {
                            const txt = (c.getAttribute?.('title') || c.textContent || '').trim();
                            if (!txt) continue;
                            if (!isVisible(c)) continue;
                            if (isNoiseText(txt)) continue;
                            out.profile_name = txt;
                            break;
                        }
                    }

                    // --- Telefone: busca em data-testid, cell-frame, selectable-text ---
                    // Prioridade 1: cell-frame-container (onde WA mostra o telefone)
                    const cellFrames = panelRoot.querySelectorAll('[data-testid="cell-frame-container"], [data-testid="cell-frame-secondary"]');
                    for (const cf of cellFrames) {
                        if (!isVisible(cf)) continue;
                        const txt = (cf.textContent || '').trim();
                        const p = pickPhone(txt);
                        if (p.length >= 10) { out.profile_phone = p; break; }
                    }

                    // Prioridade 2: selectable-text
                    if (!out.profile_phone) {
                        const selectable = panelRoot.querySelectorAll('[data-testid="selectable-text"]');
                        for (const t of selectable) {
                            if (!isVisible(t)) continue;
                            const txt = (t.textContent || '').trim();
                            if (!txt) continue;
                            const p = pickPhone(txt);
                            if (p.length >= 10) { out.profile_phone = p; break; }
                        }
                    }

                    // Prioridade 3: varredura ampla de spans
                    if (!out.profile_phone) {
                        const texts = panelRoot.querySelectorAll('span, div, p');
                        for (const t of texts) {
                            if (!isVisible(t)) continue;
                            const txt = (t.textContent || '').trim();
                            if (!txt) continue;
                            const p = pickPhone(txt);
                            if (p.length >= 10) { out.profile_phone = p; break; }
                        }
                    }

                    // Prioridade 4: busca fora do panelRoot restrito — qualquer
                    // nó visível na página que contenha telefone e esteja em
                    // container lateral/drawer
                    if (!out.profile_phone) {
                        const allNodes = document.querySelectorAll(
                            '[data-testid="cell-frame-container"] span, ' +
                            'aside span[dir="auto"], ' +
                            'section span[dir="auto"], ' +
                            '[data-testid="contact-info-drawer"] span'
                        );
                        for (const n of allNodes) {
                            if (!isVisible(n)) continue;
                            const txt = (n.textContent || '').trim();
                            const p = pickPhone(txt);
                            if (p.length >= 10) { out.profile_phone = p; break; }
                        }
                    }

                    return out;
                }"""
            ) or {}

            try:
                self._page.keyboard.press("Escape")
                self._page.wait_for_timeout(150)
            except Exception:
                pass

            _debug = str(data.get("_debug") or "")
            result = {
                "title": str(base.get("title") or "").strip(),
                "phone": str(base.get("phone") or "").strip(),
                "profile_name": str(data.get("profile_name") or "").strip(),
                "profile_phone": str(data.get("profile_phone") or "").strip(),
            }
            if not result["profile_name"] and result["title"] and _phone_from_title(result["title"]) is None:
                result["profile_name"] = result["title"]
            if not panel_visible:
                log.warning(
                    "Painel Dados do contato não ficou visível ao extrair detalhes | base=%s",
                    json.dumps(result, ensure_ascii=False),
                )
            log.info(
                "get_open_contact_details resultado | title='%s' phone='%s' profile_phone='%s' panel_visible=%s debug=%s",
                result.get("title", ""), result.get("phone", ""),
                result.get("profile_phone", ""), panel_visible, _debug,
            )
            return result

        result = self._run_on_browser_thread(_do) or {}
        return {
            "title": str(result.get("title") or "").strip(),
            "phone": normalize_phone(result.get("phone")) or "",
            "profile_name": str(result.get("profile_name") or "").strip(),
            "profile_phone": normalize_phone(result.get("profile_phone")) or "",
        }


wa_web = WhatsAppWebClient()
_LAST_SIDEBAR_ENRICHMENT_TS = 0.0


def _is_named_chat_title(title: str) -> bool:
    """True when the title looks like a saved contact name (not plain number)."""
    if not title:
        return False
    return _phone_from_title(title) is None

def _log_cycle_summary(cycle_no: int) -> Dict[str, int]:
    """Query DB for an overview of eligible follow-ups and log a summary."""
    summary = {"total_elegiveis": 0, "faixa_1_semana": 0, "faixa_1_mes": 0, "faixa_pre_retorno": 0}
    try:
        if TEST_ONLY_ID_PACIENTE is not None:
            # Força resumo consistente com o filtro de teste, independente de SQL customizado por env.
            rows = run_sql(FETCH_SQL)
            rows = [r for r in rows if str(r.get("id_paciente")) == str(TEST_ONLY_ID_PACIENTE)]
            summary["total_elegiveis"] = len(rows)
            for row in rows:
                dias = int(row.get("dias_desde_atendimento") or 0)
                if 5 <= dias <= 21:
                    summary["faixa_1_semana"] += 1
                if 25 <= dias <= 50:
                    summary["faixa_1_mes"] += 1
                if 50 <= dias <= 90:
                    summary["faixa_pre_retorno"] += 1
        else:
            rows = run_sql(SUMMARY_SQL)
            if rows:
                r = rows[0]
                summary["total_elegiveis"] = int(r.get("total_elegiveis") or 0)
                summary["faixa_1_semana"] = int(r.get("faixa_1_semana") or 0)
                summary["faixa_1_mes"] = int(r.get("faixa_1_mes") or 0)
                summary["faixa_pre_retorno"] = int(r.get("faixa_pre_retorno") or 0)
    except Exception:
        log.exception("Falha ao obter resumo de elegíveis")

    log.info("── Ciclo #%s - Resumo acompanhamento WhatsApp %s", cycle_no, "─" * 24)
    if TEST_ONLY_ID_PACIENTE is not None:
        log.info("   [TESTE] Filtro ativo: apenas id_paciente=%s", TEST_ONLY_ID_PACIENTE)
    log.info("   Pacientes elegiveis (5-90 dias) : %s", summary["total_elegiveis"])
    log.info("   Faixa 1 semana   (5-21 dias)    : %s", summary["faixa_1_semana"])
    log.info("   Faixa 1 mes      (25-50 dias)   : %s", summary["faixa_1_mes"])
    log.info("   Faixa pre-retorno (50-90 dias)   : %s", summary["faixa_pre_retorno"])
    return summary


def send_pending_followups_once(cycle_no: int) -> Dict[str, Any]:
    # ── Resumo inicial ────────────────────────────────────────────────────
    cycle_summary = _log_cycle_summary(cycle_no)

    if cycle_summary["total_elegiveis"] == 0:
        log.info("   Nenhum paciente elegivel neste ciclo.")
        return {
            "total": 0, "total_followup_items": 0, "sent": 0,
            "skipped": 0, "skipped_missing_phone": 0,
            "skipped_empty_followup": 0, "skipped_already_sent": 0,
            "skipped_not_due": 0, "skipped_test_filter": 0,
            "errors": 0, "recovered_member_phone": 0,
        }

    # ── Buscar registros elegíveis (já filtrados por data no SQL) ─────────
    rows = run_sql(FETCH_SQL)
    if TEST_ONLY_ID_PACIENTE is not None:
        before = len(rows)
        rows = [r for r in rows if str(r.get("id_paciente")) == str(TEST_ONLY_ID_PACIENTE)]
        log.info(
            "[TESTE] Ciclo #%s: filtro id_paciente=%s aplicado (antes=%s, depois=%s).",
            cycle_no,
            TEST_ONLY_ID_PACIENTE,
            before,
            len(rows),
        )
    total_rows = len(rows)
    total_followup_items = 0
    sent = 0
    skipped = 0
    skipped_missing_phone = 0
    skipped_empty_followup = 0
    skipped_already_sent = 0
    skipped_not_due = 0
    skipped_test_filter = 0
    errors = 0
    recovered_member_phone = 0

    # Pré-carrega dedupe por id_analise para evitar N consultas remotas
    sent_cache = preload_sent_messages_for_analises([r.get("id_analise") for r in rows])

    # ── Montar fila de envios ─────────────────────────────────────────────
    send_queue: List[Dict[str, Any]] = []

    for idx, row in enumerate(rows, start=1):
        if idx == 1 or idx % 25 == 0 or idx == len(rows):
            log.info("Preparando fila de envios: %s/%s", idx, len(rows))

        id_atendimento = row.get("id_atendimento")
        id_paciente = row.get("id_paciente")
        id_analise = row.get("id_analise")
        nome_paciente = row.get("nome_paciente")
        chat_url = (row.get("chat_url") or "").strip()
        url_chatgpt = (row.get("url_chatgpt") or "").strip()
        dias = int(row.get("dias_desde_atendimento") or 0)
        inicio_atendimento = row.get("datetime_atendimento_inicio") or "N/D"

        # Idade: usar data_nascimento já vinda do SQL
        idade = derive_age_from_birthdate(row.get("data_nascimento"))
        if idade == "N/D":
            idade = derive_age_from_row(row)

        # Telefone
        original_phone = normalize_phone(row.get("telefone"))
        phone, phone_source = resolve_phone_with_member_fallback(row.get("telefone"), id_paciente)
        if (not original_phone or not is_valid_br_mobile_phone(original_phone)) and phone:
            recovered_member_phone += 1

        if not phone:
            skipped += 1
            skipped_missing_phone += 1
            continue

        # Modo de teste estrito: processa somente o paciente cujo telefone
        # corresponde ao telefone de destino de testes.
        if TEST_MODE_STRICT_SINGLE_PATIENT and TEST_DESTINATION_PHONE:
            if not phones_match(phone, TEST_DESTINATION_PHONE):
                skipped += 1
                skipped_test_filter += 1
                continue

        all_itens = extract_followup_items(row.get("mensagens_acompanhamento"))
        if not all_itens:
            skipped += 1
            skipped_empty_followup += 1
            continue
        total_followup_items += len(all_itens)

        # Filter: use dias_desde_atendimento (já calculado no SQL) para
        # selecionar apenas a(s) mensagem(ns) da janela correta.
        itens = select_followup_for_timing(all_itens, inicio_atendimento)
        skipped_not_due += len(all_itens) - len(itens)

        for key, pergunta in itens:
            full_msg = f"{pergunta}\n\nPode me responder por aqui?"

            # Dedupe (rápido): usa cache pré-carregado de mensagens por análise
            try:
                aid_int = int(id_analise) if id_analise is not None else None
            except (TypeError, ValueError):
                aid_int = None
            cached_sent = sent_cache.get(aid_int, set()) if aid_int is not None else set()
            if full_msg.strip() in cached_sent:
                log.info(
                    "Ignorado por ja_enviado (confirmado no SQL) | ciclo=%s id_analise=%s id_paciente=%s tipo=%s",
                    cycle_summary.get("cycle", "N/A") if isinstance(cycle_summary, dict) else "N/A",
                    aid_int,
                    id_paciente,
                    key,
                )
                # Mesmo quando já enviado, mantém contexto para o monitor de
                # respostas (caso o processo tenha reiniciado).
                existing_sent_at = state.get_phone_context_field(phone, "sent_at")
                fallback_sent_at = existing_sent_at or (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                state.set_phone_context(
                    phone,
                    {
                        "id_atendimento": id_atendimento,
                        "id_paciente": id_paciente,
                        "nome_paciente": nome_paciente,
                        "pergunta": pergunta,
                        "question_key": key,
                        "url_chatgpt": url_chatgpt,
                        "sent_at": fallback_sent_at,
                    },
                )
                skipped += 1
                skipped_already_sent += 1
                continue

            # Dedupe fallback (state local):
            # só aplica quando já existe chat WhatsApp persistido para essa análise.
            # Sem chat DB, NÃO deve bloquear o primeiro envio.
            dedupe_key = f"{id_atendimento}:{key}:{hashlib.sha1(pergunta.encode('utf-8')).hexdigest()}"
            if state.is_sent(dedupe_key):
                # Regra solicitada: "já enviado" deve estar confirmado no SQL.
                # Se estiver apenas no state local, considera stale.
                if full_msg.strip() in cached_sent:
                    existing_sent_at = state.get_phone_context_field(phone, "sent_at")
                    fallback_sent_at = existing_sent_at or (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                    state.set_phone_context(
                        phone,
                        {
                            "id_atendimento": id_atendimento,
                            "id_paciente": id_paciente,
                            "nome_paciente": nome_paciente,
                            "pergunta": pergunta,
                            "question_key": key,
                            "url_chatgpt": url_chatgpt,
                            "sent_at": fallback_sent_at,
                        },
                    )
                    skipped += 1
                    skipped_already_sent += 1
                    continue
                log.warning(
                    "State local marcava ja_enviado sem evidência no SQL. Limpando dedupe local e reenfileirando. "
                    "| id_atendimento=%s id_paciente=%s tipo=%s",
                    id_atendimento,
                    id_paciente,
                    key,
                )
                state.unmark_sent(dedupe_key)

            send_queue.append({
                "id_atendimento": id_atendimento,
                "id_paciente": id_paciente,
                "id_analise": id_analise,
                "nome_paciente": nome_paciente,
                "chat_url": chat_url,
                "url_chatgpt": url_chatgpt,
                "phone": phone,
                "phone_source": phone_source,
                "idade": idade,
                "dias": dias,
                "inicio_atendimento": inicio_atendimento,
                "key": key,
                "pergunta": pergunta,
                "full_msg": full_msg,
                "dedupe_key": dedupe_key,
            })

    # ── Log da fila antes de iniciar envios ───────────────────────────────
    log.info("── Fila de envios %s", "─" * 50)
    log.info(
        "   Total na fila: %s | Ignorados: %s (sem_tel=%s, sem_msg=%s, ja_enviado=%s, fora_janela=%s)",
        len(send_queue), skipped, skipped_missing_phone,
        skipped_empty_followup, skipped_already_sent, skipped_not_due,
    )
    for i, item in enumerate(send_queue, 1):
        log.info(
            "   #%s | %s | %s dias | %s | tipo=%s | tel=%s",
            i,
            item["nome_paciente"] or "N/D",
            item["dias"],
            item["inicio_atendimento"],
            item["key"],
            item["phone"],
        )

    if TEST_ONLY_ID_PACIENTE is not None and total_rows > 0 and len(send_queue) == 0:
        log.warning(
            "[TESTE] id_paciente=%s permanece pendente neste ciclo (sem envio efetivo). "
            "Verifique os motivos de ignorados acima.",
            TEST_ONLY_ID_PACIENTE,
        )

    # ── Executar envios (pacientes mais antigos primeiro — já ordenados) ──
    for item in send_queue:
        try:
            preview = build_preview_with_ellipsis(item["pergunta"], max_len=140)
            log.info(
                "Enviando | Paciente: [%s] | idade: [%s] | %s dias desde atendimento "
                "| Telefone: [%s] (origem=%s) | Teste: [%s] | Atend: [%s] | Data: [%s] "
                "| Tipo: [%s] | Msg: [%s]",
                item["nome_paciente"] or "N/D",
                item["idade"] or "N/D",
                item["dias"],
                item["phone"] or "N/D",
                item["phone_source"],
                TEST_DESTINATION_PHONE,
                item["id_atendimento"] if item["id_atendimento"] is not None else "N/D",
                item["inicio_atendimento"],
                item["key"],
                preview or "N/D",
            )

            # Random delay between sends to simulate human behaviour.
            if sent > 0:
                delay = random.uniform(10, 45)
                log.info("Aguardando %.1fs antes do proximo envio...", delay)
                time.sleep(delay)

            wa_web.send_message(TEST_DESTINATION_PHONE, item["full_msg"])

            # Se o contato estiver salvo por nome no WhatsApp, persiste alias
            # (nome exibido -> telefone) para o monitor correlacionar respostas.
            try:
                ident = wa_web.get_open_chat_identity()
                alias_title = (ident.get("title") or "").strip()
                alias_phone = (ident.get("phone") or "").strip() or item["phone"]
                if alias_title and alias_phone:
                    state.set_contact_alias(alias_title, alias_phone)
                    log.info("Alias contato mapeado: '%s' -> %s", alias_title, alias_phone)
            except Exception:
                log.exception("Falha ao mapear alias do contato após envio WhatsApp")

            # Captura dados de "Dados do contato" no WhatsApp e persiste em tabela dedicada.
            try:
                details = wa_web.get_open_contact_details()
                mapped_phone = (
                    details.get("profile_phone")
                    or details.get("phone")
                    or item["phone"]
                )
                _upsert_whatsapp_contact_profile(
                    phone=mapped_phone,
                    display_name=details.get("title") or item["nome_paciente"] or "",
                    profile_name=details.get("profile_name") or "",
                    wa_chat_title=details.get("title") or "",
                    id_paciente=item.get("id_paciente"),
                    id_atendimento=item.get("id_atendimento"),
                    source="send_followup",
                )
            except Exception:
                log.exception("Falha ao persistir dados do contato WhatsApp após envio")

            sent_at_iso = utc_now_iso()
            state.mark_sent(
                item["dedupe_key"],
                {
                    "id_atendimento": item["id_atendimento"],
                    "id_paciente": item["id_paciente"],
                    "phone": item["phone"],
                    "question_key": item["key"],
                    "pergunta": item["pergunta"],
                    "sent_at": sent_at_iso,
                },
            )
            state.set_phone_context(
                item["phone"],
                {
                    "id_atendimento": item["id_atendimento"],
                    "id_paciente": item["id_paciente"],
                    "nome_paciente": item["nome_paciente"],
                    "pergunta": item["pergunta"],
                    "question_key": item["key"],
                    "url_chatgpt": item["url_chatgpt"],
                    "sent_at": sent_at_iso,
                },
            )

            # Persist in chatgpt_chats with chat_mode='whatsapp' for later lookup
            insert_whatsapp_chat(
                phone=item["phone"],
                id_paciente=item["id_paciente"],
                id_atendimento=item["id_atendimento"],
                id_analise=item["id_analise"],
                chat_url=item["chat_url"] or item["url_chatgpt"],
                first_message=item["full_msg"],
            )

            sent += 1
        except Exception:
            errors += 1
            log.exception(
                "Falha no envio para %s (atendimento=%s)",
                item["phone"], item["id_atendimento"],
            )

    return {
        "total": total_rows,
        "total_followup_items": total_followup_items,
        "sent": sent,
        "skipped": skipped,
        "skipped_missing_phone": skipped_missing_phone,
        "skipped_empty_followup": skipped_empty_followup,
        "skipped_already_sent": skipped_already_sent,
        "skipped_not_due": skipped_not_due,
        "skipped_test_filter": skipped_test_filter,
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
    if stats.get("skipped_test_filter", 0):
        reasons.append(f"bloqueado por filtro de teste={stats['skipped_test_filter']}")
    if stats.get("errors", 0):
        reasons.append(f"falha ao enviar={stats['errors']}")
    return "; ".join(reasons) if reasons else "nenhum motivo classificado"


def _phone_from_title(title: str) -> Optional[str]:
    """Try to extract a phone number from a WhatsApp chat sidebar title."""
    digits = re.sub(r"\D", "", title or "")
    if len(digits) >= 10:
        return normalize_phone(digits)
    return None


def _parse_sidebar_datetime(time_text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """
    Converte o texto de horário/data exibido na lista lateral do WhatsApp
    para datetime UTC aproximado.
    Exemplos: "22:18", "05/03/2026", "Ontem".
    """
    if not time_text:
        return None
    raw = (time_text or "").strip().lower()
    now_local = now or datetime.now()

    if re.match(r"^\d{1,2}:\d{2}$", raw):
        h, m = raw.split(":")
        dt = now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            dt = datetime.strptime(raw, fmt).replace(hour=12, minute=0, second=0, microsecond=0)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    if raw in ("ontem", "yesterday"):
        dt = (now_local - timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if raw in ("hoje", "today"):
        dt = now_local.replace(hour=23, minute=59, second=0, microsecond=0)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _match_context_for_chat_title(
    title: str,
    contexts: Dict[str, Dict[str, Any]],
    aliases: Optional[Dict[str, str]] = None
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    Tenta vincular o título do chat a um contexto monitorado, sem abrir o chat.
    Prioriza match por telefone no título; fallback por nome do paciente.
    """
    if not title:
        return None

    if aliases:
        title_key = re.sub(r"\s+", " ", title.strip().lower())
        alias_phone = normalize_phone(aliases.get(title_key))
        if alias_phone:
            for phone_key, ctx in contexts.items():
                if phones_match(phone_key, alias_phone):
                    return phone_key, ctx

    phone_from_title = _phone_from_title(title)
    if phone_from_title:
        for phone_key, ctx in contexts.items():
            if phones_match(phone_key, phone_from_title):
                return phone_key, ctx

    title_norm = re.sub(r"\s+", " ", title.strip().lower())
    for phone_key, ctx in contexts.items():
        nome = (ctx or {}).get("nome_paciente") or ""
        nome_norm = re.sub(r"\s+", " ", str(nome).strip().lower())
        if not nome_norm:
            continue
        if nome_norm in title_norm or title_norm in nome_norm:
            return phone_key, ctx

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

    # 3) Lookup em tabela dedicada de contatos WhatsApp (quando o chat aparece
    # por nome salvo e o telefone não está explícito na lista).
    for candidate_phone in [phone, normalize_phone(phone_hint) if phone_hint else None]:
        if not candidate_phone:
            continue
        profile = lookup_whatsapp_contact_profile(candidate_phone)
        if not profile:
            continue
        profile_patient = profile.get("id_paciente")
        profile_atendimento = profile.get("id_atendimento")
        if profile_patient:
            try:
                rows = run_sql(
                    "SELECT caa.id AS id_analise, caa.id_atendimento, caa.id_paciente, "
                    "       caa.chat_url, m.nome AS nome_paciente "
                    "FROM chatgpt_atendimentos_analise caa "
                    "JOIN membros m ON m.id = caa.id_paciente "
                    f"WHERE caa.id_paciente = {int(profile_patient)} "
                    "  AND caa.chat_url IS NOT NULL AND caa.chat_url <> '' "
                    "  AND caa.status = 'concluido' "
                    + (f"AND caa.id_atendimento = {int(profile_atendimento)} " if profile_atendimento else "")
                    + "ORDER BY caa.id DESC LIMIT 1"
                )
                if rows:
                    row = rows[0]
                    return {
                        "id_analise": row.get("id_analise"),
                        "id_atendimento": row.get("id_atendimento"),
                        "id_paciente": row.get("id_paciente"),
                        "chat_url": (row.get("chat_url") or "").strip(),
                        "nome_paciente": row.get("nome_paciente") or profile.get("wa_display_name"),
                        "telefone": candidate_phone,
                    }
            except Exception:
                log.exception(
                    "Falha ao resolver atendimento via chatgpt_whatsapp para phone=%s",
                    candidate_phone,
                )

    # 4) Lookup por nome previamente armazenado em chatgpt_whatsapp.
    by_name_profile = lookup_whatsapp_contact_by_display_name(title)
    if by_name_profile:
        by_name_phone = normalize_phone(by_name_profile.get("whatsapp_phone"))
        if by_name_phone:
            result = lookup_atendimento_by_phone(by_name_phone)
            if result:
                result.setdefault("telefone", by_name_phone)
                return result

    # 5) Fallback: try matching by name
    result = lookup_atendimento_by_name(title)
    return result



def enrich_named_contacts_from_sidebar(
    chat_rows: List[Dict[str, Any]],
    *,
    max_per_cycle: int = 3,
    max_attempts: int = 3,
    min_interval_sec: int = 180,
) -> int:
    """Opens a few visible named chats to capture phone/profile and cache them."""
    global _LAST_SIDEBAR_ENRICHMENT_TS
    import time, re, json # garantindo imports caso falte no escopo global
    
    now_mono = time.monotonic()
    if now_mono - _LAST_SIDEBAR_ENRICHMENT_TS < float(min_interval_sec):
        log.info(
            "Enriquecimento sidebar: pulado por intervalo mínimo | delta=%.1fs < %ss",
            now_mono - _LAST_SIDEBAR_ENRICHMENT_TS,
            min_interval_sec,
        )
        return 0

    aliases = state.get_contact_aliases()
    enriched = 0
    skipped_not_named = 0
    skipped_alias_exists = 0
    skipped_open_failed = 0
    skipped_no_phone = 0
    attempts = 0
    
    log.info(
        "Enriquecimento sidebar iniciado | chats_visíveis=%s | aliases_cache=%s | max_por_ciclo=%s | max_tentativas=%s",
        len(chat_rows), len(aliases), max_per_cycle, max_attempts,
    )
    
    for row in chat_rows:
        if enriched >= max_per_cycle or attempts >= max_attempts:
            break
            
        title = (row.get("title") or "").strip()
        if not _is_named_chat_title(title):
            skipped_not_named += 1
            continue
            
        title_key = re.sub(r"\s+", " ", title.lower())
        if aliases.get(title_key):
            skipped_alias_exists += 1
            continue
            
        attempts += 1
        log.info("Enriquecimento sidebar: tentando capturar contato nomeado '%s'", title)
        
        try:
            if not wa_web.open_chat_by_sidebar_click(title):
                skipped_open_failed += 1
                log.warning("Enriquecimento sidebar: falha ao clicar na sidebar para '%s'", title)
                continue
                
            open_identity = wa_web.get_open_chat_identity()
            open_title = (open_identity.get("title") or "").strip()
            
            if not open_title:
                skipped_open_failed += 1
                log.warning("Enriquecimento sidebar: chat não abriu | alvo='%s'", title)
                continue
                
            details = wa_web.get_open_contact_details()
            resolved_phone = normalize_phone(details.get("profile_phone") or details.get("phone") or "")
            
            # === FALLBACK: re-abre painel e tenta varredura ampla via browser thread ===
            if not resolved_phone:
                log.info("Telefone não encontrado via get_open_contact_details para '%s'. Tentando fallback amplo...", title)
                try:
                    fallback_phone = wa_web.extract_phone_from_open_chat()
                    if fallback_phone:
                        resolved_phone = normalize_phone(fallback_phone)
                        log.info("Telefone capturado via fallback header: %s", resolved_phone)
                except Exception as e:
                    log.warning("Falha no fallback de telefone para '%s': %s", title, e)
            # ===============================================
            
            if not resolved_phone:
                skipped_no_phone += 1
                continue
                
            _upsert_whatsapp_contact_profile(
                phone=resolved_phone,
                display_name=details.get("title") or title,
                profile_name=details.get("profile_name") or "",
                wa_chat_title=title,
                source="sidebar_enrichment",
            )
            
            state.set_contact_alias(title, resolved_phone)
            log.info("Enriquecimento sidebar: contato nomeado '%s' -> %s", title, resolved_phone)
            enriched += 1
            
        except Exception as e:
            log.exception("Falha no enriquecimento para '%s': %s", title, e)

    log.info(
        "Enriquecimento sidebar concluído | enriched=%s open_failed=%s no_phone=%s "
        "not_named=%s alias_exists=%s attempts=%s",
        enriched, skipped_open_failed, skipped_no_phone,
        skipped_not_named, skipped_alias_exists, attempts,
    )

    if enriched > 0:
        _LAST_SIDEBAR_ENRICHMENT_TS = time.monotonic()
    else:
        retry_backoff_sec = min(20, max(5, int(min_interval_sec)))
        _LAST_SIDEBAR_ENRICHMENT_TS = time.monotonic() - max(0, min_interval_sec - retry_backoff_sec)

    return enriched


def process_incoming_replies_once() -> Dict[str, int]:
    """Scan WhatsApp sidebar for unread chats, resolve each to a
    chatgpt_atendimentos_analise record, forward the patient reply to
    the ChatGPT simulator via chat_url, and reply back."""
    processed = 0
    skipped = 0
    no_match = 0

    # 1) Scan sidebar completo e pré-filtra por chats monitorados cujo
    # timestamp visível é posterior ao envio da pergunta.
    chat_rows = wa_web.scan_chat_list_rows()
    if not chat_rows:
        return {"processed": 0, "skipped": 0, "no_match": 0}
    log.info("Scan sidebar WhatsApp: %s chats visíveis.", len(chat_rows))

    # Mesmo sem envio recente, enriquece alguns contatos nomeados para
    # materializar mapeamentos nome->telefone na tabela dedicada.
    try:
        enrich_named_contacts_from_sidebar(chat_rows, max_per_cycle=3, min_interval_sec=180)
    except Exception:
        log.exception("Falha no enriquecimento preventivo de contatos nomeados")

    contexts = state.all_phone_contexts()
    aliases = state.get_contact_aliases()
    log.info(
        "Monitor replies: contextos_monitorados=%s | aliases_cache=%s",
        len(contexts),
        len(aliases),
    )
    candidates: List[Dict[str, Any]] = []
    now_local = datetime.now()
    skipped_not_matched = 0
    skipped_no_time = 0
    skipped_before_sent = 0
    skipped_no_recent_signal = 0
    for chat in chat_rows:
        title = (chat.get("title") or "").strip()
        if not title:
            continue
        matched = _match_context_for_chat_title(title, contexts, aliases=aliases)
        if not matched:
            skipped_not_matched += 1
            continue
        phone_key, ctx = matched
        sent_at_raw = (ctx or {}).get("sent_at") or ""
        sent_at = _parse_datetime(sent_at_raw)
        list_dt = _parse_sidebar_datetime(chat.get("time_text") or "", now=now_local)
        if not list_dt:
            skipped_no_time += 1
            continue
        if sent_at:
            if list_dt <= sent_at:
                skipped_before_sent += 1
                continue
        else:
            # Fallback de compatibilidade: sem sent_at persistido, considera
            # somente chats com sinal de atividade recente (unread/preview).
            if int(chat.get("unread_count") or 0) <= 0 and not (chat.get("preview_text") or "").strip():
                skipped_no_recent_signal += 1
                continue
        candidates.append({
            "title": title,
            "phone_key": phone_key,
            "ctx": ctx,
            "time_text": chat.get("time_text") or "",
            "unread_count": int(chat.get("unread_count") or 0),
            "preview_text": chat.get("preview_text") or "",
        })

    if not candidates:
        log.info(
            "Monitor replies: nenhum candidato após filtros | total_chats=%s | "
            "skip_not_matched=%s | skip_no_time=%s | skip_before_sent=%s | skip_no_recent_signal=%s",
            len(chat_rows),
            skipped_not_matched,
            skipped_no_time,
            skipped_before_sent,
            skipped_no_recent_signal,
        )
        return {"processed": 0, "skipped": 0, "no_match": 0}

    log.info(
        "Chats candidatos por data (msg após envio): %s",
        " | ".join(
            f"[{c['title']}]({c.get('time_text') or 'sem_hora'}, unread={c.get('unread_count', 0)})"
            for c in candidates
        ),
    )

    for chat in candidates:
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

            phone = atendimento.get("telefone") or phone_hint or chat.get("phone_key") or _phone_from_title(title)
            if phone:
                state.set_contact_alias(title, phone)

            # Atualiza snapshot do contato da sidebar/painel de dados do contato.
            try:
                details = wa_web.get_open_contact_details()
                resolved_phone = (
                    details.get("profile_phone")
                    or details.get("phone")
                    or phone
                    or chat.get("phone_key")
                )
                _upsert_whatsapp_contact_profile(
                    phone=resolved_phone,
                    display_name=details.get("title") or title,
                    profile_name=details.get("profile_name") or "",
                    wa_chat_title=title,
                    id_paciente=atendimento.get("id_paciente"),
                    id_atendimento=atendimento.get("id_atendimento"),
                    source="monitor_incoming",
                )
                if resolved_phone and title:
                    state.set_contact_alias(title, resolved_phone)
            except Exception:
                log.exception("Falha ao atualizar snapshot de contato para chat '%s'", title)
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
    cycle_no = 0
    while True:
        cycle_no += 1
        try:
            stats = send_pending_followups_once(cycle_no)
            motivos = _build_skip_reason_summary(stats)
            log.info(
                "Envio acompanhamento | total=%s itens=%s enviados=%s ignorados=%s "
                "(sem_telefone=%s, sem_mensagem=%s, ja_enviado=%s, fora_janela=%s, filtro_teste=%s, erros=%s, recuperado_membros=%s, motivos=%s)",
                stats["total"],
                stats["total_followup_items"],
                stats["sent"],
                stats["skipped"],
                stats["skipped_missing_phone"],
                stats["skipped_empty_followup"],
                stats["skipped_already_sent"],
                stats["skipped_not_due"],
                stats.get("skipped_test_filter", 0),
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
    return jsonify({"ok": True, **send_pending_followups_once(cycle_no=0)})


@app.post("/process-replies-now")
def process_replies_now():
    return jsonify({"ok": True, **process_incoming_replies_once()})


if __name__ == "__main__":
    log.info("Modo isolado ativo (sem Meta Cloud API).")
    log.info("WhatsApp Web: %s", WHATSAPP_WEB_URL)
    log.info("Simulator local: %s", SIMULATOR_URL)
    log.info("PHP remoto: %s", PHP_URL)
    log.info("Modo teste ativo: todos os envios serão direcionados para %s", TEST_DESTINATION_PHONE)
    log.info("Filtro teste estrito (somente paciente do telefone de teste): %s", TEST_MODE_STRICT_SINGLE_PATIENT)

    log.info("Iniciando browser WhatsApp. Se necessário, faça login via QR Code...")
    wa_web.start()

    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=replies_loop, daemon=True).start()

    log.info("Servidor de acompanhamento WhatsApp Web em %s:%s", HOST, PORT)
    app.run(host=HOST, port=PORT)
