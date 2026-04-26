"""Throttle global de espaçamento entre buscas web (Google/UpToDate).

Encapsula o state historicamente mantido em `server.py` como variáveis-módulo
(`_web_search_last_started_at`, `_web_search_last_interval_sec` + lock) em uma
classe pura e testável offline.

Padrão B do refactor:
- classe com `now_func` injetável;
- sem dependência de Flask/Playwright/config;
- caller (`server.py`) mantém mensagens SSE e `time.sleep`.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Callable, Mapping, Optional


class WebSearchThrottle:
    """Reserva janelas de início de busca com espaçamento humano global."""

    def __init__(
        self,
        *,
        now_func: Callable[[], float] = time.time,
        rng_func: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._now_func = now_func
        self._rng_func = rng_func
        self._lock = threading.Lock()
        self._last_started_at: float = 0.0
        self._last_interval_sec: float = 0.0

    def reserve_slot(self, min_interval_sec: float, max_interval_sec: float) -> Mapping[str, float]:
        """Reserva a próxima janela permitida e retorna o contexto de espera.

        Retorno compatível com o contrato histórico de `_reserve_web_search_slot`:
        `interval_sec`, `scheduled_start_at`, `wait_seconds`, `requested_at`.
        """
        now = float(self._now_func())
        lo = max(0.0, float(min_interval_sec))
        hi = max(lo, float(max_interval_sec))
        interval = float(self._rng_func(lo, hi))

        with self._lock:
            earliest_start = now
            if self._last_started_at > 0:
                earliest_start = max(earliest_start, self._last_started_at + interval)

            wait_seconds = max(0.0, earliest_start - now)
            self._last_started_at = float(earliest_start)
            self._last_interval_sec = interval

        return {
            "interval_sec": interval,
            "scheduled_start_at": float(earliest_start),
            "wait_seconds": float(wait_seconds),
            "requested_at": now,
        }

    def snapshot(self) -> Mapping[str, float]:
        """Snapshot serializável do estado interno (thread-safe)."""
        with self._lock:
            return {
                "last_started_at": float(self._last_started_at),
                "last_interval_sec": float(self._last_interval_sec),
            }

    # ── Acesso de testes (não usar em runtime) ──
    def _force_state(self, *, last_started_at: Optional[float] = None, last_interval_sec: Optional[float] = None) -> None:
        with self._lock:
            if last_started_at is not None:
                self._last_started_at = float(last_started_at)
            if last_interval_sec is not None:
                self._last_interval_sec = float(last_interval_sec)
