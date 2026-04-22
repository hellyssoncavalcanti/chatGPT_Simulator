"""Testa a lógica de `_extract_rate_limit_details` via seu substituto puro.

`server._extract_rate_limit_details` não pode ser importado em ambiente
offline (puxa Flask). Como parte do Lote P0 passo 5, a decisão de
rate-limit foi delegada a `error_catalog.classify_from_text`. Aqui
reimplementamos o wrapper com a MESMA ordem de resolução e garantimos
que o contrato (tupla, detecção PT-BR/EN, retry_after) segue estável.

Se este teste quebrar, quebrou também em `server.py` — tratar como
regressão contratual.
"""

from __future__ import annotations

import error_catalog as ec


def _extract_rate_limit_details_offline(error_payload):
    """Cópia exata da lógica em server._extract_rate_limit_details
    (mesmos passos, mesmo fallback heurístico). Existe apenas para
    permitir teste offline sem Flask."""
    code = ""
    message = ""
    retry_after = None

    if isinstance(error_payload, dict):
        code = str(error_payload.get("code") or "").strip().lower()
        message = str(
            error_payload.get("message") or error_payload.get("error") or ""
        ).strip()
        try:
            retry_after_raw = error_payload.get("retry_after_seconds")
            if retry_after_raw is not None:
                retry_after = max(1, int(float(retry_after_raw)))
        except Exception:
            retry_after = None
    else:
        message = str(error_payload or "").strip()

    is_rate_limited = (
        code in {"rate_limit", "too_many_requests"}
        or ec.classify_from_text(f"{code} {message}") == ec.RATE_LIMIT
    )
    return is_rate_limited, message, retry_after


class TestExplicitCodePath:
    def test_code_rate_limit_triggers(self):
        is_rl, msg, ra = _extract_rate_limit_details_offline(
            {"code": "rate_limit", "message": "x"}
        )
        assert is_rl is True
        assert msg == "x"
        assert ra is None

    def test_code_too_many_requests_triggers(self):
        is_rl, _, _ = _extract_rate_limit_details_offline(
            {"code": "too_many_requests"}
        )
        assert is_rl is True

    def test_code_is_case_insensitive(self):
        # `code` é lowercased antes do match.
        is_rl, _, _ = _extract_rate_limit_details_offline(
            {"code": "RATE_LIMIT"}
        )
        assert is_rl is True


class TestHeuristicPath:
    def test_portuguese_excesso(self):
        is_rl, msg, _ = _extract_rate_limit_details_offline(
            {"message": "excesso de solicitações, tente depois"}
        )
        assert is_rl is True
        assert "excesso" in msg.lower()

    def test_portuguese_chegou_ao_limite(self):
        is_rl, _, _ = _extract_rate_limit_details_offline(
            {"message": "Você chegou ao limite de mensagens"}
        )
        assert is_rl is True

    def test_english_too_many_requests_in_message(self):
        is_rl, _, _ = _extract_rate_limit_details_offline(
            {"message": "HTTP 429 - too many requests"}
        )
        assert is_rl is True

    def test_english_rate_limit_phrase(self):
        is_rl, _, _ = _extract_rate_limit_details_offline(
            "Rate limit reached"
        )
        assert is_rl is True


class TestNonRateLimitStillReturnsTuple:
    def test_unrelated_message(self):
        is_rl, msg, ra = _extract_rate_limit_details_offline(
            {"message": "erro de rede genérico"}
        )
        assert is_rl is False
        assert msg == "erro de rede genérico"
        assert ra is None

    def test_empty_payload(self):
        is_rl, msg, ra = _extract_rate_limit_details_offline({})
        assert (is_rl, msg, ra) == (False, "", None)

    def test_none_payload(self):
        is_rl, msg, ra = _extract_rate_limit_details_offline(None)
        assert (is_rl, msg, ra) == (False, "", None)


class TestRetryAfterParsing:
    def test_int_preserved(self):
        _, _, ra = _extract_rate_limit_details_offline(
            {"code": "rate_limit", "retry_after_seconds": 240}
        )
        assert ra == 240

    def test_float_is_truncated(self):
        _, _, ra = _extract_rate_limit_details_offline(
            {"code": "rate_limit", "retry_after_seconds": 65.9}
        )
        assert ra == 65  # int(float(65.9)) = 65

    def test_invalid_retry_after_falls_back_to_none(self):
        _, _, ra = _extract_rate_limit_details_offline(
            {"code": "rate_limit", "retry_after_seconds": "nope"}
        )
        assert ra is None

    def test_clamped_to_at_least_one(self):
        _, _, ra = _extract_rate_limit_details_offline(
            {"code": "rate_limit", "retry_after_seconds": 0}
        )
        assert ra == 1


class TestMessageFallbackToErrorKey:
    def test_error_key_used_when_message_missing(self):
        _, msg, _ = _extract_rate_limit_details_offline(
            {"error": "excesso de solicitações"}
        )
        assert "excesso" in msg.lower()

    def test_message_takes_priority_over_error(self):
        _, msg, _ = _extract_rate_limit_details_offline(
            {"message": "alpha", "error": "beta"}
        )
        assert msg == "alpha"


class TestStringPayload:
    def test_plain_string_accepted(self):
        is_rl, msg, ra = _extract_rate_limit_details_offline(
            "rate limit atingido"
        )
        assert is_rl is True
        assert msg == "rate limit atingido"
        assert ra is None

    def test_empty_string(self):
        is_rl, msg, _ = _extract_rate_limit_details_offline("")
        assert is_rl is False
        assert msg == ""
