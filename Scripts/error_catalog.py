"""Catálogo central de códigos de erro operacional do ChatGPT Simulator.

Objetivo: padronizar a identificação, mensagem curta e ação recomendada
dos erros recorrentes emitidos por `server.py`, `browser.py` e pelos
scripts Python auxiliares. Substituir strings livres (difíceis de
observar e testar) por códigos estáveis.

Este módulo é **puro**:
  - sem Flask / Werkzeug;
  - sem Playwright;
  - sem dependência de `config` (para poder ser importado em testes
    offline sem setup adicional).

Integração com os chamadores (server.py/browser.py) é deliberadamente
adiada — ver passo 5 do Lote P0 em `REFACTOR_PROGRESS.md`.

Princípios de design:
  1. Códigos em SCREAMING_SNAKE_CASE, estáveis (quebrar nomes é
     breaking change para dashboards / logs).
  2. Mensagem curta (≤80 chars, sem pontuação final) em PT-BR, para
     alinhar com logs operacionais existentes.
  3. Ação recomendada curta (≤120 chars) orientada ao operador.
  4. `http_status` é sugestão padrão; chamador pode override sem
     mudar o código semântico.
  5. `classify_from_text` cobre os mesmos padrões de string que
     `_extract_rate_limit_details` já faz ad-hoc, preservando
     compatibilidade quando a integração ocorrer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


# ─────────────────────────────────────────────────────────────
# Códigos estáveis (use-os como constantes, não literais mágicos)
# ─────────────────────────────────────────────────────────────
RATE_LIMIT = "RATE_LIMIT"
QUEUE_TIMEOUT = "QUEUE_TIMEOUT"
BROWSER_TIMEOUT = "BROWSER_TIMEOUT"
SELECTOR_MISSING = "SELECTOR_MISSING"
CONFIG_MISSING = "CONFIG_MISSING"
AUTH_FAILED = "AUTH_FAILED"
UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
PAYLOAD_INVALID = "PAYLOAD_INVALID"
PROFILE_UNAVAILABLE = "PROFILE_UNAVAILABLE"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True)
class ErrorEntry:
    """Descreve um código do catálogo de forma imutável."""

    code: str
    http_status: int
    message: str
    action: str

    def to_dict(self, **override) -> dict:
        """Retorna dict JSON-friendly; `override` permite acrescentar
        campos dinâmicos (ex.: `retry_after_seconds`, `detail`) sem
        alterar a entrada estática do catálogo.
        """
        payload = {
            "code": self.code,
            "http_status": self.http_status,
            "message": self.message,
            "action": self.action,
        }
        for key, value in override.items():
            if value is not None:
                payload[key] = value
        return payload


_CATALOG: dict[str, ErrorEntry] = {
    RATE_LIMIT: ErrorEntry(
        code=RATE_LIMIT,
        http_status=429,
        message="Rate limit do ChatGPT atingido",
        action="Aguardar cooldown sugerido e reduzir concorrência por perfil",
    ),
    QUEUE_TIMEOUT: ErrorEntry(
        code=QUEUE_TIMEOUT,
        http_status=504,
        message="Timeout aguardando slot na fila interna",
        action="Reduzir carga, aumentar PYTHON_CHAT_QUEUE_TIMEOUT_SEC ou tentar novamente",
    ),
    BROWSER_TIMEOUT: ErrorEntry(
        code=BROWSER_TIMEOUT,
        http_status=504,
        message="Timeout durante ação no navegador",
        action="Verificar saúde do perfil Chromium e reabrir sessão se necessário",
    ),
    SELECTOR_MISSING: ErrorEntry(
        code=SELECTOR_MISSING,
        http_status=502,
        message="Seletor esperado não encontrado na UI do ChatGPT",
        action="Atualizar Scripts/app_selectors.py e rodar smoke test de seletores",
    ),
    CONFIG_MISSING: ErrorEntry(
        code=CONFIG_MISSING,
        http_status=500,
        message="Configuração obrigatória ausente em config.py",
        action="Conferir config.example.py e preencher o valor faltante",
    ),
    AUTH_FAILED: ErrorEntry(
        code=AUTH_FAILED,
        http_status=401,
        message="Falha de autenticação",
        action="Verificar API key, cookie de sessão ou credenciais do usuário",
    ),
    UPSTREAM_UNAVAILABLE: ErrorEntry(
        code=UPSTREAM_UNAVAILABLE,
        http_status=503,
        message="Serviço upstream indisponível",
        action="Verificar ChatGPT UI, endpoints PHP ou web_search; aplicar circuit breaker",
    ),
    PAYLOAD_INVALID: ErrorEntry(
        code=PAYLOAD_INVALID,
        http_status=400,
        message="Payload inválido ou fora de esquema",
        action="Validar contrato de entrada do endpoint e registrar exemplo reproduzível",
    ),
    PROFILE_UNAVAILABLE: ErrorEntry(
        code=PROFILE_UNAVAILABLE,
        http_status=503,
        message="Perfil Chromium solicitado indisponível",
        action="Conferir config.CHROMIUM_PROFILES; fallback automático para 'default'",
    ),
    IDEMPOTENCY_CONFLICT: ErrorEntry(
        code=IDEMPOTENCY_CONFLICT,
        http_status=409,
        message="Requisição idempotente já processada ou em execução",
        action="Reutilizar resposta anterior ou gerar nova chave idempotente",
    ),
    INTERNAL_ERROR: ErrorEntry(
        code=INTERNAL_ERROR,
        http_status=500,
        message="Erro interno não classificado",
        action="Ver trace no log; abrir issue se persistir",
    ),
}


def all_codes() -> tuple[str, ...]:
    """Lista estável de códigos conhecidos (útil para testes e docs)."""
    return tuple(_CATALOG.keys())


def get(code: str) -> ErrorEntry:
    """Retorna a entrada do catálogo; desconhecidos caem em INTERNAL_ERROR.

    Nunca levanta — o catálogo é caminho quente de logging/observabilidade
    e não pode ser fonte de erro secundário.
    """
    key = (code or "").strip().upper()
    entry = _CATALOG.get(key)
    if entry is None:
        return _CATALOG[INTERNAL_ERROR]
    return entry


def to_dict(code: str, **override) -> dict:
    """Atalho `get(code).to_dict(**override)`."""
    return get(code).to_dict(**override)


# ─────────────────────────────────────────────────────────────
# Classificação heurística de mensagens livres
# ─────────────────────────────────────────────────────────────
# Mantém compatibilidade com o string-match histórico de
# `_extract_rate_limit_details` e `_resposta_eh_rate_limit` para que
# a integração futura possa ser drop-in.

_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (RATE_LIMIT, (
        "rate limit",
        "rate-limit",
        "too many request",
        "excesso de solicita",
        "chegou ao limite",
        "tente novamente mais tarde",
    )),
    (BROWSER_TIMEOUT, (
        "browser timeout",
        "page.goto: timeout",
        "locator.click: timeout",
        "timeout no navegador",
    )),
    (QUEUE_TIMEOUT, (
        "queue timeout",
        "timeout de fila",
        "timeout aguardando slot",
    )),
    (SELECTOR_MISSING, (
        "selector not found",
        "seletor não encontrado",
        "no element matches selector",
    )),
    (AUTH_FAILED, (
        "401 unauthorized",
        "invalid api key",
        "api key inválida",
        "sessão expirada",
        "session expired",
    )),
    (CONFIG_MISSING, (
        "config missing",
        "configuração ausente",
        "attributeerror: module 'config'",
    )),
    (UPSTREAM_UNAVAILABLE, (
        "503 service unavailable",
        "502 bad gateway",
        "connection refused",
        "serviço indisponível",
    )),
    (PROFILE_UNAVAILABLE, (
        "chromium profile not found",
        "perfil chromium indisponível",
    )),
    (IDEMPOTENCY_CONFLICT, (
        "idempotency conflict",
        "chave idempotente",
    )),
    (PAYLOAD_INVALID, (
        "invalid payload",
        "payload inválido",
        "schema validation failed",
    )),
)


def classify_from_text(text: str, *, default: str = INTERNAL_ERROR) -> str:
    """Mapeia texto livre para um código do catálogo.

    Busca por substring case-insensitive; mais específico vence por
    ordem de declaração em `_PATTERNS` (Rate limit antes de outros).
    Retorna `default` se nada casar.
    """
    haystack = (text or "").lower()
    if not haystack:
        return default
    for code, patterns in _PATTERNS:
        for needle in patterns:
            if needle in haystack:
                return code
    return default


def classify_many(texts: Iterable[str]) -> list[str]:
    """Classifica uma lista de mensagens; conveniência para triagem em lote."""
    return [classify_from_text(t) for t in texts]


def format_reason(reason: str) -> str:
    """Normaliza uma string livre de "motivo" prefixando-a com o código do
    catálogo quando classificável.

    Contrato estável (usado por `server._register_chat_rate_limit` e futuros
    caminhos de logging):
      - `reason` vazio/`None`/apenas-whitespace → retorna `""`.
      - Classificável em algum código ≠ `INTERNAL_ERROR` → retorna
        `"[<CODE>] <reason_stripped>"` (tag style consistente com o resto
        dos logs do projeto, ex.: `[CHAT_RATE_LIMIT]`, `[SECURITY_AUDIT]`).
      - Apenas INTERNAL_ERROR (nada casou) → retorna o texto stripped sem
        prefixo, evitando ruído `[INTERNAL_ERROR]` em logs operacionais.

    Idempotente: chamar duas vezes não duplica o prefixo, pois o próprio
    texto prefixado continua classificando para o mesmo código — mas a
    regex de deteção do prefixo é checada antes para ser explícita.
    """
    normalized = (reason or "").strip()
    if not normalized:
        return ""
    # Idempotência: se já vier prefixado com `[CODE] `, respeita.
    if normalized.startswith("[") and "] " in normalized:
        candidate = normalized[1 : normalized.index("] ")].strip()
        if candidate and candidate == candidate.upper() and candidate in _CATALOG:
            return normalized
    code = classify_from_text(normalized)
    if code == INTERNAL_ERROR:
        return normalized
    return f"[{code}] {normalized}"
