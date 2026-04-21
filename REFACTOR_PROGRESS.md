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

## Prompt de retomada (copiar em novo chat)
"Continue o refactor do projeto `/workspace/chatGPT_Simulator` lendo `REFACTOR_PROGRESS.md` primeiro. Nesta etapa, NÃO implemente código novo de features: apenas refine e priorize backlog técnico, com foco máximo em manter simulação humana não-robótica no browser. Respeite os requisitos já consolidados (API key primária, bootstrap de `config.py`/`sync_github_settings.ps1`, reset `admin/admin` só em fresh install, `browser_profile` end-to-end com fallback para `default`, e `sync_github` autônomo). Em seguida, proponha um plano de execução por lotes (P0→P1→P2), rode checks possíveis no ambiente, atualize `REFACTOR_PROGRESS.md`, faça commit e abra PR."
