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
#   save_chat(chat_id, title, url, msgs)
#   append_message(chat_id, role, content)
#   update_full_history(chat_id, msgs)  — sincroniza com histórico do browser
# =============================================================================
import json
import os
from datetime import datetime
import config
from utils import log
import threading
_lock = threading.Lock()
import hashlib

def append_message(chat_id, role, content):
    """Adiciona uma mensagem ao histórico de forma atômica."""
    with _lock:  # (usar o lock do fix S2)
        data = _load_chats_unlocked()
        if chat_id not in data:
            data[chat_id] = {
                "title": "Novo Chat", "url": "",
                "created_at": datetime.now().isoformat(), "messages": []
            }
        data[chat_id]['messages'].append({"role": role, "content": content})
        data[chat_id]['updated_at'] = datetime.now().isoformat()
        with open(config.CHATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

def _load_chats_unlocked():
    """Lê sem lock — usar apenas dentro de seções já protegidas."""
    if not os.path.exists(config.CHATS_FILE): return {}
    try:
        with open(config.CHATS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except: return {}

def load_chats():
    with _lock:
        return _load_chats_unlocked()

def save_chat(chat_id, title, url, messages):
    with _lock:
        data = _load_chats_unlocked()  # leitura dentro do lock
    if chat_id not in data:
        data[chat_id] = {
            "title": title or "Novo Chat",
            "url": url,
            "created_at": datetime.now().isoformat(),
            "messages": []
        }
    
    if title: data[chat_id]['title'] = title
    if url: data[chat_id]['url'] = url
    data[chat_id]['updated_at'] = datetime.now().isoformat()
    
    # Adição simples (para streaming)
    existing_contents = [m['content'] for m in data[chat_id]['messages']]
    for msg in messages:
        if msg['content'] not in existing_contents:
            data[chat_id]['messages'].append(msg)
    
    with open(config.CHATS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    return data[chat_id]


def get_meta(content):
    if not content: return ""
    return hashlib.md5(content.encode('utf-8', errors='ignore')).hexdigest()

def update_full_history(chat_id, browser_messages):
    data = load_chats()
    if chat_id not in data: return False

    local_msgs = data[chat_id]['messages']
    has_changes = False
    
    log("storage.py", f"Validando {len(browser_messages)} mensagens recebidas...")

    for i, b_msg in enumerate(browser_messages):
        b_content = b_msg['content']
        
        if i < len(local_msgs):
            l_content = local_msgs[i]['content']
            
            # --- OTIMIZAÇÃO SOLICITADA ---
            # Compara 1ª palavra e tamanho total
            b_meta = get_meta(b_content)
            l_meta = get_meta(l_content)
            
            if b_meta == l_meta:
                # Se forem idênticos nestes critérios, assumimos que é igual
                # PULA para o próximo, economizando processamento
                continue 
            
            # Se diferente, verifica se vale atualizar (se browser é mais completo)
            if len(b_content) > len(l_content) or b_content != l_content:
                log("storage.py", f"Atualizando msg #{i} (Dif: {len(b_content)-len(l_content)} chars)")
                local_msgs[i] = b_msg
                has_changes = True
        else:
            # Mensagem nova (append)
            local_msgs.append(b_msg)
            has_changes = True

    if has_changes:
        data[chat_id]['messages'] = local_msgs
        data[chat_id]['updated_at'] = datetime.now().isoformat()
        with open(config.CHATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log("storage.py", "Histórico sincronizado com sucesso.")
            
    return has_changes