#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Servidor PyWa para envio de mensagens de acompanhamento e encaminhamento das respostas
para o ChatGPT Simulator.

Fluxo:
1) Polling periódico no PHP (action=execute_sql) para buscar atendimentos com
   `mensagens_acompanhamento`.
2) Envio das mensagens ao WhatsApp do paciente via PyWa.
3) Ao receber resposta do paciente, encaminha imediatamente ao chat específico
   (`url_chatgpt`) no endpoint local `/v1/chat/completions`.

Observação importante:
- O script guarda estado local em `db/pywa_followup_state.json` para evitar reenvio.
- SQLs podem (e devem) ser ajustados via variáveis de ambiente sem editar o código.
"""

import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify
from pywa import WhatsApp, types


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

WA_PHONE_ID = os.getenv("PYWA_PHONE_ID", "")
WA_TOKEN = os.getenv("PYWA_TOKEN", "")
WA_VERIFY_TOKEN = os.getenv("PYWA_VERIFY_TOKEN", "").strip() or "pywa_local_verify_token"
WA_APP_SECRET = os.getenv("PYWA_APP_SECRET", "").strip()

HOST = os.getenv("PYWA_HOST", "0.0.0.0")
PORT = int(os.getenv("PYWA_PORT", "3011"))
POLL_INTERVAL_SEC = int(os.getenv("PYWA_POLL_INTERVAL_SEC", "120"))
REQUEST_TIMEOUT_SEC = int(os.getenv("PYWA_REQUEST_TIMEOUT_SEC", "45"))

BASE_DIR = Path(__file__).resolve().parents[1]
DB_DIR = BASE_DIR / "db"
DB_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DB_DIR / "pywa_followup_state.json"

# SQL padrão: usa mensagens_acompanhamento + url_chatgpt do mesmo atendimento.
# Ajuste conforme seu schema real através de PYWA_FETCH_SQL.
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


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("pywa_acompanhamento")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────
# ESTADO LOCAL
# ─────────────────────────────────────────────────────────────
class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.state = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {
                "sent_questions": {},
                "phone_context": {},
                "forwarded_messages": {},
                "updated_at": utc_now_iso(),
            }
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "sent_questions": {},
                "phone_context": {},
                "forwarded_messages": {},
                "updated_at": utc_now_iso(),
            }

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

    def get_phone_context(self, phone: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            return self.state["phone_context"].get(phone)

    def was_forwarded(self, msg_id: str) -> bool:
        with self.lock:
            return msg_id in self.state["forwarded_messages"]

    def mark_forwarded(self, msg_id: str, payload: Dict[str, Any]) -> None:
        with self.lock:
            self.state["forwarded_messages"][msg_id] = payload
        self.save()


state = StateStore(STATE_FILE)


# ─────────────────────────────────────────────────────────────
# CLIENTE PHP/SIMULATOR
# ─────────────────────────────────────────────────────────────
def _php_post(action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload)
    data["api_key"] = PHP_API_KEY
    url = f"{PHP_URL}?action={action}"
    r = requests.post(url, json=data, timeout=REQUEST_TIMEOUT_SEC)
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


# ─────────────────────────────────────────────────────────────
# UTILITÁRIOS
# ─────────────────────────────────────────────────────────────
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


def extract_followup_items(mensagens_acompanhamento: Any) -> List[Tuple[str, str]]:
    """
    Retorna lista [(chave, mensagem)] em ordem estável.
    Aceita dict JSON, string JSON ou string livre.
    """
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
        ordered_keys = [
            "mensagem_1_semana",
            "mensagem_1_mes",
            "mensagem_pre_retorno",
        ]
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
        out: List[Tuple[str, str]] = []
        for i, item in enumerate(payload, start=1):
            msg = str(item).strip()
            if msg:
                out.append((f"mensagem_{i}", msg))
        return out

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
        "Com base nessa resposta, forneça a orientação clínica de continuidade "
        "de forma objetiva e segura para envio ao paciente."
    )


# ─────────────────────────────────────────────────────────────
# PYWA
# ─────────────────────────────────────────────────────────────
flask_app = Flask(__name__)

wa = WhatsApp(
    phone_id=WA_PHONE_ID,
    token=WA_TOKEN,
    server=flask_app,
    verify_token=WA_VERIFY_TOKEN,
    app_secret=WA_APP_SECRET or None,
    validate_updates=bool(WA_APP_SECRET),
)


@wa.on_message
def on_patient_message(_: WhatsApp, msg: types.Message):
    sender = normalize_phone(getattr(msg, "from_user", None).wa_id if getattr(msg, "from_user", None) else getattr(msg, "from_", ""))
    text = (getattr(msg, "text", "") or "").strip()
    msg_id = getattr(msg, "id", None)

    if not sender or not text:
        return

    if msg_id and state.was_forwarded(msg_id):
        return

    ctx = state.get_phone_context(sender)
    if not ctx:
        msg.reply_text(
            "Recebi sua mensagem ✅\n"
            "No momento não encontrei um acompanhamento ativo vinculado a este número. "
            "Se precisar, a equipe irá te orientar."
        )
        return

    url_chatgpt = ctx.get("url_chatgpt")
    if not url_chatgpt:
        msg.reply_text(
            "Recebi sua resposta ✅\n"
            "Ainda não consegui localizar o chat clínico deste acompanhamento. "
            "A equipe já foi sinalizada para finalizar manualmente."
        )
        return

    prompt = build_forward_prompt(ctx, text)

    try:
        res = send_to_chatgpt(
            url_chatgpt=url_chatgpt,
            text=prompt,
            id_paciente=ctx.get("id_paciente"),
            id_atendimento=ctx.get("id_atendimento"),
        )
        answer = (res.get("html") or "").strip() or "Recebido. A equipe entrará em contato se necessário."
        msg.reply_text(answer)

        if msg_id:
            state.mark_forwarded(
                msg_id,
                {
                    "phone": sender,
                    "at": utc_now_iso(),
                    "ctx": ctx,
                    "patient_text": text,
                },
            )
    except Exception as exc:
        log.exception("Falha ao encaminhar resposta para o ChatGPT: %s", exc)
        msg.reply_text(
            "Recebi sua resposta ✅\n"
            "Houve instabilidade ao processar agora. Sua mensagem foi registrada e a equipe foi avisada."
        )


# ─────────────────────────────────────────────────────────────
# DISPARO DE ACOMPANHAMENTO
# ─────────────────────────────────────────────────────────────
def send_pending_followups_once() -> Dict[str, Any]:
    rows = run_sql(FETCH_SQL)
    sent = 0
    skipped = 0

    for row in rows:
        id_atendimento = row.get("id_atendimento")
        id_paciente = row.get("id_paciente")
        nome_paciente = row.get("nome_paciente")
        url_chatgpt = (row.get("url_chatgpt") or "").strip()

        phone = normalize_phone(row.get("telefone"))
        if not phone:
            skipped += 1
            continue

        itens = extract_followup_items(row.get("mensagens_acompanhamento"))
        if not itens:
            skipped += 1
            continue

        for key, pergunta in itens:
            dedupe_key = f"{id_atendimento}:{key}:{hash(pergunta)}"
            if state.is_sent(dedupe_key):
                continue

            try:
                wa.send_message(
                    to=phone,
                    text=(
                        "Olá! Aqui é o acompanhamento da sua consulta.\n\n"
                        f"{pergunta}\n\n"
                        "Pode me responder por aqui?"
                    ),
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
                log.exception(
                    "Falha ao enviar acompanhamento (atendimento=%s, paciente=%s, key=%s)",
                    id_atendimento,
                    id_paciente,
                    key,
                )

    return {"total": len(rows), "sent": sent, "skipped": skipped}


def scheduler_loop() -> None:
    log.info("Scheduler iniciado. Intervalo: %ss", POLL_INTERVAL_SEC)
    while True:
        try:
            stats = send_pending_followups_once()
            log.info(
                "Ciclo finalizado | total=%s enviados=%s ignorados=%s",
                stats["total"],
                stats["sent"],
                stats["skipped"],
            )
        except Exception:
            log.exception("Falha no ciclo de envio de acompanhamentos")

        time.sleep(POLL_INTERVAL_SEC)


# ─────────────────────────────────────────────────────────────
# ROTAS DE APOIO
# ─────────────────────────────────────────────────────────────
@flask_app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "service": "pywa_acompanhamento_server",
            "state_file": str(STATE_FILE),
            "poll_interval_sec": POLL_INTERVAL_SEC,
        }
    )


@flask_app.post("/send-now")
def send_now():
    stats = send_pending_followups_once()
    return jsonify({"ok": True, **stats})


if __name__ == "__main__":
    if os.getenv("PYWA_VERIFY_TOKEN", "").strip() == "":
        log.warning(
            "PYWA_VERIFY_TOKEN não informado. Usando token local padrão: %s",
            WA_VERIFY_TOKEN,
        )

    if not WA_PHONE_ID or not WA_TOKEN:
        log.error("Configure PYWA_PHONE_ID e PYWA_TOKEN antes de iniciar o servidor.")
        log.error(
            "Guia rápido de configuração:\n"
            "1) Crie app/credenciais no Meta for Developers:\n"
            "   https://developers.facebook.com/apps/\n"
            "2) Configure WhatsApp Cloud API:\n"
            "   https://developers.facebook.com/docs/whatsapp/cloud-api/get-started\n"
            "3) Pegue Phone Number ID e Access Token e defina:\n"
            "   - PYWA_PHONE_ID\n"
            "   - PYWA_TOKEN\n"
            "4) Configure o webhook e verify token (PYWA_VERIFY_TOKEN):\n"
            "   https://developers.facebook.com/docs/graph-api/webhooks/getting-started\n"
            "5) (Recomendado) Configure app secret para validar assinatura:\n"
            "   - PYWA_APP_SECRET\n"
            "   Docs PyWa: https://pywa.readthedocs.io/\n"
            "6) Se usar este servidor localmente, exponha a porta com túnel HTTPS (ex.: ngrok):\n"
            "   https://ngrok.com/docs/getting-started/\n"
            "7) Aponte o callback do webhook para: https://SEU_DOMINIO/webhooks"
        )
        sys.exit(1)

    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

    log.info("Servidor PyWa de acompanhamento iniciado em %s:%s", HOST, PORT)
    flask_app.run(host=HOST, port=PORT)
