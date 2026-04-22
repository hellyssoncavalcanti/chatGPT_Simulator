"""Helpers puros extraídos de `server.py` (Lote P0 passo 2 do refactor).

Funções aqui NÃO podem depender de:
  - Flask / Werkzeug (nada de `request`, `jsonify`, decoradores HTTP).
  - `config` (tudo que precisaria de config deve ser recebido como parâmetro).
  - Estado global de `server.py` (locks, dicts compartilhados).

Motivo: estas funções são caminho quente da fila e do feedback de espera
(rate-limit, fila Python). Mantê-las puras permite testá-las offline sem
instalar `flask` / `cryptography` e sem montar o app completo.
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Callable, Mapping, MutableSequence, Optional


def format_wait_seconds(seconds) -> str:
    """Formata um número de segundos como MM:SS (clamp em 0).

    Exemplo: `format_wait_seconds(125) == "02:05"`.
    Valores negativos ou `NaN`/`None` resultam em `"00:00"`.
    """
    try:
        remaining = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        remaining = 0
    mins, secs = divmod(remaining, 60)
    return f"{mins:02d}:{secs:02d}"


def queue_status_payload(wait_seconds, position: int, total: int, sender_label: str) -> str:
    """Produz o JSON (string) de status de fila Python no stream SSE.

    Formato estável consumido pelo frontend/analisador; manter chaves
    (`type`, `content`, `phase`, `wait_seconds`, `queue_position`,
    `queue_size`, `sender`) para não quebrar consumidores.
    """
    try:
        wait_f = float(wait_seconds)
    except (TypeError, ValueError):
        wait_f = 0.0
    wait_f = max(0.0, wait_f)
    pos = int(position)
    tot = int(total)
    return json.dumps({
        "type": "status",
        "content": (
            f"⏳ Fila interna do servidor: posição {pos}/{max(1, tot)}. "
            f"Tempo restante estimado para liberação: {format_wait_seconds(wait_f)}."
        ),
        "phase": "server_python_queue_wait",
        "wait_seconds": round(wait_f, 1),
        "queue_position": pos,
        "queue_size": tot,
        "sender": sender_label,
    }, ensure_ascii=False)


def prune_old_attempts(
    dq: MutableSequence[float],
    window_sec: int,
    *,
    now: Optional[float] = None,
    now_func: Callable[[], float] = time.time,
) -> int:
    """Remove timestamps antigos (mais velhos que `window_sec`) de `dq`.

    Retorna o número de entradas removidas. Aceita qualquer sequência
    mutável que implemente `popleft()` (normalmente `collections.deque`).

    `now`/`now_func`: ganchos para testes determinísticos — em produção,
    chamar apenas com `dq` e `window_sec` reproduz o comportamento
    histórico de `server._prune_old_attempts`.
    """
    current = float(now) if now is not None else float(now_func())
    cutoff = current - int(window_sec)
    removed = 0
    while dq and dq[0] < cutoff:
        # Usa popleft se disponível (deque); fallback para pop(0) em listas.
        if hasattr(dq, "popleft"):
            dq.popleft()  # type: ignore[attr-defined]
        else:
            dq.pop(0)
        removed += 1
    return removed


def count_active_chatgpt_profiles(profiles_map: Optional[Mapping]) -> int:
    """Quantidade de perfis Chromium/ChatGPT acionáveis em paralelo.

    Recebe o mapa `chave → diretório` (tipicamente `config.CHROMIUM_PROFILES`).
    Sempre retorna ao menos 1 para evitar divisão por zero no cálculo do
    intervalo anti-rate-limit.
    """
    if not profiles_map:
        return 1
    try:
        return max(1, len(profiles_map))
    except TypeError:
        return 1


__all__ = [
    "format_wait_seconds",
    "queue_status_payload",
    "prune_old_attempts",
    "count_active_chatgpt_profiles",
]


# Exportação auxiliar usada apenas para compatibilidade dos wrappers em
# server.py — evita ruído com o símbolo `deque` não importado em testes.
_deque_cls = deque
