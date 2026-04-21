# Refactor Progress (ChatGPT Simulator)

## Status geral
- [x] Bootstrap seguro com templates (`config.example.py` + `sync_github_settings.example.ps1`) no `0. start.bat`.
- [x] Credencial padrão alinhada para `admin/admin` apenas em instalação nova.
- [x] Allowlist priorizando API key (fallback por origem/IP apenas defesa em profundidade).
- [x] Suporte a perfil Chromium por request (`browser_profile`) no fluxo `server.py -> browser.py`.
- [x] `analisador_prontuarios.py` envia `browser_profile` em todos os payloads LLM.
- [x] Migração de persistência JSON para SQLite (`Scripts/db.py`, `storage.py`, `auth.py`).
- [x] README atualizado para refletir bootstrap seguro, SQLite/sessões persistentes e perfil dedicado do analisador.
- [x] DLQ de fila (`/api/queue/failed`) + retry manual (`/api/queue/failed/retry`).
- [x] SSE de logs (`/api/logs/stream`) com heartbeat.
- [x] Prometheus em `/metrics` com fallback quando `prometheus_client` não está instalado.
- [x] Centralização base de seletores Playwright via `Scripts/app_selectors.py` + smoke test.
- [x] README reorganizado e docs complementares em `docs/`.
- [x] Simulação humana reforçada: digitação configurável com micro-hesitações e autocorreção (typo/backspace).

## Backlog priorizado de melhorias (foco em manter comportamento humano)

### P0 — Crítico (prioridade imediata)
1. **Modelo formal de “simulação humana”**: definir contrato mínimo (latência, jitter, pausas, backspace, scroll/mouse, limites de repetição) com flags de segurança para nunca cair em padrão robótico contínuo.
2. **Guardrails anti-bot no runtime**: watchdog de padrões mecânicos (intervalos constantes, ausência de variação, bursts longos) com auto-ajuste dinâmico do perfil humano.
3. **Retry/DLQ robusto por ID estável**: além do índice, suportar `failed_id`, retry em lote, purge com filtros e trilha de auditoria.
4. **Idempotência de requisições críticas** (`/v1/chat/completions`, sync e delete): chave idempotente opcional para evitar processamento duplicado em reconexão/replay.
5. **Circuit breaker para dependências externas** (ChatGPT UI, web_search, endpoints PHP): reduzir cascata de falhas e evitar loops agressivos quando serviço está degradado.
6. **Contrato de timeout por etapa** no browser (abrir aba, digitar, enviar, aguardar resposta, extrair output) com classificação de erro padronizada.
7. **Fila com fairness forte**: starvation prevention entre origens (`remote` vs `python`) e entre tenants com cotas por janela.
8. **Rate-limit unificado**: consolidar detecção no browser + aplicação no server + feedback claro para analisador/automações.
9. **Controle de concorrência por perfil Chromium**: limite de tarefas simultâneas por `browser_profile` para preservar sessão humana e estabilidade.
10. **Baseline de testes offline obrigatórios** no CI local (sem rede): smoke de fila, auth, storage e heurísticas humanas executáveis em ambiente restrito.

### P1 — Alta (ganho grande de confiabilidade/operação)
11. **Telemetry da simulação humana**: métricas por sessão (chars/s, pausas, correções, tempo até primeiro token) com agregação em `/api/metrics`.
12. **Prometheus expandido com labels úteis**: origem, prioridade, action, perfil, resultado (success/fail/rate_limit/timeout).
13. **SSE resiliente**: `Last-Event-ID`, heartbeats configuráveis e recuperação de desconexão para streams de log/eventos.
14. **Tracing com correlation-id end-to-end**: request HTTP → tarefa de fila → browser action → persistência.
15. **Catálogo central de erros** (codes + mensagens + ação recomendada), evitando strings livres difíceis de observar.
16. **Validação de payload por esquema** (ex.: pydantic/dataclass validators) para entradas críticas de API e tarefas enfileiradas.
17. **Sanitização de logs e privacidade**: mascarar segredos/PII em logs, métricas e mensagens de erro.
18. **Migrations versionadas com checksum** + comando de verificação de drift do banco (`db/app.db`).
19. **Hardening de sessões**: rotação de token, invalidar sessões antigas por usuário e trilha de sessão ativa.
20. **Benchmark de throughput/latência** da fila e do browser worker para definir SLOs realistas.

### P2 — Média (qualidade de código e manutenção)
21. **Modularização do `server.py`** por domínios (auth, chats, observabilidade, administração, busca).
22. **Modularização do `browser.py`** por “actions” (`chat`, `sync`, `search`, `menu`) para reduzir acoplamento.
23. **Camada de “selector health”**: score de confiabilidade por seletor + fallback ordenado + relatório automático.
24. **Feature flags estruturadas** para rollout gradual (human typing, screenshots, retries, métricas avançadas).
25. **Testes de contrato da API** (golden responses) para evitar regressões em frontend/integradores.
26. **Testes de caos controlado**: falhas intermitentes de browser/context/page para validar recuperação.
27. **Documentação operacional runbook** (incidentes comuns, playbooks de recuperação, checklist de release).
28. **README raiz enxuto** como índice + aprofundamento em `docs/` com arquitetura, whatsapp, analisador e sync.
29. **Política de versionamento semântico** com changelog técnico por release.
30. **Plano de depreciação do legado JSON** com data-alvo e modo somente-leitura antes da remoção final.

### P3 — Evolutivo (otimizações futuras)
31. **Perfis humanos múltiplos** (ex.: “rápido”, “cuidadoso”, “clínico”) selecionáveis por request/origem.
32. **Motor adaptativo por contexto**: ajustar digitação conforme tamanho do prompt, idioma e urgência.
33. **Painel de observabilidade unificado** (fila, chats ativos, rate-limit, erros, perfil humano em uso).
34. **Replay determinístico para debugging** (com seed opcional) preservando modo realista em produção.
35. **Orquestração multi-worker opcional** com isolamento por perfil e limites globais de CPU/memória.
36. **Auto-tuning assistido** dos parâmetros humanos com base em métricas históricas (sem eliminar aleatoriedade).

---

## Refinamento desta etapa (sem novas features)

> Objetivo desta rodada: **refinar execução e prioridade técnica** para blindar o comportamento humano não-robótico no browser, **sem adicionar escopo funcional novo**.

### Requisitos consolidados (não-regredir)
- API key como mecanismo primário de autorização (allowlist/IP/origem apenas defesa adicional).
- Bootstrap seguro via `config.py` e `sync_github_settings.ps1` a partir dos templates de exemplo.
- Reset `admin/admin` **somente** em fresh install.
- `browser_profile` ponta-a-ponta (server → browser → integrações), com fallback explícito para `default`.
- `sync_github` autônomo mantido e não acoplado ao fluxo de chat.

### Repriorização técnica orientada à simulação humana

#### P0 (execução imediata) — reduzir risco de “assinatura robótica”
1. **Contrato formal da simulação humana + critérios de aceitação testáveis** (item 1): definir invariantes e limites para jitter, pausas, correções e repetição.
2. **Timeout por etapa com taxonomia de erro única** (item 6): impedir travas silenciosas e retries agressivos no browser.
3. **Guardrails anti-bot no runtime** (item 2): detector de padrão mecânico com ajuste automático conservador.
4. **Controle de concorrência por `browser_profile`** (item 9): preservar sessão humana por perfil e evitar sobrecarga.
5. **Rate-limit unificado (detecção + aplicação + feedback)** (item 8): evitar bursts e comportamento anti-natural.
6. **Baseline de testes offline obrigatórios** (item 10): transformar heurísticas humanas em gates mínimos de qualidade.

#### P1 (alta prioridade) — observabilidade para calibrar realismo
1. **Telemetry de simulação humana** (item 11): medir variação real de digitação/pausas/correções.
2. **Tracing com correlation-id end-to-end** (item 14): localizar rapidamente onde a “humanidade” se perde na cadeia.
3. **Catálogo central de erros** (item 15): reduzir ambiguidades operacionais e acelerar resposta.
4. **Sanitização de logs e privacidade** (item 17): preservar diagnóstico sem vazamento de dados sensíveis.
5. **Prometheus com labels operacionais** (item 12): fechar loop de tuning com métricas consistentes.
6. **SSE resiliente** (item 13): estabilidade da telemetria em desconexões e reconexões.

#### P2 (médio prazo) — reduzir acoplamento e custo de manutenção
1. **Modularização de `browser.py` por ações** (item 22): foco em isolamento do núcleo de simulação humana.
2. **Modularização de `server.py` por domínios** (item 21): diminuir risco de regressão transversal.
3. **Validação de payload por esquema** (item 16): proteger fronteiras contra entradas inconsistentes.
4. **Selector health + fallback ordenado** (item 23): reduzir quebras por drift de UI.
5. **Testes de contrato API e caos controlado** (itens 25 e 26): robustez frente a cenários intermitentes.
6. **Runbook operacional e documentação de arquitetura** (itens 27 e 28): padronizar operação e troubleshooting.

---

## Plano de execução por lotes (P0 → P1 → P2)

### Lote P0 (hardening comportamental + confiabilidade mínima)
**Meta:** eliminar padrões mecânicos detectáveis e garantir previsibilidade de falhas do browser.

**Entregáveis de planejamento**
- Matriz “sinal humano vs. sinal robótico” com thresholds operacionais.
- Definição de SLA interno por etapa do browser (abrir/digitar/enviar/aguardar/extrair).
- Critérios de fairness e concorrência por `browser_profile`.
- Lista de testes offline obrigatórios para merge.

**Critério de pronto (DoD)**
- Sem regressão dos requisitos consolidados.
- Logs e erros com classificação padronizada nas falhas de etapa.
- Checks offline mínimos executando de forma reprodutível.

### Lote P1 (instrumentação + diagnóstico)
**Meta:** tornar o comportamento humano observável e calibrável em produção.

**Entregáveis de planejamento**
- Dicionário de métricas da simulação humana.
- Modelo de correlação de eventos (request→fila→browser→persistência).
- Padrão de códigos de erro e mensagens de ação.
- Política de mascaramento de segredos/PII em logs.

**Critério de pronto (DoD)**
- Métricas e tracing suficientes para explicar anomalias de comportamento.
- SSE recuperável com desconexão sem perda operacional crítica.

### Lote P2 (arquitetura sustentável)
**Meta:** reduzir acoplamento estrutural e facilitar evolução segura.

**Entregáveis de planejamento**
- Mapa de extração de módulos de `server.py` e `browser.py`.
- Estratégia incremental de validação de payload e saúde de seletores.
- Plano de testes de contrato/caos e runbook operacional.

**Critério de pronto (DoD)**
- Superfícies críticas desacopladas e com responsabilidades claras.
- Documentação operacional cobrindo incidentes frequentes.

---

## Checks executados nesta etapa
- [x] Leitura e revisão de alinhamento do backlog com foco em simulação humana.
- [x] Repriorização e plano por lotes P0→P1→P2 documentados.
- [x] Execução de checks automatizados disponíveis no ambiente (ver seção de comando/resultado no relatório da entrega).

### Resultado objetivo dos checks (2026-04-21)
- `pytest -q tests/test_humanizer.py tests/test_shared_queue.py tests/test_selectors_smoke.py` → **PASS**.
- `pytest -q` → **falha de ambiente** durante collection por dependências indisponíveis (`flask`, `cryptography`, `requests`, `markdownify`) e bloqueio de acesso ao índice de pacotes (proxy 403).

## Prompt de retomada (copiar em novo chat)
"Continue o refactor do projeto `/workspace/chatGPT_Simulator` lendo `REFACTOR_PROGRESS.md` primeiro. Nesta etapa, NÃO implemente código novo de features: apenas refine e priorize backlog técnico, com foco máximo em manter simulação humana não-robótica no browser. Respeite os requisitos já consolidados (API key primária, bootstrap de `config.py`/`sync_github_settings.ps1`, reset `admin/admin` só em fresh install, `browser_profile` end-to-end com fallback para `default`, e `sync_github` autônomo). Em seguida, proponha um plano de execução por lotes (P0→P1→P2), rode checks possíveis no ambiente, atualize `REFACTOR_PROGRESS.md`, faça commit e abra PR."
