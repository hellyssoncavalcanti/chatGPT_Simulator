"""Sanitização de logs/PII (Lote P1 passo 1 do refactor).

Módulo puro: sem Flask, Playwright ou `config`. Oferece máscaras para
segredos e dados sensíveis que NÃO devem vazar em logs, métricas, SSE
ou audit events.

Este passo entrega apenas as funções e os testes — a integração em
`_audit_event` (server.py:213) e em `utils.file_log` é um passo
separado do Lote P1 (ver `REFACTOR_PROGRESS.md`).

Cada máscara segue dois princípios:
  1. **Não aumentar significativamente o tamanho do texto** — em caminho
     de log quente, strings muito grandes causam pressão de IO.
  2. **Preservar contexto de diagnóstico** — por exemplo, manter o
     prefixo do segredo (primeiros 4 chars) para permitir correlação
     com o valor não mascarado em um armazenamento seguro, sem expor
     o segredo completo.
"""

from __future__ import annotations

import os
import re
from typing import Iterable


# ─────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────
# Prefixo preservado em máscaras parciais. 4 chars é suficiente para
# triagem sem permitir recuperação do valor original.
PREVIEW_PREFIX_LEN = 4
MASK = "***REDACTED***"


# ─────────────────────────────────────────────────────────────
# API keys — cobre tanto o formato CVAPI_* usado neste projeto
# quanto `api_key=<valor>` / `"api_key": "<valor>"` em JSON.
# ─────────────────────────────────────────────────────────────
# Aceita hex/base64-ish com letras, dígitos, _, -, até 80 chars.
_CVAPI_RE = re.compile(r"\bCVAPI_[A-Za-z0-9_-]{8,80}\b")
_API_KEY_ASSIGN_RE = re.compile(
    r"(?i)(['\"]?api[_-]?key['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9._-]{8,160})"
)
_API_KEY_HEADER_RE = re.compile(
    r"(?i)(x-api-key\s*:\s*)([A-Za-z0-9._-]{8,160})"
)


def _preview_then_mask(m: re.Match, start_group: int = 1, secret_group: int = 2) -> str:
    """Reescreve um match mantendo o grupo de nome/preâmbulo e
    substituindo o grupo do segredo por `prefix[:4]***`."""
    prefix = m.group(start_group)
    secret = m.group(secret_group) or ""
    if len(secret) <= PREVIEW_PREFIX_LEN:
        return f"{prefix}{MASK}"
    return f"{prefix}{secret[:PREVIEW_PREFIX_LEN]}***"


def mask_api_key(text: str) -> str:
    """Mascara valores de API key em strings livres.

    Cobre:
      - Literal `CVAPI_xxxxx` (formato deste projeto).
      - `api_key=xxxx`, `api_key: "xxxx"`, `api-key=xxxx`.
      - Header `X-API-Key: xxxx`.

    Mantém 4 primeiros chars do segredo para correlação e substitui o
    restante por `***`.
    """
    if not text:
        return text
    out = _CVAPI_RE.sub(
        lambda m: f"{m.group(0)[:len('CVAPI_') + PREVIEW_PREFIX_LEN]}***",
        text,
    )
    out = _API_KEY_ASSIGN_RE.sub(_preview_then_mask, out)
    out = _API_KEY_HEADER_RE.sub(_preview_then_mask, out)
    return out


# ─────────────────────────────────────────────────────────────
# Bearer tokens — header Authorization e variantes
# ─────────────────────────────────────────────────────────────
_BEARER_RE = re.compile(
    r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]{8,400})"
)


def mask_bearer_token(text: str) -> str:
    """Mascara `Bearer <token>` em `Authorization` e afins.

    Aceita tokens JWT / opacos / longos. Preserva prefixo (4 chars do
    header + 4 chars do token) para correlação.
    """
    if not text:
        return text
    return _BEARER_RE.sub(_preview_then_mask, text)


# ─────────────────────────────────────────────────────────────
# Cookies de sessão — formato `name=value` em header Cookie
# ─────────────────────────────────────────────────────────────
_SESSION_COOKIE_NAMES = (
    "session", "sessionid", "session_id", "sid",
    "csrftoken", "xsrf-token", "auth",
)
_COOKIE_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(_SESSION_COOKIE_NAMES) + r")\s*=\s*([^;\s]{4,})"
)


def mask_session_cookie(text: str) -> str:
    """Mascara cookies de sessão reconhecidos em strings tipo header
    `Cookie:` (`session=abc; csrftoken=xyz`). Outros cookies ficam
    intactos para facilitar diagnóstico.
    """
    if not text:
        return text

    def _repl(m: re.Match) -> str:
        name = m.group(1)
        value = m.group(2) or ""
        if len(value) <= PREVIEW_PREFIX_LEN:
            return f"{name}={MASK}"
        return f"{name}={value[:PREVIEW_PREFIX_LEN]}***"

    return _COOKIE_PATTERN.sub(_repl, text)


# ─────────────────────────────────────────────────────────────
# Caminhos de perfil Chromium — revelam usuário/host
# ─────────────────────────────────────────────────────────────
# Em Windows: C:\Users\<user>\... ; em POSIX: /home/<user>/... ou /Users/<user>/...
_PROFILE_PATH_WIN = re.compile(
    r"(?i)\b([A-Z]):\\\\Users\\\\([^\\\\/\s\"']+)"
)
_PROFILE_PATH_WIN_SINGLE = re.compile(
    r"(?i)\b([A-Z]):\\Users\\([^\\\/\s\"']+)"
)
_PROFILE_PATH_POSIX = re.compile(
    r"(?<![A-Za-z0-9_])(/(?:home|Users)/)([^/\s\"']+)"
)


def mask_file_path(text: str) -> str:
    """Mascara segmentos identificadores de usuário em caminhos
    absolutos de perfis Chromium.

    - `C:\\Users\\john\\...` → `C:\\Users\\***\\...`
    - `/home/john/...`         → `/home/***/...`
    - `/Users/john/...`        → `/Users/***/...`

    Preserva o restante do caminho (ex.: `.../AppData/Local/.../Default`)
    para manter utilidade operacional.
    """
    if not text:
        return text
    out = _PROFILE_PATH_WIN.sub(r"\1:\\\\Users\\\\***", text)
    out = _PROFILE_PATH_WIN_SINGLE.sub(r"\1:\\Users\\***", out)
    out = _PROFILE_PATH_POSIX.sub(r"\1***", out)
    return out


# ─────────────────────────────────────────────────────────────
# Combina todas as máscaras em um único passe
# ─────────────────────────────────────────────────────────────
_STAGES = (
    mask_api_key,
    mask_bearer_token,
    mask_session_cookie,
    mask_file_path,
)


def sanitize(text: str) -> str:
    """Aplica todas as máscaras na ordem de confiabilidade (mais
    específica primeiro). Função idempotente: `sanitize(sanitize(x)) == sanitize(x)`.
    """
    if not text:
        return text
    out = text
    for stage in _STAGES:
        out = stage(out)
    return out


def sanitize_iter(values: Iterable[str]) -> list[str]:
    """Aplica `sanitize` sobre cada elemento de um iterável."""
    return [sanitize(v) for v in values]


def sanitize_mapping(mapping: dict) -> dict:
    """Aplica `sanitize` em todos os valores string (ou de dict aninhado)
    de um `dict`, preservando chaves. Valores não-string passam sem
    alteração. Útil para sanitizar payloads de audit event.
    """
    if not isinstance(mapping, dict):
        return mapping
    out: dict = {}
    for k, v in mapping.items():
        if isinstance(v, str):
            out[k] = sanitize(v)
        elif isinstance(v, dict):
            out[k] = sanitize_mapping(v)
        elif isinstance(v, (list, tuple)):
            out[k] = type(v)(
                sanitize(x) if isinstance(x, str)
                else sanitize_mapping(x) if isinstance(x, dict)
                else x
                for x in v
            )
        else:
            out[k] = v
    return out


__all__ = [
    "MASK",
    "PREVIEW_PREFIX_LEN",
    "mask_api_key",
    "mask_bearer_token",
    "mask_session_cookie",
    "mask_file_path",
    "sanitize",
    "sanitize_iter",
    "sanitize_mapping",
]


# Silencia warnings de lint para o único `os` que pode existir como
# referência futura em expansões (path separator platform-aware);
# hoje o módulo não lê variáveis de ambiente.
_ = os.sep
