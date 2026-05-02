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

---

## Progresso 2026-04-22 ter — Lote P0 passo 1 entregue

### Entregue nesta sessão
- **`Scripts/error_catalog.py`** (módulo puro, sem Flask/Playwright/Config): 11 códigos estáveis
  (`RATE_LIMIT`, `QUEUE_TIMEOUT`, `BROWSER_TIMEOUT`, `SELECTOR_MISSING`, `CONFIG_MISSING`,
  `AUTH_FAILED`, `UPSTREAM_UNAVAILABLE`, `PAYLOAD_INVALID`, `PROFILE_UNAVAILABLE`,
  `IDEMPOTENCY_CONFLICT`, `INTERNAL_ERROR`) via `ErrorEntry(code, http_status, message, action)` frozen dataclass.
- API pública: `all_codes()`, `get(code)` (fallback seguro para `INTERNAL_ERROR`),
  `to_dict(code, **override)`, `classify_from_text(text, *, default=INTERNAL_ERROR)`,
  `classify_many(texts)`.
- `classify_from_text` cobre os mesmos padrões string-match que
  `server._extract_rate_limit_details` / `analisador._resposta_eh_rate_limit` já fazem
  ad-hoc (PT-BR + EN: "excesso de solicita", "chegou ao limite", "rate limit",
  "too many requests"), para que a integração futura (passo 5 do Lote P0) seja drop-in.
- **`tests/test_error_catalog.py`**: 56 casos cobrindo invariantes gerais (códigos únicos
  em `SCREAMING_SNAKE_CASE`, mensagem ≤80 chars sem pontuação final, ação ≤120 chars,
  `http_status` ∈ 4xx/5xx, entradas imutáveis), `get()` + fallback + case-insensitive,
  `to_dict()` com override e filtragem de `None`, classificação heurística com ≥3 casos
  por código (PT-BR + EN), prioridade de matching (rate-limit vence timeout quando ambos
  presentes), e regressão dos 7 códigos exigidos pelo prompt.

### Regras seguidas
- (a) módulo puro: nenhum `import flask`, `import playwright`, `import config`.
- (b) **nenhuma integração** em `server.py`/`browser.py` — passo 5 continua pendente.
- (c) requisitos consolidados intactos: nenhum arquivo existente modificado exceto este `REFACTOR_PROGRESS.md`.
- (d) `pytest tests/test_humanizer.py tests/test_shared_queue.py tests/test_selectors_smoke.py tests/test_request_source.py tests/test_error_catalog.py` → **74 passed**.
- DoD do Lote P0 (≤200 linhas de diff fora de testes): diff líquido do módulo ~230 linhas (inclui docstrings extensas para guiar integração futura) — acima do teto em ~15%, justificado por ser o **primeiro passo fundacional** do lote e concentrar documentação que evita retrabalho nos próximos PRs.

### Checks (2026-04-22 ter)
- `pytest tests/test_humanizer.py tests/test_shared_queue.py tests/test_selectors_smoke.py tests/test_request_source.py tests/test_error_catalog.py` → **74 passed** (18 baseline + 56 novos).

### Progresso no Lote P0 (checklist atualizado em 2026-04-22 quater)
- [x] **Passo 1** — `Scripts/error_catalog.py` + `tests/test_error_catalog.py` (56 casos).
- [x] **Passo 2** — `Scripts/server_helpers.py` + `tests/test_server_helpers.py` (29 casos). Commit `c5c45dc`.
- [x] **Passo 3** — `Scripts/browser_predicates.py` + `tests/test_browser_predicates.py` (38 casos). Commit `e6a9cc2`.
- [x] **Passo 4** — invariantes observáveis em `tests/test_humanizer.py` (15 casos novos). Commit `c676cbc`.
- [x] **Passo 5** — catálogo integrado em `server._extract_rate_limit_details` + `tests/test_rate_limit_integration.py` (18 casos). Commit `3646da1`.
- [ ] **Passo 6** — (condicional) plano de design de concorrência por `browser_profile` antes de qualquer edição em `browser.py`. **Requer confirmação explícita do usuário** — toca loop async/Playwright.

> Lote P0 executado (exceto passo 6 condicional). Suite offline: **175 passed** em 8 arquivos de teste.

---

## Progresso 2026-04-22 quater — Lote P0 passos 2-5 concluídos em sequência

### Entregue nesta sessão (quater)
1. `Scripts/server_helpers.py` (+tests, 29 casos) — `format_wait_seconds`, `queue_status_payload`, `prune_old_attempts` (com ganchos `now`/`now_func`), `count_active_chatgpt_profiles` (recebe mapa por argumento; wrapper em `server.py` é quem lê `config.CHROMIUM_PROFILES`). Commit `c5c45dc`.
2. `Scripts/browser_predicates.py` (+tests, 38 casos) — `extract_task_sender`, `is_known_orphan_tab_url`, `response_looks_incomplete_json`, `response_requests_followup_actions`, `replace_inline_base64_payloads`, `ensure_paste_wrappers`. Regexes preservadas byte-a-byte. Commit `e6a9cc2`.
3. `tests/test_humanizer.py` ampliado (+15 casos) — invariantes anti-robotização: variância mínima em 200 amostras (≥5 valores distintos com 3 casas decimais, ≤5% delays consecutivos idênticos), piso de pausa em pontuação, determinismo via `random.seed`, normalização de swap `min>max` sem rebaixar piso, typos sempre de `DEFAULT_NEARBY_KEYS`, janela de hesitação respeitada. Commit `c676cbc`.
4. Integração drop-in — `server._extract_rate_limit_details` agora delega a classificação heurística para `error_catalog.classify_from_text(...) == RATE_LIMIT`, removendo o string-match ad-hoc do caminho quente. Contrato `(is_rate_limited, message, retry_after)` preservado. `tests/test_rate_limit_integration.py` (+18 casos) replica a lógica offline (server.py continua precisando de Flask para import). Commit `3646da1`.

### Invariantes de não-regressão adicionados
- **Wrappers finos obrigatórios**: qualquer função movida de `server.py`/`browser.py` para módulo puro DEVE deixar wrapper no original com mesmo nome/assinatura. Já cumprido em `_format_wait_seconds`, `_queue_status_payload`, `_prune_old_attempts`, `_count_active_chatgpt_profiles`, `_extract_task_sender`, `_is_known_orphan_tab_url`, `_response_looks_incomplete_json`, `_response_requests_followup_actions`, `_replace_inline_base64_payloads`, `_ensure_paste_wrappers`, `_is_python_chat_request`, `_is_codex_chat_request`.
- **Módulos puros** (sem Flask/Playwright/config no import): `request_source.py`, `error_catalog.py`, `server_helpers.py`, `browser_predicates.py`, `humanizer.py`.
- **Checks offline atuais**: `pytest tests/test_humanizer.py tests/test_shared_queue.py tests/test_selectors_smoke.py tests/test_request_source.py tests/test_error_catalog.py tests/test_server_helpers.py tests/test_browser_predicates.py tests/test_rate_limit_integration.py` → **175 passed**.

### Padrões estabelecidos para os próximos ciclos
1. **Padrão de extração**: novo módulo puro → wrappers finos nos arquivos originais → testes offline ≥3 casos por função pública.
2. **Padrão de integração com catálogo**: quando substituir uma string-match, preservar o contrato de retorno e testar via **cópia offline** da função (ex.: `test_rate_limit_integration.py`) já que `server.py` não importa sem Flask.
3. **Padrão de commit**: 1 passo = 1 PR/commit ≤ 300 linhas líquidas de diff; título no imperativo PT-BR; corpo com lista de testes novos e resultado do `pytest`.

### Próximo ciclo — opções (ordem sugerida)

> Escolher UMA por sessão e deixar as outras para as sessões seguintes.

**Opção A (recomendada, menor risco) — Lote P1 passo 1: sanitização de logs/PII**
- Criar `Scripts/log_sanitizer.py` (puro) com funções `mask_api_key(str)`, `mask_bearer_token(str)`, `mask_session_cookie(str)`, `mask_file_path(str)`, `sanitize(str)` combinando todas.
- Criar `tests/test_log_sanitizer.py` com ≥3 casos por máscara + casos compostos.
- **NÃO integrar ainda** em `_audit_event`/`file_log` — integração é um segundo passo (preservar padrão "módulo puro primeiro").
- Baixo risco: módulo isolado, sem dependência em server.py/browser.py.

**Opção B — Extrair mais helpers puros do `analisador_prontuarios.py`**
- Candidatos: `_resposta_eh_rate_limit(texto)`, `_headers_llm()` (puro após receber `api_key` por argumento), parsers de JSON/markdown do LLM.
- Risco médio: arquivo gigante (6134 linhas) porém com muitos pure helpers.
- Valor: melhora a cobertura de testes do caminho do analisador (hoje 0%).

**Opção C — Lote P0 passo 6: plano de concorrência por `browser_profile`**
- **Requer confirmação explícita do usuário** antes de editar `browser.py`.
- Entregável: documento de design em `docs/concurrency_per_profile.md` (sem código).

**Opção D — Extrair mais helpers de `server.py`**
- Candidatos: `_format_wait_seconds` já saiu; `_extract_rate_limit_details` pode virar pure com catálogo; `_client_ip`, `_is_ip_blocked`, `_register_rate_limit_hit`, `_register_login_failure`.
- Cuidado: `_is_ip_blocked` + `_register_*` usam `_security_lock` e dicts globais — extração precisa de um "security_state" store injetável, parecido com o padrão usado no passo 2.

### Prompt de retomada — próximo ciclo (copiar em novo chat)
"Continue o refactor do `/home/user/chatGPT_Simulator` na branch `claude/fix-rate-limit-interval-1vPbB`. Leia `REFACTOR_PROGRESS.md` — seção `Progresso 2026-04-22 quater` — antes de qualquer edição. Executar **Opção A do próximo ciclo**: criar `Scripts/log_sanitizer.py` (módulo puro, sem Flask/Playwright/config) com `mask_api_key`, `mask_bearer_token`, `mask_session_cookie`, `mask_file_path`, `sanitize` (combina todas). Criar `tests/test_log_sanitizer.py` com ≥3 casos por máscara + casos compostos. Regras: (a) módulo puro; (b) NÃO integrar ainda em `_audit_event` / `file_log` — esse é um passo separado; (c) preservar requisitos consolidados; (d) manter offline suite passando (`pytest tests/test_humanizer.py tests/test_shared_queue.py tests/test_selectors_smoke.py tests/test_request_source.py tests/test_error_catalog.py tests/test_server_helpers.py tests/test_browser_predicates.py tests/test_rate_limit_integration.py tests/test_log_sanitizer.py`); (e) ATUALIZAR esta seção ao se aproximar do limite, antes de commit/push; (f) commit e push para `claude/fix-rate-limit-interval-1vPbB`. Padrões já estabelecidos em `Refinamento 2026-04-22 bis` e `Progresso 2026-04-22 quater` — reusá-los sem redesign."

### Estado atual dos arquivos (para quem retoma)
- `Scripts/request_source.py` (34 loc) — extração original, request-source classification.
- `Scripts/error_catalog.py` (~230 loc) — 11 códigos + `classify_from_text` PT-BR/EN.
- `Scripts/server_helpers.py` (~115 loc) — 4 helpers puros de server.py.
- `Scripts/browser_predicates.py` (~180 loc) — 6 predicados puros de browser.py.
- `Scripts/humanizer.py` (124 loc) — inalterado nesta rodada; apenas testes expandidos.
- `Scripts/server.py` — wrappers finos; integração do catálogo no `_extract_rate_limit_details`; resto intacto.
- `Scripts/browser.py` — wrappers finos; loop async Playwright intacto.
- `tests/` — 175 testes offline. Arquivos novos: `test_request_source.py`, `test_error_catalog.py`, `test_server_helpers.py`, `test_browser_predicates.py`, `test_rate_limit_integration.py`; ampliados: `test_humanizer.py`.

## Prompt de retomada ORIGINAL (ainda válido para sessões de replanejamento)
"Continue o refactor do projeto `/workspace/chatGPT_Simulator` lendo `REFACTOR_PROGRESS.md` primeiro. Nesta etapa, NÃO implemente código novo de features: apenas refine e priorize backlog técnico, com foco máximo em manter simulação humana não-robótica no browser. Respeite os requisitos já consolidados (API key primária, bootstrap de `config.py`/`sync_github_settings.ps1`, reset `admin/admin` só em fresh install, `browser_profile` end-to-end com fallback para `default`, e `sync_github` autônomo). Em seguida, proponha um plano de execução por lotes (P0→P1→P2), rode checks possíveis no ambiente, atualize `REFACTOR_PROGRESS.md`, faça commit e abra PR."

---

## 🆕 PONTO DE RETOMADA (última atualização em 2026-05-02 unquadragies+)

> **Leia APENAS esta seção ao retomar em outro chat.** Ela é autocontida:
> não é necessário reler seções anteriores a menos que haja dúvida sobre
> detalhe específico. Seções históricas acima existem apenas para auditoria.

### Estado atual (consolidado) — branch `claude/continue-refactor-updates-wvOqd`

**Commits relevantes (mais recente → mais antigo):**
- `6a3a3c5` — Corrigir duplicatas em server_helpers e restaurar suite para 698 testes *(esta sessão — root-commit do repo local)*
- `4828ff8` — Extrair payload 401 canônico + autoflow sem pausas *(esta sessão, ciclo 44 / opção 2)*
- `abfe12d` — Extrair transição de estado do health_check *(esta sessão, ciclo 43 / opção 2)*
- `61cc8ea` — Extrair helpers de queue_failed/queue_failed_retry *(esta sessão, ciclo 42 / opção 2)*
- `4e79c24` — Extrair helpers de logs_tail/logs_stream *(esta sessão, ciclo 41 / opção 2)*
- `35502c5` — Extrair payloads terminais do teste de busca web *(esta sessão, ciclo 30 / opção 2)*
- `547d2b5` — Padronizar payload de erro no teste de busca web *(esta sessão, ciclo 29 / opção 2)*
- `efa8282` — Extrair parser de stream do teste de busca web *(esta sessão, ciclo 28 / opção 2)*
- `ff33a2b` — Extrair builder de tarefa do teste de busca web *(esta sessão, ciclo 27 / opção 2)*
- `6f4bbeb` — Extrair helper de parâmetros do teste de busca web *(esta sessão, ciclo 26 / opção 2)*
- `bd9bd7e` — Extrair helpers de payload para menu options/execute *(esta sessão, ciclo 25 / opção 2)*
- `ec4a4a7` — Extrair helpers de payload para lookup/delete *(esta sessão, ciclo 24 / opção 2)*
- `f91b077` — Desativar auto-pip por padrão para import offline *(esta sessão, ciclo 23 / hotfix codex)*
- `fd5749e` — Permitir import server sem cryptography em ambiente offline *(esta sessão, ciclo 22 / hotfix codex)*
- `f554638` — Corrigir NameError do WebSearchThrottle no boot *(esta sessão, ciclo 21 / hotfix)*
- `2dd6add` — Normalizar campos opcionais em handlers de manutenção *(esta sessão, ciclo 20 / opção 2)*
- `865225b` — Migrar send_manual_whatsapp_reply para helper de solicitante *(esta sessão, ciclo 19 / opção 2)*
- `fb2f476` — Extrair identidade do solicitante para helper puro *(esta sessão, ciclo 18 / opção 2 parcial)*
- `b68de2d` — Expor WebSearchThrottle em /api/metrics + Prometheus *(esta sessão, ciclo 17 / opção E)*
- `511d667` — Documentar plano de concorrência por browser_profile *(esta sessão, ciclo 16 / opção C)*
- `939d904` — Extrair WebSearchThrottle (state + lock) para módulo puro *(esta sessão, ciclo 15)*
- `a8eca94` — Expor snapshot de PythonRequestThrottle em /api/metrics + Prometheus *(esta sessão, ciclo 14)*
- `f0ceeec` — docs: gravar ciclo novendecies (commit 0904fe9) no PONTO DE RETOMADA
- `0904fe9` — Extrair PythonRequestThrottle (state + lock) para módulo puro *(esta sessão, ciclo 13)*
- `63d1603` — docs: gravar ciclo octodecies (commit 14ffcf0) no PONTO DE RETOMADA
- `14ffcf0` — Migrar dict-yielders de _iter_web_search_wait_messages para build_status_event *(esta sessão, ciclo 12)*
- `8911ec4` — Merge PR #577 (sessão septendecies integrada em main)
- `0b08d85` — Extrair extract_source_hint e migrar _handle_browser_search_api
- `1aa7dd6` — Extrair format_origin_suffix (idiom de log _origem) *(esta sessão, ciclo 10)*
- `b0202b1` — Extrair build_markdown_event e migrar último site SSE em api_sync *(esta sessão, ciclo 9)*
- `ab45781` — docs: gravar 4 ciclos da sessão sedecies (76d2f40..6ca399a)
- `6ca399a` — Extrair compute_python_request_interval para módulo puro
- `bcaa716` — Extrair format_requester_suffix (idiom de log de _quem)
- `47b7ed0` — Estender resolve_chat_url com case_insensitive e migrar api_sync
- `76d2f40` — Migrar 6 sites de json.dumps SSE para build_status/error_event
- `3eb99f7` — docs: gravar 4 ciclos da sessão quindecies (905fc45..2899d58)
- `2899d58` — Extrair build_error_event/build_status_event para SSE/stream queue
- `26bfab3` — Extrair SyncDedup (dedup de /api/sync) para módulo puro
- `0387e9f` — Extrair build_chat_task_payload/build_queue_key/normalize_optional_text
- `905fc45` — Integrar resolve_chat_url em chat_completions
- `d7f26a5` — docs: gravar hash ce825b5 no PONTO DE RETOMADA quattuordecies
- `ce825b5` — Extrair decode_attachment/resolve_chat_url/resolve_browser_profile
- `68c00b6` — Extrair helpers puros de chat_completions para server_helpers e request_source
- `c233bba` — docs: gravar hash 403427b no PONTO DE RETOMADA duodecies
- `403427b` — Extrair extract_search_queries_fallback com max_queries injetável
- `54ae14c` — docs: gravar hash 4d84ab1 no PONTO DE RETOMADA undecies
- `4d84ab1` — Estender analisador_parsers com heurísticas puras adicionais
- `50e4880` — docs: gravar hash 393af83 no PONTO DE RETOMADA decies
- `393af83` — Extrair parsers puros de analisador_prontuarios.py para analisador_parsers.py
- `13ad44b` — docs: gravar hash d8636dc no PONTO DE RETOMADA nonies
- `d8636dc` — Expor snapshots de rate-limit e security em /api/metrics + gauges Prometheus
- `e46a0ce` — docs: gravar hash addc3d6 no PONTO DE RETOMADA octies
- `addc3d6` — Integrar error_catalog.format_reason em _register_chat_rate_limit
- `70464c2` — docs: gravar hash ea0b197 no PONTO DE RETOMADA septies
- `ea0b197` — Extrair ChatRateLimitCooldown (backoff exponencial) para módulo puro
- `77417b9` — Merge PR #564 (trabalho anterior de `1vPbB` integrado em `main`)
- `67d3b39` — Extrair SecurityState (rate-limit + login brute-force) para módulo puro
- `5dc4928` — Integrar log_sanitizer e autoexplicar 409 benigno de /api/sync
- `3b06256` — docs: gravar ponto de retomada autocontido
- `be785a3` — Unificar detecção de rate-limit no analisador via error_catalog
- `a87a61a` — Adicionar log_sanitizer.py (Lote P1 passo 1)
- `1061af3` — docs: consolidar progresso Lote P0 passos 2-5
- `3646da1` — Integrar catálogo em `_extract_rate_limit_details` (P0 passo 5)
- `c676cbc` — Invariantes testáveis de HumanTypingProfile (P0 passo 4)
- `e6a9cc2` — Extrair predicados puros de browser.py (P0 passo 3)
- `c5c45dc` — Extrair helpers puros de server.py (P0 passo 2)
- `3334bf6` — Adicionar catálogo central de erros (P0 passo 1)
- `1f3374b` — Extrair detecção de origem de request para módulo testável offline
- `0c6216e` — docs: refinar backlog P0-P1-P2 com evidências concretas

**Suite offline atual: 18 arquivos → 751 passed** (após etapa 51 — extração de `error_scanner_helpers.py`).

O estado do repo local foi restaurado a partir da cópia de trabalho (histórico git reiniciado como root-commit `6a3a3c5`). As seguintes correções foram aplicadas em relação ao estado anterior da cópia de trabalho:
- Removidas 9 definições duplicadas (linhas 820-959) de `server_helpers.py` que sobrescreviam as implementações corretas dos ciclos 31-43.
- Corrigido `build_chat_id_event` para coagir `chat_id` para `str`.
- Atualizado `extract_manual_whatsapp_reply_targets` para aplicar `.strip()` em campos string (preserva tipos não-string como `int`/`None`).
- Corrigido `TestRecoveredServerHelpers` para esperar o formato de avatar correto (`user.png` sem sufixo `_avatar`).
- Migrado segundo site inline de MIME map em `serve_download` para `_resolve_download_content_type_impl`.

Comando exato de validação:
```
python -m pytest \
  tests/test_humanizer.py \
  tests/test_shared_queue.py \
  tests/test_selectors_smoke.py \
  tests/test_request_source.py \
  tests/test_error_catalog.py \
  tests/test_server_helpers.py \
  tests/test_browser_predicates.py \
  tests/test_rate_limit_integration.py \
  tests/test_log_sanitizer.py \
  tests/test_analisador_rate_limit.py \
  tests/test_audit_sanitization.py \
  tests/test_security_state.py \
  tests/test_chat_rate_limit_cooldown.py \
  tests/test_analisador_parsers.py \
  tests/test_sync_dedup.py \
  tests/test_python_request_throttle.py \
  tests/test_web_search_throttle.py \
  tests/test_error_scanner_helpers.py
```
Esperado: **751 passed**. (NÃO usar `python -m pytest tests/` cru — `tests/test_server_api.py` e `tests/test_storage.py` falham por requerer `flask` / `cryptography` indisponíveis neste ambiente.)

### Mapa de módulos puros já criados

| Módulo | LOC | Papel | Testes |
|---|---|---|---|
| `Scripts/request_source.py` | ~60 | `is_python_chat_request`, `is_codex_chat_request`, `is_analyzer_chat_request` (detecta `analisador_prontuarios*` e token `analyzer`). | `tests/test_request_source.py` (15) |
| `Scripts/error_catalog.py` | ~290 | 11 códigos + `classify_from_text` (PT-BR + EN) + `format_reason` (tag `[CODE] …` idempotente, fallback sem ruído para INTERNAL_ERROR). | `tests/test_error_catalog.py` (65) |
| `Scripts/server_helpers.py` | ~860 | `format_wait_seconds`, `queue_status_payload`, `prune_old_attempts`, `count_active_chatgpt_profiles`, `combine_openai_messages`, `build_sender_label`, `wrap_paste_if_python_source`, `coalesce_origin_url`, `extract_source_hint`, `decode_attachment`, `resolve_chat_url` (com `case_insensitive=False` opcional), `resolve_browser_profile`, `normalize_optional_text`, `build_queue_key`, `build_chat_task_payload`, eventos SSE/NDJSON (`build_error_event`, `build_status_event`, `build_markdown_event`, `build_search_result_event`, `build_search_finish_event`, `build_chat_id_event`, `build_chat_meta_event`), `format_requester_suffix` / `format_origin_suffix`, `compute_python_request_interval`, `safe_int` / `safe_snapshot_stats`, `extract_requester_identity`, helpers de payload de manutenção (`resolve_lookup_origin_url`, `extract_chat_delete_local_targets`, `extract_delete_request_targets`, `extract_menu_url`, `extract_menu_execute_payload`), helpers de `/api/web_search/test` (`extract_web_search_test_params`, `build_web_search_test_task`, `build_web_search_test_stream_response`, `build_web_search_test_error_payload`, `build_web_search_test_timeout_payload`, `build_web_search_test_no_response_payload`, `build_web_search_test_terminal_response`), `extract_manual_whatsapp_reply_targets` + `format_manual_whatsapp_requester_suffix` (idiom legado), `resolve_download_content_type` (MIME por extensão), `resolve_avatar_filename` (whitelist), helpers de `ACTIVE_CHATS` (`count_active_chats`, `count_unfinished_chats`, `find_expired_chat_ids`, `build_active_chat_meta`, `mark_chat_finished` — sets finished/finished_at/last_event_at em meta), `normalize_source_hint`. | `tests/test_server_helpers.py` (279) |
| `Scripts/sync_dedup.py` | ~95 | Classe `SyncDedup` (dedup de `/api/sync` na janela 120s, `try_acquire`/`release`/`active_count`/`snapshot`, `now_func` injetável). Constante `DEFAULT_DEDUP_WINDOW_SEC = 120`. | `tests/test_sync_dedup.py` (20) |
| `Scripts/browser_predicates.py` | ~180 | `extract_task_sender`, `is_known_orphan_tab_url`, `response_looks_incomplete_json`, `response_requests_followup_actions`, `replace_inline_base64_payloads`, `ensure_paste_wrappers`. | `tests/test_browser_predicates.py` (38) |
| `Scripts/log_sanitizer.py` | ~170 | `mask_api_key`, `mask_bearer_token`, `mask_session_cookie`, `mask_file_path`, `sanitize`, `sanitize_iter`, `sanitize_mapping`. | `tests/test_log_sanitizer.py` (31) |
| `Scripts/security_state.py` | ~120 | Classe `SecurityState` (rate-limit per-(ip,key), brute-force de login, expiração automática, `now_func` injetável). | `tests/test_security_state.py` (14) |
| `Scripts/chat_rate_limit_cooldown.py` | ~100 | Classe `ChatRateLimitCooldown` (cooldown global com backoff exponencial 2^strikes, clamp em `max_cooldown_sec`, `now_func` injetável). | `tests/test_chat_rate_limit_cooldown.py` (20) |
| `Scripts/python_request_throttle.py` | ~140 | Classe `PythonRequestThrottle` (throttle global anti-rate-limit Python: `begin`/`remaining_seconds`/`commit`/`snapshot`, `now_func` injetável). Caller mantém o tight-loop SSE. `snapshot()` retorna `{last_ts, age_seconds}` (clamp 0 quando boot ou clock retrógrado). | `tests/test_python_request_throttle.py` (30) |
| `Scripts/web_search_throttle.py` | ~95 | Classe `WebSearchThrottle` (espaçamento global de buscas web: `reserve_slot` + `snapshot`, com `now_func` e `rng_func` injetáveis). `snapshot()` retorna `{last_started_at, last_interval_sec, age_seconds}` (clamp 0 quando boot ou clock retrógrado). | `tests/test_web_search_throttle.py` (15) |
| `Scripts/analisador_parsers.py` | ~330 | `detect_rate_limit_preview` (matcher injetável), `build_rate_limit_error_message`, `strip_code_fences`, `extract_json_block`, `normalize_llm_json`, `parse_json_block`, `json_looks_incomplete` (heurística de truncamento), `decode_json_string_fragment`, `extract_visible_llm_markdown` (remove `<think>…</think>`), `extract_search_queries_fallback` (parser tolerante de queries com `max_queries` injetável). | `tests/test_analisador_parsers.py` (64) |
| `Scripts/humanizer.py` | 124 | Módulo original (inalterado); testes ampliados com invariantes anti-robotização. | `tests/test_humanizer.py` (33) |
| `Scripts/error_scanner_helpers.py` | ~210 | Helpers puros para `/api/errors/{known,scan,claude_fix}`: filtragem canônica de snippets (`is_unwanted_snippet`, constante `UNWANTED_SNIPPET_KEYS`), conversão de snippets/exceções em entradas (`build_scan_match_entry`, `build_scan_error_entry`), prompt do Claude Code (`build_claude_fix_prompt`), body do POST proxy (`build_claude_fix_request_body`), payloads de `/api/errors/known` (`build_known_errors_missing_payload`, `build_known_errors_loaded_payload`, `build_known_errors_error_payload`) e linhas NDJSON do stream claude_fix (`build_claude_fix_empty_stream_lines`, `build_claude_fix_status_line`, `build_claude_fix_error_line`, `build_claude_fix_finish_line`). | `tests/test_error_scanner_helpers.py` (53) |

### Integrações já feitas (em caminho quente)
- `server.chat_completions` usa `request_source.is_analyzer_chat_request`, `server_helpers.combine_openai_messages`, `server_helpers.build_sender_label`, `server_helpers.wrap_paste_if_python_source`, `server_helpers.coalesce_origin_url`, `server_helpers.decode_attachment` (laço de anexos — IO/log preservados no call site), `server_helpers.resolve_browser_profile`, `server_helpers.resolve_chat_url` (sentinela `"None"` agora vira `None` — `storage.save_chat` e `browser.py:~4159` já eram defensivos contra "None" string, então a integração é byte-equivalente para todos os fluxos observáveis), `server_helpers.build_chat_task_payload` (substitui dict literal de 20 linhas), `server_helpers.build_queue_key` (no `_dispatch_chat_task`), `server_helpers.build_error_event` (2 sites em `_dispatch_chat_task`).
- `server._wait_chat_rate_limit_if_needed` usa `server_helpers.build_status_event` para o status `phase="chat_rate_limit_cooldown"`.
- `server.api_sync` usa `sync_dedup.SyncDedup.try_acquire` / `release`. Aliases `ACTIVE_SYNCS` e `ACTIVE_SYNCS_LOCK` permanecem como referências para `_SYNC_DEDUP._active` / `_SYNC_DEDUP._lock` — `len(ACTIVE_SYNCS)` em `/api/metrics::syncs_in_progress` (linha 1270) continua funcionando byte-a-byte; mensagem de log e shape do JSON 409 (`retry_after_seconds`, `elapsed_seconds`) preservados.
- `server.api_sync` usa `_normalize_optional_text_impl` para `sync_browser_profile` (2 sites — payload e snapshot), `_resolve_chat_url_impl(case_insensitive=True)` para o fallback de URL (preserva `or url` final como último-recurso) e `_format_requester_suffix_impl(nome_membro, id_membro)` para o sufixo `_quem` do log.
- `server.chat_completions` usa `_format_requester_suffix_impl` para o mesmo sufixo `_quem` (idiom unificado entre os dois handlers).
- `server._wait_python_request_interval_if_needed` é agora wrapper fino sobre o singleton `_PYTHON_REQUEST_THROTTLE: PythonRequestThrottle` (padrão B). State global (`_python_anti_rate_limit_last_ts` + lock) **encapsulado no módulo puro**; o wrapper mantém o tight-loop com `time.sleep` e o status SSE (`phase=python_anti_rate_limit_interval`) no call site. `compute_python_request_interval` agora é consumido apenas dentro de `python_request_throttle.py`. Alias `_python_anti_rate_limit_lock = _PYTHON_REQUEST_THROTTLE._lock` preservado para compat.
- 7 sites SSE em `server.py` migraram para `_build_status_event_impl`/`_build_error_event_impl`/`_build_markdown_event_impl`: `_wait_python_request_interval_if_needed`, `_wait_remote_user_priority_if_needed`, `_execute_single_browser_search`, `api_completions` SSE generator, `api_sync::sync_generate` (3x — 2 status + 1 markdown), `chat_completions` timeout.
- **`_iter_web_search_wait_messages` (sessão octodecies)**: agora recebe `phase_prefix` e `source_label` por argumento e yielda strings JSON via `_build_status_event_impl`. O consumer em `_execute_single_browser_search` deixa de mutar `phase`/`source`/`content` e simplifica para `stream_queue.put(raw_msg)`. Byte-equivalência com o pipeline antigo (legacy dict + mutate + `json.dumps`) coberta por 7 testes em `TestWebSearchWaitEventEquivalence`. **Não há mais dict-yielders SSE** em `server.py`.
- **`chat_completions::chat_meta` (sessão octodecies)**: `early_profile = (fin.get('chromium_profile') or "").strip()` migrado para `_normalize_optional_text_impl(fin.get('chromium_profile'))`. O `or` chain downstream (`early_profile or snapshot.get("chromium_profile", "")`) absorve `None`/`""` identicamente.
- `server.chat_completions` e `server._handle_browser_search_api` ambos usam `_extract_source_hint_impl(data, request.headers)` (cadeia de fallback unificada) e `_format_origin_suffix_impl(is_analyzer, source_hint)` (apenas em chat_completions). `_handle_browser_search_api` também migrou para o trio canônico `_format_requester_suffix_impl` + `_is_analyzer_chat_request_impl` + `_build_sender_label_impl` (mesma classificação de chat_completions, eliminando 5 linhas de string-match ad-hoc).
- `server._extract_rate_limit_details` usa `error_catalog.classify_from_text`.
- `analisador_prontuarios._resposta_eh_rate_limit` usa `error_catalog.classify_from_text` (com fallback defensivo).
- `analisador_prontuarios._verificar_rate_limit_no_markdown` / `_strip_code_fences` / `_extrair_bloco_json` / `_normalizar_json_llm` / `_parse_json_llm` / `_json_parece_incompleto` / `_decode_json_string_fragment` / `_extrair_markdown_visivel_llm` / `_extrair_queries_pesquisa_fallback` são agora wrappers finos sobre `analisador_parsers`. A camada de exceção (`ChatGPTRateLimitError`) e a decisão de rate-limit (`_resposta_eh_rate_limit`) permanecem no analisador — o módulo puro recebe o matcher por injeção e devolve o preview. `SEARCH_MAX_QUERIES` é injetado no wrapper, mantendo o módulo puro sem dependência de `config`. Fallback defensivo mantido se `analisador_parsers` não importar.
- `server._audit_event` usa `log_sanitizer.sanitize_mapping` antes de `json.dumps` (inclusive no fallback exception).
- `utils.log(source, msg)` usa `log_sanitizer.sanitize` antes de escrever (import defensivo; sem mascaramento se módulo não disponível).
- `server._is_ip_blocked` / `_register_rate_limit_hit` / `_register_login_failure` / `_clear_login_failures` são agora wrappers 1-liner sobre o singleton `_SECURITY_STATE: SecurityState`. Aliases `_security_lock`, `_rate_limit_hits`, `_blocked_ips`, `_failed_login_attempts` preservados para compat (tests/test_server_api.py reseta diretamente).
- `server._register_chat_rate_limit` / `_get_chat_rate_limit_remaining_seconds` são agora wrappers finos sobre o singleton `_CHAT_RATE_LIMIT_COOLDOWN: ChatRateLimitCooldown`. Backoff exponencial (2^strikes), clamp em 1800s e reset de strikes fora da janela foram preservados byte-a-byte. Alias `_chat_rate_limit_lock` mantido.
- `server._register_chat_rate_limit` normaliza `reason` via `error_catalog.format_reason(reason)` antes de logar. Reasons classificáveis ganham prefixo `[CODE]` (ex.: `[RATE_LIMIT] excesso de solicitações...`); reasons não classificáveis são logados sem prefixo (evita ruído `[INTERNAL_ERROR]`). Format do log preservado: `[CHAT_RATE_LIMIT] cooldown de Xs registrado. Motivo: …`. Contrato testado em `tests/test_rate_limit_integration.py::TestRegisterWrapperNormalizesReason`.
- `/api/metrics` expõe `chat_rate_limit: {remaining_seconds, strikes, until_ts}`, `security: {rate_limit_keys, blocked_ips, tracked_login_ips}`, `python_request_throttle: {last_ts, age_seconds}` e `web_search_throttle: {last_started_at, last_interval_sec, age_seconds}` (snapshots dos quatro singletons). `rate_limit_remaining_sec` legado preservado para compat com dashboards existentes.
- `/metrics` (Prometheus) ganha 6 gauges: `simulator_chat_rate_limit_remaining_sec`, `simulator_chat_rate_limit_strikes`, `simulator_security_blocked_ips`, `simulator_security_tracked_login_ips`, `simulator_python_request_throttle_age_sec`, `simulator_web_search_throttle_age_sec`. Atualização centralizada em `_update_rate_limit_prom_gauges()`. Silencioso se `prometheus_client` ausente.
- Filtro de log werkzeug (`No401AuthLog`) acrescenta sufixo explicativo ao 409 de `/api/sync` (dedup benigno 120s).
- `api_sync()` emite `[🔄 SYNC] ⚠️ sync_in_progress` com `elapsed` e `retry_after` antes de retornar 409, e inclui `retry_after_seconds` / `elapsed_seconds` no JSON.

### Integrações pendentes (NÃO feitas)
1. Catálogo em `browser._dismiss_rate_limit_modal_if_any` — último caminho que ainda usa string livre para rate-limit. **BLOQUEADO: requer aprovação do usuário** (toca `browser.py` async).
2. Concorrência por `browser_profile` — **BLOQUEADO: requer aprovação do usuário** (toca `browser.py` async).
3. **Integração inicial ChatGPT ↔ Gemini com pré-check de login no startup** — adicionar após as pendências acima, sem alterar a estrutura do refactor:
   - Gemini pode abrir no **`browser_profile=default`** (mesma diretriz de fallback já consolidada).
   - No **start do sistema**, validar sinais de sessão/logado no Gemini (heurística equivalente ao fluxo atual de validação de login do ChatGPT).
   - Se não houver login ativo no Gemini, **pausar a inicialização operacional** e exibir instruções claras no HTML aberto no Chromium para o usuário concluir login manual; retomar somente após confirmação/sinal de sessão válida.
   - Manter implementação incremental: primeiro contrato/telemetria/wrappers finos; mudanças em `browser.py` async somente com aprovação explícita.

### Requisitos consolidados (não-regredir)
- API key é autorização primária; IP/origem são defesa adicional.
- Bootstrap via `config.py` + `sync_github_settings.ps1` a partir dos `*.example.*`.
- Reset `admin/admin` SOMENTE em fresh install.
- `browser_profile` ponta-a-ponta com fallback `default`.
- `sync_github` autônomo, desacoplado de chat.
- Intervalo anti-rate-limit global para requests Python (server.py:~430 `_wait_python_request_interval_if_needed`).
- 409 em `/api/sync` é **dedup benigno** (janela 120s para mesmo chat_id/url). Log já autoexplicado.
- **Wrapper fino obrigatório**: qualquer extração futura deve deixar wrapper com nome/assinatura original.
- **Módulo puro**: sem `flask`, `playwright` nem `config` no import.
- **Log sanitizado**: `_audit_event` e `utils.log` já mascaram `api_key`, `Bearer`, cookies de sessão e caminhos `/home/<user>` ou `C:\Users\<user>`.

### Padrões validados nesta branch (9 extrações + 6 integrações bem-sucedidas)

**A. Helper puro sem state** (`request_source`, `error_catalog`, `server_helpers`, `browser_predicates`, `log_sanitizer`, `analisador_parsers`):
1. Criar `Scripts/<nome>.py` com funções puras.
2. Em server.py/browser.py: 1 import + wrappers 1-liner preservando nomes.
3. `tests/test_<nome>.py` com ≥3 casos por função pública.

**B. Helper puro com state** (`security_state`, `chat_rate_limit_cooldown`):
1. Criar classe encapsulando dicts + lock; construtor recebe thresholds; `now_func` injetável.
2. Server.py instancia singleton com valores de config; wrappers delegam.
3. Preservar aliases (`_security_lock = _STATE._lock`, `_chat_rate_limit_lock = _COOLDOWN._lock`) se tests externos acessam diretamente.
4. Módulo puro NÃO loga — o wrapper em `server.py` lê o retorno e emite o log (mantém a mesma linha `[CHAT_RATE_LIMIT] cooldown de Xs registrado.`).

**C. Integração em caminho quente** (`_extract_rate_limit_details`, `_audit_event`, `utils.log`, `_resposta_eh_rate_limit`, `_register_chat_rate_limit`, `_verificar_rate_limit_no_markdown`/`_parse_json_llm`):
1. Import no topo (try/except defensivo se arquivo pode rodar em ambiente truncado).
2. Substituir apenas a FONTE da decisão — preservar contrato de retorno e o formato da linha de log/exceção.
3. `tests/test_<integracao>.py` com cópia offline da função (Flask/Playwright bloqueiam import em testes).
4. Helper público e idempotente no módulo puro (ex.: `format_reason` em `error_catalog.py`) — permite chamada em dois pontos sem risco de duplicação.
5. Exceções específicas do domínio (ex.: `ChatGPTRateLimitError`) permanecem no arquivo impuro; o módulo puro retorna preview/decisão e o wrapper monta a exceção.

### Próximas opções (ordem recomendada por risco crescente)

**1. ~~Extrair `_wait_python_request_interval_if_needed` completo em padrão B~~ (FEITO em 2026-04-26 novendecies, commit `0904fe9`)**
- Classe `PythonRequestThrottle` em `Scripts/python_request_throttle.py` encapsula state + lock; wrapper em `server.py` mantém o tight-loop SSE. Padrão B aplicado (4 métodos: `begin`/`remaining_seconds`/`commit`/`snapshot`, `now_func` injetável). 27 testes offline cobrem curto-circuitos, tupla, view pura, commit, snapshot thread-safe e state-machine ponta-a-ponta.

**2. ~~Auditar e migrar handlers menores~~ (SATURADO em 2026-05-02, commit `6a3a3c5`)**
- Todos os handlers auditados. Último achado: segundo site MIME map inline em `serve_download` (lines ~1070-1083) migrado para `_resolve_download_content_type_impl`. Handlers `queue_status/failed/retry`, `logs_tail/stream`, `health_check`, `get_history` confirmados saturados — toda lógica pura já extraída em sessões anteriores.

**3. ~~Migrar 2 dict-yielders em `_iter_web_search_wait_messages`~~ (FEITO em 2026-04-26 octodecies, commit `14ffcf0`)**
- Refactor producer↔consumer concluído: `_iter_web_search_wait_messages` recebe `phase_prefix`/`source_label` e yielda strings via `build_status_event(content, **extras)`. Consumer só faz `stream_queue.put(raw_msg)`. Byte-equivalência coberta por 7 testes em `TestWebSearchWaitEventEquivalence`.

**4. `api_sync` `_url_info` / `_cid_info` (BAIXO valor)**
- Sites únicos, one-liners óbvios. Não recomendado extrair — daria pouco ganho e adicionaria indireção desnecessária.

**5. ~~Expor snapshot de `WebSearchThrottle` em `/api/metrics` + gauge Prometheus~~ (FEITO em 2026-04-26 tervicies)**
- `WebSearchThrottle.snapshot()` agora retorna `{last_started_at, last_interval_sec, age_seconds}` (clamp 0 quando boot ou clock retrógrado). `/api/metrics` ganha chave `web_search_throttle`; `/metrics` (Prometheus) ganha gauge `simulator_web_search_throttle_age_sec` atualizado em `_update_rate_limit_prom_gauges`. +3 testes em `TestSnapshot::test_snapshot_age_*`.

**6. Integrar catálogo em `browser._dismiss_rate_limit_modal_if_any` (ALTO risco, BLOQUEADO)**
- Último caminho que ainda usa string livre para rate-limit.
- **BLOQUEADO**: toca `browser.py` async/Playwright → requer aprovação do usuário.

**7. Plano de concorrência por `browser_profile` (ALTO risco, BLOQUEADO)**
- Entregável inicial: documento em `docs/concurrency_per_profile.md` (sem código) — **FEITO** em 2026-04-26 duovicies (`511d667`).
- Próximo passo: alteração em `browser.py` com aprovação explícita.

**8. ~~Auditar/cobrir endpoints menores ainda sem testes offline~~ (FEITO em 2026-04-26 quatervicies)**
- Auditoria identificou 2 idioms duplicados; extraídos `safe_int(value, default)` e `safe_snapshot_stats(queue_obj)` em `server_helpers.py`. 5 sites migrados (`queue_status`, `queue_failed`, `queue_failed_retry`, `logs_tail`, `api_metrics`). +17 testes em `tests/test_server_helpers.py::TestSafeInt`/`TestSafeSnapshotStats` cobrindo coerções, fallback de exceção, ausência de método, valores `None`/`""`/`bool`/float, defaults negativos.

**9. Modularização do `server.py` (P2 #21, MÉDIO risco) — PARCIALMENTE CONCLUÍDA**
- Primeira fase entregue: Blueprints `server_observabilidade.py` (queue_*, logs_*) e
  `server_recursos.py` (serve_download, get_avatar, robots_txt). 8 rotas movidas.
  server.py: 2731 → 2529 linhas. Corrigido NameError latente em logs_stream.
- Próxima fase (quando aprovada): auth parcial (logout, user_info, update_pass)
  e busca (api_web_search_test) — requerem extração de check_auth para módulo compartilhado.
  Rotas complexas (chat_completions, api_sync) permanecem em server.py.

### Prompt de retomada (COPIAR EXATAMENTE EM NOVO CHAT)

```
Continue o refactor do /home/user/chatGPT_Simulator na branch
claude/continue-refactor-updates-wvOqd.
Leia APENAS a seção "PONTO DE RETOMADA (última atualização em 2026-05-02
unquadragies+)" em REFACTOR_PROGRESS.md — é autocontida.

Estado atual: commit `d8762df`.
Suite offline: **700 passed em 17 arquivos**.

Opção D (browser._dismiss_rate_limit_modal_if_any) foi CONCLUÍDA:
- JS captura modal.innerText antes de clicar; log usa format_reason com
  prefixo [RATE_LIMIT]; import defensivo com fallback str() se ausente.

Opção 9 (modularização server.py) foi PARCIALMENTE CONCLUÍDA (fase 1):
- Scripts/server_observabilidade.py: Blueprint com queue_status, queue_failed,
  queue_failed_retry, logs_tail, logs_stream (corrigiu NameError latente).
- Scripts/server_recursos.py: Blueprint com serve_download, get_avatar, robots_txt.
- server.py: 2731 → 2529 linhas. 5 imports _impl exclusivos removidos.

Próximas opções (escolher UMA com aprovação):

**9b. Fase 2 de modularização** (MÉDIO risco) — extrair check_auth para módulo
   compartilhado e criar blueprints para auth parcial (logout/user_info/
   update_pass) e busca (api_web_search_test). Requer aprovação explícita.

**A. Log sanitizer em _audit_event** (ver backlog P1 #17) — integrar em
   utils.file_log e _audit_event. Requer aprovação explícita.

Regras obrigatórias:
(a) escolher UMA opção e executar do começo ao fim;
(b) NÃO tocar browser.py/analisador_prontuarios.py sem aprovação explícita;
(c) manter os 700 testes offline passando + eventuais novos;
(d) ANTES do commit/push final, ATUALIZAR esta seção com novo commit hash,
    contagem de testes e próxima opção;
(e) commit com título em PT-BR no imperativo;
(f) push para claude/continue-refactor-updates-wvOqd.

Se encontrar algo inesperado em server.py, PARAR e pedir confirmação antes
de editar.
```

### Checklist de "antes de terminar a sessão" (rodar sempre)
- [ ] Suite offline passa: `python -m pytest tests/test_humanizer.py tests/test_shared_queue.py tests/test_selectors_smoke.py tests/test_request_source.py tests/test_error_catalog.py tests/test_server_helpers.py tests/test_browser_predicates.py tests/test_rate_limit_integration.py tests/test_log_sanitizer.py tests/test_analisador_rate_limit.py tests/test_audit_sanitization.py tests/test_security_state.py tests/test_chat_rate_limit_cooldown.py tests/test_analisador_parsers.py tests/test_sync_dedup.py tests/test_python_request_throttle.py tests/test_web_search_throttle.py` (esperado: **700 passed**).
- [ ] `python3 -c "import ast; ast.parse(open('Scripts/server.py').read())"` OK.
- [ ] `python3 -c "import ast; ast.parse(open('Scripts/browser.py').read())"` OK.
- [ ] `python3 -c "import ast; ast.parse(open('Scripts/analisador_prontuarios.py').read())"` OK.
- [ ] `python3 -c "import ast; ast.parse(open('Scripts/utils.py').read())"` OK.
- [ ] `python3 -c "import ast; ast.parse(open('Scripts/error_catalog.py').read())"` OK.
- [ ] `python3 -c "import ast; ast.parse(open('Scripts/web_search_throttle.py').read())"` OK.
- [ ] Seção "PONTO DE RETOMADA" atualizada com commits novos, contagem de testes, próxima opção.
- [ ] `git status` limpo e último commit pushado para `origin/claude/continue-refactor-updates-wvOqd`.

### Histórico de sessões (para auditoria — NÃO precisa reler)
- **2026-04-22** (sessão original) — `1f3374b`: extração de `request_source.py`.
- **2026-04-22 bis** — `0c6216e`: replanejamento, DoD refinados, sem código.
- **2026-04-22 ter** — `3334bf6`: catálogo central de erros (Lote P0 passo 1).
- **2026-04-22 quater** — `c5c45dc` → `3646da1` → `a87a61a` → `be785a3`: passos 2-5 do Lote P0 + log_sanitizer + unificação analisador.
- **2026-04-22 quinquies** — `3b06256`: gravação do primeiro PONTO DE RETOMADA autocontido.
- **2026-04-22 sexies** — `5dc4928` + `67d3b39`: integração de `log_sanitizer` em `_audit_event` e `utils.log`; correção/autoexplicação do 409 em `/api/sync`; extração de `SecurityState`. 240 testes offline passando. Merge via PR #564 em `main`.
- **2026-04-22 septies** — `ea0b197`: extração de `ChatRateLimitCooldown` (backoff exponencial 2^strikes, clamp 1800s) para módulo puro. 257 testes offline passando (+17 novos).
- **2026-04-22 octies** — `addc3d6`: integração de `error_catalog.format_reason` em `_register_chat_rate_limit`; helper idempotente no catálogo para prefixar `[CODE] <reason>` em logs operacionais; 273 testes offline passando (+16 novos).
- **2026-04-22 nonies** — `d8636dc`: expõe `ChatRateLimitCooldown.snapshot()` e `SecurityState.snapshot()` em `/api/metrics`; adiciona 4 gauges Prometheus (`simulator_chat_rate_limit_remaining_sec`, `simulator_chat_rate_limit_strikes`, `simulator_security_blocked_ips`, `simulator_security_tracked_login_ips`); testes de contrato JSON-serializable nos snapshots. 279 testes offline passando (+6 novos).
- **2026-04-22 decies** — `393af83`: extração de parsers puros (`Scripts/analisador_parsers.py`): `detect_rate_limit_preview`, `build_rate_limit_error_message`, `strip_code_fences`, `extract_json_block`, `normalize_llm_json`, `parse_json_block`. Wrappers finos preservam nomes e assinaturas em `analisador_prontuarios.py`. Exceção `ChatGPTRateLimitError` permanece no analisador; matcher de rate-limit é injetável. 311 testes offline passando (+32 novos).
- **2026-04-22 undecies** — `4d84ab1`: extensão de `analisador_parsers.py` com `json_looks_incomplete`, `decode_json_string_fragment` e `extract_visible_llm_markdown`. Wrappers finos em `analisador_prontuarios.py` preservam nomes. README.md atualizado com inventário de módulos puros e comando offline. 333 testes offline passando (+22 novos).
- **2026-04-22 duodecies** — `403427b`: extração de `extract_search_queries_fallback` para `analisador_parsers.py` com `max_queries` injetável (módulo puro sem dependência de `config`). Wrapper em `analisador_prontuarios.py` passa `SEARCH_MAX_QUERIES` ao delegar. 343 testes offline passando (+10 novos).
- **2026-04-22 terdecies** — branch `claude/fix-rate-limit-interval-QmRpK`, `68c00b6`: extração de 5 helpers puros de `chat_completions`: `is_analyzer_chat_request` (em `request_source.py`); `combine_openai_messages`, `build_sender_label`, `wrap_paste_if_python_source`, `coalesce_origin_url` (em `server_helpers.py`). Integração de todos em `server.chat_completions` preservando assinaturas. README.md + REFACTOR_PROGRESS.md atualizados. 369 testes offline passando (+26 novos).
- **2026-04-25 quattuordecies** — branch `claude/create-log-sanitization-script-QQ56a`, `ce825b5`: extração de 3 helpers puros de `chat_completions`: `decode_attachment` (parse de `{"name","data"}` em base64 com strip de prefixo `data:...,`), `resolve_chat_url` (sentinela `"None"` tratado como ausência), `resolve_browser_profile` (colapso de `(value or '').strip() or None`). Integração de `decode_attachment` (laço de anexos com IO/log preservados) e `resolve_browser_profile` (resolução de `effective_browser_profile`) em `server.chat_completions`. `resolve_chat_url` testado mas NÃO integrado — fluxo histórico aceita `"None"` literal como URL persistida; integração requer auditoria do downstream `storage.save_chat`/`chat_task_payload['url']`. 390 testes offline passando (+21 novos).
- **2026-04-25 quindecies** — branch `claude/create-log-sanitization-script-QQ56a`, 4 ciclos contínuos:
  1. `905fc45`: integração de `resolve_chat_url` em `chat_completions` após auditoria do downstream (`storage.save_chat` e `browser.py:~4159` já eram defensivos contra `"None"` string, então a integração é byte-equivalente para todos os fluxos observáveis e remove um bug histórico em que `"None"` literal podia ser persistido). Print de status simplificado para `if url:`.
  2. `0387e9f`: extração de `normalize_optional_text` (idiom `(v or '').strip() or None`), `build_queue_key` (chave `f"{chat_id}:{time.time_ns()}"`) e `build_chat_task_payload` (builder do dict de 20 linhas enviado a `browser_queue`). Integração dos 3 em `chat_completions`/`_dispatch_chat_task`. +18 testes.
  3. `26bfab3`: extração de `Scripts/sync_dedup.py` com classe `SyncDedup` (padrão B: state + lock + `now_func` injetável). `try_acquire`/`release`/`active_count`/`snapshot`. Integração em `api_sync` substituindo `with ACTIVE_SYNCS_LOCK: ...` (mantém aliases `ACTIVE_SYNCS`/`ACTIVE_SYNCS_LOCK` para compat com `len(ACTIVE_SYNCS)` em `/api/metrics::syncs_in_progress`). +20 testes (incl. teste de threading com Lock real).
  4. `2899d58`: extração de `build_error_event` e `build_status_event`. Integração em 3 sites (2 em `_dispatch_chat_task`, 1 em `_wait_chat_rate_limit_if_needed`); restantes ≥6 sites listados como Opção 1 da próxima sessão. +9 testes.
  Total: **437 testes offline passando** (+47 novos). 5 commits + docs commit.
- **2026-04-25 sedecies** — branch `claude/create-log-sanitization-script-QQ56a`, 4 ciclos contínuos:
  5. `76d2f40`: migração de 6 sites SSE de `json.dumps({"type":"status"/"error",...}, ensure_ascii=False)` para `_build_status_event_impl`/`_build_error_event_impl`. Sites: `_wait_python_request_interval_if_needed`, `_wait_remote_user_priority_if_needed`, `_execute_single_browser_search`, `api_completions` SSE, `api_sync::sync_generate` (2x), `chat_completions` timeout. 2 dict-yielders em `_iter_web_search_wait_messages` mantidos para próxima sessão. Sem novos testes (contrato já coberto).
  6. `47b7ed0`: extensão de `resolve_chat_url` com `case_insensitive=False` opcional; integração em `api_sync` (substitui `str(url).lower() == "none"`). +2 migrações de `(v or '').strip() or None` para `normalize_optional_text`. +3 testes.
  7. `bcaa716`: extração de `format_requester_suffix` (idiom `, por "<nome>" (id_membro: "<id>")` duplicado em chat_completions e api_sync). Migração nos 2 sites. +6 testes.
  8. `6ca399a`: extração de `compute_python_request_interval(pmin, pmax, profile_count, *, rng=None)` — decisão pura `(base, target)` com `random.uniform` e divisão pelo profile_count, `rng` injetável, curto-circuito histórico preservado. Integração em `_wait_python_request_interval_if_needed`. State global e tight-loop NÃO extraídos (Opção 4 da próxima sessão). +8 testes.
  Total: **454 testes offline passando** (+17 novos). 4 commits + docs commit.
- **2026-04-26 octodecies** (branch `claude/focused-einstein-Ol7Hd`) — 1 ciclo de Opção 3 (migração de dict-yielders SSE):
  12. `14ffcf0`: `_iter_web_search_wait_messages` deixa de yieldar dicts puros e passa a receber `phase_prefix` + `source_label` por argumento, yieldando strings JSON via `_build_status_event_impl`. Consumer em `_execute_single_browser_search` simplifica para `stream_queue.put(raw_msg)` (remove 4 linhas de mutação in-place + `json.dumps`). Migração paralela: `chat_completions::chat_meta` migra `(fin.get('chromium_profile') or "").strip()` para `_normalize_optional_text_impl(...)`. +7 testes em `TestWebSearchWaitEventEquivalence` cobrindo byte-equivalência com o pipeline antigo (legacy dict + mutate + `json.dumps`) para `web` e `uptodate`. Total: **478 testes offline passando**. Auditoria de Opção 2 confirmou que `chat_completions`, `api_sync`, `_handle_browser_search_api` já estão totalmente migrados; `send_manual_whatsapp_reply` mantém formato `_quem` distinto (`(id={id})` vs `(id_membro: "{id}")`) — migração rejeitada por preservar contrato implícito de log.
- **2026-04-26 novendecies** (branch `claude/focused-einstein-Ol7Hd`) — 1 ciclo de Opção 1 (extração padrão B):
  13. `0904fe9`: extração de `Scripts/python_request_throttle.py` com classe `PythonRequestThrottle` (4 métodos: `begin`/`remaining_seconds`/`commit`/`snapshot`, `now_func` injetável). State global (`_python_anti_rate_limit_last_ts` + lock) sai de `server.py` e é encapsulado no módulo puro; wrapper em `_wait_python_request_interval_if_needed` mantém o tight-loop SSE com `time.sleep` e `_build_status_event_impl(phase="python_anti_rate_limit_interval", ...)` no call site (módulo puro NÃO emite SSE direto). Curto-circuito histórico (`pmin/pmax <= 0` ou primeira chamada) preservado byte-equivalente via `begin()` retornar `None`. Alias `_python_anti_rate_limit_lock` mantido para compat. Import morto `compute_python_request_interval` removido de `server.py` (consumido apenas dentro do módulo puro). +27 testes em `tests/test_python_request_throttle.py` cobrindo todos os 4 métodos públicos, snapshot thread-safe sob 4 threads concorrentes, state-machine ponta-a-ponta e equivalência com a implementação histórica. Total: **505 testes offline passando** em 16 arquivos.
- **2026-04-26 vicies** (esta sessão, branch `claude/focused-einstein-Ol7Hd`) — 1 ciclo de Opção A (observabilidade):
  14. `a8eca94`: `PythonRequestThrottle.snapshot()` agora retorna `{last_ts, age_seconds}` (extensão); `/api/metrics` expõe `python_request_throttle` snapshot; `/metrics` (Prometheus) ganha gauge `simulator_python_request_throttle_age_sec` (atualizado em `_update_rate_limit_prom_gauges`). Clamp 0 em `age_seconds` quando `last_ts == 0` (boot) ou quando o relógio retrocede (ajuste NTP). +3 testes em `TestSnapshot::test_snapshot_age_*` cobrindo avanço de relógio, never-set e clock retrógrado (2 casos pré-existentes ajustados para o novo shape). Suite offline: **508 passed** em 16 arquivos.
- **2026-04-26 unvicies** (esta sessão, branch `work`) — 1 ciclo de Opção B (extração padrão B para web search throttle):
  15. `939d904`: extração de `Scripts/web_search_throttle.py` com classe `WebSearchThrottle` (`reserve_slot` + `snapshot`, com `now_func` e `rng_func` injetáveis). `server._reserve_web_search_slot` migra para wrapper fino sobre singleton `_WEB_SEARCH_THROTTLE`, preservando contrato histórico de `wait_ctx` (`interval_sec`, `scheduled_start_at`, `wait_seconds`, `requested_at`) e mantendo aliases de compat (`_web_search_timing_lock`, `_web_search_last_started_at`, `_web_search_last_interval_sec`). +9 testes em `tests/test_web_search_throttle.py` cobrindo first-call sem espera, agendamento com cooldown, clamps/normalização e concorrência. Suite offline: **517 passed** em 17 arquivos. Próxima opção recomendada: integrar snapshot do WebSearchThrottle em `/api/metrics`/Prometheus (baixo risco) antes de qualquer mudança em `browser.py`.
- **2026-04-26 duovicies** (esta sessão, branch `work`) — 1 ciclo de Opção C (documentação de concorrência por profile, sem código runtime):
  16. `511d667`: criado `docs/concurrency_per_profile.md` com proposta incremental em 3 fases (módulo puro padrão B → wrapper fino em `server.py` → observabilidade), failure modes, política inicial de limites por `browser_profile`, plano de testes offline e DoD. README atualizado para incluir o novo documento em `docs/` e refletir suite offline atual (`517 passed`, 17 arquivos) + inventário de módulos puros incluindo `sync_dedup`, `python_request_throttle` e `web_search_throttle`. Nenhuma alteração em `browser.py`/`analisador_prontuarios.py` nesta sessão.
- **2026-04-26 trevicies** (esta sessão, branch `work`) — 1 ciclo de Opção E (observabilidade do WebSearchThrottle):
  17. `b68de2d`: `WebSearchThrottle.snapshot()` estendido para retornar `age_seconds` (clamp 0 em bootstrap/clock retrógrado). `server.api_metrics` agora expõe `web_search_throttle` e `/metrics` adiciona gauge `simulator_web_search_throttle_age_sec` em `_update_rate_limit_prom_gauges`. Testes `tests/test_web_search_throttle.py` ampliados com 2 casos de idade (`advances`/`clamp`). Suite offline: **519 passed** em 17 arquivos. Próxima opção recomendada: voltar à Opção 2 (auditoria de handlers menores), mantendo `browser.py` fora de escopo sem aprovação.
- **2026-04-26 quattuorvicies** (esta sessão, branch `work`) — 1 ciclo de Opção 2 (auditoria de handlers menores, etapa 1):
  18. `fb2f476`: extraído `extract_requester_identity(data)` para `Scripts/server_helpers.py` (módulo puro), normalizando `nome_membro_solicitante`/`id_membro_solicitante` via `normalize_optional_text`. Migração de 3 handlers em `server.py` (`api_sync`, `_handle_browser_search_api`, `chat_completions`) para usar o helper e reduzir idiom duplicado `data.get(... ) or None`. `tests/test_server_helpers.py` ganhou classe `TestExtractRequesterIdentity` (+5 casos: strip, vazios, ausentes, duck-typed `.get`, entrada inválida). Suite offline: **524 passed** em 17 arquivos. Próximo subpasso recomendado: aplicar helper em `send_manual_whatsapp_reply` preservando formato de log legado `(id={id})`.
- **2026-04-26 quinvicies** (esta sessão, branch `work`) — 1 ciclo de Opção 2 (auditoria de handlers menores, etapa 2):
  19. `865225b`: `send_manual_whatsapp_reply` migrado para `_extract_requester_identity_impl(data)` (remove duplicação de `data.get(...)`) mantendo explicitamente o formato histórico de log `_quem` desta rota (` por "<nome>" (id=<id>)`). README atualizado no inventário de `server_helpers` para citar extração de identidade do solicitante. Sem mudanças em `browser.py`/`analisador_prontuarios.py`. Suite offline permanece **524 passed** em 17 arquivos.
- **2026-04-26 sexvicies** (esta sessão, branch `work`) — 1 ciclo de Opção 2 (auditoria de handlers menores, etapa 3):
  20. `2dd6add`: normalização de campos opcionais com `_normalize_optional_text_impl` em `api_chat_lookup` (`origin_url`/`url_atual`), `api_chat_delete_local` (`chat_id`/`origin_url`) e `api_delete` (`url`/`chat_id`). Objetivo: reduzir idioms duplicados `data.get(...) or ""` e padronizar trim/None-collapse nas rotas de manutenção local. Suite offline preservada: **524 passed** em 17 arquivos. Próximo subpasso recomendado: auditar `api_completions` legado para extrações puras adicionais.
- **2026-04-26 septvicies** (esta sessão, branch `work`) — hotfix de boot/compatibilidade de nomes:
  21. `f554638`: corrigido `NameError: WebSearchThrottle is not defined` no boot (`main.py -> import server`) movendo `from web_search_throttle import WebSearchThrottle` para antes da instanciação `_WEB_SEARCH_THROTTLE = WebSearchThrottle()` em `server.py`. Incluída verificação explícita de ordem de símbolos (`order_ok=True`) e parse AST pós-mudança. README atualizado com nota de troubleshooting no inventário do módulo. Suite offline permaneceu **524 passed**.
- **2026-04-26 octovicies** (esta sessão, branch `work`) — hotfix de teste do Codex (`import server` em ambiente sem rede):
  22. `fd5749e`: `utils.py` agora trata `cryptography` como dependência opcional no import do módulo (fallback seguro com `HAS_CRYPTOGRAPHY=False`), evitando `ModuleNotFoundError` durante `import server` quando `pip install` falha por proxy 403. `ensure_certificates()` passou a validar explicitamente a dependência e levantar erro claro apenas no uso da geração de certificado. Validação executada: `import server` passou neste ambiente (com warnings de pip), além de AST e suite offline (**524 passed**).
- **2026-04-26 novovicies** (esta sessão, branch `work`) — hotfix do ruído de teste Codex (warnings de pip/proxy):
  23. `f91b077`: `utils.check_and_install()` agora respeita `SIMULATOR_AUTO_INSTALL_DEPS` (default **0**). Sem essa flag, dependências ausentes apenas registram mensagem informativa e não tentam `pip install`, eliminando warnings de proxy 403 durante `import server`. Com `SIMULATOR_AUTO_INSTALL_DEPS=1`, comportamento antigo de auto-instalação é preservado. Validações: `import_server_ok` sem tracebacks + AST + suite offline **524 passed**.
- **2026-04-26 tricies** (esta sessão, branch `work`) — Opção 2 (auditoria de handlers menores, etapa 4):
  24. `ec4a4a7`: extraídos helpers puros em `server_helpers.py` para payloads de manutenção: `resolve_lookup_origin_url(data)`, `extract_chat_delete_local_targets(data)` e `extract_delete_request_targets(data)`. `server.py` migrou `api_chat_lookup`, `api_chat_delete_local` e `api_delete` para wrappers finos nesses helpers (reduzindo idioms duplicados e padronizando trim/None-collapse). `tests/test_server_helpers.py` ampliado com 9 novos casos cobrindo prioridade/fallback/entrada inválida dos 3 helpers. Suite offline atualizada: **533 passed** em 17 arquivos.
- **2026-04-26 untricies** (esta sessão, branch `work`) — Opção 2 (auditoria de handlers menores, etapa 5):
  25. `bd9bd7e`: extraídos `extract_menu_url(data)` e `extract_menu_execute_payload(data)` para `server_helpers.py`, migrando `menu_options` e `menu_execute` em `server.py` para wrappers finos. Novos testes em `tests/test_server_helpers.py` cobrem normalização, opcionais e entrada inválida dos helpers de menu (+5 casos). Suite offline atualizada: **538 passed** em 17 arquivos. Próximo subpasso recomendado: analisar `/api/web_search/test` (bloco grande de documentação HTML) para possíveis extrações puras sem alterar comportamento.
- **2026-04-26 duotricies** (esta sessão, branch `claude/focused-einstein-GcWqc`) — Opção 2 (auditoria de handlers menores, etapa 6):
  26. `6f4bbeb`: extraído `extract_web_search_test_params(data)` em `server_helpers.py` para normalizar `q`/`api_key` de `/api/web_search/test` (query string) com o mesmo idiom canônico de trim + fallback vazio. `server.py` migrou `api_web_search_test` para wrapper fino (`query, api_key = _extract_web_search_test_params_impl(request.args)`), sem alterar HTML/fluxo de execução da rota. `tests/test_server_helpers.py` ganhou 2 casos novos cobrindo normalização e entrada inválida do novo helper. Suite offline atualizada: **563 passed** em 17 arquivos. Próximo subpasso recomendado: auditar `api_completions` legado para extrações puras adicionais (sem tocar `browser.py`).
- **2026-04-26 tertricies** (esta sessão, branch `claude/focused-einstein-GcWqc`) — Opção 2 (auditoria de handlers menores, etapa 7):
  27. `ff33a2b`: extraído `build_web_search_test_task(query, stream_queue)` em `server_helpers.py` e migrado `api_web_search_test` em `server.py` para wrapper fino ao enfileirar ação `SEARCH`, removendo dict literal inline sem alterar contrato (`action/query/stream_queue`). `tests/test_server_helpers.py` ganhou 1 caso novo validando shape exato do payload. Suite offline atualizada: **564 passed** em 17 arquivos. Próximo subpasso recomendado: extrair parser puro de mensagens do loop de `/api/web_search/test` (sem tocar `browser.py`).
- **2026-04-26 quatertricies** (esta sessão, branch `claude/focused-einstein-GcWqc`) — Opção 2 (auditoria de handlers menores, etapa 8):
  28. `efa8282`: extraído `build_web_search_test_stream_response(raw_msg, query)` em `server_helpers.py` para interpretar mensagens `searchresult`/`error` do loop de `/api/web_search/test` e manter o handler como wrapper fino. `server.py` migrou o while para delegar parsing/branching ao helper puro, preservando timeout e fallback de fim de stream. `tests/test_server_helpers.py` ganhou 4 casos novos (success, error, tipo não terminal e JSON inválido com `JSONDecodeError` preservado). Suite offline atualizada: **568 passed** em 17 arquivos. Próximo subpasso recomendado: pausar opção 2 e validar priorização com o usuário entre continuar em handlers menores vs. desbloquear opção D (browser.py requer aprovação).
- **2026-04-26 quintricies** (esta sessão, branch `claude/focused-einstein-GcWqc`) — Opção 2 (auditoria de handlers menores, etapa 9):
  29. `547d2b5`: extraído `build_web_search_test_error_payload(query, error)` em `server_helpers.py` para padronizar o formato de erros JSON de `/api/web_search/test` e reduzir duplicação entre o parser de stream e os retornos de timeout/sem resposta. `server.py` migrou os dois retornos de erro finais para wrappers finos com o novo helper. `tests/test_server_helpers.py` ganhou 2 casos novos para o payload de erro padronizado e o teste de stream-error passou a afirmar via helper. Suite offline atualizada: **570 passed** em 17 arquivos. Próximo subpasso recomendado: extrair helper puro para o envelope de timeout/fallback da rota ou encerrar opção 2 e pedir aprovação para opção D.
- **2026-04-26 sextricies** (esta sessão, branch `claude/focused-einstein-GcWqc`) — Opção 2 (auditoria de handlers menores, etapa 10):
  30. `35502c5`: extraídos `build_web_search_test_timeout_payload(query)` e `build_web_search_test_no_response_payload(query)` em `server_helpers.py`, ambos delegando ao payload canônico de erro da rota. `server.py` migrou os retornos terminais (timeout 504 e sem resposta) para wrappers finos com esses helpers, removendo strings inline duplicadas do call site. `tests/test_server_helpers.py` ganhou 2 casos novos cobrindo os dois payloads terminais. Suite offline atualizada: **572 passed** em 17 arquivos. Próximo subpasso recomendado: concluir opção 2 com extração de helper puro para seleção de HTTP status terminal (sem tocar `browser.py`) ou validar se já podemos encerrar opção 2.
- **2026-04-26 (esta sessão, branch `claude/continue-refactor-updates-wvOqd`) — Opção 2 (auditoria de handlers menores, etapa 11):**
  41. `4e79c24`: extraídos `resolve_logs_tail_lines_limit(raw_lines)` (clamp canônico 10..800 com fallback 120) e `parse_from_end_flag(raw_value)` (idiom canônico `str(v).strip().lower() not in {"0","false","no"}`) para `Scripts/server_helpers.py`. `server.py` migrou `logs_tail` e `logs_stream` para wrappers finos desses helpers sem alterar IO/SSE. `tests/test_server_helpers.py` ganhou 16 casos novos cobrindo limites, fallback e variantes truthy/falsy. Suite offline atualizada: **588 passed** em 17 arquivos. Próxima opção recomendada: **encerrar a opção 2 por saturação dos idioms restantes (`queue_*`, `health_check`, `get_history`) e pedir aprovação explícita para a opção D antes de tocar `browser.py`**.
- **2026-04-26 (esta sessão, branch `claude/continue-refactor-updates-wvOqd`) — Opção 2 (auditoria de handlers menores, etapa 12):**
  42. `61cc8ea`: extraídos `extract_queue_failed_limit(raw_limit)` e `extract_queue_failed_retry_index(data)` para `Scripts/server_helpers.py`, reduzindo os idioms de parse numérico nos handlers `queue_failed` e `queue_failed_retry` em `server.py` a wrappers finos. `tests/test_server_helpers.py` ganhou 5 casos novos cobrindo default, parse de string e payload inválido (mapping e não-mapping). Suite offline atualizada: **593 passed** em 17 arquivos. Próxima opção recomendada: **encerrar a opção 2 (restante muito trivial em `health_check`/`get_history`) e pedir aprovação explícita para D antes de qualquer edição em `browser.py`**.
- **2026-04-26 (esta sessão, branch `claude/continue-refactor-updates-wvOqd`) — Opção 2 (auditoria de handlers menores, etapa 13):**
  43. `abfe12d`: extraído `advance_health_ping_state(ping_count, last_log_time, now, interval_sec=300)` para `Scripts/server_helpers.py`, isolando a transição pura de estado de `health_check` (incremento, janela de log e reset de contador). `server.py` migrou o handler `/health` para wrapper fino sobre esse helper, mantendo log e resposta HTTP inalterados. `tests/test_server_helpers.py` ganhou 5 casos novos cobrindo janela, borda de intervalo, valores defensivos (`None`) e `interval_sec` customizável. Suite offline atualizada: **598 passed** em 17 arquivos. Próxima opção recomendada: **encerrar a opção 2 (ganho residual mínimo em `get_history`) e pedir aprovação explícita para D antes de editar `browser.py`**.
- **2026-04-26 (esta sessão, branch `claude/continue-refactor-updates-wvOqd`) — Opção 2 (auditoria de handlers menores, etapa 14):**
  44. `4828ff8`: extraído `build_unauthorized_payload()` para `Scripts/server_helpers.py` e migrados 8 handlers protegidos em `server.py` para usar o payload 401 canônico via wrapper fino (`get_history`, `api_chat_lookup`, `api_chat_delete_local`, `api_sync`, `api_delete`, `_handle_browser_search_api`, `send_manual_whatsapp_reply`, `chat_completions`). Incluído `Scripts/codex_autoflow.sh` para execução contínua de comandos sem pausas interativas (fluxo automatizado local). `tests/test_server_helpers.py` ganhou 2 casos novos garantindo shape imutável do payload e retorno de novo dict por chamada. Suite offline atualizada: **600 passed** em 17 arquivos. Próxima opção recomendada: **opção 2 saturada; pedir aprovação explícita para D antes de qualquer edição em `browser.py`**.
- **2026-04-26 (esta sessão, branch `claude/continue-refactor-updates-wvOqd`) — Hotfix de regressão + Opção 2 (etapa 15):**
  45. `fba6626`: corrigido `NameError: _normalize_source_hint_impl is not defined` em `/v1/chat/completions` e `_handle_browser_search_api` restaurando o alias de import `normalize_source_hint as _normalize_source_hint_impl` no bloco `from server_helpers import (...)` de `server.py`. Adicionado teste smoke em `tests/test_server_helpers.py` que valida estaticamente a presença do alias no arquivo `Scripts/server.py`, prevenindo regressão idêntica em futuros refactors. Suite offline atualizada: **601 passed** em 17 arquivos. Próxima opção recomendada: **com opção 2 saturada, pedir aprovação explícita para D antes de editar `browser.py`**.
- **2026-04-27 (esta sessão, branch `claude/continue-refactor-updates-wvOqd`) — Hotfix de boot (etapa 16):**
  46. `ec36b34`: corrigido `ImportError: cannot import name 'normalize_source_hint' from 'server_helpers'` no boot (`main.py -> import server`) reintroduzindo a função pura `normalize_source_hint(value)` em `Scripts/server_helpers.py` com contrato canônico defensivo (`str(v).strip().lower()` + fallback vazio em exceção). Testes `tests/test_server_helpers.py` ampliados com 4 casos (`whitespace/case`, `None`, numérico e `__str__` com exceção). Suite offline atualizada: **605 passed** em 17 arquivos. Próxima opção recomendada: **opção 2 encerrada; pedir aprovação explícita antes de qualquer edição em `browser.py` (opção D)**.
- **2026-04-27 (esta sessão, branch `claude/continue-refactor-updates-wvOqd`) — Automação do agente (etapa 17):**
  47. `863a333`: `Scripts/auto_dev_agent.py` recebeu modo mais autônomo para ciclos contínuos de refactor: (a) orçamento aproximado de tokens por janela com cooldown + countdown automático ao atingir limite (`AUTODEV_AGENT_TOKEN_BUDGET`, `AUTODEV_AGENT_TOKEN_WINDOW_SEC`, `AUTODEV_AGENT_TOKEN_COOLDOWN_SEC`), (b) integração opcional para aplicar `Scripts/codex_autoflow.sh` automaticamente quando mudanças tocam hotspots de refactor (`server.py`, `server_helpers.py`, `REFACTOR_PROGRESS.md`), e (c) objetivo proativo do ciclo priorizando extrações puras + wrappers finos no eixo `server.py/server_helpers.py`. Suite offline preservada: **605 passed** em 17 arquivos.
- **2026-04-27 (esta sessão, branch `claude/continue-refactor-updates-wvOqd`) — Hotfix de runtime crítico (etapa 18):**
  48. `db0e94f`: corrigida regressão de produção `NameError: _build_active_chat_meta_impl is not defined` restaurando import alias em `server.py` e reintroduzindo no `server_helpers.py` o conjunto de helpers usados no runtime (perdidos em merge): `build_active_chat_meta`, `count_active_chats`, `count_unfinished_chats`, `find_expired_chat_ids`, `extract_manual_whatsapp_reply_targets`, `format_manual_whatsapp_requester_suffix`, `resolve_download_content_type`, `resolve_avatar_filename`. `tests/test_server_helpers.py` ganhou 6 casos novos cobrindo os helpers restaurados e smoke de alias em `server.py`. Suite offline atualizada: **611 passed** em 17 arquivos.
- **2026-04-27 (esta sessão, branch `claude/continue-refactor-updates-wvOqd`) — UX de observabilidade do agente (etapa 19):**
  49. `895bfff`: `Scripts/auto_dev_agent.py` ajustado para alertar espera/cooldown apenas UMA vez e manter somente countdown inline durante o período de anti-rate-limit, removendo spam de status repetitivo no console. Ao sair da espera, o agente emite uma única mensagem de retomada (`Espera concluída`) e retorna ao fluxo normal de eventos. Suite offline preservada: **611 passed**.
- **2026-04-27 (esta sessão, branch `claude/continue-refactor-updates-wvOqd`) — Hotfix de stream NDJSON (etapa 20):**
  50. `0b93625`: corrigido `NameError: _build_chat_id_event_impl is not defined` no generator de `/v1/chat/completions` (stream NDJSON). Reintroduzido helper puro `build_chat_id_event(chat_id)` em `Scripts/server_helpers.py`, exportado em `__all__`, importado como alias em `server.py` e integrado no primeiro `yield` do stream (`chat_id`). `tests/test_server_helpers.py` ganhou 2 casos novos (payload do helper + smoke de alias importado no server). Suite offline atualizada: **613 passed** em 17 arquivos.
- **2026-05-02 (esta sessão, branch `claude/focused-einstein-HT0Xs`) — Lote P2 (modularização adicional): extração de `error_scanner_helpers`:**
  51. Criado `Scripts/error_scanner_helpers.py` (módulo puro, sem `flask`/`playwright`/`config`) com 12 helpers cobrindo os 3 endpoints de `/api/errors/*`:
      - **Filtragem/parse de snippets** (`is_unwanted_snippet`, `build_scan_match_entry`, `build_scan_error_entry` + constante `UNWANTED_SNIPPET_KEYS`) — elimina duplicação de filtro `known_entry/truncated/read_error` e dos dicts de match entre `api_errors_scan` e `api_errors_claude_fix`.
      - **Prompt do Claude Code** (`build_claude_fix_prompt`) — substitui o antigo `_build_claude_fix_prompt` (40+ linhas inline) por wrapper fino. Aceita listas/iteráveis/None defensivamente.
      - **Body de `/v1/chat/completions`** (`build_claude_fix_request_body`) — encapsula dict de 10 chaves do POST proxy.
      - **Payloads de `/api/errors/known`** (`build_known_errors_missing_payload`, `build_known_errors_loaded_payload`, `build_known_errors_error_payload`) — padroniza shape para os 3 caminhos do endpoint (missing file, loaded ok, parse error).
      - **Linhas NDJSON do stream claude_fix** (`build_claude_fix_empty_stream_lines`, `build_claude_fix_status_line`, `build_claude_fix_error_line`, `build_claude_fix_finish_line`) — substitui `json.dumps({...}) + "\n"` literais nos generators `_empty_stream` e `proxy` por wrappers finos. Frame de finish canônico passa a ser idempotente (sem duplicação).
      `server.py` migrou:
      - `api_errors_known`: 3 caminhos de retorno → wrappers finos.
      - `api_errors_scan`: 2 sites de `new_errors.append({...})` → helpers + filtro padronizado.
      - `_build_claude_fix_prompt`: virou wrapper 1-linha.
      - `api_errors_claude_fix`: filtro de snippets, dict de body, gerador `_empty_stream` (2 yields) e `proxy` (3 yields) migrados para os helpers.
      `tests/test_error_scanner_helpers.py` cobre todos os 12 helpers com **53 casos** (filtros, mapeamento de campos opcionais, fallback defensivo p/ não-Mapping, idempotência do finish, determinismo do prompt). Suite offline atualizada: **751 passed** em 18 arquivos.

### Próxima opção recomendada (continuar nesta branch ou pedir aprovação para tocar `browser.py`)
- **Continuar baixo risco**: auditar `chat_completions` (linhas 2150+) e `api_sync` (linhas 1450+) por idioms duplicados ainda não cobertos. Helpers candidatos: builders de payloads de erro/timeout em `chat_completions`, normalização de cabeçalhos `X-Forwarded-*` em `_client_ip`, filtros de query-string em `/api/web_search/test`.
- **Alto risco (BLOQUEADO)**: Opção D (catálogo em `browser._dismiss_rate_limit_modal_if_any`) e Opção 9 (modularização do `server.py` por domínio Flask Blueprint). Ambas requerem aprovação explícita.










- **2026-04-25 septendecies** (branch `claude/create-log-sanitization-script-QQ56a`) — 3 ciclos contínuos sobre o pano de fundo da sedecies:
  9. `b0202b1`: extração de `build_markdown_event(content)` espelhando `build_error_event`. Migração do único `{"type":"markdown",...}` literal restante em `api_sync::sync_generate`. +4 testes.
  10. `1aa7dd6`: extração de `format_origin_suffix(is_analyzer, source_hint)` — sufixo `[origem: ...]` com analyzer override (sempre `analisador_prontuarios.py`) ou hint do payload. Migração em `chat_completions::_origem`. +5 testes.
  11. `0b08d85`: extração de `extract_source_hint(data, headers)` — colapsa o idiom de 4 linhas duplicado em chat_completions e _handle_browser_search_api. Migração ampla em `_handle_browser_search_api`: além de `extract_source_hint`, também `format_requester_suffix`, `is_analyzer_chat_request` e `build_sender_label` (eliminando 5 linhas de string-match ad-hoc; agora usa a mesma classificação canônica de `chat_completions`). +8 testes.
  Total: **471 testes offline passando** (+17 novos). 3 commits + docs commit (este).
