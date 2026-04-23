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


# ─────────────────────────────────────────────────────────────
# Contrato do wrapper server._register_chat_rate_limit
# ─────────────────────────────────────────────────────────────
# server.py não importa em ambiente offline (puxa Flask). Reimplementamos
# o wrapper ponto-a-ponto usando o mesmo singleton `ChatRateLimitCooldown`
# e o mesmo helper `error_catalog.format_reason`, e checamos que a linha
# de log final sai no formato esperado.
#
# Se este teste quebrar, quebrou o contrato do log operacional — mudanças
# precisam atualizar dashboards/alertas que dependem do prefixo `[CODE]`.

from chat_rate_limit_cooldown import ChatRateLimitCooldown


def _fake_log_and_register(cooldown_state, retry_after, reason, log_sink):
    """Réplica exata de server._register_chat_rate_limit com o mesmo
    singleton (cooldown_state) e o mesmo sink de log (log_sink.append)."""
    adjusted = cooldown_state.register(retry_after, reason)
    normalized_reason = ec.format_reason(reason)
    if normalized_reason:
        log_sink.append(
            f"[CHAT_RATE_LIMIT] cooldown de {adjusted}s registrado. "
            f"Motivo: {normalized_reason}"
        )
    else:
        log_sink.append(
            f"[CHAT_RATE_LIMIT] cooldown de {adjusted}s registrado."
        )
    return adjusted


def _cooldown():
    clock = {"t": 1000.0}
    return (
        ChatRateLimitCooldown(
            default_cooldown_sec=240,
            max_cooldown_sec=1800,
            max_strikes=6,
            now_func=lambda: clock["t"],
        ),
        clock,
    )


class TestRegisterWrapperNormalizesReason:
    def test_rate_limit_reason_gets_rate_limit_tag(self):
        state, _ = _cooldown()
        logs = []
        _fake_log_and_register(
            state, None, "excesso de solicitações, tente depois", logs
        )
        assert len(logs) == 1
        line = logs[0]
        assert line.startswith("[CHAT_RATE_LIMIT] cooldown de 240s registrado.")
        assert "Motivo: [RATE_LIMIT] excesso de solicitações, tente depois" in line

    def test_english_rate_limit_reason_gets_tag(self):
        state, _ = _cooldown()
        logs = []
        _fake_log_and_register(state, 120, "Rate limit reached", logs)
        assert "Motivo: [RATE_LIMIT] Rate limit reached" in logs[0]
        # retry_after=120 com strikes=0 → cooldown = 120.
        assert "cooldown de 120s" in logs[0]

    def test_empty_reason_omits_motivo_suffix(self):
        state, _ = _cooldown()
        logs = []
        _fake_log_and_register(state, None, "", logs)
        assert logs[0] == "[CHAT_RATE_LIMIT] cooldown de 240s registrado."
        assert "Motivo:" not in logs[0]

    def test_unclassifiable_reason_kept_without_tag(self):
        state, _ = _cooldown()
        logs = []
        _fake_log_and_register(state, None, "erro genérico de rede qualquer", logs)
        # Fallback: texto original, sem prefixo `[INTERNAL_ERROR]`.
        assert "Motivo: erro genérico de rede qualquer" in logs[0]
        assert "[INTERNAL_ERROR]" not in logs[0]

    def test_consecutive_hits_still_normalize_and_grow_cooldown(self):
        state, clock = _cooldown()
        logs = []
        # Primeiro hit: strikes=0, cooldown=60.
        _fake_log_and_register(state, 60, "excesso de solicitações", logs)
        # Segundo hit dentro da janela: strikes=1, cooldown=120.
        clock["t"] += 5
        _fake_log_and_register(state, 60, "rate limit", logs)
        # Terceiro hit ainda dentro da janela: strikes=2, cooldown=240.
        clock["t"] += 5
        _fake_log_and_register(state, 60, "too many requests", logs)
        assert "cooldown de 60s" in logs[0]
        assert "cooldown de 120s" in logs[1]
        assert "cooldown de 240s" in logs[2]
        for line in logs:
            assert "Motivo: [RATE_LIMIT] " in line

    def test_whitespace_only_reason_behaves_like_empty(self):
        state, _ = _cooldown()
        logs = []
        _fake_log_and_register(state, None, "   \n", logs)
        assert logs[0] == "[CHAT_RATE_LIMIT] cooldown de 240s registrado."

    def test_pre_tagged_reason_is_idempotent(self):
        state, _ = _cooldown()
        logs = []
        _fake_log_and_register(
            state, None, "[RATE_LIMIT] excesso de solicitações", logs
        )
        # Não deve virar "[RATE_LIMIT] [RATE_LIMIT] excesso...".
        assert logs[0].count("[RATE_LIMIT]") == 1
        assert "Motivo: [RATE_LIMIT] excesso de solicitações" in logs[0]
