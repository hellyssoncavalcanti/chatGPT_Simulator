"""Validadores puros de payload para as rotas críticas da API.

Módulo sem dependências externas (sem Flask, Playwright nem config).
Cada função retorna ``(valid: bool, errors: list[str])``.

Limites exportados como constantes para uso em testes e documentação.
"""
from __future__ import annotations

import re
from typing import Any

# ── limites ─────────────────────────────────────────────────────────────
MAX_MESSAGE_CHARS: int = 100_000
MAX_CHAT_ID_LEN: int = 128
MAX_URL_LEN: int = 2_048
MAX_BROWSER_PROFILE_LEN: int = 64
MAX_ATTACHMENT_COUNT: int = 20
MAX_ATTACHMENT_NAME_LEN: int = 256
MAX_ATTACHMENT_B64_LEN: int = 20 * 1024 * 1024  # 20 MB base64
MAX_USERNAME_LEN: int = 80
MAX_PASSWORD_LEN: int = 256
MAX_SOURCE_HINT_LEN: int = 256
MAX_MESSAGES_COUNT: int = 500

# ── helpers internos ─────────────────────────────────────────────────────
_SAFE_PROFILE_RE = re.compile(r"^[\w\-]{1,64}$")  # [a-zA-Z0-9_-]
_URL_PREFIX = ("http://", "https://")


def _is_str_or_none(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _str_len_ok(value: Any, max_len: int) -> bool:
    return not isinstance(value, str) or len(value) <= max_len


# ── validadores públicos ─────────────────────────────────────────────────

def validate_login_request(data: dict) -> tuple[bool, list[str]]:
    """Valida os campos de ``POST /login``.

    Verifica presença e tamanho de ``username`` e ``password``.
    Não verifica credenciais (isso é responsabilidade de auth.py).
    """
    errors: list[str] = []

    username = data.get("username")
    if not isinstance(username, str) or not username.strip():
        errors.append("username é obrigatório e deve ser uma string não-vazia")
    elif len(username) > MAX_USERNAME_LEN:
        errors.append(f"username excede {MAX_USERNAME_LEN} caracteres")

    password = data.get("password")
    if not isinstance(password, str) or not password:
        errors.append("password é obrigatório e deve ser uma string")
    elif len(password) > MAX_PASSWORD_LEN:
        errors.append(f"password excede {MAX_PASSWORD_LEN} caracteres")

    return (len(errors) == 0, errors)


def validate_chat_request(data: dict) -> tuple[bool, list[str]]:
    """Valida os campos de ``POST /v1/chat/completions``.

    Verifica: ``message``, ``chat_id``, ``url``, ``browser_profile``,
    ``attachments``, ``stream``, ``messages``, ``source_hint``.
    Campos ausentes/None são permitidos (lógica de default fica em server.py).
    """
    errors: list[str] = []

    # message
    message = data.get("message")
    if message is not None and not isinstance(message, str):
        errors.append("message deve ser string ou ausente")
    elif isinstance(message, str) and len(message) > MAX_MESSAGE_CHARS:
        errors.append(f"message excede {MAX_MESSAGE_CHARS} caracteres")

    # messages (array OpenAI)
    messages = data.get("messages")
    if messages is not None:
        if not isinstance(messages, list):
            errors.append("messages deve ser array ou ausente")
        elif len(messages) > MAX_MESSAGES_COUNT:
            errors.append(f"messages excede {MAX_MESSAGES_COUNT} itens")

    # chat_id
    chat_id = data.get("chat_id")
    if not _is_str_or_none(chat_id):
        errors.append("chat_id deve ser string ou ausente")
    elif not _str_len_ok(chat_id, MAX_CHAT_ID_LEN):
        errors.append(f"chat_id excede {MAX_CHAT_ID_LEN} caracteres")

    # url
    url = data.get("url")
    if url is not None and url not in ("None", "none", ""):
        if not isinstance(url, str):
            errors.append("url deve ser string ou ausente")
        elif len(url) > MAX_URL_LEN:
            errors.append(f"url excede {MAX_URL_LEN} caracteres")
        elif not url.startswith(_URL_PREFIX):
            errors.append("url deve começar com http:// ou https://")

    # browser_profile
    bp = data.get("browser_profile")
    if bp is not None and bp != "":
        if not isinstance(bp, str):
            errors.append("browser_profile deve ser string ou ausente")
        elif len(bp) > MAX_BROWSER_PROFILE_LEN:
            errors.append(f"browser_profile excede {MAX_BROWSER_PROFILE_LEN} caracteres")
        elif not _SAFE_PROFILE_RE.match(bp):
            errors.append(
                "browser_profile contém caracteres inválidos "
                "(permitido: letras, dígitos, _ e -)"
            )

    # attachments
    attachments = data.get("attachments")
    if attachments is not None:
        if not isinstance(attachments, list):
            errors.append("attachments deve ser array ou ausente")
        elif len(attachments) > MAX_ATTACHMENT_COUNT:
            errors.append(f"attachments excede {MAX_ATTACHMENT_COUNT} itens")
        else:
            for i, att in enumerate(attachments):
                if not isinstance(att, dict):
                    errors.append(f"attachments[{i}] deve ser objeto")
                    continue
                name = att.get("name", "")
                if isinstance(name, str) and len(name) > MAX_ATTACHMENT_NAME_LEN:
                    errors.append(
                        f"attachments[{i}].name excede {MAX_ATTACHMENT_NAME_LEN} caracteres"
                    )
                content = att.get("content", "")
                if isinstance(content, str) and len(content) > MAX_ATTACHMENT_B64_LEN:
                    errors.append(
                        f"attachments[{i}].content excede limite de tamanho"
                    )

    # stream
    stream = data.get("stream")
    if stream is not None and not isinstance(stream, bool):
        errors.append("stream deve ser bool ou ausente")

    # source_hint
    sh = data.get("source_hint")
    if sh is not None and not isinstance(sh, str):
        errors.append("source_hint deve ser string ou ausente")
    elif isinstance(sh, str) and len(sh) > MAX_SOURCE_HINT_LEN:
        errors.append(f"source_hint excede {MAX_SOURCE_HINT_LEN} caracteres")

    return (len(errors) == 0, errors)


def validate_sync_request(data: dict) -> tuple[bool, list[str]]:
    """Valida os campos de ``POST /api/sync``.

    Verifica: ``url`` (obrigatório se ``chat_id`` ausente), ``chat_id``,
    ``browser_profile``, ``sync_browser_profile``.
    """
    errors: list[str] = []

    url = data.get("url")
    chat_id = data.get("chat_id")

    # url
    if url is not None and url not in ("None", "none", ""):
        if not isinstance(url, str):
            errors.append("url deve ser string ou ausente")
        elif len(url) > MAX_URL_LEN:
            errors.append(f"url excede {MAX_URL_LEN} caracteres")
        elif not url.startswith(_URL_PREFIX):
            errors.append("url deve começar com http:// ou https://")

    # chat_id
    if not _is_str_or_none(chat_id):
        errors.append("chat_id deve ser string ou ausente")
    elif not _str_len_ok(chat_id, MAX_CHAT_ID_LEN):
        errors.append(f"chat_id excede {MAX_CHAT_ID_LEN} caracteres")

    # presença mínima — ao menos url ou chat_id
    url_present = isinstance(url, str) and url.strip() and url not in ("None", "none")
    cid_present = isinstance(chat_id, str) and chat_id.strip()
    if not url_present and not cid_present:
        errors.append("url ou chat_id deve ser fornecido")

    # browser_profile / sync_browser_profile
    for field in ("browser_profile", "sync_browser_profile"):
        bp = data.get(field)
        if bp is not None and bp != "":
            if not isinstance(bp, str):
                errors.append(f"{field} deve ser string ou ausente")
            elif len(bp) > MAX_BROWSER_PROFILE_LEN:
                errors.append(f"{field} excede {MAX_BROWSER_PROFILE_LEN} caracteres")
            elif not _SAFE_PROFILE_RE.match(bp):
                errors.append(
                    f"{field} contém caracteres inválidos "
                    "(permitido: letras, dígitos, _ e -)"
                )

    return (len(errors) == 0, errors)


__all__ = [
    "MAX_MESSAGE_CHARS",
    "MAX_CHAT_ID_LEN",
    "MAX_URL_LEN",
    "MAX_BROWSER_PROFILE_LEN",
    "MAX_ATTACHMENT_COUNT",
    "MAX_ATTACHMENT_NAME_LEN",
    "MAX_ATTACHMENT_B64_LEN",
    "MAX_USERNAME_LEN",
    "MAX_PASSWORD_LEN",
    "MAX_SOURCE_HINT_LEN",
    "MAX_MESSAGES_COUNT",
    "validate_login_request",
    "validate_chat_request",
    "validate_sync_request",
]
