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
import unicodedata
import sys
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("acompanhamento_whatsapp.py")

# Logger dedicado para enriquecimento — exibe [Associar_nome_contato_ao_numero] no log
_elog_handler = logging.StreamHandler()
_elog_handler.setFormatter(logging.Formatter("%(asctime)s [Associar_nome_contato_ao_numero] %(message)s"))
elog = logging.getLogger("enrich_contact_phone")
elog.setLevel(logging.INFO)
elog.addHandler(_elog_handler)
elog.propagate = False  # não duplicar no root logger

app = Flask(__name__)

ANSI_RESET = "\033[0m"
ANSI_CYAN = "\033[36m"
ANSI_GREEN = "\033[32m"


def log_table(title: str, rows: List[Tuple[str, str]]) -> None:
    """Renderiza logs em tabela compacta/colorida para facilitar leitura no CMD."""
    safe_rows = rows or [("info", "-")]
    k_w = max(len(k) for k, _ in safe_rows)
    v_w = max(len(v) for _, v in safe_rows)
    width = max(len(title) + 4, k_w + v_w + 7)
    top = f"╔{'═' * width}╗"
    head = f"║ {title.ljust(width - 2)} ║"
    sep = f"╠{'═' * (k_w + 2)}╦{'═' * (width - k_w - 3)}╣"
    log.info("%s%s%s", ANSI_CYAN, top, ANSI_RESET)
    log.info("%s%s%s", ANSI_CYAN, head, ANSI_RESET)
    log.info("%s%s%s", ANSI_CYAN, sep, ANSI_RESET)
    for key, value in safe_rows:
        row = f"║ {key.ljust(k_w)} ║ {value.ljust(width - k_w - 6)} ║"
        log.info("%s%s%s", ANSI_GREEN, row, ANSI_RESET)
    bottom = f"╚{'═' * width}╝"
    log.info("%s%s%s", ANSI_CYAN, bottom, ANSI_RESET)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_contact_name(name: str) -> str:
    """Normalize contact name: lowercase, keep only letters/digits/spaces, collapse whitespace.

    Removes emojis, punctuation and symbols while keeping accented chars (á, ç, etc.).
    """
    name = (name or "").strip().lower()
    # Keep only letters (including accented), digits, and spaces
    cleaned = "".join(
        c for c in name
        if unicodedata.category(c)[0] in ("L", "N") or c == " "
    )
    return re.sub(r"\s+", " ", cleaned).strip()


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
            "enrichment_failures": {},
            "official_wa_accounts": [],
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

    def record_enrichment_failure(self, title_key: str, reason: str = "") -> None:
        """Record a failed enrichment attempt for a contact title."""
        with self.lock:
            failures = self.state.setdefault("enrichment_failures", {})
            entry = failures.get(title_key)
            if not entry or not isinstance(entry, dict):
                entry = {"count": 0, "first_at": utc_now_iso(), "last_at": "", "last_reason": ""}
            entry["count"] = entry.get("count", 0) + 1
            entry["last_at"] = utc_now_iso()
            entry["last_reason"] = reason or entry.get("last_reason", "")
            failures[title_key] = entry
        self.save()

    def get_enrichment_failures(self) -> Dict[str, Dict[str, Any]]:
        """Return all enrichment failure entries."""
        with self.lock:
            return dict(self.state.get("enrichment_failures", {}))

    def should_skip_enrichment(self, title_key: str, max_failures: int = 3, cooldown_hours: int = 24) -> bool:
        """True if this contact has failed enrichment too many times recently."""
        with self.lock:
            entry = self.state.get("enrichment_failures", {}).get(title_key)
            if not entry or not isinstance(entry, dict):
                return False
            count = entry.get("count", 0)
            if count < max_failures:
                return False
            # After max_failures, skip for cooldown_hours
            last_at = entry.get("last_at", "")
            if last_at:
                try:
                    last_dt = datetime.fromisoformat(last_at)
                    if datetime.now(timezone.utc) - last_dt < timedelta(hours=cooldown_hours):
                        return True
                except (ValueError, TypeError):
                    pass
            # Cooldown expired — reset counter
            entry["count"] = 0
            return False

    def clear_enrichment_failure(self, title_key: str) -> None:
        """Clear failure count when enrichment succeeds."""
        with self.lock:
            failures = self.state.get("enrichment_failures", {})
            if title_key in failures:
                del failures[title_key]
        self.save()

    def reset_all_enrichment_failures(self) -> int:
        """Reset all enrichment failure counters. Returns number of entries cleared."""
        with self.lock:
            failures = self.state.get("enrichment_failures", {})
            count = len(failures)
            if count > 0:
                self.state["enrichment_failures"] = {}
        if count > 0:
            self.save()
        return count

    def mark_official_wa_account(self, title_key: str) -> None:
        """Mark a contact as an official WhatsApp account (no phone number)."""
        with self.lock:
            accounts = self.state.setdefault("official_wa_accounts", [])
            if title_key not in accounts:
                accounts.append(title_key)
        self.save()

    def is_official_wa_account(self, title_key: str) -> bool:
        """Check if a contact is marked as official WhatsApp account."""
        with self.lock:
            return title_key in self.state.get("official_wa_accounts", [])

    def get_official_wa_accounts(self) -> List[str]:
        """Return list of official WhatsApp account title keys."""
        with self.lock:
            return list(self.state.get("official_wa_accounts", []))


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


def _sql_utf8mb4_literal(value: Any) -> str:
    """Build a utf8mb4 SQL string literal via hex payload.

    This prevents free-text content from interfering with SQL parser/filters.
    """
    raw = str(value or "")
    return f"CONVERT(0x{raw.encode('utf-8').hex()} USING utf8mb4)"


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
    # Normalize names: lowercase, no emojis/special chars
    norm_display = _normalize_contact_name(display_name)
    norm_chat_title = _normalize_contact_name(wa_chat_title)
    safe_display_name = _sql_escape(norm_display)
    safe_profile_name = _sql_escape(profile_name)
    safe_chat_title = _sql_escape(norm_chat_title)
    safe_source = _sql_escape(source)
    profile_json = json.dumps(
        {
            "captured_at_utc": utc_now_iso(),
            "display_name": norm_display or "",
            "profile_name": profile_name or "",
            "wa_chat_title": norm_chat_title or "",
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

    # Busca correspondência na tabela membros pelo telefone
    norm_phone = normalize_phone(phone)
    if norm_phone and id_paciente_sql == "NULL":
        matched_ids = _find_membros_by_phone(norm_phone)
        if matched_ids:
            # Salva os ids encontrados como id_paciente (lista separada por vírgula)
            ids_str = ",".join(str(mid) for mid in matched_ids)
            try:
                run_sql(
                    f"UPDATE chatgpt_whatsapp "
                    f"SET id_paciente = '{_sql_escape(ids_str)}', updated_at = UTC_TIMESTAMP() "
                    f"WHERE whatsapp_phone = '{_sql_escape(norm_phone)}'"
                )
                elog.info(
                    "Correlação membros encontrada: phone=%s → id_paciente=%s",
                    norm_phone, ids_str,
                )
            except Exception:
                log.exception("Falha ao atualizar id_paciente para phone=%s", norm_phone)


def _find_membros_by_phone(phone: str) -> List[int]:
    """Search membros table by telefone1/telefone2 matching the given phone.

    membros.telefone format is "(81) 99729-2372". We normalize both sides
    to digits-only for comparison.
    """
    norm = re.sub(r"\D", "", phone)
    if len(norm) < 10:
        return []
    # Try matching the last 10-11 digits (without country code)
    # to handle both +55 and without
    suffix = norm[-11:] if len(norm) >= 11 else norm[-10:]
    try:
        rows = run_sql(
            "SELECT id, telefone1, telefone2 FROM membros "
            "WHERE telefone1 IS NOT NULL AND telefone1 <> '' "
            "   OR telefone2 IS NOT NULL AND telefone2 <> ''"
        )
        matched = []
        for row in (rows or []):
            for col in ("telefone1", "telefone2"):
                val = row.get(col) or ""
                val_digits = re.sub(r"\D", "", val)
                if not val_digits or len(val_digits) < 10:
                    continue
                val_suffix = val_digits[-11:] if len(val_digits) >= 11 else val_digits[-10:]
                if val_suffix == suffix:
                    mid = int(row["id"])
                    if mid not in matched:
                        matched.append(mid)
        return matched
    except Exception:
        log.exception("Falha ao buscar membros por telefone=%s", phone)
        return []


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
    """Find the latest WhatsApp contact profile by saved display name.

    Searches using normalized name (lowercase, no special chars) to match
    how names are stored by _upsert_whatsapp_contact_profile.
    """
    norm_name = _normalize_contact_name(display_name)
    if len(norm_name) < 2:
        return None
    safe_name = _sql_escape(norm_name)
    query = (
        "SELECT whatsapp_phone, wa_display_name, wa_profile_name, wa_chat_title, "
        "       id_paciente, id_atendimento, is_named_contact, last_seen_at "
        "FROM chatgpt_whatsapp "
        f"WHERE wa_display_name = '{safe_name}' OR wa_chat_title = '{safe_name}' "
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
    )
    try:
        rows = run_sql(
            "SELECT id, mensagens FROM chatgpt_chats "
            f"WHERE whatsapp_paciente = '{safe_phone}' AND chat_mode = 'whatsapp' "
            "ORDER BY id DESC LIMIT 1"
        )
        if not rows:
            return
        chat_id = int(rows[0]["id"])
        raw = rows[0].get("mensagens")
        existing: List[Dict[str, Any]] = []
        if isinstance(raw, str) and raw.strip():
            try:
                existing = json.loads(raw, strict=False)
            except Exception:
                existing = []
        elif isinstance(raw, list):
            existing = raw
        if not isinstance(existing, list):
            existing = []

        existing.append(json.loads(new_msg))
        payload = json.dumps(existing, ensure_ascii=False)
        query = (
            "UPDATE chatgpt_chats SET "
            f"mensagens = {_sql_utf8mb4_literal(payload)}, "
            "datetime_atualizacao = NOW() "
            f"WHERE id = {chat_id}"
        )
        run_sql(query)
    except Exception:
        log.exception("Falha ao atualizar mensagens do chat WhatsApp para phone=%s", phone)


def sync_whatsapp_messages_to_db(
    phone: str,
    wa_messages: List[Dict[str, str]],
) -> int:
    """Sync visible WhatsApp messages into chatgpt_chats.mensagens.

    Reads the existing mensagens JSON from the DB, compares with the WhatsApp
    messages list, and appends any messages not already stored.

    Uses text content + direction as fingerprint to avoid duplicates.
    Returns the number of new messages appended.

    Args:
        phone: normalized phone number (digits only).
        wa_messages: list of {text, direction, time_text, id} from WhatsApp DOM.
    """
    safe_phone = (phone or "").replace("'", "")
    if not safe_phone or not wa_messages:
        return 0

    # 1) Load existing mensagens from DB
    existing_msgs: List[Dict[str, Any]] = []
    try:
        rows = run_sql(
            "SELECT mensagens FROM chatgpt_chats "
            f"WHERE whatsapp_paciente LIKE '%{safe_phone[-9:]}' "
            "  AND chat_mode = 'whatsapp' "
            "ORDER BY id DESC LIMIT 1"
        )
        if rows and rows[0].get("mensagens"):
            raw = rows[0]["mensagens"]
            if isinstance(raw, str):
                # Remove control characters (except common whitespace) that break json.loads
                import re as _re_json
                cleaned = _re_json.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', ' ', raw)
                existing_msgs = json.loads(cleaned, strict=False)
            elif isinstance(raw, list):
                existing_msgs = raw
    except Exception:
        log.exception("sync_whatsapp_messages_to_db: falha ao ler mensagens existentes para phone=%s", phone)
        return 0

    # 2) Build fingerprints of messages already in DB to avoid duplicates
    #    Fingerprint: (role, first_80_chars_of_content)
    existing_fps = set()
    for msg in existing_msgs:
        content = (msg.get("content") or "")[:80].strip().lower()
        role = msg.get("role") or ""
        existing_fps.add((role, content))

    # 3) Convert WhatsApp messages to DB format and filter new ones
    new_msgs: List[Dict[str, Any]] = []
    for wa_msg in wa_messages:
        text = (wa_msg.get("text") or "").strip()
        if not text:
            continue
        direction = wa_msg.get("direction") or "in"
        role = "user" if direction == "in" else "assistant"
        source = "whatsapp"

        # Check fingerprint
        fp = (role, text[:80].strip().lower())
        if fp in existing_fps:
            continue  # already stored
        existing_fps.add(fp)

        time_text = wa_msg.get("time_text") or ""
        new_msgs.append({
            "role": role,
            "content": text,
            "timestamp": time_text or utc_now_iso(),
            "source": source,
        })

    if not new_msgs:
        return 0

    # 4) Persist all new messages in a single UPDATE
    appended = 0
    try:
        rows = run_sql(
            "SELECT id FROM chatgpt_chats "
            f"WHERE whatsapp_paciente LIKE '%{safe_phone[-9:]}' "
            "  AND chat_mode = 'whatsapp' "
            "ORDER BY id DESC LIMIT 1"
        )
        if not rows:
            return 0
        chat_id = int(rows[0]["id"])
        merged = existing_msgs + new_msgs
        merged_json = json.dumps(merged, ensure_ascii=False)
        query = (
            "UPDATE chatgpt_chats SET "
            f"mensagens = {_sql_utf8mb4_literal(merged_json)}, "
            "datetime_atualizacao = NOW() "
            f"WHERE id = {chat_id}"
        )
        run_sql(query)
        appended = len(new_msgs)
    except Exception:
        log.exception("sync_whatsapp_messages_to_db: falha ao salvar lote para phone=%s", phone)

    if appended > 0:
        log.info(
            "  \033[96m📝 Sync mensagens WhatsApp → DB: phone=%s | %s novas mensagens salvas "
            "(total visíveis=%s, já no DB=%s)\033[0m",
            phone, appended, len(wa_messages), len(existing_msgs),
        )
    return appended


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
    """Send message to ChatGPT simulator and stream status updates to CMD."""
    headers = {"Authorization": f"Bearer {SIMULATOR_API_KEY}", "X-Request-Source": "acompanhamento_whatsapp.py"}
    payload = {
        "model": "ChatGPT Simulator",
        "message": text,
        "url": url_chatgpt,
        "stream": True,
        "request_source": "acompanhamento_whatsapp.py",
        "id_paciente": id_paciente,
        "id_atendimento": id_atendimento,
    }
    log_table("ChatGPT Simulator | Envio", [
        ("remetente", "acompanhamento_whatsapp.py"),
        ("paciente", str(id_paciente)),
        ("url", build_preview_with_ellipsis(url_chatgpt, 60)),
    ])
    r = requests.post(SIMULATOR_URL, headers=headers, json=payload, timeout=600, stream=True)
    r.raise_for_status()

    def _merge_stream_markdown(current: str, incoming: str) -> str:
        """Merge stream chunks, preferring latest full snapshot over blind append."""
        cur = current or ""
        inc = incoming or ""
        if not inc:
            return cur
        if not cur:
            return inc
        # Snapshot mode: servidor envia o texto completo repetidamente.
        if inc.startswith(cur):
            return inc
        # Chunk atrasado/menor do snapshot atual.
        if cur.startswith(inc):
            return cur
        # Duplicação exata
        if inc == cur:
            return cur
        # Se parece um novo snapshot completo (tamanho relevante), substitui
        # para evitar mistura com resposta anterior.
        if len(inc) >= max(120, int(len(cur) * 0.6)):
            return inc
        # Delta mode (chunk pequeno): anexa somente o sufixo novo quando possível.
        overlap_max = min(len(cur), len(inc))
        for k in range(overlap_max, 0, -1):
            if cur.endswith(inc[:k]):
                return cur + inc[k:]
        return cur + inc

    # Processa stream NDJSON — cada linha é um JSON com {type, content}
    full_html = ""
    last_status = ""
    chat_url_returned = ""
    inline_status_open = False
    raw_stream_lines: List[str] = []

    def _clean_status_text(raw: Any) -> str:
        msg = str(raw or "").strip()
        if not msg:
            return ""
        msg = re.sub(r"^\s*Remetente:\s*[^|]+\|\s*", "", msg, flags=re.IGNORECASE)
        return msg.strip()

    def _print_inline_status(msg: str) -> None:
        nonlocal inline_status_open
        txt = _clean_status_text(msg)
        if not txt:
            return
        line = f"  ⏳ ChatGPT Simulator: {txt}"
        width = 140
        if len(line) > width:
            line = line[: width - 3].rstrip() + "..."
        sys.stdout.write("\r" + line.ljust(width))
        sys.stdout.flush()
        inline_status_open = True

    def _close_inline_status() -> None:
        nonlocal inline_status_open
        if inline_status_open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            inline_status_open = False

    for raw_line in r.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, bytes) else raw_line.strip()
        # Skip SSE prefix if present (backwards compat)
        if line.startswith("data: "):
            line = line[len("data: "):]
        if line == "[DONE]":
            break
        raw_stream_lines.append(line)
        try:
            chunk = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        msg_type = chunk.get("type") or ""
        msg_content = chunk.get("content") or ""
        # Status updates (navigating, typing, waiting, etc.)
        if msg_type == "status":
            if msg_content and msg_content != last_status:
                _print_inline_status(str(msg_content))
                last_status = msg_content
        # Markdown content (the actual response text)
        elif msg_type == "markdown":
            if isinstance(msg_content, str):
                full_html = _merge_stream_markdown(full_html, msg_content)
        # Finish signal — may contain final content
        elif msg_type == "finish":
            if isinstance(msg_content, dict):
                chat_url_returned = msg_content.get("url") or ""
                final_text = (
                    msg_content.get("markdown")
                    or msg_content.get("html")
                    or msg_content.get("content")
                    or ""
                )
                if isinstance(final_text, str) and final_text.strip():
                    full_html = _merge_stream_markdown(full_html, final_text.strip())
        # Also handle OpenAI-compatible choices format (fallback)
        choices = chunk.get("choices") or []
        for choice in choices:
            delta = choice.get("delta") or {}
            content = delta.get("content") or ""
            if content:
                full_html = _merge_stream_markdown(full_html, content)

    # Fallback: se não recebeu stream, tenta ler JSON normal da resposta
    _close_inline_status()
    if not full_html:
        try:
            body = r.json() if hasattr(r, "_content") else {}
            full_html = (body.get("html") or "").strip()
        except Exception:
            pass

    full_html = _sanitize_simulator_answer(full_html)

    # DEBUG: registra retorno bruto exato do simulator para investigação de
    # mistura de respostas prévias vs resposta atual.
    if raw_stream_lines:
        raw_dump = "\n".join(raw_stream_lines)
        log.info(
            "=== ChatGPT Simulator | Retorno bruto (início) ===\n%s\n=== ChatGPT Simulator | Retorno bruto (fim) ===",
            raw_dump,
        )

    log_table("ChatGPT Simulator | Resposta", [
        ("remetente", "acompanhamento_whatsapp.py"),
        ("chars", str(len(full_html))),
        ("preview", build_preview_with_ellipsis(full_html, 150)),
    ])
    return {"html": full_html}


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
    core = (
        "[RESPOSTA WHATSAPP DE ACOMPANHAMENTO]\n"
        f"Paciente: {nome}\n"
        f"ID atendimento: {atendimento}\n"
        f"Pergunta/mensagem de acompanhamento: {pergunta}\n"
        f"Resposta do paciente: {patient_text}\n\n"
        "Com base nessa resposta, forneça orientação clínica de continuidade, "
        "objetiva e segura para envio ao paciente.\n\n"
        "IMPORTANTE (FORMATO WHATSAPP):\n"
        "- Responda em texto puro compatível com WhatsApp.\n"
        "- Use apenas marcações do WhatsApp: *negrito*, _itálico_, ~tachado~, ```monoespaçado```.\n"
        "- Não use Markdown avançado (títulos #, tabelas, links markdown [texto](url), HTML).\n"
        "- Se usar listas, prefira '-' ou '•'.\n"
        "- Entregue somente a mensagem final ao paciente, sem metacomentários."
    )
    return f"[INICIO_TEXTO_COLADO]\n{core}\n[FIM_TEXTO_COLADO]"


def _sanitize_simulator_answer(text: str) -> str:
    """Remove artefatos de status de pensamento que vazam para a resposta final."""
    out = (text or "").strip()
    if not out:
        return ""
    # Ex.: "[PensandoMessage  Que bom...]" -> "Que bom..."
    out = re.sub(r"^\[\s*(pensando|thinking)\s*message\b[:\s-]*", "", out, flags=re.IGNORECASE)
    # Ex.: "Thought for 7s\nEntendi..." ou "Pensou por 7s\n..."
    out = re.sub(r"(?im)^\s*(thought\s+for|pensou\s+por)\s+\d+\s*s\s*$", "", out)
    # Remove prefixos avulsos no início
    out = re.sub(r"^(pensando|thinking)\b[:\s-]*", "", out, flags=re.IGNORECASE)
    # Caso comum sem separador: "PensandoEntendi..."
    out = re.sub(r"^(pensando|thinking)\s*(?=[A-ZÁÉÍÓÚÂÊÔÃÕÀÇ])", "", out, flags=re.IGNORECASE)
    # Fecha colchete pendente logo após o prefixo removido
    out = re.sub(r"^\]\s*", "", out)
    # Remove blocos duplicados consecutivos por parágrafo.
    parts = [p.strip() for p in re.split(r"\n{2,}", out) if p.strip()]
    dedup_parts: List[str] = []
    for p in parts:
        if not dedup_parts or dedup_parts[-1] != p:
            dedup_parts.append(p)
    if dedup_parts:
        out = "\n\n".join(dedup_parts)
    return out.strip()


def _normalize_whatsapp_format(text: str) -> str:
    """Normalize common Markdown artifacts into WhatsApp-friendly plain text."""
    out = (text or "").strip()
    if not out:
        return ""
    # Remove Markdown headings and blockquote markers.
    out = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", out)
    out = re.sub(r"(?m)^\s*>\s?", "", out)
    # Convert markdown links to plain "texto (url)".
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", out)
    # Normalize bullets.
    out = re.sub(r"(?m)^\s*[*+]\s+", "- ", out)
    # Collapse excessive blank lines.
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


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
                        const nameSpan = nameArea.querySelector('span[title]') || nameArea.querySelector('span[dir="auto"]');
                        if (!nameSpan) continue;
                        const name = (nameSpan.getAttribute('title') || nameSpan.textContent || '').trim();
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
                    const textNode = last.querySelector('span[data-testid="selectable-text"]');
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
                        const nameSpan = nameArea.querySelector('span[title]') || nameArea.querySelector('span[dir="auto"]');
                        if (!nameSpan) continue;
                        const title = (nameSpan.getAttribute('title') || nameSpan.textContent || '').trim();
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

    def resolve_phone_from_open_chat_internals(self) -> Optional[str]:
        """Extract phone from the currently open chat using WA's internal React/Store data.

        This avoids needing to open the contact panel at all — it reads the
        chat ID (which IS the phone number) from the DOM's React fiber props
        or from the #main header's data attributes.
        """
        self.start()

        def _do():
            if not getattr(self, '_page', None):
                return None

            return self._page.evaluate(
                """() => {
                    const pickPhone = (s) => {
                        const d = String(s || '').replace(/\\D/g, '');
                        return (d.length >= 10 && d.length <= 15) ? d : null;
                    };

                    // Strategy 1: React Fiber on #main or header — chat ID is the phone
                    const main = document.querySelector('#main');
                    if (main) {
                        const fiberKey = Object.keys(main).find(
                            k => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance')
                        );
                        if (fiberKey) {
                            let node = main[fiberKey];
                            for (let i = 0; i < 20 && node; i++) {
                                const props = node.memoizedProps || node.pendingProps || {};
                                // Common prop names for chat ID
                                for (const key of ['chatId', 'id', 'jid', 'peer', 'contact']) {
                                    let val = props[key];
                                    if (val && typeof val === 'object') {
                                        val = val._serialized || val.user || val.toString();
                                    }
                                    const phone = pickPhone(val);
                                    if (phone) return phone;
                                }
                                // Check nested chat object
                                if (props.chat) {
                                    const chatId = props.chat.id;
                                    if (chatId) {
                                        const ser = typeof chatId === 'object'
                                            ? (chatId._serialized || chatId.user || '')
                                            : String(chatId);
                                        const phone = pickPhone(ser);
                                        if (phone) return phone;
                                    }
                                }
                                node = node.return;
                            }
                        }
                    }

                    // Strategy 2: data-id attribute on any ancestor
                    const header = document.querySelector('#main header');
                    if (header) {
                        let el = header;
                        while (el) {
                            const dataId = el.getAttribute?.('data-id') || '';
                            const phone = pickPhone(dataId);
                            if (phone) return phone;
                            el = el.parentElement;
                        }
                    }

                    // Strategy 3: WA Store — get currently active chat
                    try {
                        const Store = window.Store || window.require?.('WAWebCollections');
                        if (Store) {
                            // Active chat
                            const active = Store.Chat?.getActive?.() || Store.Cmd?.activeChat;
                            if (active) {
                                const id = active.id?._serialized || active.id?.user || '';
                                const phone = pickPhone(id);
                                if (phone) return phone;
                            }
                        }
                    } catch(e) {}

                    return null;
                }"""
            )

        result = self._run_on_browser_thread(_do)
        return normalize_phone(result) if result else None

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
        Usa evaluate_handle + ElementHandle.click() para clique real a nível de browser.
        Retorna True se conseguiu abrir o chat, False caso contrário.
        """
        self.start()

        def _do():
            if not getattr(self, '_page', None):
                return False

            title = " ".join(target_title.split())

            # 1) Obtém referência direta ao elemento DOM via evaluate_handle
            el_handle = self._page.evaluate_handle("""(target) => {
                const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
                const rows = document.querySelectorAll('#pane-side div[role="row"]');
                for (const row of rows) {
                    const nameArea = row.querySelector('div._ak8q');
                    const span = nameArea ? (nameArea.querySelector('span[title]') || nameArea.querySelector('span[dir="auto"]')) : (row.querySelector('span[title]') || row.querySelector('span[dir="auto"]'));
                    if (!span) continue;
                    const txt = norm(span.getAttribute('title') || span.textContent || '');
                    if (txt === target) {
                        row.scrollIntoView({ block: 'center' });
                        return row;
                    }
                }
                return null;
            }""", title)

            el = el_handle.as_element() if el_handle else None
            if not el:
                elog.warning("open_chat_by_sidebar_click: título '%s' não encontrado na sidebar", title)
                return False

            # 2) Clique real via Playwright ElementHandle (eventos mousedown/mouseup/click reais)
            try:
                self._page.wait_for_timeout(200)
                el.click(timeout=5000)
            except Exception as e:
                elog.warning("open_chat_by_sidebar_click: falha no clique para '%s': %s", title, e)
                return False

            # 3) Aguarda o header do chat renderizar — retry com espera para conexão lenta
            for wait_attempt in range(3):
                try:
                    self._page.wait_for_selector('#main header span[dir="auto"], #main header span[title]', timeout=3000)
                    self._page.wait_for_timeout(500)
                    return True
                except Exception:
                    if wait_attempt < 2:
                        elog.info(
                            "open_chat_by_sidebar_click: header ainda não apareceu para '%s', "
                            "aguardando mais 3s (tentativa %s/3)...",
                            title, wait_attempt + 1,
                        )
                        self._page.wait_for_timeout(3000)
                    else:
                        # Última tentativa: tenta seletor alternativo
                        try:
                            self._page.wait_for_selector('#main header', timeout=3000)
                            self._page.wait_for_timeout(500)
                            return True
                        except Exception:
                            pass
            elog.warning("open_chat_by_sidebar_click: header não apareceu após 3 tentativas para '%s'", title)
            return False

        return self._run_on_browser_thread(_do)

    def resolve_phone_via_wa_store(self, chat_title: str) -> Optional[str]:
        """
        Tenta resolver o telefone de um contato pelo título usando o store
        interno do WhatsApp Web (window.Store), sem necessidade de abrir o chat.
        Retorna o telefone normalizado ou None.
        """
        self.start()

        def _do():
            if not getattr(self, '_page', None):
                return None

            title = " ".join(chat_title.split())

            phone = self._page.evaluate("""(target) => {
                const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();

                // Estratégia 1: window.Store.Chat (API interna do WA Web)
                try {
                    const Store = window.Store || window.require?.('WAWebCollections');
                    if (Store && Store.Chat) {
                        const chats = Store.Chat.getModelsArray?.() || Store.Chat._models || [];
                        for (const chat of chats) {
                            const name = norm(chat.name || chat.formattedTitle || chat.contact?.pushname || '');
                            if (name === target || norm(chat.formattedTitle || '') === target) {
                                const id = chat.id?._serialized || chat.id?.user || '';
                                const digits = id.replace(/\\D/g, '');
                                if (digits.length >= 10 && digits.length <= 15) return digits;
                            }
                        }
                    }
                } catch(e) {}

                // Estratégia 2: window.Store.Contact
                try {
                    const Store = window.Store || window.require?.('WAWebCollections');
                    if (Store && Store.Contact) {
                        const contacts = Store.Contact.getModelsArray?.() || Store.Contact._models || [];
                        for (const c of contacts) {
                            const name = norm(c.pushname || c.name || c.formattedName || '');
                            if (name === target || norm(c.formattedName || '') === target) {
                                const id = c.id?._serialized || c.id?.user || '';
                                const digits = id.replace(/\\D/g, '');
                                if (digits.length >= 10 && digits.length <= 15) return digits;
                            }
                        }
                    }
                } catch(e) {}

                // Estratégia 3: busca na sidebar o data-id da row correspondente
                try {
                    const rows = document.querySelectorAll('#pane-side div[role="row"]');
                    for (const row of rows) {
                        const nameArea = row.querySelector('div._ak8q');
                        const span = nameArea ? (nameArea.querySelector('span[title]') || nameArea.querySelector('span[dir="auto"]')) : (row.querySelector('span[title]') || row.querySelector('span[dir="auto"]'));
                        if (!span) continue;
                        const txt = norm(span.getAttribute('title') || span.textContent || '');
                        if (txt !== target) continue;

                        // O link da row ou container pai pode ter o phone no data-id
                        const container = row.closest('[data-id]') || row.querySelector('[data-id]');
                        if (container) {
                            const dataId = container.getAttribute('data-id') || '';
                            const digits = dataId.replace(/\\D/g, '');
                            if (digits.length >= 10 && digits.length <= 15) return digits;
                        }

                        // Tenta extrair do atributo interno do React
                        const fiber = Object.keys(row).find(k => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance'));
                        if (fiber) {
                            try {
                                let node = row[fiber];
                                for (let i = 0; i < 15 && node; i++) {
                                    const props = node.memoizedProps || node.pendingProps || {};
                                    const id = props?.id || props?.chatId || props?.contact?.id;
                                    if (id) {
                                        const ser = typeof id === 'object' ? (id._serialized || id.user || '') : String(id);
                                        const d = ser.replace(/\\D/g, '');
                                        if (d.length >= 10 && d.length <= 15) return d;
                                    }
                                    node = node.return;
                                }
                            } catch(e) {}
                        }
                        break;
                    }
                } catch(e) {}

                return null;
            }""", title)

            return phone

        result = self._run_on_browser_thread(_do)
        return normalize_phone(result) if result else None

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
                        const textNode = msg.querySelector('span[data-testid="selectable-text"]');
                        const text = textNode ? textNode.textContent.trim() : '';
                        if (!text) continue;
                        const msgId = msg.getAttribute('data-id') || msg.id || '';
                        results.push({ id: msgId || text, text: text });
                    }
                    return results;
                }"""
            )

        return self._run_on_browser_thread(_do)

    def read_last_messages_from_open_chat(self, limit: int = 10) -> List[Dict[str, str]]:
        """Read last N messages (inbound + outbound) from the open chat.

        Returns list of {id, text, direction} where direction is 'in' or 'out',
        ordered from oldest to newest.
        """
        self.start()

        def _do():
            return self._page.evaluate(
                """(limit) => {
                    const allMsgs = Array.from(document.querySelectorAll('div.message-in, div.message-out'));
                    const slice = allMsgs.slice(-limit);
                    const results = [];
                    for (const msg of slice) {
                        const textNode = msg.querySelector('span[data-testid="selectable-text"]');
                        const text = textNode ? textNode.textContent.trim() : '';
                        if (!text) continue;
                        const msgId = msg.getAttribute('data-id') || msg.id || '';
                        const direction = msg.classList.contains('message-in') ? 'in' : 'out';
                        results.push({ id: msgId || text, text: text, direction: direction });
                    }
                    return results;
                }""",
                limit,
            )

        return self._run_on_browser_thread(_do) or []

    def read_all_visible_messages_from_open_chat(self) -> List[Dict[str, str]]:
        """Read ALL visible messages (inbound + outbound) from the open chat,
        including timestamps extracted from the DOM.

        Returns list of {id, text, direction, time_text} ordered oldest→newest.
        direction: 'in' (received) or 'out' (sent).
        time_text: visible timestamp string (e.g. '09:00', '23:19') or ''.
        """
        self.start()

        def _do():
            return self._page.evaluate(
                """() => {
                    const allMsgs = Array.from(document.querySelectorAll('div.message-in, div.message-out'));
                    const results = [];
                    for (const msg of allMsgs) {
                        const textNode = msg.querySelector('span[data-testid="selectable-text"]');
                        const text = textNode ? textNode.textContent.trim() : '';
                        if (!text) continue;
                        const msgId = msg.getAttribute('data-id') || msg.id || '';
                        const direction = msg.classList.contains('message-in') ? 'in' : 'out';
                        // Try to get timestamp from message metadata.
                        let timeText = '';
                        const prePlain = msg.querySelector('[data-pre-plain-text]');
                        if (prePlain) {
                            const attr = prePlain.getAttribute('data-pre-plain-text') || '';
                            const m = attr.match(/\\[(\\d{1,2}:\\d{2})/);
                            if (m) timeText = m[1];
                        }
                        if (!timeText) {
                            // Restrict fallback selectors to explicit time labels only.
                            const t1 = msg.querySelector('span[data-testid="msg-time"]');
                            const t2 = msg.querySelector('div.copyable-text span[aria-hidden="true"]');
                            const t3 = msg.querySelector('span[dir="auto"][aria-label*=":"]');
                            const timeEl = t1 || t2 || t3;
                            if (timeEl) {
                                const txt = (timeEl.textContent || '').trim();
                                const m = txt.match(/(\\d{1,2}:\\d{2})/);
                                if (m) timeText = m[1];
                            }
                        }
                        results.push({ id: msgId || text, text: text, direction: direction, time_text: timeText });
                    }
                    return results;
                }"""
            )

        return self._run_on_browser_thread(_do) or []

    def get_open_chat_identity(self) -> Dict[str, str]:
        """Return current open chat header identity {title, phone} when possible."""
        self.start()

        def _do():
            return self._page.evaluate(
                """() => {
                    const header = document.querySelector('#main header');
                    if (!header) return { title: '', phone: '' };

                    let title = '';
                    const titleEl = header.querySelector('span[dir="auto"]') || header.querySelector('span[title]');
                    if (titleEl) title = (titleEl.getAttribute('title') || titleEl.textContent || '').trim();

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
        """Open contact details panel via menu and extract profile info.

        Simulates the exact manual user route:
          1. Escape (close any existing panel/menu)
          2. Read header identity (title + phone if visible)
          3. Click "Mais opções" button (three-dots menu)
          4. Click "Dados do contato" menu item
          5. Wait for contact details section to render (including phone)
          6. Extract profile_name + profile_phone from the section
          7. Escape (close panel)

        Returns:
          {title, phone, profile_name, profile_phone}
        """
        self.start()

        def _do():
            # ── SUB-ETAPA A: Fecha possível painel/menu já aberto ──────
            try:
                self._page.keyboard.press("Escape")
                self._page.wait_for_timeout(300)
            except Exception:
                pass

            header = self._page.locator("#main header").first
            if header.count() == 0:
                elog.info("get_open_contact_details: #main header não encontrado")
                return {"title": "", "phone": "", "profile_name": "", "profile_phone": ""}

            # ── SUB-ETAPA B: Captura identidade do header ──────────────
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
                    for (const s of header.querySelectorAll('span[title], span[dir="auto"], span')) {
                        const txt = (s.getAttribute?.('title') || s.textContent || '').trim();
                        const p = pickPhone(txt);
                        if (p.length >= 10) { phone = p; break; }
                    }
                    return { title, phone };
                }"""
            ) or {}
            elog.info(
                "get_open_contact_details sub-B: header title='%s' phone='%s'",
                base.get("title", ""), base.get("phone", ""),
            )

            # ── SUB-ETAPA C: Clique em "Mais opções" (three-dots menu) ─
            menu_opened = False
            try:
                mais_opcoes = self._page.locator(
                    '#main header button[aria-label="Mais opções"], '
                    '#main button[aria-label="Mais opções"], '
                    'button[aria-label="More options"]'
                ).first
                if mais_opcoes.count() > 0:
                    mais_opcoes.click(timeout=3000)
                    self._page.wait_for_timeout(500)
                    menu_opened = True
                    elog.info("  sub-C: 'Mais opções' clicado via locator")
            except Exception as e:
                elog.debug("  sub-C: locator click falhou: %s", e)

            if not menu_opened:
                try:
                    js_result = self._page.evaluate(
                        """() => {
                            const realClick = (el) => {
                                if (!el) return false;
                                el.scrollIntoView({ block: 'center' });
                                const rect = el.getBoundingClientRect();
                                const x = rect.left + rect.width / 2;
                                const y = rect.top + rect.height / 2;
                                const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y };
                                el.dispatchEvent(new MouseEvent('mousedown', opts));
                                el.dispatchEvent(new MouseEvent('mouseup', opts));
                                el.dispatchEvent(new MouseEvent('click', opts));
                                return true;
                            };
                            const btn = document.querySelector('#main button[aria-label="Mais opções"]')
                                || document.querySelector('button[aria-label="Mais opções"]')
                                || document.querySelector('#main button[aria-label="More options"]')
                                || document.querySelector('button[aria-label="More options"]');
                            if (!btn) return { ok: false, reason: 'button_not_found' };
                            return { ok: realClick(btn), reason: 'js_click' };
                        }"""
                    ) or {}
                    menu_opened = bool(js_result.get("ok"))
                    if menu_opened:
                        self._page.wait_for_timeout(500)
                        elog.info("  sub-C: 'Mais opções' clicado via JS")
                    else:
                        elog.warning(
                            "  sub-C: botão 'Mais opções' não encontrado: %s",
                            js_result.get("reason", ""),
                        )
                except Exception as e:
                    elog.warning("  sub-C: JS click 'Mais opções' falhou: %s", e)

            if not menu_opened:
                elog.warning("  FALHA ao abrir menu 'Mais opções'")
                return {
                    "title": str(base.get("title") or "").strip(),
                    "phone": str(base.get("phone") or "").strip(),
                    "profile_name": "",
                    "profile_phone": "",
                    "_click_failed": True,
                }

            # ── SUB-ETAPA D: Clique em "Dados do contato" (menu item) ──
            panel_clicked = False
            try:
                dados_btn = self._page.locator(
                    'button[aria-label="Dados do contato"], '
                    'div[aria-label="Dados do contato"][role="menuitem"], '
                    '[aria-label="Dados do contato"], '
                    'button[aria-label="Contact info"], '
                    '[aria-label="Contact info"][role="menuitem"]'
                ).first
                if dados_btn.count() > 0:
                    dados_btn.click(timeout=3000)
                    panel_clicked = True
                    elog.info("  sub-D: 'Dados do contato' clicado via locator")
            except Exception as e:
                elog.debug("  sub-D: locator click 'Dados do contato' falhou: %s", e)

            if not panel_clicked:
                try:
                    js_result = self._page.evaluate(
                        """() => {
                            const realClick = (el) => {
                                if (!el) return false;
                                el.scrollIntoView({ block: 'center' });
                                const rect = el.getBoundingClientRect();
                                const x = rect.left + rect.width / 2;
                                const y = rect.top + rect.height / 2;
                                const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y };
                                el.dispatchEvent(new MouseEvent('mousedown', opts));
                                el.dispatchEvent(new MouseEvent('mouseup', opts));
                                el.dispatchEvent(new MouseEvent('click', opts));
                                return true;
                            };
                            const btn = document.querySelector('[aria-label="Dados do contato"]')
                                || document.querySelector('[aria-label="Contact info"]');
                            if (!btn) return { ok: false, reason: 'dados_contato_not_found' };
                            return { ok: realClick(btn), reason: 'js_click' };
                        }"""
                    ) or {}
                    panel_clicked = bool(js_result.get("ok"))
                    if panel_clicked:
                        elog.info("  sub-D: 'Dados do contato' clicado via JS")
                    else:
                        elog.warning(
                            "  sub-D: item 'Dados do contato' não encontrado: %s",
                            js_result.get("reason", ""),
                        )
                except Exception as e:
                    elog.warning("  sub-D: JS click 'Dados do contato' falhou: %s", e)

            if not panel_clicked:
                # Menu não tem "Dados do contato" — é conta oficial do WhatsApp
                try:
                    self._page.keyboard.press("Escape")
                    self._page.wait_for_timeout(200)
                except Exception:
                    pass
                elog.warning(
                    "  Menu não contém 'Dados do contato' — provável conta oficial WhatsApp (sem número)"
                )
                return {
                    "title": str(base.get("title") or "").strip(),
                    "phone": str(base.get("phone") or "").strip(),
                    "profile_name": "",
                    "profile_phone": "",
                    "_click_failed": True,
                    "_no_dados_contato": True,
                }

            # ── SUB-ETAPA E: Aguarda seção de dados do contato carregar ─
            panel_visible = False
            panel_selectors = [
                'section span[data-testid="selectable-text"]',
                '[data-testid="contact-info-drawer"]',
                'section img[draggable="false"]',
            ]
            for sel in panel_selectors:
                try:
                    self._page.wait_for_selector(sel, timeout=5000)
                    panel_visible = True
                    elog.info("  sub-E: painel detectado via '%s'", sel)
                    break
                except Exception:
                    continue

            if not panel_visible:
                elog.warning(
                    "  sub-E: painel não detectado por seletores. Aguardando 4s como fallback..."
                )
                self._page.wait_for_timeout(4000)
            else:
                # Pausa para o telefone renderizar (~1-6s observado manualmente)
                self._page.wait_for_timeout(3000)

            # ── SUB-ETAPA F: Extrai dados da section com retries ────────
            extract_details_js = """() => {
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
                const isNoiseText = (txt) => {
                    const lower = String(txt || '').trim().toLowerCase();
                    if (!lower) return true;
                    return (
                        lower.includes('dados do contato')
                        || lower.includes('contact info')
                        || lower.includes('mais opções')
                        || lower.includes('more options')
                        || lower.includes('mídia')
                        || lower.includes('media')
                        || lower.includes('mensagens favoritas')
                        || lower.includes('silenciar')
                        || lower.includes('mensagens temporárias')
                        || lower.includes('criptografia')
                        || lower.includes('bloquear')
                        || lower.includes('denunciar')
                        || lower.includes('apagar conversa')
                        || lower.includes('limpar conversa')
                        || lower.includes('adicionar aos favoritos')
                        || lower.includes('privacidade')
                        || lower.includes('pesquisar')
                        || lower.includes('editar')
                        || lower.includes('notas')
                        || lower.includes('desativad')
                        || lower.includes('fechar conversa')
                        || lower.includes('selecionar mensagens')
                        || lower.includes('trancar conversa')
                        || lower.includes('etiquetar')
                        || lower.includes('wa-wordmark')
                        || lower.includes('meta ai')
                        || lower.includes('mostrar foto')
                        || lower.includes('adicione notas')
                    );
                };
                const out = { profile_name: '', profile_phone: '', _debug: '' };

                // Busca a section do painel de detalhes (lado direito)
                // A section contém a foto, nome, telefone, mídia, etc.
                const sections = document.querySelectorAll('section');
                let panelRoot = null;
                for (const s of sections) {
                    if (!isVisible(s)) continue;
                    const rect = s.getBoundingClientRect();
                    // A section de contato é grande e fica à direita
                    if (rect.width >= 200 && rect.height >= 300) {
                        panelRoot = s;
                        break;
                    }
                }

                // Fallback: data-testid drawer
                if (!panelRoot) {
                    panelRoot = document.querySelector('[data-testid="contact-info-drawer"]');
                }

                if (!panelRoot) {
                    out._debug = 'panelRoot(section) not found';
                    return out;
                }
                out._debug = 'panelRoot=' + panelRoot.tagName +
                    ' size=' + panelRoot.getBoundingClientRect().width + 'x' +
                    panelRoot.getBoundingClientRect().height;

                // --- Nome do perfil ---
                // No HTML o nome aparece em span[data-testid="selectable-text"]
                const nameNodes = panelRoot.querySelectorAll(
                    'span[data-testid="selectable-text"], span[dir="auto"], span[title]'
                );
                for (const n of nameNodes) {
                    if (!isVisible(n)) continue;
                    const txt = (n.getAttribute?.('title') || n.textContent || '').trim();
                    if (!txt || txt.length < 2) continue;
                    if (isNoiseText(txt)) continue;
                    // Se parece telefone, pular — queremos o nome primeiro
                    const maybePhone = pickPhone(txt);
                    if (maybePhone) continue;
                    out.profile_name = txt;
                    break;
                }

                // --- Telefone ---
                // No HTML o telefone aparece como "+55 81 8148-7277" dentro de spans
                const allSpans = panelRoot.querySelectorAll(
                    'span[data-testid="selectable-text"], span[dir="auto"], span, div'
                );
                for (const s of allSpans) {
                    if (!isVisible(s)) continue;
                    const txt = (s.textContent || '').trim();
                    if (!txt) continue;
                    const p = pickPhone(txt);
                    if (p) {
                        out.profile_phone = p;
                        break;
                    }
                }

                // Fallback: cell-frame-container
                if (!out.profile_phone) {
                    const cells = panelRoot.querySelectorAll(
                        '[data-testid="cell-frame-container"], [data-testid="cell-frame-secondary"]'
                    );
                    for (const c of cells) {
                        if (!isVisible(c)) continue;
                        const p = pickPhone((c.textContent || '').trim());
                        if (p) { out.profile_phone = p; break; }
                    }
                }

                return out;
            }"""

            data = {}
            for attempt in range(1, 13):
                data = self._page.evaluate(extract_details_js) or {}
                _debug_attempt = str(data.get("_debug") or "")
                found_phone = normalize_phone(data.get("profile_phone"))
                if attempt == 1 or found_phone:
                    elog.info(
                        "  sub-F: tentativa %s/12 | phone='%s' name='%s' debug='%s'",
                        attempt, found_phone or "(nenhum)",
                        data.get("profile_name", ""), _debug_attempt[:120],
                    )
                if found_phone:
                    break
                if attempt < 12:
                    self._page.wait_for_timeout(500 if attempt <= 4 else 750)

            # ── SUB-ETAPA G: Fecha painel ──────────────────────────────
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
            elog.info(
                "  RESULTADO: title='%s' phone='%s' profile_name='%s' "
                "profile_phone='%s' panel_visible=%s debug=%s",
                result.get("title", ""), result.get("phone", ""),
                result.get("profile_name", ""), result.get("profile_phone", ""),
                panel_visible, _debug[:100],
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

    # 0) Try alias cache: resolve named contact to phone
    alias_phone = None
    try:
        aliases = state.get_contact_aliases()
        title_key = re.sub(r"\s+", " ", title.strip().lower())
        alias_phone = normalize_phone(aliases.get(title_key))
    except Exception:
        pass

    # 1) Fast path: lookup by whatsapp_paciente in chatgpt_chats
    candidate_phones = [phone, normalize_phone(phone_hint) if phone_hint else None]
    if alias_phone and alias_phone not in candidate_phones:
        candidate_phones.append(alias_phone)
    for candidate_phone in candidate_phones:
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
    for cp in candidate_phones:
        if not cp:
            continue
        result = lookup_atendimento_by_phone(cp)
        if result:
            result.setdefault("telefone", cp)
            return result

    # 3) Lookup em tabela dedicada de contatos WhatsApp (quando o chat aparece
    # por nome salvo e o telefone não está explícito na lista).
    for candidate_phone in candidate_phones:
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



def _enrich_single_contact(title: str) -> Tuple[Optional[str], str, str, str]:
    """Try to enrich a single named contact. Returns (phone, detail_title, detail_profile, failure_reason).

    Logs each browser step for DEBUG comparison with the manual route:
      manual: sidebar click → header renders → click header → panel opens → phone appears
    """
    detail_title = title
    detail_profile = ""

    # ── ETAPA 1: WA Store (sem abrir chat) ──────────────────────────
    elog.info("  [ETAPA 1/5] resolve_phone_via_wa_store('%s')...", title)
    try:
        store_phone = wa_web.resolve_phone_via_wa_store(title)
        if store_phone:
            elog.info("  [ETAPA 1/5] ✓ WA Store resolveu: %s", store_phone)
            return store_phone, detail_title, detail_profile, ""
        elog.info("  [ETAPA 1/5] WA Store: nenhum resultado")
    except Exception as e:
        elog.info("  [ETAPA 1/5] WA Store: exceção: %s", e)

    # ── ETAPA 2: Clicar no contato na sidebar ───────────────────────
    elog.info("  [ETAPA 2/5] open_chat_by_sidebar_click('%s')...", title)
    if not wa_web.open_chat_by_sidebar_click(title):
        reason = "sidebar_click_failed"
        elog.warning("  [ETAPA 2/5] FALHA: clique na sidebar não abriu o chat")
        return None, detail_title, detail_profile, reason

    # ── ETAPA 3: Verificar header do chat aberto ────────────────────
    elog.info("  [ETAPA 3/5] get_open_chat_identity() — verificando header...")
    open_identity = wa_web.get_open_chat_identity()
    open_title = (open_identity.get("title") or "").strip()
    open_phone = (open_identity.get("phone") or "").strip()
    elog.info(
        "  [ETAPA 3/5] Header: title='%s' phone='%s'",
        open_title or "(vazio)", open_phone or "(nenhum)",
    )

    if not open_title:
        reason = "header_empty_after_click"
        elog.warning("  [ETAPA 3/5] FALHA: header vazio — chat não abriu")
        return None, detail_title, detail_profile, reason

    # Se o header já mostra telefone, temos o resultado
    if open_phone and normalize_phone(open_phone):
        elog.info("  [ETAPA 3/5] ✓ Telefone já visível no header: %s", open_phone)
        return normalize_phone(open_phone), open_title, detail_profile, ""

    # ── ETAPA 3.5: React internals do chat aberto ──────────────────
    elog.info("  [ETAPA 3.5] resolve_phone_from_open_chat_internals() — tentando React/Store...")
    try:
        react_phone = wa_web.resolve_phone_from_open_chat_internals()
        if react_phone:
            elog.info("  [ETAPA 3.5] ✓ Telefone via React internals: %s", react_phone)
            return react_phone, open_title, detail_profile, ""
        elog.info("  [ETAPA 3.5] React internals: nenhum telefone encontrado")
    except Exception as e:
        elog.info("  [ETAPA 3.5] React internals: exceção: %s", e)

    # ── ETAPA 4: Abrir painel "Dados do contato" via menu ───────────
    elog.info("  [ETAPA 4/5] get_open_contact_details() — abrindo via Mais opções → Dados do contato...")
    details = wa_web.get_open_contact_details()
    detail_title = details.get("title") or title
    detail_profile = details.get("profile_name") or ""
    panel_phone = normalize_phone(details.get("profile_phone") or "")
    header_phone = normalize_phone(details.get("phone") or "")

    # Se o menu não contém "Dados do contato", é conta oficial do WhatsApp
    if details.get("_no_dados_contato"):
        reason = "official_wa_account_no_dados_contato"
        elog.info(
            "  [ETAPA 4/5] Conta oficial do WhatsApp detectada (sem 'Dados do contato' no menu) — ignorando permanentemente"
        )
        return None, detail_title, detail_profile, reason

    elog.info(
        "  [ETAPA 4/5] Resultado painel: title='%s' profile_name='%s' "
        "profile_phone='%s' header_phone='%s'",
        detail_title, detail_profile,
        panel_phone or "(nenhum)", header_phone or "(nenhum)",
    )

    if panel_phone:
        elog.info("  [ETAPA 4/5] ✓ Telefone capturado via painel: %s", panel_phone)
        return panel_phone, detail_title, detail_profile, ""
    if header_phone:
        elog.info("  [ETAPA 4/5] ✓ Telefone capturado via header (retorno do painel): %s", header_phone)
        return header_phone, detail_title, detail_profile, ""

    # ── ETAPA 5: Fallback — extract_phone_from_open_chat ────────────
    elog.info("  [ETAPA 5/5] extract_phone_from_open_chat() — fallback no header direto...")
    try:
        fallback_phone = wa_web.extract_phone_from_open_chat()
        if fallback_phone:
            norm = normalize_phone(fallback_phone)
            if norm:
                elog.info("  [ETAPA 5/5] ✓ Telefone capturado via header direto: %s", norm)
                return norm, detail_title, detail_profile, ""
        elog.info("  [ETAPA 5/5] Header direto: nenhum telefone encontrado")
    except Exception as e:
        elog.warning("  [ETAPA 5/5] Exceção no fallback header: %s", e)

    reason = (
        "no_phone_all_strategies|"
        f"store=fail|sidebar=ok|header='{open_title}'|"
        f"panel_profile='{detail_profile}'|panel_phone=none|header_phone=none"
    )
    elog.warning(
        "  [RESULTADO] Nenhum telefone encontrado para '%s' | motivo: %s",
        title, reason,
    )
    return None, detail_title, detail_profile, reason


def enrich_named_contacts_from_sidebar(
    chat_rows: List[Dict[str, Any]],
    *,
    max_per_cycle: int = 3,
    max_attempts: int = 5,
    min_interval_sec: int = 180,
) -> int:
    """Opens a few visible named chats to capture phone/profile and cache them."""
    global _LAST_SIDEBAR_ENRICHMENT_TS
    import time, re, json # garantindo imports caso falte no escopo global

    now_mono = time.monotonic()
    if now_mono - _LAST_SIDEBAR_ENRICHMENT_TS < float(min_interval_sec):
        return 0

    aliases = state.get_contact_aliases()
    contexts = state.all_phone_contexts()
    official_accounts = state.get_official_wa_accounts()
    enriched = 0
    skipped_not_named = 0
    skipped_alias_exists = 0
    skipped_no_phone = 0
    skipped_cooldown = 0
    skipped_official = 0
    attempts = 0

    # ── Filtra e classifica candidatos ──────────────────────────────
    all_failures = state.get_enrichment_failures()
    eligible_rows: List[Dict[str, Any]] = []
    monitored_rows: List[Dict[str, Any]] = []
    retry_rows: List[Dict[str, Any]] = []
    total_named = 0
    for row in chat_rows:
        title = (row.get("title") or "").strip()
        if not title or not _is_named_chat_title(title):
            skipped_not_named += 1
            continue
        total_named += 1
        title_key = re.sub(r"\s+", " ", title.lower())
        if aliases.get(title_key):
            skipped_alias_exists += 1
            continue
        # Conta oficial do WhatsApp — ignorar permanentemente
        if state.is_official_wa_account(title_key):
            skipped_official += 1
            continue
        # Contato em cooldown (>=5 falhas nas últimas 24h) — pula
        if state.should_skip_enrichment(title_key, max_failures=5, cooldown_hours=24):
            skipped_cooldown += 1
            continue
        # Classifica o candidato
        failure_entry = all_failures.get(title_key)
        is_retry = failure_entry and failure_entry.get("count", 0) > 0
        if _match_context_for_chat_title(title, contexts, aliases=aliases):
            monitored_rows.append(row)
        elif is_retry:
            retry_rows.append(row)
        else:
            eligible_rows.append(row)
    # Prioridade: monitorados > retries (com falha anterior) > novos
    prioritized = monitored_rows + retry_rows + eligible_rows

    # ── Log inteligente: resumo ou detalhado ────────────────────────
    has_pending_real = len(prioritized) > 0
    total_accounted = skipped_alias_exists + skipped_official + skipped_not_named
    if not has_pending_real:
        # Todos os contatos nomeados estão resolvidos — log resumido
        if skipped_official > 0:
            elog.info(
                "=== Contatos JÁ associados (nome → telefone): %s de %s listados "
                "| %s são contas oficiais do WhatsApp (sem número) ===",
                skipped_alias_exists, total_named, skipped_official,
            )
        else:
            elog.info(
                "=== Contatos JÁ associados (nome → telefone): %s de %s listados ===",
                skipped_alias_exists, total_named,
            )
    else:
        # Há contatos pendentes — log detalhado
        elog.info(
            "=== Contatos JÁ associados (nome → telefone): %s de %s listados "
            "| oficiais_whatsapp=%s | pendentes=%s ===",
            skipped_alias_exists, total_named, skipped_official, len(prioritized),
        )
        if aliases:
            for alias_name, alias_phone in sorted(aliases.items()):
                elog.info("  ✓ '%s' → %s", alias_name, alias_phone)
        if official_accounts:
            elog.info("  Contas oficiais WhatsApp (sem número): %s", ", ".join(official_accounts))
        if all_failures:
            for fk, fv in sorted(all_failures.items(), key=lambda x: x[1].get("count", 0), reverse=True):
                if state.is_official_wa_account(fk):
                    continue
                in_cooldown = state.should_skip_enrichment(fk, max_failures=5, cooldown_hours=24)
                elog.info(
                    "  ✗ '%s' | tentativas=%s | motivo='%s' | cooldown=%s",
                    fk, fv.get("count", 0), fv.get("last_reason", "?"),
                    "SIM" if in_cooldown else "não",
                )

    # Sync: garante que todos os aliases existentes no cache tenham entrada no DB
    if aliases:
        for alias_name, alias_phone in aliases.items():
            try:
                existing = lookup_whatsapp_contact_by_display_name(alias_name)
                if not existing:
                    _upsert_whatsapp_contact_profile(
                        phone=alias_phone,
                        display_name=alias_name,
                        profile_name="",
                        wa_chat_title=alias_name,
                        source="alias_cache_sync",
                    )
            except Exception:
                pass

    # Se todos os candidatos estão em cooldown e não há nenhum novo, reseta os
    # contadores de falha para permitir retries com a lógica atualizada.
    if len(prioritized) == 0 and skipped_cooldown > 0:
        cleared = state.reset_all_enrichment_failures()
        elog.info(
            "TODOS os %s candidatos em cooldown — "
            "resetando %s contadores de falha para permitir retry com lógica atualizada",
            skipped_cooldown, cleared,
        )
        # Re-scan after reset
        skipped_cooldown = 0
        for row in chat_rows:
            title = (row.get("title") or "").strip()
            if not title or not _is_named_chat_title(title):
                continue
            title_key = re.sub(r"\s+", " ", title.lower())
            if aliases.get(title_key):
                continue
            if _match_context_for_chat_title(title, contexts, aliases=aliases):
                monitored_rows.append(row)
            else:
                retry_rows.append(row)
        prioritized = monitored_rows + retry_rows + eligible_rows

    if has_pending_real:
        elog.info(
            "=== Iniciando enriquecimento | candidatos=%s (monitorados=%s, retries=%s, novos=%s) ===",
            len(prioritized), len(monitored_rows), len(retry_rows), len(eligible_rows),
        )

    # Log dos candidatos que serão tentados neste ciclo
    if prioritized:
        elog.info("=== Candidatos a tentar neste ciclo (%s): ===", len(prioritized))
        for i, prow in enumerate(prioritized[:max_attempts]):
            ptitle = (prow.get("title") or "").strip()
            ptitle_key = re.sub(r"\s+", " ", ptitle.lower())
            pfail = all_failures.get(ptitle_key)
            pfail_count = pfail.get("count", 0) if pfail else 0
            elog.info("  %s. '%s' (falhas anteriores=%s)", i + 1, ptitle, pfail_count)
    else:
        elog.info("=== Nenhum candidato para enriquecer neste ciclo ===")

    for row in prioritized:
        if enriched >= max_per_cycle or attempts >= max_attempts:
            break

        title = (row.get("title") or "").strip()
        title_key = re.sub(r"\s+", " ", title.lower())
        prev_failure = all_failures.get(title_key)

        attempts += 1
        if prev_failure and prev_failure.get("count", 0) > 0:
            elog.info(
                ">>> [%s/%s] RETRY '%s' (falhas=%s, motivo anterior='%s')",
                attempts, max_attempts, title,
                prev_failure.get("count", 0),
                prev_failure.get("last_reason", "?"),
            )
        else:
            elog.info(">>> [%s/%s] tentando '%s' (primeira vez)", attempts, max_attempts, title)

        try:
            resolved_phone, detail_title, detail_profile, failure_reason = _enrich_single_contact(title)

            if not resolved_phone:
                # Detecta conta oficial do WhatsApp — marcar permanentemente
                if failure_reason == "official_wa_account_no_dados_contato":
                    state.mark_official_wa_account(title_key)
                    state.clear_enrichment_failure(title_key)
                    # Salva no DB como contato sem telefone
                    _upsert_whatsapp_contact_profile(
                        phone=None,
                        display_name=title,
                        profile_name="",
                        wa_chat_title=title,
                        source="official_wa_no_phone",
                    )
                    elog.info(
                        "  Marcado como conta oficial WhatsApp (ignorado permanentemente): '%s'",
                        title,
                    )
                else:
                    skipped_no_phone += 1
                    state.record_enrichment_failure(title_key, reason=failure_reason)
                    # Após 3+ falhas sem telefone, marcar como conta sem número e ignorar
                    fail_entry = state.get_enrichment_failures().get(title_key) or {}
                    if fail_entry.get("count", 0) >= 3:
                        state.mark_official_wa_account(title_key)
                        state.clear_enrichment_failure(title_key)
                        _upsert_whatsapp_contact_profile(
                            phone=None,
                            display_name=title,
                            profile_name="",
                            wa_chat_title=title,
                            source="no_phone_after_retries",
                        )
                        elog.info(
                            "  Conta sem telefone após %s tentativas — "
                            "salvo no DB e ignorado permanentemente: '%s'",
                            fail_entry.get("count", 0), title,
                        )
                continue

            _upsert_whatsapp_contact_profile(
                phone=resolved_phone,
                display_name=detail_title,
                profile_name=detail_profile,
                wa_chat_title=title,
                source="sidebar_enrichment",
            )

            state.set_contact_alias(title, resolved_phone)
            state.clear_enrichment_failure(title_key)
            elog.info("✓✓✓ ASSOCIADO: '%s' -> %s", title, resolved_phone)
            enriched += 1

        except Exception as e:
            state.record_enrichment_failure(title_key, reason=f"exception:{e}")
            elog.exception("Falha no enriquecimento para '%s': %s", title, e)

    elog.info(
        "=== Enriquecimento concluído | enriched=%s no_phone=%s "
        "not_named=%s alias_exists=%s official_wa=%s cooldown=%s attempts=%s ===",
        enriched, skipped_no_phone,
        skipped_not_named, skipped_alias_exists, skipped_official, skipped_cooldown, attempts,
    )

    if enriched > 0:
        _LAST_SIDEBAR_ENRICHMENT_TS = time.monotonic()
    else:
        retry_backoff_sec = min(20, max(5, int(min_interval_sec)))
        _LAST_SIDEBAR_ENRICHMENT_TS = time.monotonic() - max(0, min_interval_sec - retry_backoff_sec)

    return enriched


def process_incoming_replies_once() -> Dict[str, int]:
    """Scan WhatsApp sidebar for chats with monitored contacts, check each
    for new inbound messages using own message history (independent of
    unread count or sidebar time), forward new patient replies to the
    ChatGPT simulator and reply back via WhatsApp.
    """
    # ANSI color codes for CMD output
    C_RESET = "\033[0m"
    C_BOLD = "\033[1m"
    C_GREEN = "\033[92m"
    C_YELLOW = "\033[93m"
    C_RED = "\033[91m"
    C_CYAN = "\033[96m"
    C_GRAY = "\033[90m"
    C_BLUE = "\033[94m"
    C_MAGENTA = "\033[95m"
    C_WHITE = "\033[97m"
    C_BG_GREEN = "\033[42m"
    C_BG_RED = "\033[41m"
    C_BG_YELLOW = "\033[43m"

    processed = 0
    skipped = 0
    no_match = 0
    results_table: List[Dict[str, str]] = []  # for final summary table

    # 1) Scan sidebar
    chat_rows = wa_web.scan_chat_list_rows()
    if not chat_rows:
        return {"processed": 0, "skipped": 0, "no_match": 0}
    log.info("Scan sidebar WhatsApp: %s chats visíveis.", len(chat_rows))

    # Enriquecimento preventivo de contatos nomeados
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

    # 2) Seleciona candidatos: qualquer chat que corresponda a um contexto monitorado
    candidates: List[Dict[str, Any]] = []
    skipped_not_matched = 0
    for chat in chat_rows:
        title = (chat.get("title") or "").strip()
        if not title:
            continue
        matched = _match_context_for_chat_title(title, contexts, aliases=aliases)
        if not matched:
            skipped_not_matched += 1
            continue
        phone_key, ctx = matched
        candidates.append({
            "title": title,
            "phone_key": phone_key,
            "ctx": ctx,
        })

    if not candidates:
        log.info(
            "%s── Monitor replies: nenhum candidato │ total_chats=%s │ sem_contexto=%s ──%s",
            C_GRAY, len(chat_rows), skipped_not_matched, C_RESET,
        )
        return {"processed": 0, "skipped": 0, "no_match": 0}

    # Header da tabela de verificação
    log.info(
        "\n%s%s╔══════════════════════════════════════════════════════════════════════════════╗%s\n"
        "%s%s║  📋 MONITOR DE RESPOSTAS — %s chats monitorados a verificar                  ║%s\n"
        "%s%s╚══════════════════════════════════════════════════════════════════════════════╝%s",
        C_BOLD, C_CYAN, C_RESET,
        C_BOLD, C_CYAN, len(candidates), C_RESET,
        C_BOLD, C_CYAN, C_RESET,
    )

    for i, chat in enumerate(candidates, 1):
        title = chat["title"]
        try:
            short_title = title[:45] + "…" if len(title) > 45 else title

            # 3) Open the chat by clicking in the sidebar
            if not wa_web.open_chat_by_sidebar_click(title):
                reason = "❌ Falha ao abrir chat na sidebar"
                results_table.append({"n": str(i), "title": short_title, "status": "ERRO", "reason": reason})
                log.info(
                    "  %s%s│ %s/%s │ %-45s │ %s%s%s",
                    C_RED, C_BOLD, i, len(candidates), short_title, reason, C_RESET, "",
                )
                skipped += 1
                continue

            # 4) Read ALL visible messages (in + out) for history sync
            all_visible_msgs = wa_web.read_all_visible_messages_from_open_chat()
            if not all_visible_msgs:
                reason = "📭 Chat vazio — nenhuma mensagem encontrada"
                results_table.append({"n": str(i), "title": short_title, "status": "SKIP", "reason": reason})
                log.info(
                    "  %s│ %s/%s │ %-45s │ %s%s",
                    C_GRAY, i, len(candidates), short_title, reason, C_RESET,
                )
                skipped += 1
                continue

            # 4b) Sync messages to DB for contacts with membros match
            sync_phone = normalize_phone(chat.get("phone_key"))
            if sync_phone:
                membros_ids = _find_membros_by_phone(sync_phone)
                if membros_ids:
                    try:
                        sync_whatsapp_messages_to_db(sync_phone, all_visible_msgs)
                    except Exception:
                        log.exception("Falha ao sync mensagens para phone=%s", sync_phone)

            # Use last messages for reply detection
            last_msgs = all_visible_msgs
            # 5) Check if the last message is INBOUND (from patient)
            last_msg = last_msgs[-1]
            last_text_preview = build_preview_with_ellipsis(last_msg["text"], 40)

            if last_msg["direction"] != "in":
                reason = f"📤 Última msg é ENVIADA (out) — sem resposta pendente"
                detail = f"última=[{last_text_preview}]"
                results_table.append({"n": str(i), "title": short_title, "status": "SKIP", "reason": f"{reason} | {detail}"})
                log.info(
                    "  %s│ %s/%s │ %-45s │ %s │ %s%s",
                    C_GRAY, i, len(candidates), short_title, reason, detail, C_RESET,
                )
                skipped += 1
                continue

            # 6) Check if this inbound message was already processed
            phone_key_ctx = chat.get("phone_key") or title
            msg_key = last_msg.get("id") or hashlib.sha1(last_msg["text"].encode("utf-8")).hexdigest()

            if msg_key == state.get_last_seen_inbound(phone_key_ctx):
                reason = f"🔄 Msg inbound já vista (last_seen_inbound match)"
                detail = f"última=[{last_text_preview}]"
                results_table.append({"n": str(i), "title": short_title, "status": "SKIP", "reason": f"{reason} | {detail}"})
                log.info(
                    "  %s│ %s/%s │ %-45s │ %s │ %s%s",
                    C_YELLOW, i, len(candidates), short_title, reason, detail, C_RESET,
                )
                skipped += 1
                continue

            dedupe_key = f"{phone_key_ctx}:{msg_key}"
            if state.was_forwarded(dedupe_key):
                state.set_last_seen_inbound(phone_key_ctx, msg_key)
                reason = f"✅ Msg inbound já encaminhada (dedupe match)"
                detail = f"última=[{last_text_preview}]"
                results_table.append({"n": str(i), "title": short_title, "status": "SKIP", "reason": f"{reason} | {detail}"})
                log.info(
                    "  %s│ %s/%s │ %-45s │ %s │ %s%s",
                    C_YELLOW, i, len(candidates), short_title, reason, detail, C_RESET,
                )
                skipped += 1
                continue

            # 7) Resolve phone from alias/header
            phone_hint = wa_web.extract_phone_from_open_chat()
            if not phone_hint and chat.get("phone_key"):
                phone_hint = normalize_phone(chat["phone_key"])

            # 8) Resolve to an atendimento record
            atendimento = _resolve_chat_to_atendimento(title, phone_hint)
            if not atendimento or not atendimento.get("chat_url"):
                reason = f"⚠️  Sem atendimento/chat_url no DB (phone_hint={phone_hint or 'None'})"
                detail = f"última=[{last_text_preview}]"
                results_table.append({"n": str(i), "title": short_title, "status": "NO_MATCH", "reason": f"{reason} | {detail}"})
                log.info(
                    "  %s%s│ %s/%s │ %-45s │ %s │ %s%s",
                    C_RED, C_BOLD, i, len(candidates), short_title, reason, detail, C_RESET,
                )
                no_match += 1
                continue

            phone = atendimento.get("telefone") or phone_hint or chat.get("phone_key") or _phone_from_title(title)
            if phone:
                state.set_contact_alias(title, phone)
                phone_key_ctx = phone

            chat_url = atendimento["chat_url"]
            id_atendimento = atendimento.get("id_atendimento")
            id_paciente = atendimento.get("id_paciente")
            nome_paciente = atendimento.get("nome_paciente") or title

            # ═══ NOVA MENSAGEM DETECTADA ═══
            log.info(
                "\n%s%s  ┌─────────────────────────────────────────────────────────────────────┐%s\n"
                "%s%s  │ 🆕 NOVA MSG INBOUND │ %-46s │%s\n"
                "%s%s  │    Paciente: %-55s │%s\n"
                "%s%s  │    Phone: %-58s │%s\n"
                "%s%s  │    Atendimento: %-51s │%s\n"
                "%s%s  │    Mensagem: [%-54s] │%s\n"
                "%s%s  │    ChatGPT URL: %-51s │%s\n"
                "%s%s  └─────────────────────────────────────────────────────────────────────┘%s",
                C_GREEN, C_BOLD, C_RESET,
                C_GREEN, C_BOLD, short_title, C_RESET,
                C_GREEN, C_BOLD, nome_paciente[:55], C_RESET,
                C_GREEN, C_BOLD, phone or "(desconhecido)", C_RESET,
                C_GREEN, C_BOLD, str(id_atendimento), C_RESET,
                C_GREEN, C_BOLD, last_text_preview, C_RESET,
                C_GREEN, C_BOLD, build_preview_with_ellipsis(chat_url, 51), C_RESET,
                C_GREEN, C_BOLD, C_RESET,
            )

            # 9) Forward to ChatGPT simulator
            ctx = {
                "id_atendimento": id_atendimento,
                "id_paciente": id_paciente,
                "nome_paciente": nome_paciente,
                "pergunta": state.get_phone_context_field(phone_key_ctx, "pergunta") or "(acompanhamento)",
            }
            prompt = build_forward_prompt(ctx, last_msg["text"])
            log.info(
                "  %s🤖 Encaminhando ao ChatGPT Simulator...%s",
                C_BLUE, C_RESET,
            )
            res = send_to_chatgpt(
                url_chatgpt=chat_url,
                text=prompt,
                id_paciente=id_paciente,
                id_atendimento=id_atendimento,
            )
            answer_raw = _sanitize_simulator_answer((res.get("html") or "").strip())
            answer = _normalize_whatsapp_format(answer_raw) or "Recebido. A equipe entrará em contato se necessário."

            # 10) Log patient message and simulator response
            if phone:
                append_whatsapp_message(phone, role="user", content=last_msg["text"], source="whatsapp")
                append_whatsapp_message(phone, role="assistant", content=answer, source="chatgpt_simulator")

            # 11) Reply to the patient
            dest_phone = TEST_DESTINATION_PHONE if TEST_DESTINATION_PHONE else phone
            if dest_phone:
                wa_web.send_message(dest_phone, answer)
                log.info(
                    "  %s%s✅ Resposta enviada ao paciente '%s' (phone=%s)%s\n"
                    "  %s│ Resposta: [%s]%s",
                    C_BG_GREEN, C_WHITE, nome_paciente, dest_phone, C_RESET,
                    C_GREEN, build_preview_with_ellipsis(answer, 100), C_RESET,
                )
            else:
                log.warning(
                    "  %s⚠️  Sem telefone para responder ao chat '%s'%s",
                    C_YELLOW, title, C_RESET,
                )

            state.mark_forwarded(
                dedupe_key,
                {
                    "phone": phone_key_ctx,
                    "at": utc_now_iso(),
                    "id_atendimento": id_atendimento,
                    "id_paciente": id_paciente,
                    "patient_text": last_msg["text"],
                    "inbound_key": msg_key,
                    "chat_url": chat_url,
                },
            )
            state.set_last_seen_inbound(phone_key_ctx, msg_key)
            processed += 1
            results_table.append({
                "n": str(i), "title": short_title, "status": "PROCESSADO",
                "reason": f"✅ Encaminhado e respondido | msg=[{last_text_preview}]",
            })

            # 12) Atualiza snapshot do contato no DB
            try:
                _upsert_whatsapp_contact_profile(
                    phone=phone,
                    display_name=title,
                    profile_name="",
                    wa_chat_title=title,
                    id_paciente=id_paciente,
                    id_atendimento=id_atendimento,
                    source="monitor_incoming",
                )
            except Exception:
                log.exception("Falha ao atualizar snapshot de contato para chat '%s'", title)

        except Exception:
            log.exception("Falha ao processar resposta do chat '%s'", title)
            results_table.append({
                "n": str(i), "title": title[:45], "status": "ERRO",
                "reason": "💥 Exceção inesperada",
            })

    # ═══ TABELA RESUMO FINAL ═══
    sep = f"{C_CYAN}{'─' * 100}{C_RESET}"
    log.info(
        "\n%s%s╔══════════════════════════════════════════════════════════════════════════════════════════════════╗%s\n"
        "%s%s║  📊 RESUMO DO MONITOR DE RESPOSTAS                                                             ║%s\n"
        "%s%s╠══════╦══════════════════════════════════════════════════╦════════════╦═════════════════════════════╣%s\n"
        "%s%s║  #   ║ Contato                                        ║ Status     ║ Motivo                      ║%s\n"
        "%s%s╠══════╬══════════════════════════════════════════════════╬════════════╬═════════════════════════════╣%s",
        C_BOLD, C_CYAN, C_RESET,
        C_BOLD, C_CYAN, C_RESET,
        C_BOLD, C_CYAN, C_RESET,
        C_BOLD, C_CYAN, C_RESET,
        C_BOLD, C_CYAN, C_RESET,
    )
    for row in results_table:
        status = row["status"]
        if status == "PROCESSADO":
            color = C_GREEN
            icon = "✅"
        elif status == "NO_MATCH":
            color = C_RED
            icon = "⚠️ "
        elif status == "ERRO":
            color = C_RED
            icon = "❌"
        else:
            color = C_GRAY
            icon = "⏭️ "
        log.info(
            "%s║ %-4s ║ %-48s ║ %s%-10s%s ║ %-27s ║%s",
            C_CYAN, row["n"], row["title"][:48],
            color, f"{icon}{status}", C_CYAN,
            build_preview_with_ellipsis(row["reason"], 27),
            C_RESET,
        )
    log.info(
        "%s%s╠══════╩══════════════════════════════════════════════════╩════════════╩═════════════════════════════╣%s\n"
        "%s%s║  %s✅ Processadas: %-3s%s  %s⏭️  Ignoradas: %-3s%s  %s⚠️  Sem match: %-3s%s                                 %s║%s\n"
        "%s%s╚══════════════════════════════════════════════════════════════════════════════════════════════════╝%s",
        C_BOLD, C_CYAN, C_RESET,
        C_BOLD, C_CYAN,
        C_GREEN, processed, C_CYAN,
        C_YELLOW, skipped, C_CYAN,
        C_RED, no_match, C_CYAN,
        C_CYAN, C_RESET,
        C_BOLD, C_CYAN, C_RESET,
    )

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
            process_incoming_replies_once()
        except Exception:
            log.exception("Falha no monitor de respostas")
        time.sleep(REPLY_POLL_INTERVAL_SEC)


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "service": "acompanhamento_whatsapp_server",
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
