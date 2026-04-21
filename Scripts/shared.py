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
import time
import itertools
from collections import OrderedDict, deque
from typing import Any

# ─────────────────────────────────────────────────────────────
# CAPTURA CONFIGURAÇÃO DE DEBUG (que é estabelecida no arquivo "config.py").
# ─────────────────────────────────────────────────────────────
# Verifica se config já foi importado; se não, importa.
# Quando já estiver em sys.modules, ainda precisamos vinculá-lo no escopo local.
if 'config' in sys.modules and sys.modules.get('config') is not None:
    config = sys.modules['config']
else:
    import config

# Tenta importar DEBUG_LOG do módulo config já carregado
try:
    DEBUG_LOG = config.DEBUG_LOG
except AttributeError:
    DEBUG_LOG = False  # fallback se a variável não existir no config
    print("⚠️ DEBUG_LOG não encontrado no config.py. Usando False como padrão.")

class BrowserTaskQueue:
    """
    Fila estruturada para tarefas do browser com prioridade estável (FIFO por prioridade).

    Mantém interface compatível com queue.Queue (`put`, `get`, `qsize`, `empty`) para
    não exigir mudanças invasivas em server.py/browser.py.
    """

    def __init__(self):
        self._queues_by_priority: dict[int, OrderedDict[str, deque[tuple[float, int, dict[str, Any]]]]] = {}
        self._size = 0
        self._not_empty = threading.Condition(threading.Lock())
        self._seq = itertools.count()
        self._stats = {
            "enqueued_total": 0,
            "dequeued_total": 0,
            "wait_ms_total": 0.0,
            "max_wait_ms": 0.0,
            "by_priority_enqueued": {},
            "by_priority_dequeued": {},
            "by_origin_enqueued": {"remote": 0, "python": 0, "unknown": 0},
            "by_origin_dequeued": {"remote": 0, "python": 0, "unknown": 0},
        }

    def _classify_origin(self, task: dict[str, Any]) -> str:
        if not isinstance(task, dict):
            return "unknown"
        src = str(task.get("request_source") or task.get("sender") or "").strip().lower()
        if not src:
            return "unknown"
        if src.endswith(".py") or ".py/" in src or src.startswith("python:") or "analisador_prontuarios" in src:
            return "python"
        return "remote"

    def _resolve_priority(self, task: dict[str, Any]) -> int:
        if not isinstance(task, dict):
            return 100

        # Prioridade explícita ganha de qualquer heurística.
        explicit = task.get("queue_priority")
        if explicit is not None:
            try:
                return int(explicit)
            except Exception:
                pass

        action = str(task.get("action") or "").upper()
        if action == "STOP":
            return 0
        if action in {"EXEC_MENU", "GET_MENU", "DELETE"}:
            return 5
        if action == "CHAT":
            # Pedidos remotos sempre priorizados frente a scripts/automações.
            origin = self._classify_origin(task)
            if origin == "remote":
                return 10
            return 40
        if action in {"SYNC", "WEB_SEARCH", "UPTODATE_SEARCH"}:
            return 30
        return 100

    def _resolve_tenant(self, task: dict[str, Any]) -> str:
        if not isinstance(task, dict):
            return "__global__"
        return (
            str(task.get("chat_id") or "").strip()
            or str(task.get("origin_url") or "").strip()
            or str(task.get("url") or "").strip()
            or str(task.get("request_source") or "").strip()
            or "__global__"
        )

    def _ensure_lane(self, priority: int) -> OrderedDict[str, deque[tuple[float, int, dict[str, Any]]]]:
        lane = self._queues_by_priority.get(priority)
        if lane is None:
            lane = OrderedDict()
            self._queues_by_priority[priority] = lane
        return lane

    def _pop_next(self) -> tuple[float, int, dict[str, Any], int]:
        for priority in sorted(self._queues_by_priority.keys()):
            lane = self._queues_by_priority[priority]
            while lane:
                tenant, tenant_queue = next(iter(lane.items()))
                if not tenant_queue:
                    del lane[tenant]
                    continue
                enqueued_at, _seq, task = tenant_queue.popleft()
                if tenant_queue:
                    lane.move_to_end(tenant)  # round-robin entre tenants da mesma prioridade
                else:
                    del lane[tenant]
                if not lane:
                    del self._queues_by_priority[priority]
                return enqueued_at, _seq, task, priority
        raise queue.Empty

    def put(self, task: dict[str, Any], block: bool = True, timeout: float | None = None):
        priority = self._resolve_priority(task)
        created_at = time.time()
        seq = next(self._seq)
        tenant = self._resolve_tenant(task)
        origin = self._classify_origin(task)

        with self._not_empty:
            lane = self._ensure_lane(priority)
            if tenant not in lane:
                lane[tenant] = deque()
            lane[tenant].append((created_at, seq, task))
            self._size += 1
            self._stats["enqueued_total"] += 1
            self._stats["by_priority_enqueued"][priority] = self._stats["by_priority_enqueued"].get(priority, 0) + 1
            self._stats["by_origin_enqueued"][origin] = self._stats["by_origin_enqueued"].get(origin, 0) + 1
            self._not_empty.notify()

    def get(self, block: bool = True, timeout: float | None = None) -> dict[str, Any]:
        deadline = None if timeout is None else (time.time() + max(0.0, timeout))
        with self._not_empty:
            while self._size == 0:
                if not block:
                    raise queue.Empty
                if deadline is None:
                    self._not_empty.wait()
                else:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise queue.Empty
                    self._not_empty.wait(timeout=remaining)

            enqueued_at, _seq, task, priority = self._pop_next()
            self._size -= 1

            waited_ms = max(0.0, (time.time() - enqueued_at) * 1000.0)
            origin = self._classify_origin(task)
            self._stats["dequeued_total"] += 1
            self._stats["wait_ms_total"] += waited_ms
            self._stats["max_wait_ms"] = max(self._stats["max_wait_ms"], waited_ms)
            self._stats["by_priority_dequeued"][priority] = self._stats["by_priority_dequeued"].get(priority, 0) + 1
            self._stats["by_origin_dequeued"][origin] = self._stats["by_origin_dequeued"].get(origin, 0) + 1
            return task

    def qsize(self) -> int:
        with self._not_empty:
            return self._size

    def empty(self) -> bool:
        with self._not_empty:
            return self._size == 0

    def snapshot_stats(self) -> dict[str, Any]:
        with self._not_empty:
            dequeued = max(1, int(self._stats["dequeued_total"]))
            avg_wait_ms = float(self._stats["wait_ms_total"]) / dequeued
            lane_sizes = {
                str(priority): int(sum(len(q) for q in lane.values()))
                for priority, lane in self._queues_by_priority.items()
            }
            return {
                "queue_size": int(self._size),
                "avg_wait_ms": round(avg_wait_ms, 2),
                "max_wait_ms": round(float(self._stats["max_wait_ms"]), 2),
                "enqueued_total": int(self._stats["enqueued_total"]),
                "dequeued_total": int(self._stats["dequeued_total"]),
                "by_priority_enqueued": dict(self._stats["by_priority_enqueued"]),
                "by_priority_dequeued": dict(self._stats["by_priority_dequeued"]),
                "by_origin_enqueued": dict(self._stats["by_origin_enqueued"]),
                "by_origin_dequeued": dict(self._stats["by_origin_dequeued"]),
                "lane_sizes": lane_sizes,
            }


# Fila principal de comunicação entre o Flask e o Browser.
# Agora estruturada por prioridade, mas com API compatível.
browser_queue = BrowserTaskQueue()

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
