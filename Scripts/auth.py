# =============================================================================
# auth.py — Autenticação e gerenciamento de sessões do ChatGPT Simulator
# =============================================================================
#
# RESPONSABILIDADE:
#   Gerencia login/logout de usuários, hash de senhas, tokens de sessão em
#   memória e leitura/escrita do arquivo users.json. Não usa banco de dados
#   externo — o estado de sessão vive apenas em memória (SESSIONS dict).
#
# RELAÇÕES:
#   • Importado por: server.py (valida requisições HTTP)
#   • Lê/escreve: config.USERS_FILE (db/users/users.json)
#
# FUNÇÕES PRINCIPAIS:
#   verify_login(username, password) → token | None
#   check_session(request)           → username | None
#   get_user_info(token)             → {username, avatar} | None
#   logout(token)
#   change_password(username, new_password)
#   update_avatar(username, filename)
# =============================================================================
import os
import json
import hashlib
import uuid
import config
from datetime import datetime, timedelta

# Sessões ativas: {token: user_id}
SESSIONS = {}

def load_users():
    if not os.path.exists(config.USERS_FILE):
        # Cria admin padrão se não existir
        default_users = {
            "admin": {
                "password": hash_password("32713091"),
                "avatar": None
            }
        }
        save_users(default_users)
        return default_users
    
    with open(config.USERS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_users(users_data):
    with open(config.USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users_data, f, indent=4)

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def verify_login(username, password):
    users = load_users()
    if username not in users: return None
    
    if users[username]['password'] == hash_password(password):
        token = str(uuid.uuid4())
        SESSIONS[token] = username
        return token
    return None

def change_password(username, new_password):
    users = load_users()
    if username in users:
        users[username]['password'] = hash_password(new_password)
        save_users(users)
        return True
    return False

def update_avatar(username, filename):
    users = load_users()
    if username in users:
        users[username]['avatar'] = filename
        save_users(users)
        return True
    return False

def get_user_info(token):
    user_id = SESSIONS.get(token)
    if not user_id: return None
    
    users = load_users()
    user_data = users.get(user_id, {})
    return {
        "username": user_id,
        "avatar": user_data.get("avatar")
    }

def check_session(request):
    cookie = request.cookies.get('session_token')
    if cookie and cookie in SESSIONS:
        return SESSIONS[cookie]
    return None

def logout(token):
    if token in SESSIONS:
        del SESSIONS[token]