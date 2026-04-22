"""Cooldown global de chat-ChatGPT extraído de `server.py` (Lote P1 opção 1).

Encapsula o estado e a lógica de backoff exponencial usados quando o ChatGPT
responde com rate-limit. Antes desta extração, os três globals
(`_chat_rate_limit_until`, `_chat_rate_limit_strikes`, `_chat_rate_limit_lock`)
ficavam em `server.py` e só eram testáveis indiretamente (via testes que
dependiam de Flask).

Módulo puro: sem Flask, Playwright ou `config`. Os parâmetros vêm do
construtor para que o chamador em `server.py` possa injetar os valores
históricos (cooldown default 240s, teto 1800s, até 6 strikes) sem precisar
duplicar constantes.

Segue o "Padrão B (helper puro com state)" já validado por `SecurityState`:
- Classe encapsula dicts/state + lock próprio.
- `now_func` é ponto de injeção para testes determinísticos.
- Wrappers em `server.py` delegam ao singleton e preservam assinatura original.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class ChatRateLimitCooldown:
    """Gerencia o cooldown global após rate-limit do ChatGPT.

    Backoff exponencial:
    - Primeira infração (strikes=0) → cooldown base (ou `retry_after_seconds`).
    - Cada nova infração DENTRO do cooldown atual incrementa `strikes`
      (clamp em `max_strikes`), dobrando o multiplicador.
    - Infração FORA do cooldown atual reseta `strikes` para 0.
    - `adjusted_cooldown = min(max_cooldown_sec, base_cooldown * 2**strikes)`.
    - `_until_ts` nunca regride (`max(old, now + adjusted)`).
    """

    def __init__(
        self,
        *,
        default_cooldown_sec: int = 240,
        max_cooldown_sec: int = 1800,
        max_strikes: int = 6,
        now_func: Callable[[], float] = time.time,
    ) -> None:
        self.default_cooldown_sec = max(1, int(default_cooldown_sec))
        self.max_cooldown_sec = max(self.default_cooldown_sec, int(max_cooldown_sec))
        self.max_strikes = max(0, int(max_strikes))
        self._now_func = now_func
        self._lock = threading.Lock()
        self._until_ts: float = 0.0
        self._strikes: int = 0

    # ─────────────────────────────────────────────────────────
    # API pública
    # ─────────────────────────────────────────────────────────
    def register(self, retry_after_seconds=None, reason: str = "") -> int:
        """Registra uma ocorrência de rate-limit e devolve o `adjusted_cooldown`
        efetivo (em segundos, inteiro).

        `retry_after_seconds` substitui o default quando recebido do upstream.
        `reason` é preservado apenas para o chamador registrar no log externo
        — o módulo puro não emite logs.
        """
        cooldown = retry_after_seconds or self.default_cooldown_sec
        cooldown = max(1, int(cooldown))
        now = self._now_func()
        with self._lock:
            remaining = max(0.0, self._until_ts - now)
            if remaining > 0:
                self._strikes = min(self.max_strikes, self._strikes + 1)
            else:
                self._strikes = 0
            backoff_multiplier = 2 ** self._strikes
            adjusted_cooldown = min(
                self.max_cooldown_sec,
                int(cooldown * backoff_multiplier),
            )
            until_ts = now + adjusted_cooldown
            self._until_ts = max(self._until_ts, until_ts)
        return adjusted_cooldown

    def remaining_seconds(self) -> float:
        """Segundos restantes do cooldown ativo (0.0 se expirado)."""
        with self._lock:
            return max(0.0, self._until_ts - self._now_func())

    def snapshot(self) -> dict:
        """Resumo observável para `/api/metrics` e testes."""
        with self._lock:
            remaining = max(0.0, self._until_ts - self._now_func())
            return {
                "remaining_seconds": round(remaining, 3),
                "strikes": self._strikes,
                "until_ts": self._until_ts,
            }


__all__ = ["ChatRateLimitCooldown"]
