# =============================================================================
# storage.py — Persistência de histórico de chats em JSON
# =============================================================================
#
# RESPONSABILIDADE:
#   Leitura e escrita thread-safe do arquivo history.json que armazena todos
#   os chats do simulador (título, URL, mensagens). Utiliza threading.Lock
#   para evitar condições de corrida entre múltiplas threads.
#
# RELAÇÕES:
#   • Importado por: server.py (lê/salva chats), browser.py (não diretamente —
#                    server.py intermedia)
#   • Lê/escreve: config.CHATS_FILE (db/history.json)
#   • Importa: config.py, utils.py
#
# FUNÇÕES PRINCIPAIS:
#   load_chats()                        → dict de todos os chats
#   save_chat(chat_id, title, url, msgs, origin_url=None)
#   append_message(chat_id, role, content)
#   update_full_history(chat_id, msgs, title=None, url=None)  — sincroniza com histórico do browser
# =============================================================================
import json
import os
from datetime import datetime
import config
from utils import log
import threading
import hashlib
from urllib.parse import parse_qs, urlparse

_lock = threading.Lock()


def _write_chats_unlocked(data):
    with open(config.CHATS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _ensure_chat_dict(data, chat_id, title="Novo Chat", url="", origin_url=""):
    if chat_id not in data:
        data[chat_id] = {
            "title": title or "Novo Chat",
            "url": url or "",
            "origin_url": origin_url or "",
            "created_at": datetime.now().isoformat(),
            "messages": []
        }
    else:
        data[chat_id].setdefault('title', title or 'Novo Chat')
        data[chat_id].setdefault('url', url or '')
        data[chat_id].setdefault('origin_url', origin_url or '')
        data[chat_id].setdefault('messages', [])
    return data[chat_id]


def append_message(chat_id, role, content):
    """Adiciona uma mensagem ao histórico de forma atômica, evitando duplicata consecutiva."""
    with _lock:
        data = _load_chats_unlocked()
        chat = _ensure_chat_dict(data, chat_id)
        nova = {"role": role, "content": content}
        mensagens = chat['messages']
        if not mensagens or mensagens[-1] != nova:
            mensagens.append(nova)
        chat['updated_at'] = datetime.now().isoformat()
        _write_chats_unlocked(data)


def _load_chats_unlocked():
    """Lê sem lock — usar apenas dentro de seções já protegidas."""
    if not os.path.exists(config.CHATS_FILE):
        return {}
    try:
        with open(config.CHATS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def load_chats():
    with _lock:
        return _load_chats_unlocked()


def save_chat(chat_id, title, url, messages, origin_url=None):
    with _lock:
        data = _load_chats_unlocked()
        chat = _ensure_chat_dict(data, chat_id, title=title or "Novo Chat", url=url or "", origin_url=origin_url or "")

        if title:
            chat['title'] = title
        if url is not None:
            chat['url'] = url
        if origin_url:
            chat['origin_url'] = origin_url

        chat['updated_at'] = datetime.now().isoformat()
        existing_pairs = {(m.get('role'), m.get('content')) for m in chat['messages']}
        for msg in messages:
            par = (msg.get('role'), msg.get('content'))
            if par not in existing_pairs:
                chat['messages'].append(msg)
                existing_pairs.add(par)

        _write_chats_unlocked(data)
        return chat


def get_meta(content):
    if not content:
        return ""
    return hashlib.md5(content.encode('utf-8', errors='ignore')).hexdigest()


def update_full_history(chat_id, browser_messages, title=None, url=None):
    with _lock:
        data = _load_chats_unlocked()
        chat = _ensure_chat_dict(data, chat_id, title=title or "Novo Chat", url=url or "")
        local_msgs = chat['messages']
        has_changes = False

        log("storage.py", f"Validando {len(browser_messages)} mensagens recebidas...")

        for i, b_msg in enumerate(browser_messages):
            b_content = b_msg['content']

            if i < len(local_msgs):
                l_content = local_msgs[i]['content']
                b_meta = get_meta(b_content)
                l_meta = get_meta(l_content)

                if b_meta == l_meta:
                    continue

                if len(b_content) > len(l_content) or b_content != l_content:
                    log("storage.py", f"Atualizando msg #{i} (Dif: {len(b_content)-len(l_content)} chars)")
                    local_msgs[i] = b_msg
                    has_changes = True
            else:
                local_msgs.append(b_msg)
                has_changes = True

        if title and chat.get('title') != title:
            chat['title'] = title
            has_changes = True
        if url and chat.get('url') != url:
            chat['url'] = url
            has_changes = True

        if has_changes:
            chat['messages'] = local_msgs
            chat['updated_at'] = datetime.now().isoformat()
            _write_chats_unlocked(data)
            log("storage.py", "Histórico sincronizado com sucesso.")

        return has_changes


def _normalize_lookup_value(value):
    if value is None:
        return None
    value = str(value).strip()
    if not value or value.lower() in {"null", "none", "undefined"}:
        return None
    return value


def _extract_origin_lookup_ids(origin_url: str):
    """Extrai ids de contexto da própria origin_url para busca resiliente."""
    if not origin_url:
        return {
            'id_paciente': None,
            'id_atendimento': None,
            'id_receita': None,
        }

    try:
        query = parse_qs(urlparse(origin_url).query, keep_blank_values=True)
    except Exception:
        query = {}

    return {
        'id_paciente': _normalize_lookup_value((query.get('id_paciente') or [None])[0]),
        'id_atendimento': _normalize_lookup_value((query.get('id_atendimento') or [None])[0]),
        'id_receita': _normalize_lookup_value((query.get('id_receita') or [None])[0]),
    }


def find_chat_by_origin(origin_url: str):
    """Retorna o chat local mais recente associado ao contexto da origin_url."""
    if not origin_url:
        return None

    target_ids = _extract_origin_lookup_ids(origin_url)
    has_target_ids = any(v is not None for v in target_ids.values())

    with _lock:
        data = _load_chats_unlocked()

    candidatos = []
    for chat_id, chat in data.items():
        chat_origin_url = chat.get('origin_url') or ''
        chat_ids = _extract_origin_lookup_ids(chat_origin_url)

        if has_target_ids:
            if chat_ids != target_ids:
                continue
        elif chat_origin_url != origin_url:
            continue

        candidatos.append((chat.get('updated_at') or chat.get('created_at') or '', chat_id, chat))

    if not candidatos:
        return None

    candidatos.sort(reverse=True)
    _dt, chat_id, chat = candidatos[0]
    return {
        'chat_id': chat_id,
        'title': chat.get('title') or 'Novo Chat',
        'url': chat.get('url') or '',
        'origin_url': chat.get('origin_url') or '',
        'messages': chat.get('messages') or [],
        'updated_at': chat.get('updated_at') or chat.get('created_at') or '',
    }


def delete_chat(chat_id: str) -> bool:
    """Remove um chat do histórico local por chat_id."""
    if not chat_id:
        return False
    with _lock:
        data = _load_chats_unlocked()
        if chat_id in data:
            del data[chat_id]
            _write_chats_unlocked(data)
            log("storage.py", f"Chat {chat_id} removido do histórico local.")
            return True
    return False


def delete_chats_by_origin(origin_url: str) -> int:
    """Remove todos os chats associados a uma origin_url. Retorna a quantidade removida."""
    if not origin_url:
        return 0

    target_ids = _extract_origin_lookup_ids(origin_url)
    has_target_ids = any(v is not None for v in target_ids.values())

    with _lock:
        data = _load_chats_unlocked()
        to_delete = []
        for chat_id, chat in data.items():
            chat_origin_url = chat.get('origin_url') or ''
            chat_ids = _extract_origin_lookup_ids(chat_origin_url)

            if has_target_ids:
                if chat_ids == target_ids:
                    to_delete.append(chat_id)
            elif chat_origin_url == origin_url:
                to_delete.append(chat_id)

        for cid in to_delete:
            del data[cid]

        if to_delete:
            _write_chats_unlocked(data)
            log("storage.py", f"{len(to_delete)} chat(s) removido(s) por origin_url.")

    return len(to_delete)
