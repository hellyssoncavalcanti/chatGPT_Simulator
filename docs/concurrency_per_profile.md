# Concurrency por `browser_profile` (design proposal)

> Status: **planejamento (sem código ainda)**.
> Objetivo: criar um limite de concorrência por perfil Chromium para preservar
> sessão humana (anti-robotização) sem quebrar o contrato atual de fila.

## Problema atual

Hoje o servidor já controla fairness global e alguns throttles (chat, web search,
requests Python), mas ainda não existe um limitador explícito por
`browser_profile` no caminho quente do browser worker.

Risco operacional:
- múltiplas tasks concorrentes podem disputar o mesmo perfil e gerar padrões
  artificiais de interação (cliques/typing overlap);
- maior chance de rate-limit/bloqueios intermitentes do ChatGPT UI;
- ruído de diagnóstico (falhas parecem aleatórias quando na prática são contenção).

## Requisitos (não-regredir)

1. Preservar comportamento atual para `browser_profile` ausente (`default`).
2. Não alterar contrato HTTP/SSE existente (`/v1/chat/completions`, `/api/sync`, buscas).
3. Manter `sync_github` desacoplado.
4. Não reduzir observabilidade atual (metrics/logs já existentes).

## Estratégia proposta (incremental)

### Fase 1 — Estado puro (padrão B)

Extrair um módulo puro, por exemplo `Scripts/profile_concurrency.py`, com:
- classe `ProfileConcurrencyLimiter`;
- lock interno + contadores por perfil;
- `now_func` injetável para testes;
- API mínima:
  - `try_acquire(profile: str, limit: int) -> (acquired: bool, in_use: int)`
  - `release(profile: str) -> None`
  - `snapshot() -> dict[str, int]`.

Sem Flask/Playwright/config import no módulo.

### Fase 2 — Wrapper fino em `server.py`

- Instanciar singleton (`_PROFILE_CONCURRENCY = ProfileConcurrencyLimiter(...)`).
- Integrar apenas no ponto de despacho de task para browser (sem tocar no loop
  async do `browser.py` nesta fase).
- Em caso de não aquisição imediata:
  - enfileirar/aguardar com mensagens SSE de status (mesmo padrão já usado);
  - timeout reaproveitando contrato de fila já existente.

### Fase 3 — Observabilidade

Expor em `/api/metrics` e `/metrics`:
- sessões ativas por profile;
- recusas/esperas por profile;
- tempo médio de espera por profile.

## Política de limites (proposta inicial)

- `default`: limite 1 (mais conservador).
- perfis dedicados técnicos (ex.: `segunda_chance`): limite 1 inicialmente.
- expansão para limite >1 apenas após medir estabilidade por 7 dias.

## Failure modes e mitigação

1. **Leak de slot** por exceção no caminho de execução.
   - Mitigação: `try/finally` obrigatório no wrapper que adquire/release.
2. **Starvation** entre perfis.
   - Mitigação: fairness por fila + timeout explícito com feedback SSE.
3. **Config inválida de limite** (0 ou negativo).
   - Mitigação: clamp para mínimo 1 e log de warning sanitizado.

## Plano de testes (offline-first)

Criar `tests/test_profile_concurrency.py` com cenários:
- acquire/release básico;
- limite respeitado com concorrência;
- release idempotente;
- snapshot consistente sob múltiplas threads;
- clamp de limites inválidos.

## Critério de pronto (DoD)

- módulo puro + testes offline (`>= 15` casos);
- wrappers finos preservando contratos existentes;
- sem alteração no formato de payload de erro/status já consumido pelos clientes;
- atualização do `REFACTOR_PROGRESS.md` + README com comando da suite.

## Não escopo desta etapa

- alterações no `browser.py` async/Playwright;
- mudança de semântica de retries/DLQ;
- qualquer feature nova fora de concorrência por profile.
