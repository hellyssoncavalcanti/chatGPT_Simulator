import pytest

import error_catalog as ec


ALL_CODES = [
    ec.RATE_LIMIT,
    ec.QUEUE_TIMEOUT,
    ec.BROWSER_TIMEOUT,
    ec.SELECTOR_MISSING,
    ec.CONFIG_MISSING,
    ec.AUTH_FAILED,
    ec.UPSTREAM_UNAVAILABLE,
    ec.PAYLOAD_INVALID,
    ec.PROFILE_UNAVAILABLE,
    ec.IDEMPOTENCY_CONFLICT,
    ec.INTERNAL_ERROR,
]


# ─────────────────────────────────────────────────────────────
# Invariantes gerais do catálogo
# ─────────────────────────────────────────────────────────────
class TestCatalogInvariants:
    def test_all_codes_listed(self):
        listed = set(ec.all_codes())
        assert listed == set(ALL_CODES)
        assert len(listed) == len(ALL_CODES), "códigos duplicados"

    def test_codes_are_screaming_snake(self):
        for code in ec.all_codes():
            assert code == code.upper()
            assert " " not in code
            assert "-" not in code
            assert code.replace("_", "").isalnum()

    def test_messages_are_short_and_not_empty(self):
        for code in ec.all_codes():
            entry = ec.get(code)
            assert 1 <= len(entry.message) <= 80
            assert not entry.message.endswith(".")
            assert not entry.message.endswith("!")

    def test_actions_are_present_and_bounded(self):
        for code in ec.all_codes():
            entry = ec.get(code)
            assert 1 <= len(entry.action) <= 120

    def test_http_statuses_are_valid(self):
        for code in ec.all_codes():
            entry = ec.get(code)
            assert 400 <= entry.http_status <= 599

    def test_entries_are_frozen(self):
        entry = ec.get(ec.RATE_LIMIT)
        with pytest.raises(Exception):
            entry.message = "mutado"  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────
# get() e fallback seguro
# ─────────────────────────────────────────────────────────────
class TestGet:
    def test_known_code_returns_exact_entry(self):
        entry = ec.get(ec.RATE_LIMIT)
        assert entry.code == ec.RATE_LIMIT
        assert entry.http_status == 429

    def test_unknown_code_falls_back_to_internal_error(self):
        entry = ec.get("DOES_NOT_EXIST")
        assert entry.code == ec.INTERNAL_ERROR

    def test_empty_and_none_fall_back(self):
        assert ec.get("").code == ec.INTERNAL_ERROR
        assert ec.get(None).code == ec.INTERNAL_ERROR  # type: ignore[arg-type]

    def test_case_and_whitespace_insensitive(self):
        assert ec.get("rate_limit").code == ec.RATE_LIMIT
        assert ec.get("  Rate_Limit  ").code == ec.RATE_LIMIT


# ─────────────────────────────────────────────────────────────
# to_dict() e override de campos dinâmicos
# ─────────────────────────────────────────────────────────────
class TestToDict:
    def test_contains_all_base_fields(self):
        payload = ec.to_dict(ec.RATE_LIMIT)
        assert set(payload.keys()) == {"code", "http_status", "message", "action"}

    def test_override_adds_dynamic_fields(self):
        payload = ec.to_dict(ec.RATE_LIMIT, retry_after_seconds=240, detail="cooldown")
        assert payload["retry_after_seconds"] == 240
        assert payload["detail"] == "cooldown"
        assert payload["code"] == ec.RATE_LIMIT

    def test_override_ignores_none(self):
        payload = ec.to_dict(ec.RATE_LIMIT, retry_after_seconds=None)
        assert "retry_after_seconds" not in payload

    def test_unknown_code_still_serializes(self):
        payload = ec.to_dict("__UNKNOWN__")
        assert payload["code"] == ec.INTERNAL_ERROR


# ─────────────────────────────────────────────────────────────
# Classificação heurística (mínimo ≥3 casos por código)
# ─────────────────────────────────────────────────────────────
class TestClassifyRateLimit:
    def test_portuguese_excesso(self):
        assert ec.classify_from_text("ChatGPT: excesso de solicitações, tente depois") == ec.RATE_LIMIT

    def test_portuguese_chegou_ao_limite(self):
        assert ec.classify_from_text("Você chegou ao limite de mensagens") == ec.RATE_LIMIT

    def test_english_too_many_requests(self):
        assert ec.classify_from_text("HTTP 429: too many requests") == ec.RATE_LIMIT

    def test_english_rate_limit_dash(self):
        assert ec.classify_from_text("Rate-Limit detected by browser") == ec.RATE_LIMIT


class TestClassifyBrowserTimeout:
    def test_playwright_goto_timeout(self):
        assert ec.classify_from_text("page.goto: Timeout 30000ms exceeded") == ec.BROWSER_TIMEOUT

    def test_click_timeout(self):
        assert ec.classify_from_text("locator.click: Timeout while waiting") == ec.BROWSER_TIMEOUT

    def test_portuguese_timeout_no_navegador(self):
        assert ec.classify_from_text("Timeout no navegador ao enviar prompt") == ec.BROWSER_TIMEOUT


class TestClassifyQueueTimeout:
    def test_english_queue_timeout(self):
        assert ec.classify_from_text("Queue timeout after 120s") == ec.QUEUE_TIMEOUT

    def test_portuguese_timeout_de_fila(self):
        assert ec.classify_from_text("Timeout de fila (90s) aguardando slot") == ec.QUEUE_TIMEOUT

    def test_timeout_aguardando_slot(self):
        assert ec.classify_from_text("Timeout aguardando slot interno") == ec.QUEUE_TIMEOUT


class TestClassifySelectorMissing:
    def test_playwright_selector_not_found(self):
        assert ec.classify_from_text("Error: selector not found") == ec.SELECTOR_MISSING

    def test_portuguese_seletor_nao_encontrado(self):
        assert ec.classify_from_text("Seletor não encontrado no DOM") == ec.SELECTOR_MISSING

    def test_no_element_matches(self):
        assert ec.classify_from_text("no element matches selector 'button.send'") == ec.SELECTOR_MISSING


class TestClassifyAuthFailed:
    def test_http_401(self):
        assert ec.classify_from_text("401 Unauthorized: token inválido") == ec.AUTH_FAILED

    def test_invalid_api_key(self):
        assert ec.classify_from_text("Invalid API key supplied") == ec.AUTH_FAILED

    def test_portuguese_sessao_expirada(self):
        assert ec.classify_from_text("Sessão expirada, refaça o login") == ec.AUTH_FAILED


class TestClassifyConfigMissing:
    def test_english_config_missing(self):
        assert ec.classify_from_text("config missing: CHROMIUM_PROFILES") == ec.CONFIG_MISSING

    def test_portuguese_configuracao_ausente(self):
        assert ec.classify_from_text("Configuração ausente para API_KEY") == ec.CONFIG_MISSING

    def test_attribute_error_on_config(self):
        assert ec.classify_from_text(
            "AttributeError: module 'config' has no attribute 'X'"
        ) == ec.CONFIG_MISSING


class TestClassifyUpstream:
    def test_http_503(self):
        assert ec.classify_from_text("503 Service Unavailable") == ec.UPSTREAM_UNAVAILABLE

    def test_http_502(self):
        assert ec.classify_from_text("502 Bad Gateway (nginx)") == ec.UPSTREAM_UNAVAILABLE

    def test_connection_refused(self):
        assert ec.classify_from_text("connection refused by 127.0.0.1:3003") == ec.UPSTREAM_UNAVAILABLE


class TestClassifyProfileUnavailable:
    def test_english_profile_not_found(self):
        assert ec.classify_from_text("chromium profile not found: segunda_chance") == ec.PROFILE_UNAVAILABLE

    def test_portuguese_perfil_indisponivel(self):
        assert ec.classify_from_text("Perfil Chromium indisponível") == ec.PROFILE_UNAVAILABLE

    def test_non_matching_is_not_profile(self):
        assert ec.classify_from_text("perfil carregado com sucesso") == ec.INTERNAL_ERROR


class TestClassifyIdempotency:
    def test_english_conflict(self):
        assert ec.classify_from_text("idempotency conflict detected") == ec.IDEMPOTENCY_CONFLICT

    def test_portuguese_chave_idempotente(self):
        assert ec.classify_from_text("Chave idempotente já usada") == ec.IDEMPOTENCY_CONFLICT

    def test_unrelated_text_does_not_match(self):
        assert ec.classify_from_text("tudo ok") == ec.INTERNAL_ERROR


class TestClassifyPayloadInvalid:
    def test_english_invalid_payload(self):
        assert ec.classify_from_text("invalid payload: missing 'messages'") == ec.PAYLOAD_INVALID

    def test_portuguese_payload_invalido(self):
        assert ec.classify_from_text("Payload inválido recebido") == ec.PAYLOAD_INVALID

    def test_schema_validation_failed(self):
        assert ec.classify_from_text("schema validation failed: field X") == ec.PAYLOAD_INVALID


# ─────────────────────────────────────────────────────────────
# Fallback e robustez de classify_from_text
# ─────────────────────────────────────────────────────────────
class TestClassifyFallback:
    def test_empty_text_returns_default(self):
        assert ec.classify_from_text("") == ec.INTERNAL_ERROR
        assert ec.classify_from_text(None) == ec.INTERNAL_ERROR  # type: ignore[arg-type]

    def test_custom_default(self):
        assert ec.classify_from_text("", default=ec.PAYLOAD_INVALID) == ec.PAYLOAD_INVALID

    def test_rate_limit_wins_over_timeout_when_both_present(self):
        # Ordem de declaração em _PATTERNS coloca RATE_LIMIT antes;
        # trava a prioridade que o chamador histórico assume.
        text = "Rate limit after browser timeout"
        assert ec.classify_from_text(text) == ec.RATE_LIMIT

    def test_classify_many_preserves_order(self):
        inputs = ["rate limit", "401 unauthorized", "nada aqui"]
        assert ec.classify_many(inputs) == [
            ec.RATE_LIMIT,
            ec.AUTH_FAILED,
            ec.INTERNAL_ERROR,
        ]


# ─────────────────────────────────────────────────────────────
# Requisitos explícitos do prompt (códigos listados devem existir)
# ─────────────────────────────────────────────────────────────
class TestRequiredCodesPresent:
    @pytest.mark.parametrize("code", [
        "RATE_LIMIT",
        "QUEUE_TIMEOUT",
        "BROWSER_TIMEOUT",
        "SELECTOR_MISSING",
        "CONFIG_MISSING",
        "AUTH_FAILED",
        "UPSTREAM_UNAVAILABLE",
    ])
    def test_required_code_is_registered(self, code):
        entry = ec.get(code)
        assert entry.code == code


# ─────────────────────────────────────────────────────────────
# `format_reason`: helper usado por server._register_chat_rate_limit
# para normalizar a string livre em `Motivo: ...` do log.
# ─────────────────────────────────────────────────────────────
class TestFormatReason:
    def test_empty_string_returns_empty(self):
        assert ec.format_reason("") == ""

    def test_none_returns_empty(self):
        assert ec.format_reason(None) == ""  # type: ignore[arg-type]

    def test_only_whitespace_returns_empty(self):
        assert ec.format_reason("   \t\n ") == ""

    def test_rate_limit_pt_br_gets_tag(self):
        out = ec.format_reason("excesso de solicitações, tente depois")
        assert out == "[RATE_LIMIT] excesso de solicitações, tente depois"

    def test_rate_limit_en_gets_tag(self):
        out = ec.format_reason("HTTP 429 - too many requests")
        assert out == "[RATE_LIMIT] HTTP 429 - too many requests"

    def test_browser_timeout_gets_tag(self):
        out = ec.format_reason("page.goto: Timeout ao carregar")
        assert out.startswith("[BROWSER_TIMEOUT] ")

    def test_unclassifiable_returns_stripped_original(self):
        # Não deve prefixar com [INTERNAL_ERROR] para evitar ruído em logs.
        out = ec.format_reason("  erro genérico de rede qualquer  ")
        assert out == "erro genérico de rede qualquer"

    def test_idempotent_on_already_prefixed(self):
        original = "[RATE_LIMIT] excesso de solicitações, tente depois"
        assert ec.format_reason(original) == original

    def test_does_not_double_prefix_even_in_two_passes(self):
        once = ec.format_reason("excesso de solicitações, tente depois")
        twice = ec.format_reason(once)
        assert once == twice
