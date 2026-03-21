# =============================================================================
# config.py — Configurações centrais do ChatGPT Simulator
# =============================================================================
#
# RESPONSABILIDADE:
#   Define todas as constantes globais do sistema: versão, portas, caminhos
#   de arquivos e diretórios. É importado por praticamente todos os outros
#   módulos e garante que as pastas necessárias existam ao ser carregado.
#
# RELAÇÕES:
#   • Importado por: main.py, server.py, browser.py, auth.py, storage.py,
#                    utils.py, analisador_prontuarios.py
#
# CONSTANTES PRINCIPAIS:
#   VERSION       — versão atual do sistema
#   PORT          — porta HTTPS principal (3002); HTTP auxiliar = PORT+1 (3003)
#   API_KEY       — chave de autenticação usada pelo analisador e pelo PHP
#   BASE_DIR      — raiz do projeto em disco
#   DIRS          — dicionário com todos os subdiretórios criados automaticamente
#   CHATS_FILE    — JSON com histórico de chats
#   USERS_FILE    — JSON com usuários e senhas
#   CERT_FILE     — certificado TLS autoassinado
#   KEY_FILE      — chave privada TLS
# =============================================================================
# -*- coding: utf-8 -*-
import os
from datetime import datetime

VERSION = "11.0"
PORT = 3002
API_KEY = "CVAPI_2b9c80c2abf94a76baf8b3e68d89cb7e"
BASE_DIR = r"C:\chatgpt_simulator"

DIRS = {
    "certs": os.path.join(BASE_DIR, "certs"),
    "frontend": os.path.join(BASE_DIR, "frontend"),
    "db": os.path.join(BASE_DIR, "db"),
    "users": os.path.join(BASE_DIR, "db", "users"), # Novo diretório
    "logs": os.path.join(BASE_DIR, "logs"),
    "profile": os.path.join(BASE_DIR, "chrome_profile"),
    "temp": os.path.join(BASE_DIR, "temp"),
    "downloads": os.path.join(BASE_DIR, "downloads")
}

CHATS_FILE = os.path.join(DIRS["db"], "history.json")
USERS_FILE = os.path.join(DIRS["users"], "users.json") # Arquivo de usuários
CERT_FILE = os.path.join(DIRS["certs"], "cert.pem")
KEY_FILE = os.path.join(DIRS["certs"], "key.pem")
FRONTEND_FILE = os.path.join(DIRS["frontend"], "index.html")

log_filename = datetime.now().strftime("simulator-%d_%m_%Y-%H_%M_%S.log")
LOG_PATH = os.path.join(DIRS["logs"], log_filename)

for d in DIRS.values(): os.makedirs(d, exist_ok=True)
