"""Dedupe de pedidos `/api/sync` na janela de `window_sec`.

Padrão B do refactor (módulo puro com state + lock):
  - Classe `SyncDedup` encapsula um `dict[sync_key -> started_at]` e o
    `threading.Lock` que o protege.
  - `now_func` injetável permite testes determinísticos sem `time.sleep`.
  - Método `try_acquire(sync_key)` retorna `(acquired, elapsed,
    retry_after)`; o chamador decide se devolve 409 ou prossegue.
  - Método `release(sync_key)` remove a chave (chamado no `finally` do
    handler — idempotente para chaves ausentes).
  - Método `snapshot()` devolve dict JSON-serializável usado pelo
    `/api/metrics`.

NÃO importa `flask`/`config`. Idiomas históricos preservados:
  - Janela default 120s (ver `server.api_sync` → `(time.time() -
    started_at) < 120`).
  - `retry_after = max(1, window_sec - elapsed)` evita 0/negativos.
  - `elapsed` é truncado para `int` (mensagem amigável no log).
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Tuple


DEFAULT_DEDUP_WINDOW_SEC = 120


class SyncDedup:
    def __init__(
        self,
        window_sec: int = DEFAULT_DEDUP_WINDOW_SEC,
        *,
        now_func: Callable[[], float] = time.time,
    ) -> None:
        self._window_sec = int(window_sec)
        self._now = now_func
        self._lock = threading.Lock()
        self._active: Dict[str, float] = {}

    @property
    def window_sec(self) -> int:
        return self._window_sec

    def try_acquire(self, sync_key: str) -> Tuple[bool, int, int]:
        """Tenta reservar o slot para `sync_key`.

        Retorna `(acquired, elapsed_sec, retry_after_sec)`:
          - `acquired=True`: slot reservado neste momento; `elapsed=0` e
            `retry_after=0` (campos só fazem sentido na resposta 409).
          - `acquired=False`: já existe sync em andamento dentro da
            janela; `elapsed` e `retry_after` (≥1) descrevem o estado
            para o cliente.

        Quando a entrada existente expirou (fora da janela), a chave é
        sobrescrita silenciosamente — comportamento idêntico ao
        `server.api_sync` histórico que apenas comparava `< 120`.
        """
        with self._lock:
            now = float(self._now())
            started = self._active.get(sync_key)
            if started is not None and (now - started) < self._window_sec:
                elapsed = int(now - started)
                retry_after = max(1, self._window_sec - elapsed)
                return False, elapsed, retry_after
            self._active[sync_key] = now
            return True, 0, 0

    def release(self, sync_key: str) -> None:
        """Libera o slot. Idempotente para chaves ausentes."""
        with self._lock:
            self._active.pop(sync_key, None)

    def active_count(self) -> int:
        """Quantidade de syncs ativos (usado por `/api/metrics`).

        Equivalente ao histórico `len(ACTIVE_SYNCS)` em `server.py`.
        """
        with self._lock:
            return len(self._active)

    def snapshot(self) -> dict:
        """Snapshot JSON-serializável para `/api/metrics`."""
        with self._lock:
            return {
                "window_sec": self._window_sec,
                "active_keys": len(self._active),
            }


__all__ = [
    "DEFAULT_DEDUP_WINDOW_SEC",
    "SyncDedup",
]
