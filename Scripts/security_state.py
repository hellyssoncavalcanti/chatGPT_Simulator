"""Estado de segurança per-IP extraído de `server.py` (Lote P1 opção 2).

Encapsula os dicts e lock que controlam:
  - rate-limit por combinação (IP, chave) em janela deslizante;
  - bloqueio de IP por tentativas de login falhas (brute-force);
  - expiração automática do bloqueio.

Módulo puro: sem Flask, Playwright ou `config`. Os limites são parâmetros
do construtor — o chamador em `server.py` injeta os valores vindos de
`config.SECURITY_*`. Isso permite testar toda a lógica offline.

Mantém compatibilidade com os wrappers existentes em `server.py`:
`_is_ip_blocked`, `_register_rate_limit_hit`, `_register_login_failure`,
`_clear_login_failures`. Cada um delega a um método do singleton.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Optional


class SecurityState:
    """Guarda e gerencia o estado volátil de defesa contra abuso.

    Thread-safe: todas as operações mutáveis passam pelo mesmo `threading.Lock`.
    `now_func` é ponto de injeção para testes determinísticos.
    """

    def __init__(
        self,
        *,
        rate_limit_window_sec: int = 60,
        rate_limit_per_min: int = 120,
        login_max_fails: int = 8,
        login_block_sec: int = 900,
        now_func: Callable[[], float] = time.time,
    ) -> None:
        self.rate_limit_window_sec = max(1, int(rate_limit_window_sec))
        self.rate_limit_per_min = max(1, int(rate_limit_per_min))
        self.login_max_fails = max(1, int(login_max_fails))
        self.login_block_sec = max(1, int(login_block_sec))
        self._now_func = now_func
        self._lock = threading.Lock()
        self._rate_limit_hits: dict[str, deque[float]] = {}
        self._blocked_ips: dict[str, dict] = {}
        self._failed_login_attempts: dict[str, deque[float]] = {}

    # ─────────────────────────────────────────────────────────
    # Helpers internos
    # ─────────────────────────────────────────────────────────
    def _prune(self, dq: deque[float], window_sec: int, now: Optional[float] = None) -> None:
        cutoff = (now if now is not None else self._now_func()) - window_sec
        while dq and dq[0] < cutoff:
            dq.popleft()

    # ─────────────────────────────────────────────────────────
    # API pública
    # ─────────────────────────────────────────────────────────
    def is_ip_blocked(self, ip: str) -> tuple[bool, float, str]:
        """Retorna `(blocked, seconds_remaining, reason)`.

        Se o bloqueio expirou, remove do mapa e retorna `(False, 0, "")`.
        """
        with self._lock:
            rec = self._blocked_ips.get(ip)
            if not rec:
                return False, 0.0, ""
            until = float(rec.get("until", 0.0))
            now = self._now_func()
            if until <= now:
                self._blocked_ips.pop(ip, None)
                return False, 0.0, ""
            return True, max(0.0, until - now), str(rec.get("reason", "blocked"))

    def register_rate_limit_hit(self, ip: str, key: str) -> tuple[bool, float]:
        """Adiciona 1 hit em `(ip, key)`. Retorna `(excedeu?, retry_after)`.

        A janela é `rate_limit_window_sec`; o teto é `rate_limit_per_min`.
        """
        map_key = f"{ip}:{key}"
        with self._lock:
            dq = self._rate_limit_hits.setdefault(map_key, deque())
            now = self._now_func()
            dq.append(now)
            self._prune(dq, self.rate_limit_window_sec, now=now)
            if len(dq) > self.rate_limit_per_min:
                retry_after = max(1.0, self.rate_limit_window_sec - (now - dq[0]))
                return True, retry_after
            return False, 0.0

    def register_login_failure(self, ip: str) -> bool:
        """Conta uma falha de login. Retorna `True` se o IP ficou bloqueado
        nesta chamada (útil para audit). Sempre expira falhas antigas."""
        now = self._now_func()
        with self._lock:
            dq = self._failed_login_attempts.setdefault(ip, deque())
            dq.append(now)
            self._prune(dq, self.login_block_sec, now=now)
            if len(dq) >= self.login_max_fails:
                self._blocked_ips[ip] = {
                    "until": now + self.login_block_sec,
                    "reason": "bruteforce_login",
                }
                return True
            return False

    def clear_login_failures(self, ip: str) -> None:
        with self._lock:
            self._failed_login_attempts.pop(ip, None)

    def snapshot(self) -> dict:
        """Retorna um resumo observável (para `/api/metrics` futuro)."""
        with self._lock:
            return {
                "rate_limit_keys": len(self._rate_limit_hits),
                "blocked_ips": len(self._blocked_ips),
                "tracked_login_ips": len(self._failed_login_attempts),
            }


__all__ = ["SecurityState"]
