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
import threading
import sys

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
    print("⚠️ DEBUG_LOG não encontrado no config.py. Usando False como padrão.")

# Fila principal de comunicação entre o Flask e o Browser
browser_queue = queue.Queue()

# Registro de URLs de arquivos do ChatGPT para proxy sob demanda.
# Mapeamento: file_id → {"url": url_original, "name": nome_exibição}
# Populado por browser.py ao detectar links de download na resposta.
# Consultado por server.py ao receber requisição de download do usuário.
_file_registry = {}
_file_registry_lock = threading.Lock()


def register_file(
    file_id: str,
    url: str,
    name: str,
    *,
    payload_b64: str | None = None,
    content_type: str | None = None
):
    """Registra um arquivo para proxy futuro (URL remota ou payload em memória)."""
    with _file_registry_lock:
        _file_registry[file_id] = {
            "url": url,
            "name": name,
            "payload_b64": payload_b64,
            "content_type": content_type
        }


def get_file_info(file_id: str) -> dict | None:
    """Retorna info do arquivo ou None se não registrado."""
    with _file_registry_lock:
        return _file_registry.get(file_id)


def list_files() -> dict:
    """Retorna cópia do registro inteiro (para debug)."""
    with _file_registry_lock:
        return dict(_file_registry)
