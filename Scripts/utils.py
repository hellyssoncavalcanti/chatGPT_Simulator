# =============================================================================
# utils.py — Utilitários de infraestrutura do ChatGPT Simulator
# =============================================================================
#
# RESPONSABILIDADE:
#   Funções auxiliares usadas por múltiplos módulos: geração de certificados
#   TLS autoassinados, setup do frontend estático e logging em arquivo.
#
# RELAÇÕES:
#   • Importado por: main.py (setup inicial), server.py (log), browser.py (log),
#                    storage.py (log)
#
# FUNÇÕES PRINCIPAIS:
#   ensure_certificates() — gera cert.pem/key.pem em config.DIRS["certs"]
#                           se não existirem
#   setup_frontend()      — copia ou garante existência do index.html em
#                           config.DIRS["frontend"]
#   log(module, message)  — escreve no arquivo de log e no stdout com timestamp
# =============================================================================
import sys
import subprocess
import importlib.util
import os
import shutil
import logging
import socket
import ipaddress
from datetime import datetime, timedelta

# --- AUTO-INSTALLER (CORE) ---
def check_and_install(package, import_name=None):
    if import_name is None: import_name = package
    if importlib.util.find_spec(import_name) is None:
        print(f"🔧 Instalando dependência: {package}...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", package])
            print(f"✅ {package} instalado com sucesso!")
        except Exception as e:
            print(f"❌ Erro ao instalar {package}: {e}")

# Verifica dependências básicas do Backend
check_and_install("cryptography")
check_and_install("flask")
check_and_install("requests")
check_and_install("markdownify")

# Imports Seguros (agora que garantimos a instalação)
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
import config

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", handlers=[logging.FileHandler(config.LOG_PATH, encoding='utf-8'), logging.StreamHandler()])
def log(source, msg): logging.info(f"[{source}] {msg}")

def ensure_certificates():
    if os.path.exists(config.CERT_FILE) and os.path.exists(config.KEY_FILE): return
    log("utils.py", "🔐 Gerando certificados SSL...")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"localhost")])
    cert = x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(datetime.utcnow()).not_valid_after(datetime.utcnow() + timedelta(days=3650)).add_extension(x509.SubjectAlternativeName([x509.DNSName(u"localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False).sign(key, hashes.SHA256())
    with open(config.KEY_FILE, "wb") as f: f.write(key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.TraditionalOpenSSL, encryption_algorithm=serialization.NoEncryption()))
    with open(config.CERT_FILE, "wb") as f: f.write(cert.public_bytes(serialization.Encoding.PEM))

def setup_frontend():
    # Variáveis JS injetadas (Sanitização básica)
    safe_api_key = config.API_KEY.strip()
    js_config_block = f"""
    const API_KEY = "{safe_api_key}";
    const HTTP_PORT = {config.PORT + 1};
    """

    # Template HTML

    html_template = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>ChatGPT Simulator</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
    :root { --bg-body: #343541; --bg-sidebar: #202123; --text: #ececf1; --bg-user: #343541; --bg-gpt: #444654; }
    body { margin: 0; font-family: 'Segoe UI', sans-serif; background: var(--bg-body); color: var(--text); display: flex; height: 100vh; overflow: hidden; }
    .sidebar { width: 260px; background: var(--bg-sidebar); border-right: 1px solid #4d4d4f; display: flex; flex-direction: column; }
    .chat-list { flex: 1; overflow-y: auto; padding: 10px; }
    .chat-item { padding: 10px; margin: 5px 0; border-radius: 5px; cursor: pointer; color: #ececf1; transition: 0.2s; display: flex; align-items: center; justify-content: space-between; position: relative; }
    .chat-item:hover { background: #2a2b32; }
    .chat-item.active { background: #343541; border: 1px solid #555; }
    .chat-title { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; flex: 1; }
    .chat-menu-btn { visibility: hidden; padding: 2px 6px; border-radius: 4px; cursor: pointer; color: #aaa; font-weight: bold; }
    .chat-item:hover .chat-menu-btn { visibility: visible; }
    .chat-menu-btn:hover { background: #555; color: #fff; }
    .context-menu { position: absolute; background: #202123; border: 1px solid #444; border-radius: 5px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); z-index: 1000; width: 150px; display: none; }
    .context-menu.visible { display: block; }
    .menu-option { padding: 8px 12px; cursor: pointer; font-size: 0.9rem; }
    .menu-option:hover { background: #343541; }
    .main { flex: 1; display: flex; flex-direction: column; position: relative; }
    .chat-area { flex: 1; overflow-y: auto; padding-bottom: 150px; padding-top: 60px; }
    .message-row { padding: 24px; border-bottom: 1px solid rgba(0,0,0,0.1); display: flex; justify-content: center; width: 100%; }
    /* Sincronizado com o role 'assistant' do servidor */
    .message-row.gpt { background: var(--bg-gpt); border-bottom: 1px solid rgba(0,0,0,0.3); }        
    .message-content { width: 100%; max-width: 800px; display: flex; gap: 20px; font-size: 1rem; line-height: 1.6; }
    .message-row.new-message { border-left: 4px solid #19c37d; animation: flash 2s; }
    @keyframes flash { 0% { background-color: #1a3a2a; } 100% { background-color: inherit; } }
    .message-content { width: 100%; max-width: 800px; display: flex; gap: 20px; font-size: 1rem; line-height: 1.6; }
    .avatar { width: 30px; height: 30px; border-radius: 2px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-weight: bold; color: #fff; }
    .avatar.user { background: #5436DA; }
    .avatar.gpt { background: #19c37d; }
    .text-block { flex: 1; overflow-x: hidden; color: #ececf1; }
    .text-block button { display: none !important; }
    .text-block p { margin-bottom: 1rem; }
    .text-block pre { background: #000; padding: 15px; border-radius: 6px; overflow-x: auto; }
    .text-block code { background: rgba(0,0,0,0.3); padding: 2px 4px; border-radius: 3px; font-family: monospace; }
    .text-block table { border-collapse: collapse; width: 100%; margin: 10px 0; }
    .text-block th, .text-block td { border: 1px solid #555; padding: 8px; }
    .status-float { position: absolute; bottom: 140px; left: 50%; transform: translateX(-50%); background: #19c37d; color: white; padding: 8px 16px; border-radius: 20px; font-size: 0.9rem; opacity: 0; transition: opacity 0.3s; display: flex; align-items: center; gap: 8px; pointer-events: none; }
    .status-float.visible { opacity: 1; }
    .spinner { width: 14px; height: 14px; border: 2px solid white; border-top-color: transparent; border-radius: 50%; animation: spin 1s infinite linear; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .input-container { position: absolute; bottom: 0; width: 100%; background: linear-gradient(180deg, transparent, #343541 20%); padding: 30px 0 50px; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; }
    .input-wrapper { width: 100%; max-width: 800px; position: relative; }
    .file-list { display: none; padding: 10px; background: #40414f; border-top-left-radius: 6px; border-top-right-radius: 6px; border: 1px solid #565869; border-bottom: none; gap: 10px; overflow-x: auto; }
    .file-list.visible { display: flex; }
    .file-preview { display: flex; align-items: center; background: #565869; padding: 5px 10px; border-radius: 4px; gap: 8px; font-size: 0.8rem; white-space: nowrap; }
    .file-preview img { height: 30px; width: 30px; object-fit: cover; border-radius: 4px; }
    .file-preview .close-btn { cursor: pointer; color: #ff6b6b; font-weight: bold; margin-left: 5px; }
    .input-box { width: 100%; background: #40414f; border: 1px solid #565869; border-radius: 6px; box-shadow: 0 0 15px rgba(0,0,0,0.1); display: flex; align-items: flex-end; }
    .input-box.has-file { border-top-left-radius: 0; border-top-right-radius: 0; }
    textarea { flex: 1; padding: 16px 10px 16px 16px; background: transparent; border: none; color: white; resize: none; outline: none; font-family: inherit; font-size: 1rem; box-sizing: border-box; max-height: 200px; }
    .attach-btn { padding: 12px; cursor: pointer; color: #aaa; transition: color 0.2s; display: flex; align-items: center; }
    .attach-btn:hover { color: #fff; }
    .send-btn { margin: 10px; background: #19c37d; border: none; padding: 8px 12px; border-radius: 4px; color: white; cursor: pointer; }
    
    .auth-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: #343541; z-index: 99999; display: flex; align-items: center; justify-content: center; }
    .auth-box { background: #202123; padding: 40px; border-radius: 10px; box-shadow: 0 0 20px rgba(0,0,0,0.5); width: 320px; text-align: center; }
    .auth-box h2 { margin-top: 0; color: #ececf1; }
    .auth-box input { width: 100%; padding: 10px; margin: 10px 0; background: #343541; border: 1px solid #555; color: white; border-radius: 5px; box-sizing: border-box; }
    .auth-box button { width: 100%; padding: 10px; background: #19c37d; color: white; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; }
    .auth-box button:hover { background: #15a369; }

    .top-user-menu { position: fixed; top: 10px; right: 20px; z-index: 100; }
    .user-avatar-btn { width: 40px; height: 40px; border-radius: 50%; cursor: pointer; border: 2px solid #555; background-size: cover; background-position: center; transition: 0.2s; background-color: #5436DA; }
    .user-avatar-btn:hover { border-color: #19c37d; }
    .user-dropdown { position: absolute; top: 50px; right: 0; background: #202123; border: 1px solid #444; border-radius: 6px; width: 180px; display: none; box-shadow: 0 4px 12px rgba(0,0,0,0.5); }
    .user-dropdown.visible { display: block; }
    .user-option { padding: 10px 15px; cursor: pointer; color: #ececf1; font-size: 0.9rem; border-bottom: 1px solid #333; }
    .user-option:last-child { border-bottom: none; }
    .user-option:hover { background: #343541; }

    .share-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 9999; display: none; align-items: center; justify-content: center; font-family: 'Segoe UI', sans-serif; }
    .share-overlay.visible { display: flex; }
    .popover { background: #171717; color: #ececf1; border-radius: 20px; width: 100%; max-width: 900px; box-shadow: 0 0 20px rgba(0,0,0,0.5); overflow: hidden; position: relative; display: flex; flex-direction: column; max-height: 90vh; }
    .share-header { padding: 20px; border-bottom: 1px solid rgba(255,255,255,0.1); display: flex; justify-content: space-between; align-items: center; background: #171717; z-index: 2; }
    .share-header h2 { margin: 0; font-size: 1.3rem; font-weight: 600; }
    .share-close { background: none; border: none; color: #aaa; cursor: pointer; display: flex; align-items: center; }
    .share-close:hover { color: white; }
    .share-content { padding: 20px; background: #171717; overflow-y: auto; }
    .share-preview-box { border: 1px solid rgba(255,255,255,0.2); border-radius: 12px; overflow: hidden; margin-bottom: 20px; position: relative; }
    .share-preview-header { background: #353535; padding: 10px 15px; font-weight: bold; font-size: 0.9rem; }
    .share-preview-body { background: #202123; padding: 15px; font-size: 0.9rem; color: #d1d5db; max-height: 300px; overflow-y: hidden; position: relative; }
    .fade-overlay { position: absolute; bottom: 0; left: 0; right: 0; height: 80px; background: linear-gradient(to bottom, transparent, #202123); pointer-events: none; z-index: 10; }
    .preview-msg-user { background: #343541; padding: 8px 12px; border-radius: 12px; display: inline-block; margin-bottom: 15px; margin-left: auto; max-width: 80%; color: white; }
    .preview-msg-gpt { color: #ececf1; line-height: 1.5; }
    .preview-row { display: flex; width: 100%; margin-bottom: 15px; }
    .preview-row.user { justify-content: flex-end; }
    .share-actions { display: flex; justify-content: center; gap: 20px; margin-top: 10px; }
    .share-action-btn { display: flex; flex-direction: column; align-items: center; gap: 10px; background: none; border: none; color: #ececf1; cursor: pointer; opacity: 1 !important; }
    .share-icon-circle { width: 50px; height: 50px; border-radius: 50%; display: flex; align-items: center; justify-content: center; background: #343541; transition: 0.2s; }
    .share-action-btn:hover .share-icon-circle { background: #444654; }
    .share-label { font-size: 0.8rem; }
    .copy-toast { position: fixed; top: 20px; left: 50%; transform: translateX(-50%); background: #343541; border: 1px solid #555; color: white; padding: 10px 20px; border-radius: 8px; font-size: 0.9rem; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 10000; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
    .copy-toast.visible { opacity: 1; margin-top: 10px; }
    .monitor-toast {
        position: fixed; right: 20px; bottom: 20px; width: 420px; max-width: calc(100vw - 40px);
        background: #202123; border: 1px solid #4a4a4a; border-radius: 10px; z-index: 10050;
        box-shadow: 0 10px 30px rgba(0,0,0,0.5); display: none; overflow: hidden;
    }
    .monitor-toast.visible { display: block; }
    .monitor-header {
        display: flex; justify-content: space-between; align-items: center;
        padding: 10px 12px; font-size: 0.92rem; font-weight: 700;
        background: #2a2b32; border-bottom: 1px solid #3c3d45;
    }
    .monitor-close { cursor: pointer; color: #bbb; font-weight: bold; }
    .monitor-close:hover { color: #fff; }
    .monitor-body {
        max-height: 340px; overflow: auto; padding: 10px 12px;
        font-size: 0.82rem; line-height: 1.45; color: #d8d8d8;
        font-family: Consolas, Menlo, Monaco, monospace; white-space: pre-wrap;
    }
    .monitor-tabs { display: flex; border-bottom: 1px solid #353741; background: #24252c; }
    .monitor-tab { flex: 1; text-align: center; padding: 8px; cursor: pointer; font-size: 0.82rem; color: #c9c9cf; }
    .monitor-tab.active { background: #343541; color: #fff; font-weight: 700; }
    .monitor-panel { display: none; }
    .monitor-panel.active { display: block; }
    .monitor-meta { color: #9aa0aa; font-size: 0.78rem; margin-bottom: 8px; font-family: 'Segoe UI', sans-serif; }
    .simple-modal { background: #202123; border: 1px solid #444; border-radius: 8px; padding: 20px; width: 300px; max-width: 90%; color: #ececf1; display: flex; flex-direction: column; gap: 15px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }
    .simple-modal h3 { margin: 0; font-size: 1.1rem; }
    .simple-modal input { background: #343541; border: 1px solid #555; color: white; padding: 8px; border-radius: 4px; outline: none; }
    .simple-modal input:focus { border-color: #19c37d; }
    .modal-buttons { display: flex; justify-content: flex-end; gap: 10px; }
    .modal-btn { padding: 6px 12px; border-radius: 4px; border: none; cursor: pointer; font-weight: bold; font-size: 0.9rem; }
    .btn-cancel { background: transparent; color: #aaa; border: 1px solid #444; }
    .btn-cancel:hover { background: #343541; color: white; }
    .btn-confirm { background: #19c37d; color: white; }
    .btn-confirm:hover { background: #15a369; }
    .btn-danger { background: #ef4146; color: white; }
    .btn-danger:hover { background: #c9353a; }
@keyframes slideIn {
    from { transform: translateX(100%); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
}
    .code-block { background: #1e1e1e; color: #d4d4d4; padding: 15px; font-family: 'Consolas', monospace; font-size: 0.85rem; overflow-x: auto; white-space: pre; border: 1px solid #333; border-radius: 6px; margin-bottom: 15px; }
    .code-title { font-size: 0.9rem; font-weight: bold; margin: 15px 0 5px; color: #19c37d; }
    .api-section { margin-bottom: 30px; border-bottom: 1px solid #333; padding-bottom: 20px; }
</style>
</head>
<body>
<div id="authOverlay" class="auth-overlay">
    <div class="auth-box">
        <h2>Bem-vindo</h2>
        <input type="text" id="loginUser" placeholder="Usuário">
        <input type="password" id="loginPass" placeholder="Senha" onkeydown="if(event.key==='Enter') doLogin()">
        <button onclick="doLogin()">Entrar</button>
        <div id="loginError" style="color:#ff6b6b; margin-top:10px; font-size:0.9rem;"></div>
    </div>
</div>

<div class="top-user-menu">
    <div class="user-avatar-btn" id="userAvatarBtn" onclick="toggleUserMenu()"></div>
    <div class="user-dropdown" id="userDropdown">
        <div class="user-option" onclick="openModal('passModal')">Alterar Senha</div>
        <div class="user-option" onclick="openModal('avatarModal')">Alterar Avatar</div>
        <div class="user-option" onclick="openApiModal()">Guia de API</div>
        <div class="user-option" onclick="openQueueMonitorToast()">Status da Fila</div>
        <div class="user-option" onclick="openLogMonitorToast()">Log em tempo real</div>
        <div class="user-option" style="color:#ff6b6b" onclick="doLogout()">Sair</div>
    </div>
</div>

<div class="sidebar">
    <div style="padding:15px; font-weight:bold; border-bottom:1px solid #444">Histórico</div>
    <div class="chat-list" id="chatList"></div>
</div>
<div class="main">
    <div class="chat-area" id="chatArea"></div>
    <div class="status-float" id="statusFloat"><div class="spinner"></div> <span id="statusText">...</span></div>
    <div class="input-container">
        <div class="input-wrapper">
            <div class="file-list" id="fileList"></div>
            <div class="input-box" id="inputBox">
                <label class="attach-btn" title="Anexar arquivos" onclick="triggerFileSelect()">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"></path></svg>
                </label>
                <textarea id="userInput" rows="1" placeholder="Envie uma mensagem..." onkeydown="if(event.key==='Enter' && !event.shiftKey) { event.preventDefault(); sendMsg(); }"></textarea>
                <button class="send-btn" id="sendBtn" onclick="sendMsg()">➤</button>
            </div>
            <input type="file" id="fileInput" multiple style="display: none;" onchange="handleFileSelect(this)">
        </div>
    </div>
</div>
<div id="contextMenu" class="context-menu"></div>

<div id="shareOverlay" class="share-overlay"><div class="popover"><div class="share-header"><h2 id="shareTitle">...</h2><button class="share-close" onclick="closeShare()">✖</button></div><div class="share-content"><div class="share-preview-box"><div class="share-preview-header">Chat Preview</div><div class="share-preview-body"><div class="fade-overlay"></div><div class="preview-row user"><div class="preview-msg-user" id="sharePreviewUser"></div></div><div class="preview-row gpt"><div class="preview-msg-gpt" id="sharePreviewGPT"></div></div></div></div><div class="share-actions"><button class="share-action-btn" onclick="copyShare()"><div class="share-icon-circle">🔗</div><div class="share-label">Copiar link</div></button></div></div></div></div>

<div id="renameOverlay" class="share-overlay"><div class="simple-modal"><h3>Renomear conversa</h3><input type="text" id="renameInput" placeholder="Novo nome..."><div class="modal-buttons"><button class="modal-btn btn-cancel" onclick="closeModal('renameOverlay')">Cancelar</button><button class="modal-btn btn-confirm" onclick="confirmRename()">Salvar</button></div></div></div>

<div id="deleteOverlay" class="share-overlay"><div class="simple-modal"><h3>Excluir conversa?</h3><p style="font-size:0.9rem; color:#ccc;">Exclusão permanente.</p><div class="modal-buttons"><button class="modal-btn btn-cancel" onclick="closeModal('deleteOverlay')">Cancelar</button><button class="modal-btn btn-danger" onclick="confirmDelete()">Excluir</button></div></div></div>

<div id="passModal" class="share-overlay"><div class="simple-modal"><h3>Alterar Senha</h3><input type="password" id="newPass" placeholder="Nova senha"><div class="modal-buttons"><button class="modal-btn btn-cancel" onclick="closeModal('passModal')">Cancelar</button><button class="modal-btn btn-confirm" onclick="changePass()">Salvar</button></div></div></div>
<div id="avatarModal" class="share-overlay"><div class="simple-modal"><h3>Alterar Avatar</h3><input type="file" id="avatarInput" accept="image/*"><div class="modal-buttons"><button class="modal-btn btn-cancel" onclick="closeModal('avatarModal')">Cancelar</button><button class="modal-btn btn-confirm" onclick="uploadAvatar()">Enviar</button></div></div></div>

<div id="apiModal" class="share-overlay">
    <div class="popover">
        <div class="share-header">
            <h2>Guia de API Remota</h2>
            <button class="share-close" onclick="closeModal('apiModal')">✖</button>
        </div>
        <div class="share-content" style="color:#dcdcdc; font-size:0.9rem;">
            <p>O simulador aceita comandos via requisição <strong>POST (HTTP)</strong>. Use a porta <strong style="color:#19c37d"><span id="displayPort">...</span></strong> para evitar erros de certificado.</p>
            
            <div class="api-section">
                <div class="code-title">1. Configuração</div>
                <p><strong>Endpoint:</strong> <span id="endpointUrl" style="background:#333; padding:2px 6px; border-radius:4px; font-family:monospace">...</span></p>
                <p><strong>API Key:</strong> <code style="color:#19c37d" id="apiKeyDisplay">***</code></p>
            </div>

            <div class="api-section">
                <div class="code-title">2. Formato de Envio (JSON)</div>
                <div class="code-block" id="jsonRequest"></div>
                <div class="code-title">3. Formato de Resposta (Stream vs JSON)</div>
                <div class="code-block" id="jsonResponse"></div>
            </div>

            <div class="code-title">Exemplo: cURL</div>
            <div class="code-block" id="curlCode"></div>

            <div class="code-title">Exemplo: Python (Requests)</div>
            <div class="code-block" id="pythonCode"></div>

            <div class="code-title">Exemplo: JavaScript (Fetch)</div>
            <div class="code-block" id="jsCode"></div>
        </div>
    </div>
</div>

<div id="copyToast" class="copy-toast">Copiado!</div>
<div id="queueMonitorToast" class="monitor-toast">
    <div class="monitor-header">
        <span>📊 Status da fila (tempo real)</span>
        <span class="monitor-close" onclick="closeMonitorToast('queueMonitorToast')">✖</span>
    </div>
    <div class="monitor-body" id="queueMonitorBody">Carregando...</div>
</div>
<div id="logMonitorToast" class="monitor-toast" style="bottom: 380px;">
    <div class="monitor-header">
        <span>🧾 Log do ChatGPT Simulator (tempo real)</span>
        <span class="monitor-close" onclick="closeMonitorToast('logMonitorToast')">✖</span>
    </div>
    <div class="monitor-tabs">
        <div class="monitor-tab active" id="logTabBtn" onclick="switchLogMonitorTab('log')">Log</div>
        <div class="monitor-tab" id="metricsTabBtn" onclick="switchLogMonitorTab('metrics')">Métricas</div>
    </div>
    <div class="monitor-panel active" id="logPanel">
        <div class="monitor-body" id="logMonitorBody">Carregando...</div>
    </div>
    <div class="monitor-panel" id="metricsPanel">
        <div class="monitor-body" id="metricsMonitorBody">Carregando...</div>
    </div>
</div>
<input type="text" id="hiddenCopyInput" style="position:absolute; left:-9999px; opacity:0;">

<script>
    console.log("✅ Script Loaded");
    /* INJECTED_CONFIG_HERE */

    // --- LOGGER SYSTEM ---
    const ConsoleLog = {
        style: (bg, color='white') => `background: ${bg}; color: ${color}; padding: 2px 5px; border-radius: 3px; font-weight: bold;`,
        info: (cat, msg) => console.log(`%c${cat}`, ConsoleLog.style('#007acc'), msg),
        success: (cat, msg) => console.log(`%c${cat}`, ConsoleLog.style('#19c37d'), msg),
        warn: (cat, msg) => console.log(`%c${cat}`, ConsoleLog.style('#e6a23c'), msg),
        error: (cat, msg) => console.log(`%c${cat}`, ConsoleLog.style('#f56c6c'), msg),
        browser: (msg) => console.log(`%c[Browser]`, ConsoleLog.style('#909399'), msg),
    };

    console.group('🚀 Simulator System');
    ConsoleLog.info('[SYS]', 'Initializing frontend...');
    
    // Fill Key in Modal Only
    document.getElementById('apiKeyDisplay').innerText = API_KEY;

    let currentChatId = null;
    let currentFiles = [];
    let pendingAction = null; 
    let queueMonitorTimer = null;
    let logMonitorTimer = null;

    function setStatus(text) {
        const el = document.getElementById('statusFloat');
        if(text) { document.getElementById('statusText').innerText = text; el.classList.add('visible'); }
        else { el.classList.remove('visible'); }
    }
    function showToast(msg) {
        const t = document.getElementById('copyToast'); t.innerText = msg; t.classList.add('visible');
        setTimeout(() => t.classList.remove('visible'), 2000);
    }
    function openModal(id) { document.getElementById(id).classList.add('visible'); document.getElementById('userDropdown').classList.remove('visible'); }
    function closeModal(id) { document.getElementById(id).classList.remove('visible'); }

    function closeMonitorToast(id) {
        document.getElementById(id)?.classList.remove('visible');
        if (id === 'queueMonitorToast' && queueMonitorTimer) {
            clearInterval(queueMonitorTimer);
            queueMonitorTimer = null;
        }
        if (id === 'logMonitorToast' && logMonitorTimer) {
            clearInterval(logMonitorTimer);
            logMonitorTimer = null;
        }
    }

    function openQueueMonitorToast() {
        document.getElementById('userDropdown').classList.remove('visible');
        const el = document.getElementById('queueMonitorToast');
        el.classList.add('visible');
        const render = async () => {
            const body = document.getElementById('queueMonitorBody');
            try {
                const res = await fetch('/api/queue/status');
                const json = await res.json();
                if (!json.success) {
                    body.innerText = `Falha: ${json.error || 'erro desconhecido'}`;
                    return;
                }
                const q = json.queue || {};
                const lines = [
                    `qsize: ${q.qsize ?? 0}`,
                    `enqueued_total: ${q.enqueued_total ?? 0}`,
                    `dequeued_total: ${q.dequeued_total ?? 0}`,
                    `avg_wait_ms: ${q.avg_wait_ms ?? 0}`,
                    `max_wait_ms: ${q.max_wait_ms ?? 0}`,
                    '',
                    `by_origin_enqueued: ${JSON.stringify(q.by_origin_enqueued || {})}`,
                    `by_origin_dequeued: ${JSON.stringify(q.by_origin_dequeued || {})}`,
                    `lane_sizes: ${JSON.stringify(q.lane_sizes || {})}`,
                    '',
                    `Atualizado em: ${new Date().toLocaleTimeString()}`
                ];
                body.innerText = lines.join('\\n');
            } catch (e) {
                body.innerText = `Erro ao consultar fila: ${e}`;
            }
        };
        render();
        if (queueMonitorTimer) clearInterval(queueMonitorTimer);
        queueMonitorTimer = setInterval(render, 1500);
    }

    function openLogMonitorToast() {
        document.getElementById('userDropdown').classList.remove('visible');
        const el = document.getElementById('logMonitorToast');
        el.classList.add('visible');
        switchLogMonitorTab('log');
        const render = async () => {
            const logBody = document.getElementById('logMonitorBody');
            const metricsBody = document.getElementById('metricsMonitorBody');
            try {
                const res = await fetch('/api/logs/tail?lines=120');
                const json = await res.json();
                if (!json.success) {
                    logBody.innerText = `Falha: ${json.error || 'erro desconhecido'}`;
                } else {
                    const header = `[${json.path || 'log'}]\\nlinhas: ${json.line_count || 0}\\n---\\n`;
                    logBody.innerText = header + (json.lines || []).join('\\n');
                    logBody.scrollTop = logBody.scrollHeight;
                }
            } catch (e) {
                logBody.innerText = `Erro ao consultar log: ${e}`;
            }

            try {
                const resm = await fetch('/api/metrics');
                const jm = await resm.json();
                if (!jm.success) {
                    metricsBody.innerText = `Falha: ${jm.error || 'erro desconhecido'}`;
                    return;
                }
                const m = jm.metrics || {};
                const lines = [
                    `uptime_sec: ${m.uptime_sec ?? 0}`,
                    `queue_qsize: ${m.queue_qsize ?? 0}`,
                    `active_chats_total: ${m.active_chats_total ?? 0}`,
                    `active_chats_remote: ${m.active_chats_remote ?? 0}`,
                    `active_chats_analyzer: ${m.active_chats_analyzer ?? 0}`,
                    `active_chats_stale_candidates: ${m.active_chats_stale_candidates ?? 0}`,
                    `syncs_in_progress: ${m.syncs_in_progress ?? 0}`,
                    `rate_limit_remaining_sec: ${m.rate_limit_remaining_sec ?? 0}`,
                    `request_timeout_sec: ${m.request_timeout_sec ?? 0}`,
                    '',
                    `queue.by_origin_enqueued: ${JSON.stringify((m.queue || {}).by_origin_enqueued || {})}`,
                    `queue.by_origin_dequeued: ${JSON.stringify((m.queue || {}).by_origin_dequeued || {})}`,
                    `queue.lane_sizes: ${JSON.stringify((m.queue || {}).lane_sizes || {})}`,
                    `queue.avg_wait_ms: ${(m.queue || {}).avg_wait_ms ?? 0}`,
                    `queue.max_wait_ms: ${(m.queue || {}).max_wait_ms ?? 0}`,
                    '',
                    `Atualizado em: ${new Date().toLocaleTimeString()}`
                ];
                metricsBody.innerText = lines.join('\\n');
                if (document.getElementById('metricsPanel').classList.contains('active')) {
                    metricsBody.scrollTop = metricsBody.scrollHeight;
                }
            } catch (e) {
                metricsBody.innerText = `Erro ao consultar métricas: ${e}`;
            }
        };
        render();
        if (logMonitorTimer) clearInterval(logMonitorTimer);
        logMonitorTimer = setInterval(render, 2000);
    }

    function switchLogMonitorTab(tab) {
        const logBtn = document.getElementById('logTabBtn');
        const metricsBtn = document.getElementById('metricsTabBtn');
        const logPanel = document.getElementById('logPanel');
        const metricsPanel = document.getElementById('metricsPanel');
        const isLog = tab === 'log';
        logBtn.classList.toggle('active', isLog);
        metricsBtn.classList.toggle('active', !isLog);
        logPanel.classList.toggle('active', isLog);
        metricsPanel.classList.toggle('active', !isLog);
    }

    // --- LOGIN ---
    async function doLogin() {
        const u = document.getElementById('loginUser').value;
        const p = document.getElementById('loginPass').value;
        console.group('🔐 Authentication Flow');
        ConsoleLog.info('[auth.py][AUTH]', `Attempting login for: ${u}`);
        try {
            const res = await fetch('/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({username:u, password:p})});
            if (!res.ok) {
                const errData = await res.json();
                ConsoleLog.warn('[auth.py][AUTH]', `Login failed: ${errData.error}`);
                document.getElementById('loginError').innerText = errData.error || 'Erro de autenticação';
                console.groupEnd();
                return;
            }
            const data = await res.json();
            if(data.success) {
                ConsoleLog.success('[auth.py][AUTH]', 'Login success!');
                document.getElementById('authOverlay').style.display = 'none';
                checkLogin(); // Continues flow
            }
        } catch(e) { 
            ConsoleLog.error('[auth.py][AUTH]', `Connection error: ${e}`);
            document.getElementById('loginError').innerText = 'Erro de conexão'; 
            console.groupEnd();
        }
    }

    async function doLogout() { 
        ConsoleLog.info('[auth.py][AUTH]', 'Logging out...');
        await fetch('/logout', {method:'POST'}); 
        location.reload(); 
    }

    // --- CHECKLOGIN ---
    async function checkLogin() {
        // If not in group (e.g. initial load), creates one
        // If coming from doLogin, this nests or appends.
        // Let's assume standalone check or sequence.
        ConsoleLog.info('[auth.py][AUTH]', 'Checking session validity...');
        try {
            const res = await fetch('/api/user/info');
            if(res.ok) {
                const data = await res.json();
                const userName = data.username || data.user || 'Unknown';
                ConsoleLog.success('[auth.py][AUTH]', `Session valid for user: ${userName}`);
                document.getElementById('authOverlay').style.display = 'none';
                updateUserAvatar(data.avatar);
                loadChats();
                // Tenta pegar da URL (?chat=ID)
                const urlParams = new URLSearchParams(window.location.search);
                let chat_id = urlParams.get('chat_id');
                // Se encontrou um ID, dispara a abertura do chat
                if (chat_id) {
                    console.log("🚀 Abrindo chat específico automaticamente:", chat_id);
        
                    // Substitua 'loadChat' pela função que você usa para selecionar um chat na lateral
                    if (typeof selectChat === 'function') {
                        selectChat(chat_id, true);
                    } else {
                        // Caso não tenha função de load, simula o clique no elemento da lista lateral
                        const chatElement = document.querySelector(`[data-id="${chat_id}"]`);
                        if (chatElement) chatElement.click();
                    }
                }
            } else {
                ConsoleLog.warn('[auth.py][AUTH]', 'Session invalid/expired (401).');
                document.getElementById('authOverlay').style.display = 'flex'; 
            }
        } catch(e) { 
            ConsoleLog.error('[auth.py][AUTH]', 'Network error during checkLogin.');
            document.getElementById('authOverlay').style.display = 'flex'; 
        }
        try { console.groupEnd(); } catch(e){} // Close Auth Group
    }
    
    // --- LÓGICA DE ABAS DA API ---
    window.switchApiTab = function(evt, paneId) {
        document.querySelectorAll('.api-tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.api-pane').forEach(p => p.classList.remove('active'));
        evt.currentTarget.classList.add('active');
        document.getElementById(paneId).classList.add('active');
    };

    // --- API GUIDE RENDERER (COMPLETA E REDUNDANTE) ---
    function openApiModal() {
        const host = window.location.hostname;
        const apiUrl = `http://${host}:${HTTP_PORT}/v1/chat/completions`;
        const historyUrl = `http://${host}:${HTTP_PORT}/api/history`;
        const syncUrl = `http://${host}:${HTTP_PORT}/api/sync`;
        const deleteUrl = `http://${host}:${HTTP_PORT}/api/delete`;
        const searchUrl = `http://${host}:${HTTP_PORT}/api/web_search`;

        // 1. Injeta os estilos das abas
        if (!document.getElementById('apiTabsStyle')) {
            const style = document.createElement('style');
            style.id = 'apiTabsStyle';
            style.innerHTML = `
                .api-tab { background: transparent; color: #888; border: none; padding: 12px 20px; cursor: pointer; font-weight: bold; font-size: 0.95rem; border-bottom: 2px solid transparent; transition: 0.2s; outline: none; white-space: nowrap; }
                .api-tab:hover { color: #ececf1; }
                .api-tab.active { color: #19c37d; border-bottom: 2px solid #19c37d; }
                .api-pane { display: none; animation: fadeIn 0.3s; }
                .api-pane.active { display: block; }
                @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }
                .api-code-block { background: #0d0d0d; padding: 15px; border-radius: 8px; overflow-x: auto; color: #fff; font-family: monospace; border: 1px solid #333; font-size: 0.85rem; margin-bottom: 15px; }
                .api-info-box { background: #1e1e1e; padding: 12px; border-radius: 6px; font-family: monospace; font-size: 0.85rem; margin-bottom: 15px; border: 1px solid #333; color: #ececf1; }
                .api-modal-body h4 { margin-bottom: 5px; font-size: 0.9rem; color: #ececf1; margin-top: 25px; border-bottom: 1px solid #444; padding-bottom: 5px; }
            `;
            document.head.appendChild(style);
        }

        const modalEl = document.getElementById('apiModal');
        const contentBox = modalEl.querySelector('.share-box') || modalEl.querySelector('.simple-modal') || modalEl.firstElementChild;
        
        contentBox.style.padding = "0"; 
        contentBox.style.overflow = "hidden"; 
        contentBox.style.display = "flex";
        contentBox.style.flexDirection = "column";
        contentBox.style.maxHeight = "85vh";

        // 3. Monta a estrutura HTML
        contentBox.innerHTML = `
            <div style="padding: 20px 20px 0 20px; background: #202123;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                    <h2 style="margin: 0; color: #19c37d;">Documentação da API</h2>
                    <button onclick="closeModal('apiModal')" style="background: transparent; border: none; color: #aaa; cursor: pointer; font-size: 1.5rem;">&times;</button>
                </div>
                
                <div style="display: flex; border-bottom: 1px solid #444; margin-bottom: 0; overflow-x: auto;">
                    <button class="api-tab active" onclick="switchApiTab(event, 'tab-send')">Enviar Msg & Anexos</button>
                    <button class="api-tab" onclick="switchApiTab(event, 'tab-history')">Ver Chats</button>
                    <button class="api-tab" onclick="switchApiTab(event, 'tab-sync')">Sincronizar Chat</button>
                    <button class="api-tab" onclick="switchApiTab(event, 'tab-delete')">Excluir Chat</button>
                    <button class="api-tab" onclick="switchApiTab(event, 'tab-search')">🔍 Pesquisa Web</button>
                </div>
            </div>

            <div class="api-modal-body" style="padding: 20px; overflow-y: auto; flex: 1; background: #343541;">
                
                <div id="tab-send" class="api-pane active">
                    <p style="font-size: 0.9rem; color: #ccc; margin-top: 0;">Envie prompts e arquivos (em base64) para a LLM, suportando respostas em Stream (NDJSON) ou bloco único.</p>
                    <div class="api-info-box">
                        <span style="color: #e6a23c; font-weight: bold;">POST</span> ${apiUrl}<br>
                        <span style="color: #888;">Header:</span> Authorization: Bearer ${API_KEY}<br>
                        <span style="color: #888;">Body:</span> { "api_key": "${API_KEY}", "message": "...", "chat_id": null, "stream": true, "attachments": [{"name": "...", "data": "base64..."}] }
                    </div>
                    
                    <h4>Exemplo Python (Com Arquivo PDF):</h4>
                    <pre class="api-code-block"><code>import requests, base64

def get_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

payload = {
    "api_key": "${API_KEY}",
    "message": "Analise este documento e me dê um resumo.",
    "chat_id": None, # Deixe nulo para novo chat, ou use um UUID existente
    "stream": True,  # True para receber linha por linha
    "attachments": [
        {"name": "relatorio.pdf", "data": get_b64("relatorio.pdf")}
    ]
}

resp = requests.post("${apiUrl}", headers={"Authorization": "Bearer ${API_KEY}"}, json=payload, stream=True)
for line in resp.iter_lines():
    if line: print(line.decode())</code></pre>

                    <h4>Exemplo Javascript (Com Arquivo):</h4>
                    <pre class="api-code-block"><code>const file = document.querySelector('input[type="file"]').files[0];
const reader = new FileReader();

reader.onload = async () => {
    // reader.result já contém o prefixo data:image/png;base64,
    const response = await fetch("${apiUrl}", {
        method: "POST",
        headers: { 
            "Content-Type": "application/json",
            "Authorization": "Bearer ${API_KEY}"
        },
        body: JSON.stringify({
            api_key: "${API_KEY}",
            message: "O que tem nesta imagem?",
            stream: false,
            attachments: [{ name: file.name, data: reader.result }]
        })
    });
    console.log(await response.json());
};
reader.readAsDataURL(file);</code></pre>

                    <h4>Retorno do Servidor (Modo Stream = True):</h4>
                    <pre class="api-code-block"><code style="color: #aaa;">// O servidor devolve NDJSON. O chat_id é garantido na 1ª linha antes da digitação começar!
{"type":"chat_id","content":"1234-uuid-..."}
{"type":"status","content":"Anexando 1 arquivos..."}
{"type":"status","content":"Digitando... 15%"}
{"type":"status","content":"Aguardando resposta..."}
{"type":"markdown","content":"O arquivo relatorio.pdf cont\u00e9m dados sobre..."}
{"type":"finish","content":{"chat_id":"1234-uuid","title":"An\u00e1lise Relat\u00f3rio","url":"https://chatgpt.com/c/..."}}</code></pre>

                    <h4>Retorno do Servidor (Modo Stream = False):</h4>
                    <pre class="api-code-block"><code style="color: #19c37d;">{
  "success": true,
  "chat_id": "1234-uuid-...",
  "html": "O arquivo relatorio.pdf contém dados sobre as vendas...\n\n**Conclusão:** Crescimento de 15%.",
  "url": "https://chatgpt.com/c/6996c791-...",
  "title": "Análise Relatório"
}</code></pre>
                </div>

                <div id="tab-history" class="api-pane">
                    <p style="font-size: 0.9rem; color: #ccc; margin-top: 0;">Baixa o histórico armazenado em cache local <code>history.json</code> de forma instantânea.</p>
                    <div class="api-info-box">
                        <span style="color: #5436DA; font-weight: bold;">GET</span> ${historyUrl}<br>
                        <span style="color: #888;">Header:</span> Authorization: Bearer ${API_KEY}<br>
                        <span style="color: #888;">URL Query:</span> ?api_key=${API_KEY}
                    </div>

                    <h4>Exemplo cURL:</h4>
                    <pre class="api-code-block"><code>curl -X GET "${historyUrl}?api_key=${API_KEY}" -H "Authorization: Bearer ${API_KEY}"</code></pre>

                    <h4>Exemplo de Resposta (Múltiplos Chats):</h4>
                    <pre class="api-code-block"><code style="color: #19c37d;">{
  "dc982484-e7a0-450d-abc9-6e88711ce316": {
    "title": "Data comemorativa 19 fev",
    "url": "https://chatgpt.com/c/6996c791-...",
    "created_at": "2026-02-18T20:06:08",
    "messages": [
      { "role": "user", "content": "Tem alguma data comemorativa amanhã?" },
      { "role": "assistant", "content": "Sim! Amanhã, 19 de fevereiro, é o Dia do Esportista." }
    ]
  }
}</code></pre>
                </div>

                <div id="tab-sync" class="api-pane">
                    <p style="font-size: 0.9rem; color: #ccc; margin-top: 0;">Sincroniza remotamente via OpenAI. <strong style="color: #19c37d;">Suporta Reconexão (Takeover):</strong> Se uma requisição de envio de msg deu Timeout, faça um POST enviando apenas o <code>chat_id</code> com <code>stream: true</code> para se reconectar e retomar o streaming em andamento.</p>
                    <div class="api-info-box">
                        <span style="color: #e6a23c; font-weight: bold;">POST</span> ${syncUrl}<br>
                        <span style="color: #888;">Header:</span> Authorization: Bearer ${API_KEY}<br>
                        <span style="color: #888;">Body (Sync Padrão):</span> { "api_key": "${API_KEY}", "chat_id": "...", "url": "..." }<br>
                        <span style="color: #888;">Body (Takeover):</span> { "api_key": "${API_KEY}", "chat_id": "...", "stream": true }
                    </div>

                    <h4>Exemplo cURL (Reconexão Stream):</h4>
                    <pre class="api-code-block"><code>curl -X POST ${syncUrl} \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${API_KEY}" \\
  -d '{"api_key": "${API_KEY}", "chat_id": "dc98...", "stream": true}'</code></pre>

                    <h4>Exemplo de Resposta (Takeover de Stream):</h4>
                    <pre class="api-code-block"><code style="color: #aaa;">{"type":"status","content":"Reconectado ao processo ativo..."}
{"type":"status","content":"Aguardando resposta..."}
{"type":"markdown","content":"...continuando recebimento da resposta..."}
{"type":"finish","content":{"chat_id":"dc982484..."}}</code></pre>

                    <h4>Exemplo de Resposta (Sync Padrão Concluído):</h4>
                    <pre class="api-code-block"><code style="color: #19c37d;">{
  "success": true,
  "updated": true,
  "chat": {
    "chat_id": "dc982484-...",
    "title": "Explicação sobre API REST",
    "url": "https://chatgpt.com/c/699...",
    "messages": [ { "role": "assistant", "content": "..." } ]
  }
}</code></pre>
                </div>

                <div id="tab-delete" class="api-pane">
                    <p style="font-size: 0.9rem; color: #ccc; margin-top: 0;">Abre a janela do chat, acessa o menu e exclui a conversa remotamente da OpenAI, apagando-a também localmente.</p>
                    <div class="api-info-box">
                        <span style="color: #e6a23c; font-weight: bold;">POST</span> ${deleteUrl}<br>
                        <span style="color: #888;">Header:</span> Authorization: Bearer ${API_KEY}<br>
                        <span style="color: #888;">Body:</span> { "api_key": "${API_KEY}", "chat_id": "...", "url": "..." }
                    </div>

                    <h4>Exemplo cURL:</h4>
                    <pre class="api-code-block"><code>curl -X POST ${deleteUrl} \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${API_KEY}" \\
  -d '{
    "api_key": "${API_KEY}",
    "chat_id": "dc982484-e7a0-450d-abc9-6e88711ce316",
    "url": "https://chatgpt.com/c/6996c791..."
  }'</code></pre>

                    <h4>Exemplo de Resposta (Sucesso):</h4>
                    <pre class="api-code-block"><code style="color: #19c37d;">{
  "success": true,
  "deleted": true
}</code></pre>
                </div>

                <div id="tab-search" class="api-pane">
                    <p style="font-size: 0.9rem; color: #ccc; margin-top: 0;">Pesquisa no Google via Playwright — abre uma aba real do Chromium, digita a query com timing humano e retorna resultados estruturados.</p>
                    <div class="api-info-box">
                        <span style="color: #e6a23c; font-weight: bold;">POST</span> ${searchUrl}<br>
                        <span style="color: #888;">Header:</span> Authorization: Bearer ${API_KEY}<br>
                        <span style="color: #888;">Body:</span> { "queries": ["termo de busca 1", "termo de busca 2"] }
                    </div>

                    <h4>Exemplo Python:</h4>
                    <pre class="api-code-block"><code>import requests

resp = requests.post(
    "${searchUrl}",
    json={"queries": [
        "methylphenidate children adverse effects site:pubmed.ncbi.nlm.nih.gov",
        "risperidone autism pediatric guidelines"
    ]},
    headers={"Authorization": "Bearer ${API_KEY}"},
    timeout=90
)
data = resp.json()
for res in data["results"]:
    for item in res.get("results", []):
        print(f"{item['title']} — {item['url']}")</code></pre>

                    <h4>Exemplo cURL:</h4>
                    <pre class="api-code-block"><code>curl -X POST ${searchUrl} \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${API_KEY}" \\
  -d '{"queries": ["metilfenidato efeitos adversos crianças"]}'</code></pre>

                    <h4>Exemplo de Resposta:</h4>
                    <pre class="api-code-block"><code style="color: #19c37d;">{
  "success": true,
  "results": [
    {
      "success": true,
      "query": "metilfenidato efeitos adversos crianças",
      "count": 10,
      "results": [
        {
          "position": 1,
          "title": "Methylphenidate for children and adolescents...",
          "url": "https://pubmed.ncbi.nlm.nih.gov/36971690/",
          "snippet": "Our updated meta-analyses suggest that...",
          "type": "organic"
        }
      ]
    }
  ]
}</code></pre>

                    <h4>Tipos de resultado:</h4>
                    <pre class="api-code-block"><code style="color: #aaa;">// type: "organic"          → resultado orgânico (título + URL + snippet)
// type: "featured_snippet" → resposta em destaque do Google
// type: "people_also_ask"  → "As pessoas também perguntam"</code></pre>

                    <h4>Modo LLM (search_queries):</h4>
                    <p style="font-size: 0.85rem; color: #aaa;">Quando a LLM precisa pesquisar, ela retorna um JSON especial. O frontend detecta automaticamente e executa a busca.</p>
                    <pre class="api-code-block"><code style="color: #19c37d;">// A LLM retorna isto em vez de texto:
{
  "search_queries": [
    {
      "query": "clonidina bula profissional anvisa posologia pediátrica",
      "reason": "verificar posologia pediátrica aprovada pela ANVISA"
    },
    {
      "query": "risperidone metabolic side effects children monitoring",
      "reason": "verificar efeitos metabólicos e monitoramento"
    }
  ]
}

<span style="color: #888;">// O sistema então:
// 1. Detecta search_queries no JSON
// 2. Chama POST /api/web_search com as queries
// 3. Formata os resultados e envia de volta à LLM
// 4. LLM responde usando os resultados reais</span></code></pre>

                    <h4>Limites:</h4>
                    <pre class="api-code-block"><code style="color: #aaa;">Máx. queries por request:   5 (recomendado: 1-3)
Máx. resultados por query:  10
Timeout por query:          ~60s (browser digita com timing humano)
Concorrência:               1 aba por query (sequencial)</code></pre>

                    <p style="font-size: 0.8rem; color: #888; margin-top: 15px;">
                        🧪 <a href="/api/web_search/test" target="_blank" style="color: #19c37d;">Abrir página de teste interativo →</a>
                    </p>
                </div>

            </div>
        `;

        openModal('apiModal');
    }

    function updateUserAvatar(filename) {
        const el = document.getElementById('userAvatarBtn');
        if(filename) el.style.backgroundImage = `url('/api/user/avatar/${filename}')`;
        else el.style.backgroundImage = 'none';
    }
    function toggleUserMenu() { document.getElementById('userDropdown').classList.toggle('visible'); }
    
    document.addEventListener('click', (e) => {
        if (!e.target.closest('.top-user-menu')) document.getElementById('userDropdown').classList.remove('visible');
        if (!e.target.closest('.context-menu') && !e.target.closest('.chat-menu-btn')) document.getElementById('contextMenu').classList.remove('visible');
    });

    async function changePass() {
        const p = document.getElementById('newPass').value; if(!p) return;
        const res = await fetch('/api/user/update_password', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({new_password:p})});
        if((await res.json()).success) { alert('Senha alterada!'); closeModal('passModal'); } else alert('Erro');
    }

    async function uploadAvatar() {
        const file = document.getElementById('avatarInput').files[0]; if(!file) return;
        const fd = new FormData(); fd.append('file', file);
        const res = await fetch('/api/user/upload_avatar', {method:'POST', body:fd});
        const data = await res.json();
        if(data.success) { updateUserAvatar(data.avatar); closeModal('avatarModal'); } else alert(data.error || 'Erro');
    }

async function openShareLocal(id) {
        // 1. Abre o modal e limpa os campos
        const modal = document.getElementById('shareOverlay');
        if (!modal) return;
        modal.classList.add('visible');
        
        document.getElementById('shareTitle').innerText = "Sincronizando...";
        document.getElementById('sharePreviewUser').innerHTML = "...";
        document.getElementById('sharePreviewGPT').innerHTML = "...";

        // 2. Cria e exibe o mini-toast de sincronização
        const syncToast = document.createElement('div');
        syncToast.style = "position:fixed; top:20px; right:20px; background:#19c37d; color:white; padding:10px 20px; border-radius:5px; z-index:10000; font-size:14px; box-shadow:0 4px 12px rgba(0,0,0,0.2); animation: slideIn 0.3s ease-out;";
        syncToast.innerHTML = "🔄 Sincronizando conteúdo...";
        document.body.appendChild(syncToast);

        try {
            // 3. Busca o histórico
            const res = await fetch('/api/history');
            const history = await res.json();
            const chat = history[id];

            if (!chat) {
                document.getElementById('shareTitle').innerText = "Chat não encontrado";
                syncToast.style.background = "#ff4a4a";
                syncToast.innerText = "❌ Erro ao localizar chat";
                setTimeout(() => syncToast.remove(), 3000);
                return;
            }

            // 4. Identifica as mensagens
            const userMsgs = chat.messages.filter(m => m.role === 'user');
            const gptMsgs = chat.messages.filter(m => m.role === 'assistant' || m.role === 'gpt');
            
            // Tratamento de quebras de linha: 
            // Substituímos as quebras de linha literais por <br> caso o innerHTML não as reconheça
            const formatContent = (txt) => {
                if(!txt) return "...";
                return txt.replace(/\\n/g,'<br>');
            };

            const userLast = userMsgs.length ? formatContent(userMsgs[userMsgs.length - 1].content) : "...";
            const gptLast = gptMsgs.length ? formatContent(gptMsgs[gptMsgs.length - 1].content) : "...";

            // 5. Preenche a interface
            document.getElementById('shareTitle').innerText = chat.title || "Conversa";
            document.getElementById('sharePreviewUser').innerHTML = userLast;
            document.getElementById('sharePreviewGPT').innerHTML = gptLast;

            // 6. GERA O LINK CORRETO (Se for projeto, usa a URL do projeto, se não, a normal)
            // Gera o link apontando para a rota local
            const shareUrl = window.location.origin + window.location.pathname + "?chat_id=" + id;
            document.getElementById('hiddenCopyInput').value = shareUrl;

            // 7. Remove o toast com sucesso
            syncToast.innerHTML = "✅ Sincronizado!";
            setTimeout(() => syncToast.remove(), 2000);

        } catch (e) {
            console.error("Erro no compartilhamento:", e);
            syncToast.style.background = "#ff4a4a";
            syncToast.innerText = "❌ Erro na sincronização";
            setTimeout(() => syncToast.remove(), 3000);
        }
    }
    function closeShare() { document.getElementById('shareOverlay').classList.remove('visible'); }
    function copyShare() { const c = document.getElementById("hiddenCopyInput"); c.select(); navigator.clipboard.writeText(c.value); showToast("Link copiado!"); }

    function openRenameLocal(url, oldName) {
        pendingAction = { 'url': url, 'option': 'Renomear', 'oldName': oldName }; 
        document.getElementById('renameInput').value = oldName;
        document.getElementById('renameOverlay').classList.add('visible');
        setTimeout(()=>document.getElementById('renameInput').focus(),100);
    }
    async function confirmRename() {
        const n = document.getElementById('renameInput').value; if(!n.trim())return;
        closeModal('renameOverlay'); await triggerExec(pendingAction.url, pendingAction.option, n);
    }

    function openDeleteLocal(url) { pendingAction = { 'url': url, 'option': 'Excluir' }; document.getElementById('deleteOverlay').classList.add('visible'); }
    async function confirmDelete() { closeModal('deleteOverlay'); await triggerExec(pendingAction.url, pendingAction.option); }

    function openMenu(e, id, url, title) {
        e.stopPropagation();
        const m = document.getElementById('contextMenu');
        
        m.style.top = e.clientY + 'px'; 
        m.style.left = e.clientX + 'px'; 
        m.innerHTML = ''; 
        m.classList.add('visible');
        
        const options = ['Compartilhar', 'Renomear', 'Excluir'];
        
        options.forEach(opt => {
            const d = document.createElement('div'); 
            d.className = 'menu-option'; 
            d.innerText = opt; 
            
            if (opt === 'Excluir') d.style.color = '#ff4a4a';
            
            // AGORA ENVIAMOS O 'id' TAMBÉM!
            d.onclick = () => handleMenuOption(id, url, opt, title); 
            m.appendChild(d);
        });
    }

    function handleMenuOption(id, url, opt, title) {
        document.getElementById('contextMenu').classList.remove('visible');
        
        if (opt.includes("Compartilhar")) {
            // Repassamos o ID para o toast saber qual chat carregar
            openShareLocal(id);
        }
        else if(opt.includes("Renomear")||opt.includes("Rename")) openRenameLocal(url, title);
        else if(opt.includes("Excluir")||opt.includes("Delete")) openDeleteLocal(url);
        else triggerExec(url, opt);
    }
    async function triggerExec(url, opt, nName=null) {
        setStatus(`Exec: ${opt}...`);
        ConsoleLog.info('[server.py][API]', `Triggering menu action: ${opt} on ${url}`);
        try {
            const res = await fetch('/api/menu/execute', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url:url, option:opt, new_name:nName})});
            const reader = res.body.getReader(); const dec = new TextDecoder();
            while(true){ const {done,value}=await reader.read(); if(done)break; }
        } catch(e){ ConsoleLog.error('[server.py][API]', `Menu execution failed: ${e}`); }
        setStatus(null); loadChats();
    }

    function triggerFileSelect() { document.getElementById('fileInput').click(); }
    function handleFileSelect(i) { if(i.files) Array.from(i.files).forEach(f=>processFile(f)); i.value=''; }
    function processFile(f) { 
        ConsoleLog.info('[browser.py][FILE]', `Processing upload: ${f.name}`);
        const r=new FileReader(); 
        r.onload=(e)=>{ currentFiles.push({name:f.name,data:e.target.result}); renderFiles(); }; 
        r.readAsDataURL(f); 
    }
    function renderFiles() { const l=document.getElementById('fileList'),b=document.getElementById('inputBox'); l.innerHTML=''; if(currentFiles.length){l.classList.add('visible');b.classList.add('has-file');currentFiles.forEach((f,i)=>{l.innerHTML+=`<div class="file-preview">${f.data.startsWith('data:image')?`<img src="${f.data}">`:'📄'} <span>${f.name}</span> <span class="close-btn" onclick="currentFiles.splice(${i},1);renderFiles()">✖</span></div>`});}else{l.classList.remove('visible');b.classList.remove('has-file');} }
    document.getElementById('userInput').addEventListener('paste', (event) => { const items = (event.clipboardData || event.originalEvent.clipboardData).items; for (let index in items) { if (items[index].kind === 'file') processFile(items[index].getAsFile()); } });

    async function loadChats() {
        try {
            const res = await fetch('/api/history');
            if(res.status === 401) { return; } // handled by checkLogin
            const data = await res.json();
            const l = document.getElementById('chatList');
            l.innerHTML = '<div class="chat-item" onclick="selectChat(null)">+ Novo Chat</div>';
            Object.keys(data).sort((a,b)=>new Date(data[b].updated_at)-new Date(data[a].updated_at)).forEach(id=>{
                const chat = data[id];
                const escTitle = chat.title.replace(/'/g, "\\'");
                const d = document.createElement('div');
                d.className = `chat-item ${currentChatId===id?'active':''}`;
                d.onclick = () => selectChat(id);
                d.innerHTML = `<span class="chat-title">${chat.title}</span><span class="chat-menu-btn" onclick="openMenu(event, '${id}', '${chat.url}', '${escTitle}')">⋮</span>`;
                l.appendChild(d);
            });
            return data;
        } catch(e) { return {}; }
    }

async function selectChat(id, force=false) {
        ConsoleLog.info('[chat.py][CHAT]', `Selected chat: ${id || 'New Chat'}`);
        currentChatId = id; 
        
        let localMessagesCount = 0;
        
        if(!force) {
            const allChats = await loadChats();
            if(allChats[id]) localMessagesCount = allChats[id].messages.length;
        }

        const area = document.getElementById('chatArea'); area.innerHTML = '';
        if(!id) { area.innerHTML = `<div class="message-row gpt"><div class="message-content"><div class="avatar gpt">G</div><div class="text-block">Novo chat.</div></div></div>`; return; }
        
        const res = await fetch('/api/history'); const data = await res.json();
        const chat = data[id];
        
        if (chat) {
             chat.messages.forEach(msg => {
                 let content = msg.content || "";
                 let isHtml = false;
                 
                 // Se for mensagem da IA, converte o Markdown em HTML com try/catch seguro
                 if (msg.role === 'assistant' && typeof marked !== 'undefined') {
                     try {
                         content = marked.parse(content);
                         isHtml = true; // Impede que o appendMessage fuja a formatação HTML
                     } catch(e) {
                         console.error("Erro ao renderizar Markdown:", e);
                     }
                 }
                 
                 appendMessage(msg.role, content, isHtml);
             });
             localMessagesCount = chat.messages.length; 
        }
        
        area.scrollTop = area.scrollHeight;
        
        if(chat && chat.url && !force) { 
            syncChat(id, chat.url, localMessagesCount); 
        }
    }


    async function syncChat(id, url, preSyncCount) {
        // Garantimos que o "toast" de status aparece ANTES de qualquer falha
        setStatus("Sincronizando..."); 
        ConsoleLog.info('[chat.py][CHAT]', `Syncing with ChatGPT... (Local: ${preSyncCount})`);
        try {
            const res = await fetch('/api/sync', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url:url, chat_id:id})});
            const d = await res.json();
            if(d.deleted) { 
                showToast("Chat deletado na origem."); 
                currentChatId=null; loadChats(); document.getElementById('chatArea').innerHTML=''; 
                ConsoleLog.warn('[chat.py][CHAT]', 'Remote chat deleted.');
            }
            else if(d.updated) {
                           await selectChat(id, true);
                           const res2 = await fetch('/api/history'); const data2 = await res2.json();
                           const postSyncCount = data2[id].messages.length;
                           const diff = postSyncCount - preSyncCount;
                           ConsoleLog.success('[chat.py][CHAT]', `Synced ✅. Local: ${preSyncCount} | Remoto: ${postSyncCount}`);
                           showToast(`Sincronizado! ${diff > 0 ? '+'+diff+' novas' : 'Atualizado'}`);
            } else {
                           ConsoleLog.success('[chat.py][CHAT]', `Synced ✅. Compatible (Count: ${preSyncCount})`);
                           showToast("Chat 100% Sincronizado!");
            }
        } catch(e){
            ConsoleLog.error('[chat.py][CHAT]', `Sync error: ${e}`);
            showToast("Erro ao sincronizar.");
        }
        // Desliga o toast de status da tela
        setStatus(null);
    }

    function appendMessage(role, content, isHtml=false) {
        const area = document.getElementById('chatArea');
        const d = document.createElement('div'); d.className = `message-row ${role}`;
        const av = role==='user'?'<div class="avatar user">U</div>':'<div class="avatar gpt">G</div>';
        d.innerHTML = `<div class="message-content">${av}<div class="text-block">${isHtml?content:content.replace(/\\n/g,'<br>')}</div></div>`;
        area.appendChild(d); return d.querySelector('.text-block');
    }

    async function sendMsg() {
        const inp = document.getElementById('userInput');
        const txt = inp.value;
        if(!txt.trim() && !currentFiles.length) return;
        
        ConsoleLog.info('[server.py][API]', 'Sending message to backend...');
        if(currentFiles.length) ConsoleLog.info('[server.py][API]', `Attaching ${currentFiles.length} files.`);
        
        appendMessage('user', currentFiles.length ? `[Anexos: ${currentFiles.length}] `+txt : txt);
        inp.value=''; inp.disabled=true;
        const gptBlock = appendMessage('assistant', '', true);
        setStatus("Enviando...");
        
        console.group('🌊 Response Stream'); // Start Grouping Stream
        try {
            const res = await fetch('/v1/chat/completions', { 
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({api_key: API_KEY, chat_id:currentChatId, message:txt, attachments:currentFiles})
            });
            const reader = res.body.getReader(); const dec = new TextDecoder();
            while(true) {
                const {done, value} = await reader.read(); if(done) break;
                const chunk = dec.decode(value, {stream:true});
                console.log('%c[CHUNK]', 'color: gray', chunk); // Log raw chunk
                chunk.split('\\n').forEach(line => {
                    if(!line.trim()) return;
                    try {
                        const d = JSON.parse(line);
                        
                        // Handle Log from Server
                        if(d.type === 'log') {
                             if(d.content.includes('[browser.py]')) ConsoleLog.browser(d.content.replace('[SERVER] [browser.py] ', ''));
                             else ConsoleLog.info('[server.py][SERVER]', d.content);
                        } 
                        // Handle Normal Types
                        // SE FOR HTML OU MARKDOWN, ESCREVE NA TELA
                        else if(d.type === 'html' || d.type === 'markdown') {
                            // console.debug('[STREAM]', 'Received chunk (HTML update)');
                                                try {
                                                    if(d.type === 'markdown') {
                                                              // Verifica se o marked carregou, senão exibe como texto puro (fallback)
                                                              if (typeof marked !== 'undefined') {
                                                                  gptBlock.innerHTML = marked.parse(d.content);
                                                              } else {
                                                                  gptBlock.innerHTML = `<pre style="white-space: pre-wrap;">${d.content}</pre>`;
                                                                  console.warn("Biblioteca Marked.js não carregou! A resposta será renderizada como texto puro.");
                                                              }
                                                    } else {
                                                               gptBlock.innerHTML = d.content;
                                                    }
                                                    document.getElementById('chatArea').scrollTop = document.getElementById('chatArea').scrollHeight;
                                                } catch (renderError) {
                                                    console.error("Erro ao renderizar:", renderError);
                                                    gptBlock.innerText = d.content; // Fallback de emergência
                                                }
                        }
                        else if (d.type === 'think') {
                                         // Encontra o container da mensagem de resposta
                                         let asstMsg = document.querySelector('.message-row.gpt:last-child');
                        
                                         if (asstMsg) {
                                             // Procura se já criamos uma caixa de pensamento
                                             let thinkBox = asstMsg.querySelector('.think-box');
                            
                                             // Se não existir, cria uma caixa cinza clarinha para exibir o raciocínio
                                             if (!thinkBox) {
                                                 thinkBox = document.createElement('div');
                                                 thinkBox.className = 'think-box';
                                                 thinkBox.style = "background-color: #f3f4f6; color: #4b5563; padding: 10px; border-radius: 8px; font-size: 13px; font-style: italic; margin-bottom: 10px; border-left: 3px solid #10a37f; white-space: pre-wrap;";
                                
                                                 // Insere a caixa de pensamento ANTES do texto principal da resposta
                                                 const textBlock = asstMsg.querySelector('.text-block');
                                                 if (textBlock) {
                                                     textBlock.parentNode.insertBefore(thinkBox, textBlock);
                                                 }
                                             }
                                             
                                             // Atualiza o texto do raciocínio em tempo real
                                             thinkBox.innerText = "💭 " + d.content;
                                             
                                             // Rola para o final da tela suavemente
                                             window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
                                         }
                                     }
                        else if(d.type==='status') {
                            ConsoleLog.info('[server.py][STATUS]', d.content);
                            setStatus(d.content);
                        }
                        else if(d.type==='finish') { 
                            ConsoleLog.success('[server.py][STREAM]', 'Stream finished.');
                            currentChatId = d.chat_id || currentChatId; 
                            loadChats(); 
                        }
                    } catch(e){}
                });
            }
        } catch(e) { 
            ConsoleLog.error('[server.py][API]', `Send error: ${e}`);
            alert(e); 
        }
        console.groupEnd(); // End Grouping
        currentFiles=[]; renderFiles(); setStatus(null); inp.disabled=false; inp.focus();
    }
    
    // --- FECHAR AO CLICAR FORA ---
    window.onclick = function(e) {
        if (e.target.classList.contains('share-overlay')) {
            closeModal(e.target.id);
        }
    }

    // Start Logic (Cookie Check)
    const hasCookie = document.cookie.split(';').some(c => c.trim().startsWith('session_token='));
    if (hasCookie) {
        console.group('🔐 Authentication Flow'); // Open group for initial check
        checkLogin();
    } else {
        ConsoleLog.warn('[auth.py][SYS]', 'No session cookie found. Waiting for login.');
        document.getElementById('authOverlay').style.display = 'flex';
    }
    console.groupEnd();
</script>
</body>
</html>"""

    # INJEÇÃO SEGURA: Substitui apenas o placeholder específico dentro da tag script
    final_html = html_template.replace("/* INJECTED_CONFIG_HERE */", js_config_block)

    with open(config.FRONTEND_FILE, 'w', encoding='utf-8') as f:
        f.write(final_html)
