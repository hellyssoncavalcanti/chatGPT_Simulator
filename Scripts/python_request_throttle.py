"""Throttle global de pedidos `request_source` Python para o `/v1/chat/completions`.

Encapsula o state historicamente mantido em `server.py` como duas
variáveis-módulo (`_python_anti_rate_limit_last_ts` + lock) em uma classe
testável offline. Padrão B do refactor: classe com `now_func` injetável,
sem dependência de Flask/Playwright/config; o caller mantém o tight-loop
de SSE/`time.sleep`.

API pública:
- ``begin(pmin, pmax, profile_count, *, rng=None) -> Optional[Tuple[base, target, last_ts]]``
  - Retorna ``None`` quando o caller deve seguir adiante sem esperar (curto-circuito histórico
    quando ``pmin <= 0 and pmax <= 0`` ou primeira chamada com ``last_ts`` zero). Ambos os
    casos atualizam ``_last_ts`` para o ``now()`` antes de retornar.
  - Caso contrário, retorna a tupla ``(base, target, last_ts)`` para alimentar o loop de
    espera. ``base``/``target`` são calculados via
    :func:`server_helpers.compute_python_request_interval` (extração anterior).
- ``remaining_seconds(target, last_ts) -> float``
  - Calcula ``max(0, target - (now() - last_ts))``. Pure function de leitura — não
    acessa o lock. Usada dentro do tight-loop do caller.
- ``commit() -> None``
  - Marca ``_last_ts = now()`` após o caller completar a espera.
- ``snapshot() -> Mapping[str, float]``
  - Snapshot thread-safe do estado interno (``last_ts``). Útil para
    ``/api/metrics`` ou debugging.

Invariantes preservados (vs. implementação histórica em ``server.py``):
1. Curto-circuito imediato quando ``pmin/pmax <= 0`` — registra ``last_ts``
   sem entrar no loop.
2. Primeira chamada (``last_ts == 0``) registra ``last_ts`` e retorna sem
   esperar (evita penalizar o primeiro pedido após boot).
3. ``_last_ts`` é sempre lido sob o lock; escrito sob o lock.
4. ``rng`` é repassado a ``compute_python_request_interval`` para
   determinismo em testes.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Mapping, Optional, Tuple

try:
    from server_helpers import compute_python_request_interval
except Exception:  # pragma: no cover — fallback defensivo se módulo truncado
    def compute_python_request_interval(pmin, pmax, profile_count, *, rng=None):
        import random as _random
        lo = max(0.0, float(pmin))
        hi = max(lo, float(pmax))
        rng = rng or _random.uniform
        base = rng(lo, hi)
        target = base / max(1, int(profile_count))
        return (base, target)


class PythonRequestThrottle:
    """Throttle thread-safe para pedidos Python, com `now_func` injetável."""

    def __init__(self, *, now_func: Callable[[], float] = time.time) -> None:
        self._now_func = now_func
        self._lock = threading.Lock()
        self._last_ts: float = 0.0

    def begin(
        self,
        pmin: float,
        pmax: float,
        profile_count: int,
        *,
        rng: Optional[Callable[[float, float], float]] = None,
    ) -> Optional[Tuple[float, float, float]]:
        """Decide se a espera é necessária; retorna `(base, target, last_ts)` ou `None`.

        Quando retorna ``None``, o caller já pode seguir — não há intervalo
        a respeitar. Em ambos os curto-circuitos (limites zero ou primeira
        chamada), ``_last_ts`` foi atualizado para ``now()``.
        """
        if pmin <= 0 and pmax <= 0:
            with self._lock:
                self._last_ts = self._now_func()
            return None

        with self._lock:
            last_ts = self._last_ts
            if last_ts <= 0:
                self._last_ts = self._now_func()
                return None

        base, target = compute_python_request_interval(
            pmin, pmax, profile_count, rng=rng
        )
        return (float(base), float(target), float(last_ts))

    def remaining_seconds(self, target: float, last_ts: float) -> float:
        """Calcula o tempo restante de espera com base no relógio atual.

        Pure function: lê ``now_func()`` mas não toca o lock nem ``_last_ts``.
        ``target`` e ``last_ts`` vêm de :meth:`begin`.
        """
        elapsed = self._now_func() - float(last_ts)
        return max(0.0, float(target) - elapsed)

    def commit(self) -> None:
        """Marca o término da espera atualizando ``_last_ts``."""
        with self._lock:
            self._last_ts = self._now_func()

    def snapshot(self) -> Mapping[str, float]:
        """Snapshot serializável do estado interno (thread-safe).

        Campos:
        - ``last_ts``: timestamp Unix do último ``commit()`` (ou ``0.0``
          se ``begin()`` nunca foi chamado).
        - ``age_seconds``: segundos desde o último ``commit()``
          (``0.0`` quando ``last_ts == 0``).

        Pensado para `/api/metrics` — `age_seconds` é o campo observável
        útil; `last_ts` cru ajuda em correlação cross-process.
        """
        with self._lock:
            last_ts = float(self._last_ts)
            if last_ts <= 0:
                age = 0.0
            else:
                age = max(0.0, self._now_func() - last_ts)
            return {
                "last_ts": last_ts,
                "age_seconds": round(age, 3),
            }

    # ── Acesso de testes (nunca usar em produção) ──
    def _force_last_ts(self, value: float) -> None:
        """Setter intencional para fixtures de teste; NÃO usar em runtime."""
        with self._lock:
            self._last_ts = float(value)
