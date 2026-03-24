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
import re
import threading
import time
from datetime import datetime, timezone
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
  caa.id_atendimento,
  caa.id_paciente,
  COALESCE(m.telefone1,m.telefone2,m.telefone1pais,m.telefone2pais) AS telefone,
  m.nome AS nome_paciente,
  caa.mensagens_acompanhamento,
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


def resolve_phone_with_member_fallback(raw_phone: Any, id_paciente: Any) -> Optional[str]:
    direct = normalize_phone(raw_phone)
    if is_valid_br_mobile_phone(direct):
        return direct

    try:
        id_int = int(id_paciente)
    except (TypeError, ValueError):
        return direct if direct else None

    try:
        rows = run_sql(
            f"SELECT telefone1, telefone2 FROM membros WHERE id = {id_int} LIMIT 1"
        )
    except Exception:
        log.exception("Falha ao buscar telefone fallback em membros para id_paciente=%s", id_paciente)
        return direct if direct else None

    if not rows:
        return direct if direct else None

    row = rows[0] or {}
    candidates = [row.get("telefone1"), row.get("telefone2"), raw_phone]
    for candidate in candidates:
        normalized = normalize_phone(candidate)
        if is_valid_br_mobile_phone(normalized):
            return normalized

    return direct if direct else None


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


class WhatsAppWebClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._playwright = None
        self._browser = None
        self._page = None

    def start(self) -> None:
        with self._lock:
            if self._page is not None:
                return
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(WHATSAPP_PROFILE_DIR),
                headless=False,
                args=["--start-maximized"],
            )
            self._page = self._browser.new_page()
            self._page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded")
            self._wait_ready()
            log.info("WhatsApp Web pronto.")

    def _wait_ready(self, timeout_ms: int = 180000) -> None:
        assert self._page is not None
        try:
            self._page.wait_for_selector('div[aria-label="Chat list"], #pane-side', timeout=timeout_ms)
        except PlaywrightTimeoutError:
            log.error(
                "WhatsApp Web não autenticado. Abra a janela e faça login via QR Code em %s",
                WHATSAPP_WEB_URL,
            )
            raise

    def _open_chat(self, phone: str) -> None:
        assert self._page is not None
        url = f"https://web.whatsapp.com/send?phone={phone}&text={quote('')}&app_absent=0"
        self._page.goto(url, wait_until="domcontentloaded")
        self._page.wait_for_timeout(1200)
        self._page.wait_for_selector("footer div[contenteditable='true']", timeout=30000)

    def send_message(self, phone: str, text: str) -> None:
        with self._lock:
            self.start()
            self._open_chat(phone)
            box = self._page.locator("footer div[contenteditable='true']").first
            box.click()
            box.fill(text)
            box.press("Enter")
            self._page.wait_for_timeout(500)

    def read_last_inbound(self, phone: str) -> Optional[Dict[str, str]]:
        with self._lock:
            self.start()
            self._open_chat(phone)
            data = self._page.evaluate(
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
            return data


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
    errors = 0
    recovered_member_phone = 0

    for row in rows:
        id_atendimento = row.get("id_atendimento")
        id_paciente = row.get("id_paciente")
        nome_paciente = row.get("nome_paciente")
        url_chatgpt = (row.get("url_chatgpt") or "").strip()
        original_phone = normalize_phone(row.get("telefone"))
        phone = resolve_phone_with_member_fallback(row.get("telefone"), id_paciente)
        if (not original_phone or not is_valid_br_mobile_phone(original_phone)) and phone:
            recovered_member_phone += 1

        if not phone:
            skipped += 1
            skipped_missing_phone += 1
            continue

        itens = extract_followup_items(row.get("mensagens_acompanhamento"))
        if not itens:
            skipped += 1
            skipped_empty_followup += 1
            continue
        total_followup_items += len(itens)

        for key, pergunta in itens:
            dedupe_key = f"{id_atendimento}:{key}:{hashlib.sha1(pergunta.encode('utf-8')).hexdigest()}"
            if state.is_sent(dedupe_key):
                skipped += 1
                skipped_already_sent += 1
                continue

            try:
                wa_web.send_message(
                    phone,
                    "Olá! Aqui é o acompanhamento da sua consulta.\n\n"
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
    if stats.get("errors", 0):
        reasons.append(f"falha ao enviar={stats['errors']}")
    return "; ".join(reasons) if reasons else "nenhum motivo classificado"


def process_incoming_replies_once() -> Dict[str, int]:
    processed = 0
    skipped = 0

    contexts = state.all_phone_contexts()
    for phone, ctx in contexts.items():
        try:
            inbound = wa_web.read_last_inbound(phone)
            if not inbound:
                skipped += 1
                continue

            msg_key = inbound.get("id") or hashlib.sha1(inbound["text"].encode("utf-8")).hexdigest()
            if msg_key == state.get_last_seen_inbound(phone):
                skipped += 1
                continue

            dedupe_key = f"{phone}:{msg_key}"
            if state.was_forwarded(dedupe_key):
                state.set_last_seen_inbound(phone, msg_key)
                skipped += 1
                continue

            url_chatgpt = (ctx.get("url_chatgpt") or "").strip()
            if not url_chatgpt:
                skipped += 1
                continue

            prompt = build_forward_prompt(ctx, inbound["text"])
            res = send_to_chatgpt(
                url_chatgpt=url_chatgpt,
                text=prompt,
                id_paciente=ctx.get("id_paciente"),
                id_atendimento=ctx.get("id_atendimento"),
            )
            answer = (res.get("html") or "").strip() or "Recebido. A equipe entrará em contato se necessário."
            wa_web.send_message(phone, answer)

            state.mark_forwarded(
                dedupe_key,
                {
                    "phone": phone,
                    "at": utc_now_iso(),
                    "ctx": ctx,
                    "patient_text": inbound["text"],
                    "inbound_key": msg_key,
                },
            )
            state.set_last_seen_inbound(phone, msg_key)
            processed += 1

        except Exception:
            log.exception("Falha ao processar resposta do telefone %s", phone)

    return {"processed": processed, "skipped": skipped}


def scheduler_loop() -> None:
    log.info("Scheduler de envios iniciado. Intervalo: %ss", POLL_INTERVAL_SEC)
    while True:
        try:
            stats = send_pending_followups_once()
            motivos = _build_skip_reason_summary(stats)
            log.info(
                "Envio acompanhamento | total=%s itens=%s enviados=%s ignorados=%s "
                "(sem_telefone=%s, sem_mensagem=%s, ja_enviado=%s, erros=%s, recuperado_membros=%s, motivos=%s)",
                stats["total"],
                stats["total_followup_items"],
                stats["sent"],
                stats["skipped"],
                stats["skipped_missing_phone"],
                stats["skipped_empty_followup"],
                stats["skipped_already_sent"],
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
    log.info("Monitor de respostas iniciado. Intervalo: %ss", REPLY_POLL_INTERVAL_SEC)
    while True:
        try:
            stats = process_incoming_replies_once()
            if stats["processed"] > 0:
                log.info("Respostas processadas: %s", stats["processed"])
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

    log.info("Iniciando browser WhatsApp. Se necessário, faça login via QR Code...")
    wa_web.start()

    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=replies_loop, daemon=True).start()

    log.info("Servidor de acompanhamento WhatsApp Web em %s:%s", HOST, PORT)
    app.run(host=HOST, port=PORT)
