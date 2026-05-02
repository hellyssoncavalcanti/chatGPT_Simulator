"""Suporte a Correlation-ID ponta-a-ponta.

Módulo puro: sem Flask, Playwright nem config.

O correlation-id é um identificador opaco (UUID4 curto ou valor passado
pelo cliente via ``X-Correlation-Id``) que acompanha um request desde
a entrada HTTP até o task na fila, os eventos SSE e o log de auditoria.

Uso típico em server.py::

    cid = extract_correlation_id(request.headers)
    log(f"[{cid}] iniciando chat_completions...")
    task_payload["correlation_id"] = cid

"""
from __future__ import annotations

import uuid
from typing import Mapping

# Nome canônico do header HTTP.
CORRELATION_ID_HEADER = "X-Correlation-Id"

# Prefixo para facilitar grep nos logs: "[cid:xxxx]".
LOG_PREFIX_FORMAT = "[cid:{cid}]"

# Tamanho máximo aceito de um correlation-id enviado pelo cliente.
MAX_CORRELATION_ID_LEN = 128


def generate_correlation_id() -> str:
    """Gera um correlation-id único — primeiros 8 hex do UUID4.

    Formato compacto (8 chars) suficiente para distinguir requests no log
    sem poluir a linha. Colisão em 1/2^32 ≈ negligível para o volume esperado.
    """
    return uuid.uuid4().hex[:8]


def extract_correlation_id(
    headers: Mapping[str, str],
    fallback: str | None = None,
) -> str:
    """Extrai o correlation-id do header HTTP ou gera um novo.

    Args:
        headers: cabeçalhos do request (``request.headers`` ou dict).
        fallback: valor a usar se o header estiver ausente/inválido.
                  Se ``None``, gera novo ID automaticamente.

    Returns:
        String não-vazia com o correlation-id efetivo.
    """
    raw = (headers.get(CORRELATION_ID_HEADER) or "").strip()
    if raw and len(raw) <= MAX_CORRELATION_ID_LEN:
        # Aceita apenas caracteres seguros para log (alphanum + -_.).
        safe = "".join(c for c in raw if c.isalnum() or c in "-_.")
        if safe:
            return safe
    if fallback:
        return fallback
    return generate_correlation_id()


def format_log_prefix(correlation_id: str) -> str:
    """Formata o prefixo ``[cid:xxxx]`` para uso em linhas de log."""
    return LOG_PREFIX_FORMAT.format(cid=correlation_id or "?")


def inject_into_payload(payload: dict, correlation_id: str) -> dict:
    """Injeta ``correlation_id`` no payload sem mutar o original.

    Retorna um **novo dict** com a chave adicionada. Seguro para usar
    em payloads de fila (evita efeitos colaterais no chamador).
    """
    result = dict(payload)
    result["correlation_id"] = correlation_id
    return result


__all__ = [
    "CORRELATION_ID_HEADER",
    "MAX_CORRELATION_ID_LEN",
    "generate_correlation_id",
    "extract_correlation_id",
    "format_log_prefix",
    "inject_into_payload",
]
