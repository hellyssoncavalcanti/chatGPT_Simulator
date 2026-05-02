# =============================================================================
# profile_concurrency.py — Rastreador thread-safe de tarefas ativas por perfil
# =============================================================================
#
# RESPONSABILIDADE:
#   Contador de tarefas concorrentes por perfil Chromium, para observabilidade
#   em /api/metrics e Prometheus. NÃO limita concorrência por si só — o limite
#   é imposto por asyncio.Semaphore no browser_loop_async (browser.py).
#
# GARANTIAS:
#   • Sem dependência de flask, playwright ou config.
#   • Thread-safe via threading.Lock (chamável do loop asyncio via thread-safe ops).
#   • release() idempotente (não gera KeyError/negativo).
#   • snapshot() retorna apenas perfis com tarefas ativas.
# =============================================================================
import threading


class ProfileConcurrencyLimiter:
    """
    Rastreia quantas tarefas estão em execução por perfil Chromium.

    Usado por browser.py (acquire/release ao redor de cada tarefa) e
    por server.py (/api/metrics via snapshot()).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._active: dict[str, int] = {}

    def _key(self, profile: str) -> str:
        return str(profile or "default").strip() or "default"

    def acquire(self, profile: str) -> None:
        """Registra o início de uma tarefa neste perfil."""
        k = self._key(profile)
        with self._lock:
            self._active[k] = self._active.get(k, 0) + 1

    def release(self, profile: str) -> None:
        """Registra o término de uma tarefa neste perfil (idempotente)."""
        k = self._key(profile)
        with self._lock:
            current = self._active.get(k, 0)
            if current <= 1:
                self._active.pop(k, None)
            else:
                self._active[k] = current - 1

    def active_count(self, profile: str) -> int:
        k = self._key(profile)
        with self._lock:
            return self._active.get(k, 0)

    def snapshot(self) -> dict[str, int]:
        """Retorna {profile: contagem} apenas para perfis com tarefas ativas."""
        with self._lock:
            return {k: v for k, v in self._active.items() if v > 0}
