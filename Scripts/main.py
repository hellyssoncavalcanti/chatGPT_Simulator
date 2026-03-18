# =============================================================================
# main.py — Ponto de entrada do ChatGPT Simulator
# =============================================================================
#
# RESPONSABILIDADE:
#   Inicializa todos os componentes do sistema em threads separadas e sobe
#   os servidores Flask. É o único arquivo executado diretamente pelo usuário
#   (via 0__start.bat).
#
# RELAÇÕES:
#   • Importa e orquestra: config, server, browser, utils, shared
#
# THREADS INICIADAS:
#   t_browser  — executa browser.browser_loop() (Playwright assíncrono)
#   t_http     — servidor HTTP na porta 3003 (acesso remoto sem TLS)
#   main       — servidor HTTPS na porta 3002 (acesso local com TLS)
#
# FLUXO DE INICIALIZAÇÃO:
#   1. Gera certificados TLS (utils.ensure_certificates)
#   2. Sobe thread do browser (Playwright + Chromium)
#   3. Sobe thread HTTP auxiliar (porta 3003)
#   4. Prepara frontend (utils.setup_frontend)
#   5. Sobe HTTPS principal (porta 3002) no processo principal
# =============================================================================
import threading
import time
import socket
import sys
import os
import ssl

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config
import server
import browser
import utils
from shared import browser_queue

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

def start_browser_thread():
    # Inicia o loop do navegador (agora assíncrono internamente)
    browser.browser_loop()

def start_http_server():
    http_port = config.PORT + 1
    # Servidor HTTP auxiliar
    server.app.run(host="0.0.0.0", port=http_port, debug=False, use_reloader=False)

if __name__ == "__main__":
    os.system('cls' if os.name == 'nt' else 'clear')
    
    print(f"\n=== CHATGPT SIMULATOR v{config.VERSION} (Async Tabs) ===")
    print("[INFO] Inicializando sistema...")

    utils.ensure_certificates()

    # 1. Thread do Navegador (Playwright Async)
    t_browser = threading.Thread(target=start_browser_thread)
    t_browser.daemon = True
    t_browser.start()

    # 2. Thread do Servidor HTTP (Porta 3003)
    t_http = threading.Thread(target=start_http_server)
    t_http.daemon = True
    t_http.start()

    utils.setup_frontend()

    local_ip = get_local_ip()
    print(f"\n[SERVIDOR ONLINE]")
    print(f" 🔒 HTTPS (Seguro):   https://localhost:{config.PORT}")
    print(f" 🌍 HTTP (Remoto):    http://{local_ip}:{config.PORT + 1}")
    print(f"\n[ADMIN] User: admin | Pass: 32713091")
    print("--------------------------------------------------\n")

    # 3. Processo Principal: Servidor HTTPS (Porta 3002)
    try:
        ssl_context = (config.CERT_FILE, config.KEY_FILE)
        server.app.run(host="0.0.0.0", port=config.PORT, debug=False, use_reloader=False, ssl_context=ssl_context)
    except Exception as e:
        print(f"[ERRO] Falha ao iniciar HTTPS: {e}")