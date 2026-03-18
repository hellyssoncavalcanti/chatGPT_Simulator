# =============================================================================
# shared.py — Fila de comunicação entre o Flask (server.py) e o Playwright (browser.py)
# =============================================================================
#
# RESPONSABILIDADE:
#   Fornece a fila thread-safe browser_queue que desacopla o servidor HTTP
#   do loop assíncrono do navegador. Qualquer módulo que precise enviar
#   uma tarefa ao browser importa esta fila e faz .put(task).
#
# RELAÇÕES:
#   • Importado por: server.py (produz tarefas), browser.py (consome tarefas),
#                    main.py (importa para garantir inicialização única)
#
# FLUXO:
#   server.py  →  browser_queue.put({action, ...})
#   browser.py →  browser_queue.get()  →  executa ação no Chromium
# =============================================================================
import queue

# Fila principal de comunicação entre o Flask e o Browser
browser_queue = queue.Queue()