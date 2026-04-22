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

### Itens atualizados nesta rodada (marcados)
- [x] Revalidação explícita dos requisitos consolidados (não-regredir).
- [x] Repriorização do backlog técnico com foco em anti-robotização no browser.
- [x] Plano de execução em lotes P0→P1→P2 com entregáveis e DoD.
- [x] Registro dos checks possíveis no ambiente e limitações de execução completa.

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

---

## Progresso 2026-04-22 (branch `claude/fix-rate-limit-interval-1vPbB`)

### Entregue nesta sessão
- **Extração de `_is_python_chat_request` e `_is_codex_chat_request` para `Scripts/request_source.py`** — módulo puro, sem Flask/HTTP, reutilizado por `server.py` via import e wrappers internos (nomes `_is_python_chat_request` / `_is_codex_chat_request` preservados para não alterar o fluxo de `/v1/chat/completions`).
- **Novo `tests/test_request_source.py`** cobrindo:
  - sufixo `.py`, `.py/<lane>` e prefixo `python:`;
  - inputs vazios/`None`, frontend PHP, chatgpt-ui;
  - classificação de Codex por `source_hint`, `url` e `origin_url`, incluindo case-insensitive.
- Objetivo: tornar o gating do intervalo anti-rate-limit (`_wait_python_request_interval_if_needed`) e da fila Python FIFO testável offline, sem exigir `flask`/`cryptography` no ambiente.

### Resultado dos checks (2026-04-22)
- `pytest tests/test_humanizer.py tests/test_shared_queue.py tests/test_selectors_smoke.py tests/test_request_source.py` → **18 passed**.
- `python3 -c "import ast; ast.parse(open('Scripts/server.py').read())"` → OK.
- `pytest -q` completo ainda não executável neste ambiente (mesma limitação de dependências).

### Próximos itens sugeridos (continuar em outro chat se necessário)
Ordem sugerida, todos com escopo pequeno e testáveis offline (preserva simulação humana):
1. **Catálogo central de erros (backlog P1 #15)** — criar `Scripts/error_catalog.py` com códigos (`RATE_LIMIT`, `QUEUE_TIMEOUT`, `BROWSER_TIMEOUT`, `SELECTOR_MISSING`, etc.), mensagens e ação recomendada. Substituir strings livres nos pontos críticos de `server.py` e `browser.py`. Adicionar `tests/test_error_catalog.py`.
2. **Sanitização de logs/PII (backlog P1 #17)** — criar `Scripts/log_sanitizer.py` com mascaramento de `api_key`, tokens `Bearer`, cookies de sessão, caminhos de perfil Chromium. Integrar em `_audit_event` (server.py:209) e em `utils.file_log`. Adicionar `tests/test_log_sanitizer.py`.
3. **Teste puro do cálculo do intervalo anti-rate-limit** — extrair `compute_python_interval_target(pmin, pmax, profile_count, rng)` de `_wait_python_request_interval_if_needed` para módulo puro e cobrir bordas (`pmin>pmax`, zero, profile_count=1/2/N). Item vinculado ao backlog P0 #8.
4. **Concorrência por `browser_profile` (backlog P0 #9)** — modelar como sessão semáforo limitada em `browser.py`. **Não implementar sem passar por planejamento**: toca o loop do navegador e exige plano explícito antes de mudar código.

### Prompt de retomada (copiar em novo chat)
"Continue o refactor do `/home/user/chatGPT_Simulator` na branch `claude/fix-rate-limit-interval-1vPbB`. Leia `REFACTOR_PROGRESS.md` (seção `Progresso 2026-04-22`) primeiro. Implemente o próximo item pendente da lista `Próximos itens sugeridos` (começar pelo item 1 — catálogo central de erros). Regras: (a) sem novas features além do item sugerido; (b) preservar os requisitos consolidados (API key primária, bootstrap `config.py`/`sync_github_settings.ps1`, reset `admin/admin` só em fresh install, `browser_profile` end-to-end com fallback `default`, `sync_github` autônomo, intervalo anti-rate-limit global para requests Python já em server.py:424); (c) manter testes offline passando (`pytest tests/test_humanizer.py tests/test_shared_queue.py tests/test_selectors_smoke.py tests/test_request_source.py`); (d) sempre que estiver próximo ao limite do chat, ATUALIZAR esta seção com o que foi feito e o próximo passo ANTES de commit/push; (e) commit e push para `claude/fix-rate-limit-interval-1vPbB`. Se o item escolhido envolver `browser.py`, parar e pedir confirmação antes de editar."

---

## Refinamento 2026-04-22 bis (replanejamento sem novo código de feature)

> Escopo desta rodada: **apenas refinar prioridades e plano**, com base em evidências concretas do código atual. Nenhum código de feature foi adicionado nesta seção — apenas documentação, teste de baseline e atualização do roadmap.

### Requisitos consolidados (revalidados, permanecem intactos)
- API key como mecanismo primário de autorização (IP/origem são defesa adicional).
- Bootstrap seguro via `config.py` + `sync_github_settings.ps1` a partir dos `*.example.*`.
- Reset `admin/admin` **somente** em fresh install.
- `browser_profile` ponta-a-ponta (server → browser → analisador) com fallback `default`.
- `sync_github` autônomo, não acoplado ao fluxo de chat.
- **Adicionado:** intervalo anti-rate-limit global para requests Python (server.py:428, `_wait_python_request_interval_if_needed`) — aplicado a qualquer `request_source` terminando em `.py`, contendo `.py/`, ou com prefixo `python:`; isento para Codex Cloud.
- **Adicionado:** detecção de origem de request centralizada em `Scripts/request_source.py` (módulo puro, testável sem Flask).

### Evidências medidas do código atual (subsídio para priorização)
Coletadas em `2026-04-22` via `wc -l` / `grep -nE "def "`:

| Arquivo | Linhas | Defs | Observação |
|---|---|---|---|
| `Scripts/browser.py` | 5086 | 27 | Maior hotspot arquitetural. Async Playwright entrelaçado com predicados puros. |
| `Scripts/analisador_prontuarios.py` | 6134 | — | Maior arquivo. Contém muitos helpers puros (regex, parse, heurísticas) sem testes. |
| `Scripts/server.py` | 2522 | 68 | Organizado por seções. Vários helpers puros prontos para extração. |
| `Scripts/humanizer.py` | 124 | 4 | Já é módulo puro com testes — **template validado** de como extrair. |
| `Scripts/request_source.py` | 34 | 2 | Criado em 2026-04-22, padrão confirmado. |

**Conclusão operacional:** o padrão "extrair helper puro → testar offline → manter wrapper fino no chamador" já foi validado duas vezes (`humanizer.py`, `request_source.py`) e deve virar **prática obrigatória antes de qualquer mudança comportamental** nos itens P0 (#1, #2, #6, #8, #9).

### Repriorização do backlog (alinhamento com evidências)

#### P0 (revisado) — priorizar o que blinda simulação humana
1. **(Promovido)** **Catálogo central de erros** (backlog #15, antes P1) — hoje `_extract_rate_limit_details` (server.py:319) já faz catálogo ad-hoc por string-match; consolidar é **pré-requisito** para `Rate-limit unificado` (#8). Entregável inicial: `Scripts/error_catalog.py` + testes offline; sem tocar em `browser.py`.
2. **(Novo)** **Extração contínua de helpers puros para módulos testáveis** — padrão `humanizer.py`/`request_source.py` aplicado a:
   - `server.py`: `_format_wait_seconds`, `_extract_rate_limit_details`, `_queue_status_payload`, `_count_active_chatgpt_profiles`, `_prune_old_attempts`.
   - `browser.py` (pure only, sem async): `_is_known_orphan_tab_url`, `_is_python_sender`, `_response_looks_incomplete_json`, `_response_requests_followup_actions`, `_replace_inline_base64_payloads`, `_ensure_paste_wrappers`.
   - DoD por helper extraído: módulo novo + ≥3 testes offline + wrapper mantido no chamador original.
3. **Contrato formal da simulação humana + critérios de aceitação testáveis** (backlog #1) — já parcialmente atendido por `HumanTypingProfile` em `humanizer.py`; falta escrever os **invariantes observáveis** (ex.: "nunca dois delays consecutivos idênticos até 3 casas decimais", "pausa mínima após pontuação ≥ 80ms p95").
4. **Timeout por etapa com taxonomia de erro única** (backlog #6) — depende do item 1 (catálogo) para nomear erros consistentemente.
5. **Guardrails anti-bot no runtime** (backlog #2) — após itens 1–3; watchdog consome taxonomia de erros e telemetria do humanizer.
6. **Rate-limit unificado** (backlog #8) — após itens 1 e 4; agora há base para unificar detecção browser ↔ aplicação server.
7. **Controle de concorrência por `browser_profile`** (backlog #9) — **continua último P0**: toca `browser.py` estrutural; requer plano de design explícito e confirmação antes de editar código.
8. **Baseline de testes offline obrigatórios** (backlog #10) — já parcialmente atendido (`test_humanizer.py`, `test_shared_queue.py`, `test_selectors_smoke.py`, `test_request_source.py` = 18 testes). **Novo DoD:** qualquer PR que toque server.py/browser.py deve adicionar pelo menos um teste offline.

#### P1 (revisado) — observabilidade dirigida por taxonomia
1. **Sanitização de logs/PII** (backlog #17) — **independente** dos P0; pode ser executado em paralelo (escopo pequeno, módulo puro `Scripts/log_sanitizer.py`).
2. **Telemetry da simulação humana** (backlog #11) — depende de P0 item 3 (invariantes observáveis).
3. **Tracing com correlation-id end-to-end** (backlog #14) — depende de P0 itens 1 e 4 (taxonomia nomeada).
4. **Prometheus com labels operacionais** (backlog #12) — depende de P0 itens 1 e 4.
5. **SSE resiliente** (backlog #13) — menor acoplamento; reclassificado para P2.

#### P2 (inalterado na ordem, DoD refinado)
1. **Modularização de `browser.py` por ações** — DoD novo: **apenas após** conclusão dos itens P0 1, 2, 3 (senão extração embaralha simulação humana).
2. **Modularização de `server.py` por domínios** — DoD novo: começar por `_security_*` (já coeso) e depois `_rate_limit_*`; **nunca** tocar `/v1/chat/completions` sem plano de contrato.
3. Demais itens (validação de payload, selector health, testes de contrato/caos, runbook) — ordem inalterada.

### Plano de execução por lotes (DoD refinados em 2026-04-22 bis)

#### Lote P0 — hardening comportamental
**Meta:** eliminar padrões mecânicos detectáveis e garantir previsibilidade de falhas.

**Sequência sugerida (cada item em PR pequeno e isolado):**
1. `Scripts/error_catalog.py` + `tests/test_error_catalog.py` (puro).
2. Extração lote-A em `server.py` (`_format_wait_seconds`, `_queue_status_payload`, `_prune_old_attempts`, `_count_active_chatgpt_profiles`) → `Scripts/server_helpers.py` + testes.
3. Extração lote-B de predicados puros em `browser.py` → `Scripts/browser_predicates.py` + testes. **Não tocar async/Playwright.**
4. Invariantes testáveis de `HumanTypingProfile` (ex.: geração determinística via `random.seed`) → ampliar `tests/test_humanizer.py`.
5. Uso do catálogo em `_extract_rate_limit_details` + `_register_chat_rate_limit` (server.py).
6. (Condicional) Plano de design de concorrência por `browser_profile` antes de qualquer edição em `browser.py`.

**Critério de pronto (DoD do Lote P0):**
- Cada PR do lote: ≤200 linhas de diff líquido fora de testes; wrapper fino no chamador; ≥3 testes offline novos; `pytest` offline passa.
- Nenhuma regressão nos requisitos consolidados (checados por code review).
- `_extract_rate_limit_details` consome catálogo central (string-match removido do caminho quente).

#### Lote P1 — instrumentação dirigida
**Sequência sugerida:**
1. `Scripts/log_sanitizer.py` + testes + integração em `_audit_event` (server.py:213) e `utils.file_log`.
2. Dicionário de métricas da simulação humana (documento em `docs/` + labels no humanizer).
3. `X-Correlation-Id` ponta-a-ponta (pass-through sem lógica nova).
4. Labels operacionais em Prometheus (mudança incremental).

**DoD Lote P1:**
- Logs nunca emitem `api_key=<valor>`, `Authorization: Bearer ...`, cookies de sessão, caminho absoluto de perfil Chromium sem máscara.
- Correlation-id propagado request → `browser_queue` → stream.

#### Lote P2 — arquitetura sustentável
**Sequência inalterada; início condicionado à conclusão dos Lotes P0 e P1.**

### Checks executados nesta etapa (2026-04-22 bis)
- `pytest tests/test_humanizer.py tests/test_shared_queue.py tests/test_selectors_smoke.py tests/test_request_source.py` → **18 passed** (baseline preservada).
- `wc -l Scripts/server.py Scripts/browser.py Scripts/humanizer.py Scripts/analisador_prontuarios.py Scripts/request_source.py` → tabela acima.
- `grep -nE "def " Scripts/server.py Scripts/browser.py` → identificação de helpers puros candidatos à extração.
- `pytest -q` completo não executado — mesma limitação histórica de ambiente (sem `flask`, `cryptography`, `requests`, `markdownify` e sem acesso ao índice PyPI).

### Escopo explicitamente NÃO executado nesta etapa
- Nenhum código de feature novo.
- Nenhuma extração real de helper (apenas mapeada).
- Nenhuma alteração em `browser.py`, `analisador_prontuarios.py`, `humanizer.py`, `request_source.py`.
- Esta rodada produz **somente** planejamento documental + checks de baseline.

### Prompt de retomada (atualizado para o próximo ciclo)
"Continue o refactor do `/home/user/chatGPT_Simulator` na branch `claude/fix-rate-limit-interval-1vPbB`. Leia `REFACTOR_PROGRESS.md` — em especial a seção `Refinamento 2026-04-22 bis` — antes de qualquer edição. Execute o **Lote P0, passo 1**: criar `Scripts/error_catalog.py` (códigos `RATE_LIMIT`, `QUEUE_TIMEOUT`, `BROWSER_TIMEOUT`, `SELECTOR_MISSING`, `CONFIG_MISSING`, `AUTH_FAILED`, `UPSTREAM_UNAVAILABLE`, etc., com mensagem curta e ação recomendada) + `tests/test_error_catalog.py` (≥3 casos por código). Regras: (a) módulo puro, sem Flask/Playwright; (b) NÃO substituir nenhum uso ainda — essa integração é o passo 5 do Lote P0; (c) preservar todos os requisitos consolidados (ver seção correspondente); (d) manter `pytest tests/test_humanizer.py tests/test_shared_queue.py tests/test_selectors_smoke.py tests/test_request_source.py tests/test_error_catalog.py` passando; (e) ATUALIZAR esta seção ao se aproximar do limite, antes de commit/push; (f) commit e push para `claude/fix-rate-limit-interval-1vPbB`."

## Prompt de retomada ORIGINAL (ainda válido para sessões de replanejamento)
"Continue o refactor do projeto `/workspace/chatGPT_Simulator` lendo `REFACTOR_PROGRESS.md` primeiro. Nesta etapa, NÃO implemente código novo de features: apenas refine e priorize backlog técnico, com foco máximo em manter simulação humana não-robótica no browser. Respeite os requisitos já consolidados (API key primária, bootstrap de `config.py`/`sync_github_settings.ps1`, reset `admin/admin` só em fresh install, `browser_profile` end-to-end com fallback para `default`, e `sync_github` autônomo). Em seguida, proponha um plano de execução por lotes (P0→P1→P2), rode checks possíveis no ambiente, atualize `REFACTOR_PROGRESS.md`, faça commit e abra PR."
