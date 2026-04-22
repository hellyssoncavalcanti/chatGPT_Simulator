"""Testa a decisão de rate-limit em analisador_prontuarios._resposta_eh_rate_limit.

`analisador_prontuarios.py` não importa offline (depende de `requests`,
`logging` em cadeia com `config`, setup de logs em disco). Aqui
replicamos a lógica pura (superset via `error_catalog.classify_from_text`
com fallback à lista histórica) e travamos o contrato booleano para
evitar regressão na integração commitada em Scripts/analisador_prontuarios.py.
"""

import error_catalog as ec


_HISTORIC_PATTERNS = [
    "chegou ao limite",
    "excesso de solicitações",
    "tente novamente mais tarde",
    "rate limit",
    "too many requests",
]


def _resposta_eh_rate_limit(texto: str) -> bool:
    """Cópia da lógica integrada. Se este teste quebrar, o comportamento
    em produção também quebrou — tratar como regressão contratual.
    """
    if not texto:
        return False
    if ec is not None:
        return ec.classify_from_text(texto) == ec.RATE_LIMIT
    texto_lower = texto.lower()
    return any(p in texto_lower for p in _HISTORIC_PATTERNS)


class TestHistoricPatternsStillWork:
    """Todos os padrões do array histórico continuam detectados."""

    def test_chegou_ao_limite(self):
        assert _resposta_eh_rate_limit("Você chegou ao limite de mensagens")

    def test_excesso_de_solicitacoes(self):
        assert _resposta_eh_rate_limit("excesso de solicitações, tente depois")

    def test_tente_novamente_mais_tarde(self):
        assert _resposta_eh_rate_limit("Por favor, tente novamente mais tarde")

    def test_rate_limit_english(self):
        assert _resposta_eh_rate_limit("Rate limit reached")

    def test_too_many_requests_english(self):
        assert _resposta_eh_rate_limit("HTTP 429: too many requests")


class TestSupersetPatternsAdded:
    """Padrões EXTRAS cobertos pelo catálogo (rate-limit expandido)."""

    def test_hifenado_rate_limit(self):
        assert _resposta_eh_rate_limit("Rate-limit detected by proxy")

    def test_case_insensitive(self):
        assert _resposta_eh_rate_limit("RATE LIMIT")


class TestNonRateLimitText:
    def test_empty_string(self):
        assert not _resposta_eh_rate_limit("")

    def test_none(self):
        assert not _resposta_eh_rate_limit(None)

    def test_unrelated_error(self):
        assert not _resposta_eh_rate_limit("erro genérico de rede")

    def test_browser_timeout_is_not_rate_limit(self):
        # Importante: evitar falso positivo quando outras falhas também
        # podem aparecer no pipeline.
        assert not _resposta_eh_rate_limit("page.goto: timeout 30000ms")

    def test_auth_failure_not_rate_limit(self):
        assert not _resposta_eh_rate_limit("401 Unauthorized: token inválido")
