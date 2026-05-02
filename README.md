# ChatGPT_Simulator

## Visão geral

O **ChatGPT_Simulator** é um sistema híbrido para automação do ChatGPT via navegador real (Chromium + Playwright), exposto como API HTTP/HTTPS e com interface web própria. O objetivo do projeto é permitir que outros clientes — frontend local, integrações PHP e processos de automação clínica — enviem mensagens para o ChatGPT, sincronizem históricos, façam pesquisas web no Google e operem chats existentes de forma programática, mas usando a interface real do ChatGPT por trás.

Em vez de falar diretamente com uma API oficial de modelo, o sistema usa um navegador persistente controlado por Playwright. O `server.py` recebe requisições REST, converte essas requisições em tarefas e as envia para o `browser.py` por uma fila thread-safe. O `browser.py` executa as ações no Chromium e devolve eventos de progresso, streaming e resultado final para o servidor Flask, que então responde ao cliente chamador.

---

## Objetivo do sistema

Este repositório resolve quatro necessidades principais:

1. **Automação do ChatGPT usando navegador real**
   O sistema abre o ChatGPT em um perfil persistente de Chromium e interage com a UI real: digita mensagens, cola blocos longos, anexa arquivos, sincroniza histórico e clica em menus de contexto.

2. **Exposição de uma API estável para terceiros**
   Clientes externos podem chamar endpoints REST para:
   - enviar prompts;
   - receber resposta em streaming;
   - listar chats locais;
   - sincronizar um chat inteiro;
   - deletar chats;
   - realizar pesquisa web automatizada.

3. **Frontend local para operação humana**
   O projeto também sobe uma interface web estilo ChatGPT para uso manual, incluindo login, histórico, upload de arquivos, compartilhamento e documentação de API.

4. **Uso em automações clínicas**
   O arquivo `analisador_prontuarios.py` roda como daemon e usa o simulador para analisar prontuários, consultar dados via PHP, enriquecer condutas com pesquisa web e persistir resultados estruturados.

---

## Arquitetura de alto nível

```text
Cliente humano / PHP / analisador_prontuarios.py
                    |
                    v
         Flask API (server.py)
                    |
                    v
      browser_queue (shared.py)
                    |
                    v
     Playwright + Chromium (browser.py)
                    |
                    v
          Interface real do ChatGPT
```

### Componentes centrais

- **`Scripts/main.py`**
  Ponto de entrada. Sobe o browser em uma thread, o servidor HTTP auxiliar em outra thread e o servidor HTTPS principal no processo principal.

- **`Scripts/server.py`**
  Camada HTTP/REST principal. Autentica, valida origem, recebe chamadas da UI/API (chat, sync, busca, métricas), envia tarefas para o browser e consolida respostas em JSON ou streaming. As rotas de suporte foram extraídas para Blueprints Flask dedicados (ver abaixo).

- **`Scripts/server_observabilidade.py`** *(Blueprint Flask)*
  Rotas de monitoramento: status da fila (`/api/queue/status`, `/api/queue/failed`), retry de tarefas falhas e streaming de logs (`/api/logs/tail`, `/api/logs/stream`).

- **`Scripts/server_recursos.py`** *(Blueprint Flask)*
  Servir avatares de usuário, proxy de downloads do ChatGPT sob demanda e `robots.txt`.

- **`Scripts/server_usuario.py`** *(Blueprint Flask)*
  Autenticação leve e perfil: logout, info do usuário, troca de senha e upload de avatar.

- **`Scripts/server_admin.py`** *(Blueprint Flask)*
  Diagnóstico e correção automática: listagem de erros conhecidos, varredura de logs e encaminhamento ao Claude Code via streaming NDJSON.

- **`Scripts/server_busca.py`** *(Blueprint Flask)*
  Pesquisa web automatizada: rota `/api/web_search/test` com streaming NDJSON de progresso e resultado.

- **`Scripts/browser.py`**
  Motor de automação com Playwright. É responsável por abrir o ChatGPT, digitar/colar mensagens, anexar arquivos, sincronizar histórico, pesquisar no Google e manipular menus de contexto.
  Correções recentes:
  - **Watchdog — mensagem vazia**: `asyncio.TimeoutError.__str__()` retorna `""`, fazendo o log exibir `aba não respondeu ()`. Corrigido para exibir o nome da classe quando a descrição é vazia.
  - **Snapshots HTML em erros**: função `_save_error_html()` salva o HTML renderizado em `logs/html_dos_erros/` ao detectar watchdog timeout, timeout 660 s ou erro genérico de chat.
  - **`is_disabled()` sem timeout bloqueava 30 s**: chamadas `is_disabled()` e `is_visible()` agora usam `timeout=2000` ms, evitando que o Playwright aguarde 30 s (default) pela reestabilização da página após conversão de paste em anexos.
  - **Timeout de submit proporcional a anexos**: quando o ChatGPT converte um bloco colado em múltiplos anexos, o tempo de espera pelo botão de envio é calculado como `max(20 s, 5 s + (n_anexos − 3) × 5 s)` — por exemplo, 70 s para 13 anexos em vez dos 12 s anteriores.
  - **Round-robin de perfis corrigido**: `_choose_profile_for_new_chat` agora trata `browser_profile="default"` como "sem preferência explícita", permitindo que o sistema alterne entre `default` e `segunda_chance`. Antes, qualquer string truthy (inclusive `"default"`) cortocircuitava o round-robin.
  - **Concorrência por perfil Chromium**: semáforos asyncio por perfil (`config.CHROMIUM_PROFILE_CONCURRENCY`) limitam tarefas simultâneas por perfil. Com dois perfis e limite=1 cada, duas análises simultâneas do analisador podem ser processadas em paralelo.

- **`Scripts/shared.py`**
  Define a fila `browser_queue`, que desacopla o Flask do loop assíncrono do Playwright. Também exporta o singleton `profile_concurrency_tracker` (`ProfileConcurrencyLimiter`) para rastrear tarefas ativas por perfil Chromium.

- **`Scripts/profile_concurrency.py`**
  Módulo puro (sem Flask/Playwright) que implementa `ProfileConcurrencyLimiter`: rastreador thread-safe de tarefas ativas por perfil Chromium, consumido pelo `browser.py` (asyncio) e exposto em `/api/metrics` pelo `server.py`.

- **`Scripts/db.py`**
  Camada SQLite compartilhada com schema/migração inicial (chats, mensagens, usuários e sessões).

- **`Scripts/storage.py`**
  Persistência local do histórico de chats em SQLite (`db/app.db`), mantendo API compatível (`load_chats`, `save_chat`, `append_message`, etc.).

- **`Scripts/auth.py`**
  Login, sessão persistente em SQLite com TTL (`SIMULATOR_SESSION_TTL_HOURS`) e gerenciamento de usuários/avatares.

- **`Scripts/utils.py`**
  Infraestrutura auxiliar: geração de certificados TLS, logging e materialização do frontend HTML.

- **`Scripts/analisador_prontuarios.py`**
  Serviço de automação clínica que usa o simulador como backend LLM local.

---

## Fluxo de inicialização

Ao iniciar pelo `0. start.bat`, o sistema segue, em essência, esta ordem:

1. verifica se `Scripts/config.py` e `Scripts/sync_github_settings.ps1` existem; se não, cria a partir dos templates versionados (`*.example.*`);
2. quando detecta instalação nova (`config.py` ausente), limpa `db/users/users.json` e `db/app.db` para reset seguro de credenciais default;
3. cria/ativa `.venv`;
4. instala dependências de `requirements.txt` (runtime) e `requirements-test.txt` (testes), quando presentes;
5. executa `Scripts/main.py`.

Dentro do `main.py`, a inicialização acontece assim:

1. gera certificados TLS autoassinados, se necessário;
2. sobe a thread do navegador (`browser.browser_loop()`);
3. sobe um servidor HTTP auxiliar em `PORT + 1` (3003);
4. prepara/garante o frontend;
5. sobe o servidor HTTPS principal em `PORT` (3002).

---

## Portas e modos de acesso

- **HTTPS local:** `https://localhost:3002`
  Interface principal “segura”, com certificado autoassinado.

- **HTTP auxiliar/remoto:** `http://<IP>:3003`
  Usado para integrações remotas e automações que não querem lidar com TLS local.

---

## Estrutura da fila interna (server → browser) e priorização

Para reduzir gargalos e starvation entre chats concorrentes, a fila global
`browser_queue` foi evoluída para uma estrutura com:

1. **Lanes por prioridade** (ações críticas primeiro, como `STOP`);
2. **Subfilas por tenant/chat** (`chat_id` / `url` / `request_source`);
3. **Round-robin entre tenants da mesma prioridade**, evitando que um único
   chat monopolize a execução;
4. **Priorização explícita de pedidos remotos** sobre pedidos oriundos de
   scripts Python/autônomos (ex.: analisador).

Isso mantém compatibilidade de uso com `put/get` e melhora previsibilidade em
cenários com múltiplas origens concorrendo por execução no navegador.

### Endpoint de observabilidade da fila

- `GET /api/queue/status`
- `GET /api/queue/failed?limit=100`
- `POST /api/queue/failed/retry` (body: `{ "index": <int> }`)
  - Retorna `qsize`, contadores de enfileiramento/consumo, métricas de espera
    (`avg_wait_ms`, `max_wait_ms`) e distribuição por origem/prioridade.
  - Exige autenticação (mesma política dos demais endpoints privados).

### Endpoint de log em tempo real (polling)

- `GET /api/logs/tail?lines=120`
- `GET /api/logs/stream` (SSE para streaming contínuo de logs)
  - Retorna as últimas linhas do arquivo de log ativo (`config.LOG_PATH`).
  - Ideal para atualização periódica no frontend sem abrir shell.
  - Exige autenticação.

### Endpoint de métricas operacionais (polling)

- `GET /api/metrics`
- `GET /metrics` (formato Prometheus)
  - Retorna uptime do servidor, estado de `ACTIVE_CHATS`, status de syncs,
    janela de rate-limit e snapshot da fila (`browser_queue`).
  - Exige autenticação.

### Frontend: novos itens no menu do usuário (`userDropdown`)

No avatar/menu superior direito foram adicionadas duas ações:

1. **Status da Fila**
   Abre um toast com atualização em tempo real do `/api/queue/status`.

2. **Log em tempo real**
   Abre um toast com **abas**:
   - **Log** → tail via `/api/logs/tail` ou stream SSE via `/api/logs/stream`
   - **Métricas** → painel em tempo real via `/api/metrics` e scrape Prometheus via `/metrics`

Esses painéis são focados em observabilidade operacional durante uso em
produção/local, sem interromper a conversa ativa.

---

## Hardening de segurança da API (issue #522)

Foi adicionado um conjunto de proteções no `server.py` para reduzir abuso,
facilitar auditoria e melhorar rastreabilidade:

1. **Rate limiting por IP/path** (janela de 60s) em `before_request`;
2. **Bloqueio temporário de IP por brute force de login**;
3. **Auditoria estruturada de segurança** em formato JSON (`[SECURITY_AUDIT]`)
   para eventos de acesso sensível, falha de autenticação, CSRF e bloqueios;
4. **Validação CSRF para fluxo de sessão/cookie**, com cookie `csrf_token`
   emitido no login;
5. **CORS configurável via ambiente** (`SIMULATOR_CORS_ALLOWED_ORIGINS`) com
   `supports_credentials=True`;
6. **Política de cookie de sessão configurável** (`secure`/`samesite`) por
   variáveis de ambiente;
7. **Validação de payload** (`Scripts/payload_validators.py`) nos endpoints
   críticos: limites de tamanho em `message` (100k chars), `password` (256 chars),
   `url` (2048 chars), sanitização de `browser_profile` (alphanum + `-_`),
   quota de `attachments` (20 itens), rejeição de 400 com mensagem clara antes
   de qualquer processamento;
8. **Correlation-ID ponta-a-ponta** (`Scripts/correlation.py`): lê
   `X-Correlation-Id` do request ou gera UUID4-8hex; propaga para o payload
   da fila de browser e aparece nos logs como `[cid:xxxx]`.

### Variáveis novas de segurança (config.py)

- `SIMULATOR_CORS_ALLOWED_ORIGINS` (CSV de origens permitidas)
- `SIMULATOR_SESSION_COOKIE_SECURE` (`true/false`)
- `SIMULATOR_SESSION_COOKIE_SAMESITE` (`Lax`, `Strict`, `None`)
- `SIMULATOR_SESSION_TTL_HOURS` (default `24`)
- `SIMULATOR_RATE_LIMIT_PER_MIN` (default `120`)
- `SIMULATOR_LOGIN_MAX_FAILS` (default `8`)
- `SIMULATOR_LOGIN_BLOCK_SEC` (default `900`)

Essas medidas ajudam na prevenção de acesso indevido/acidental e na evidência
de eventos suspeitos para auditoria operacional (incluindo conformidade LGPD).

---

## Testes automatizados e CI/CD (issue #528)

Foi adicionada uma base de testes com **pytest** e um workflow de CI no
**GitHub Actions** para execução automática e geração de cobertura.

### Estrutura de testes adicionada

- `tests/test_shared_queue.py`
  - cobre priorização de chats remotos vs origem Python;
  - cobre round-robin entre tenants na mesma prioridade.

- `tests/test_storage.py`
  - cobre persistência e deduplicação de mensagens;
  - cobre busca por `origin_url` com resolução do chat mais recente.

- `tests/test_server_api.py`
  - smoke de `/health` e `/api/metrics`;
  - valida bloqueio por brute force de login;
  - valida endpoint `/api/logs/tail` e smoke de `/api/logs/stream`.

- `tests/conftest.py`
  - prepara `PYTHONPATH` para módulos em `Scripts/` durante execução.

### CI em GitHub Actions

- Workflow: `.github/workflows/tests.yml`
- Executa em `push` e `pull_request`:
  1. instala Python 3.11;
  2. instala dependências de teste (`requirements-test.txt`);
  3. roda `pytest` com cobertura (`--cov=Scripts`);
  4. publica `coverage.xml` como artifact;
  5. envia para **Codecov** quando `CODECOV_TOKEN` estiver configurado.

### Como rodar localmente

```bash
pip install -r requirements-test.txt
pytest --cov=Scripts --cov-report=term-missing
```

### Suite offline de módulos puros (refactor em andamento)

O refactor (branch `claude/fix-rate-limit-interval-*`) vem extraindo lógica
pura de `server.py`, `browser.py` e `analisador_prontuarios.py` para
módulos testáveis sem Flask/Playwright/config. A suite offline correspondente
roda em ambientes mínimos (só `pytest` instalado):

```bash
python3 -m pytest \
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
  tests/test_error_scanner_helpers.py \
  tests/test_profile_concurrency.py \
  tests/test_browser_log_sanitization.py \
  tests/test_payload_validators.py \
  tests/test_correlation.py
```

Esperado: **908 passed**. (`tests/test_server_api.py` é excluído porque
exige `flask` e `cryptography` — roda no CI completo via o comando da
seção anterior. `tests/test_storage.py` também roda no CI completo.)

> Compatibilidade offline: `import server` agora funciona mesmo quando
> `cryptography`/`markdownify` não puderem ser instalados (ex.: proxy 403).
> A geração de certificado continua exigindo `cryptography` em runtime.
> Se quiser reativar auto-instalação via `pip`, defina
> `SIMULATOR_AUTO_INSTALL_DEPS=1`.

#### Inventário de módulos puros (extraídos do caminho quente)

| Módulo | Responsabilidade | Testes |
|---|---|---|
| `Scripts/request_source.py` | Detecção de origem Python/Codex/analyzer no request HTTP. | `tests/test_request_source.py` |
| `Scripts/error_catalog.py` | 11 códigos estáveis + classificador PT/EN + `format_reason` (tag `[CODE]`). | `tests/test_error_catalog.py` |
| `Scripts/server_helpers.py` | Wait-time formatting, payloads de fila, prune de deque, contagem de perfis Chromium, concatenação de mensagens OpenAI-style, rotulagem de remetente, extração de identidade do solicitante (e idiom legado de WhatsApp), helpers de payload para lookup/delete/menu, wrappers de texto colado, coalescência de `origin_url`, normalização de source-hint (`str(v).strip().lower()` defensivo), construção de eventos SSE/NDJSON (`status`/`error`/`markdown`/`searchresult`/`finish`/`chat_id`/`chat_meta`) e frames `text/event-stream` para `/api/logs/stream` (`build_log_stream_{line,ping,error}_sse`), terminais HTTP `(payload, status_code)` para `/api/web_search/test`, contagem agregada e init de meta para `ACTIVE_CHATS`, contagem minimal `count_unfinished_chats` para gauges Prometheus, identificação de IDs expirados em `_cleanup_active_chats` (`find_expired_chat_ids`), `mark_chat_finished` (sets finished/finished_at/last_event_at em uma passada), resolução de `Content-Type` por extensão para `/api/downloads/<file_id>`, validação de filename de avatar (whitelist `.jpg/.jpeg/.png/.gif/.webp`). | `tests/test_server_helpers.py` |
| `Scripts/browser_predicates.py` | Predicados do DOM do ChatGPT (extract sender, orphan tabs, incomplete JSON, inline base64, paste wrappers). | `tests/test_browser_predicates.py` |
| `Scripts/log_sanitizer.py` | `mask_api_key`, `mask_bearer_token`, `mask_session_cookie`, `mask_file_path`, `sanitize*`. | `tests/test_log_sanitizer.py` |
| `Scripts/security_state.py` | Classe `SecurityState` — rate-limit per-(ip,key) + brute-force de login, expiração automática. | `tests/test_security_state.py` |
| `Scripts/chat_rate_limit_cooldown.py` | Classe `ChatRateLimitCooldown` — cooldown global com backoff exponencial 2^strikes (clamp 1800s). | `tests/test_chat_rate_limit_cooldown.py` |
| `Scripts/sync_dedup.py` | Classe `SyncDedup` — dedup thread-safe de `/api/sync` (janela 120s), com `try_acquire`/`release`/`snapshot`. | `tests/test_sync_dedup.py` |
| `Scripts/python_request_throttle.py` | Classe `PythonRequestThrottle` — throttle global anti-rate-limit para requests Python com `begin`/`remaining_seconds`/`commit`/`snapshot`. | `tests/test_python_request_throttle.py` |
| `Scripts/web_search_throttle.py` | Classe `WebSearchThrottle` — agendamento global de busca web com intervalo humano (`reserve_slot`/`snapshot`) e injeção de `now_func`/`rng_func` (`snapshot` inclui `age_seconds`). Import carregado no topo de `server.py` para evitar `NameError` no boot. | `tests/test_web_search_throttle.py` |
| `Scripts/analisador_parsers.py` | Detecção de rate-limit em texto, strip/extract/normalize/parse JSON tolerante, heurística de truncamento, remoção de `<think>…</think>`, parser de fallback para queries de pesquisa com `max_queries` injetável. | `tests/test_analisador_parsers.py` |
| `Scripts/payload_validators.py` | Validação de entrada para rotas críticas: `validate_login_request` (username/password com limites de tamanho), `validate_chat_request` (message, chat_id, url, browser_profile, attachments, stream, messages, source_hint) e `validate_sync_request` (url/chat_id obrigatório par, browser_profile sanitizado). Módulo puro; server.py importa via try/except defensivo. | `tests/test_payload_validators.py` |
| `Scripts/correlation.py` | Suporte a Correlation-ID ponta-a-ponta: `extract_correlation_id` (lê `X-Correlation-Id` ou gera UUID4-8hex), `format_log_prefix` (prefixo `[cid:xxxx]` para logs), `inject_into_payload` (injeta sem mutar o original). Propagado em `chat_completions` e `api_sync`. | `tests/test_correlation.py` |

Os callers (`server.py`, `browser.py`, `analisador_prontuarios.py`,
`utils.py`) mantêm wrappers finos com as mesmas assinaturas originais —
mudanças no comportamento são capturadas pelos testes offline antes de
chegarem ao caminho Flask/Playwright. Progresso detalhado e histórico
de sessões ficam em `REFACTOR_PROGRESS.md` (seção "PONTO DE RETOMADA").

---

## Servidor de acompanhamento WhatsApp Web (modo isolado, sem Meta)

Foi adicionado o script `Scripts/acompanhamento_whatsapp.py`, responsável por:

1. Buscar no banco os registros com `mensagens_acompanhamento`;
2. Enviar as mensagens ao WhatsApp do paciente via **automação do WhatsApp Web**;
3. Receber a resposta do paciente e encaminhar automaticamente para a **URL específica do chat daquele paciente** (`url_chatgpt`) no endpoint local do Simulator (`/v1/chat/completions`);
4. Responder o paciente com a saída retornada pelo ChatGPT Simulator.

### Como executar

```bash
pip install -U requests flask playwright
playwright install chromium

python Scripts/acompanhamento_whatsapp.py
```

> No primeiro uso, a janela do navegador abrirá em `https://web.whatsapp.com/` para login via QR Code.
> Se o servidor HTTP do acompanhamento falhar/retornar inesperadamente,
> o processo tenta reiniciar automaticamente em loop.

### Endpoints auxiliares

- `GET /health` — status básico do serviço
- `POST /send-now` — força um ciclo imediato de envio de mensagens pendentes
- `POST /process-replies-now` — força um ciclo imediato de captura e processamento de respostas
- `POST /send-manual-reply` — envia resposta manual de profissional/secretária ao paciente via WhatsApp Web

### Variáveis de ambiente principais

- `PYWA_PHP_URL` (default: URL PHP da integração)
- `PYWA_PHP_API_KEY`
- `PYWA_SIMULATOR_URL` (default: `http://127.0.0.1:3003/v1/chat/completions`)
- `PYWA_SIMULATOR_API_KEY`
- `PYWA_POLL_INTERVAL_SEC` (default: `120`)
- `PYWA_REPLY_POLL_INTERVAL_SEC` (default: `20`)
- `PYWA_FETCH_SQL` (permite customizar a query de captação das mensagens de acompanhamento)

### Tabela SQL dedicada para contatos WhatsApp nomeados

Além de `chatgpt_chats` (histórico da conversa), o serviço de acompanhamento
passa a usar uma tabela de identidade/cache chamada `chatgpt_whatsapp`, criada
pela migration:

- `Scripts/migrations/002_create_chatgpt_whatsapp.sql`

O sistema de notificações de pendência profissional utiliza a coluna
`chatgpt_chats.notificacao_pendente`, criada pela migration:

- `Scripts/migrations/003_chatgpt_chats_add_notificacao_pendente.sql`

Objetivo dessa tabela:

1. Guardar telefone WhatsApp normalizado (`whatsapp_phone`);
2. Guardar nome exibido no chat (`wa_display_name`) e nome do painel
   **Dados do contato** (`wa_profile_name`);
3. Relacionar o contato com `id_paciente` / `id_atendimento` quando possível;
4. Permitir que o monitor resolva chats cujo título é nome próprio (não número),
   reduzindo falhas de correlação de respostas.
5. Executar enriquecimento preventivo da sidebar (amostra de chats nomeados),
   mesmo sem envio novo no ciclo, para popular o cache nome→telefone.

### Sistema de notificações de pendência profissional

Quando a LLM/ChatGPT Simulator responde a um paciente via WhatsApp e menciona que irá consultar o médico (Dr/Dra) ou a secretária, o sistema detecta automaticamente essa intenção e cria uma notificação pendente para que o profissional ou a secretária responda diretamente.

#### Coluna `chatgpt_chats.notificacao_pendente`

- **Migration:** `Scripts/migrations/003_chatgpt_chats_add_notificacao_pendente.sql`
- **Tipo:** `VARCHAR(20) NOT NULL DEFAULT 'false'`
- **Valores possíveis:**
  - `"false"` — sem pendência (padrão)
  - `"id_criador"` — pendência direcionada ao profissional criador do atendimento (o sistema exibe alerta ao usuário cujo `membros.id` corresponda a `chatgpt_chats.id_criador`)
  - `"id_secretaria"` — pendência direcionada a secretárias (o sistema identifica secretárias por: `membros.classificacao = 'profissional'` AND (`membros.registro_conselho` IS NULL OR vazio OR `'0'`) AND `'clinica_membros'` está contido na lista `membros.incluir`, que usa `&` como separador)

#### Fluxo completo

```text
Paciente envia mensagem via WhatsApp
        │
        ▼
acompanhamento_whatsapp.py recebe e encaminha ao ChatGPT Simulator
        │
        ▼
ChatGPT Simulator gera resposta (ex: "Vou verificar com a secretária")
        │
        ▼
detect_professional_inquiry() detecta keywords na resposta
        │
        ├─ "secretária/secretaria/agenda/recepção" → notificacao_pendente = 'id_secretaria'
        └─ "Dr./Dra./médico/profissional"          → notificacao_pendente = 'id_criador'
        │
        ▼
set_notificacao_pendente() atualiza a coluna no banco
(para 'id_criador', também garante que chatgpt_chats.id_criador está preenchido
 a partir de chatgpt_atendimentos_analise.id_criador)
        │
        ▼
Frontend PHP (`chat.js.php`) faz polling a cada 30s
via ?action=check_pendencias
        │
        ├─ Badge vermelho aparece no botão toggle (#ow-toggle-btn)
        └─ Contador aparece no item "Pendências" do menu lateral (#ow-sidebar)
        │
        ▼
Usuário abre "Pendências" → vê lista de chats pendentes → abre chat completo
        │
        ▼
Usuário digita resposta → JS envia via ?action=send_manual_whatsapp_reply
        │
        ▼
PHP proxy → server.py /api/send_manual_whatsapp_reply
        │
        ▼
server.py repassa ao acompanhamento_whatsapp.py /send-manual-reply
        │
        ▼
acompanhamento_whatsapp.py envia a mensagem via WhatsApp Web ao paciente,
registra no histórico (chatgpt_chats.mensagens) e reseta notificacao_pendente = 'false'
```

#### Handlers PHP (`chat.js.php`)

| Action | Método | Descrição |
|---|---|---|
| `?action=check_pendencias` | POST | Verifica se há chats com `notificacao_pendente != 'false'` relevantes ao usuário logado. Para `id_criador`, compara com `$row_login_atual['id']`. Para `id_secretaria`, verifica critérios de secretária. Retorna array de pendências com mensagens completas. |
| `?action=resolver_pendencia` | POST | Marca `notificacao_pendente = 'false'` para um `chat_id` específico. |
| `?action=send_manual_whatsapp_reply` | POST | Resolve IP do servidor Python (porta 3003) e repassa payload ao `server.py` `/api/send_manual_whatsapp_reply`. |
| `?action=save_chat_meta` | POST | Salva metadados do chat (título, URLs, contexto clínico). **Agora também vincula `id_chatgpt_atendimentos_analise`** automaticamente: busca em `chatgpt_atendimentos_analise` por `id_atendimento` (prioridade 1) ou `id_criador + id_paciente` (prioridade 2), e preenche o campo caso esteja NULL/0. Também sobrescreve a vinculação existente se a análise referenciada tiver sido deletada do banco. |

#### Endpoint server.py

| Rota | Método | Descrição |
|---|---|---|
| `/api/send_manual_whatsapp_reply` | POST | Recebe `phone`, `message`, `id_membro_solicitante`, `nome_membro_solicitante`, etc. Repassa ao `acompanhamento_whatsapp.py` na porta 3011 via `/send-manual-reply`. |

#### Funções acompanhamento_whatsapp.py

| Função | Descrição |
|---|---|
| `detect_professional_inquiry(answer_text)` | Analisa resposta da LLM e retorna `"id_criador"`, `"id_secretaria"` ou `None` conforme keywords detectadas. |
| `set_notificacao_pendente(phone, tipo, id_atendimento)` | Atualiza `chatgpt_chats.notificacao_pendente` no banco via SQL. Para `id_criador`, também preenche `chatgpt_chats.id_criador` a partir de `chatgpt_atendimentos_analise.id_criador` (JOIN via `cc.id_chatgpt_atendimentos_analise = caa.id`). |
| `insert_whatsapp_chat(phone, id_paciente, id_atendimento, id_analise, chat_url, first_message)` | Insere registro em `chatgpt_chats` para conversa WhatsApp. Busca `id_criador` automaticamente de `chatgpt_atendimentos_analise` usando `id_analise` antes do INSERT. |
| `/send-manual-reply` (endpoint Flask) | Envia mensagem via WhatsApp Web, registra no histórico (`chatgpt_chats.mensagens` com source `"manual_reply"`) e reseta o flag de notificação. |

#### Interface do usuário (sidebar)

- **Badge vermelho** no botão `#ow-toggle-btn` com contador (anima com `pulseBadge`)
- **Item "Pendências"** no menu lateral (`#ow-sidebar`) com contador de pendências
- **View de lista** (`#sb-view-pendencias`): cards com nome do paciente, telefone, tipo de notificação (Dr/Dra ou Secretária)
- **View de chat** (`#sb-view-pendencias-chat`): histórico completo de mensagens (paciente/equipe/sistema) + campo de input para resposta + botão enviar
- **Polling automático** a cada 30 segundos com toast notification para novas pendências

#### Imagens e downloads nas mensagens da IA

- **Imagens**: todas as `<img>` dentro de `.msg-ai` (base64 e URLs externas) são envolvidas em `.ow-img-scroll` (scroll horizontal) e possuem click-to-expand via overlay fullscreen (`#ow-screenshot-overlay`). O handler delegado (`document.addEventListener('click')`) detecta cliques em qualquer imagem dentro de `.msg-ai`, excluindo `.ow-screenshot-thumb` (que já possui handler próprio). Fechar: clique fora, botão × ou tecla Escape.
- **Preservação de mídia em `<button>`**: o ChatGPT envolve imagens de preview e cards de arquivo dentro de `<button>`. O `browser.py` usa o helper `stripButtonsKeepMedia()` (em `scrape_full_chat()`, Estratégias 1, 2 e 3) e uma variante em Python em `clean_html()` que removem o `<button>` mas preservam `<img>` e `<a>` internos. Antes dessa correção, a remoção ingênua de `<button>…</button>` (via regex) apagava as imagens que o ChatGPT exibia dentro de botões, causando regressão visível no fluxo de SYNC (mensagens perdiam ~42KB de base64 de imagem).
- **Downloads (detecção em camadas)**: o `browser.py` agora tem 4 caminhos complementares:
  1. **Network capture**: `_install_conversation_file_capture(page)` instala um listener em `page.on("response")` que intercepta as respostas JSON da API interna do ChatGPT (`/backend-api/conversation/{id}` e `/backend-api/files/{id}/download`). Ele extrai `file_id`, `filename` e `download_url` diretamente dos campos `metadata.attachments`, `aggregate_result.messages[].results[].files[]` e `content.parts[].asset_pointer` (formato `file-service://…`). `_register_captured_files()` resolve os file-ids para URLs pré-assinadas via fetch dentro do contexto do browser e registra em `shared.file_registry`.
  2. **DOM card scanning (preferencial para UI nova)**: `_scan_file_cards(page)` varre o DOM procurando por `div.group.my-4.w-full.rounded-2xl` ou `div[class*="corner-superellipse"]` (estrutura de card do code interpreter/canvas). Para cada card, extrai o filename do `div.truncate.text-sm.font-medium` e o preview do `<img src="…">` interno, incluindo base64 data URIs. Também identifica o `data-message-id` do turn ancestral para saber a qual mensagem o card pertence. No fluxo de SYNC, os cards detectados são injetados na mensagem correta via `turn_index`, preservando a posição original do arquivo e a imagem de preview. Cruza por nome de arquivo com os metadados capturados via network listener para resolver a URL real de download.
  3. **DOM scraping legado**: `_detect_and_register_files()` detecta links via 5 seletores DOM (`/backend-api/files/`, `files.oaiusercontent.com`, `sandbox:/`, atributo `download`, e qualquer `<a>` cujo texto/href termine com extensão de arquivo como `.xlsx`, `.pdf`, etc.) + padrão secundário no markdown (links com extensão de arquivo).
  4. **Click fallback**: `_click_chatgpt_download_elements()` clica em elementos de download do code interpreter para disparar o evento `page.on("download")` do Playwright.

  Os arquivos capturados por qualquer caminho são reescritos como `/api/downloads/{file_id}` no markdown. O `_postProcessHtml()` no frontend reescreve essas URLs para `?action=download_file&name=...` (proxy PHP) e aplica a classe `.ow-file-download` com ícone 📎. O handler PHP `?action=download_file` faz proxy via cURL para o `server.py` que usa o contexto autenticado do browser para fetch do arquivo.

### Guia rápido de configuração (modo isolado)

1. Garanta acesso ao WhatsApp Web:
   https://web.whatsapp.com/
2. Garanta Playwright + Chromium instalados:
   https://playwright.dev/python/
3. Faça login via QR Code na primeira execução e mantenha o perfil persistente.

---

## Autenticação e segurança

O sistema possui camadas simples, porém explícitas, de segurança:

### 1. API Key
A API pode ser autenticada por:
- header `Authorization: Bearer <API_KEY>`;
- campo `api_key` no JSON do corpo;
- `api_key` por query string.

### 2. Sessão web
A UI usa login com cookie `session_token`.

### 3. Restrições de origem
A autenticação primária é por API key (Bearer/body/query) e sessão web. A validação de `Origin`/`Referer`/IP funciona como defesa em profundidade quando a requisição não traz credencial válida.

### 4. Usuário padrão
Em instalação nova (quando `config.py` é recriado), o sistema inicializa:
- **usuário:** `admin`
- **senha:** `admin`

### 5. Sessões
As sessões web agora são persistidas em SQLite (`db/app.db`) com expiração por TTL configurável (`SIMULATOR_SESSION_TTL_HOURS`). Reiniciar o processo não invalida todas as sessões imediatamente; sessões expiradas são limpas automaticamente.

---

## Modelo operacional: não usa API oficial do ChatGPT

A peça mais importante para entender este projeto é:

> **O sistema não conversa diretamente com a API oficial da OpenAI.**

Em vez disso, ele automatiza o **site real do ChatGPT** com Playwright. Isso implica algumas características:

- depende da UI real do ChatGPT estar acessível;
- mudanças na estrutura HTML/CSS do ChatGPT podem quebrar seletores;
- o histórico e o estado da conta vivem no perfil persistente do Chromium (`chrome_profile/`);
- uploads, menus e streaming são derivados do comportamento real da página.

Esse design permite reproduzir capacidades da interface web mesmo sem integração via API nativa do modelo.

---

## Fila de tarefas entre API e navegador

A comunicação entre servidor e browser é mediada por `browser_queue`.

### Lado do servidor
O `server.py` recebe uma requisição HTTP, cria uma tarefa com um campo `action` e uma `stream_queue` de retorno, e faz `browser_queue.put(task)`.

### Lado do navegador
O `browser.py` consome a fila, abre/usa uma aba do Chromium e executa a ação. O retorno acontece por eventos em `stream_queue`, como:
- `log`
- `status`
- `markdown`
- `searchresult`
- `error`

Esse mecanismo desacopla o Flask (thread síncrona) do Playwright (loop assíncrono).

---

## Ações suportadas pelo `browser.py`

O `browser.py` aceita tarefas com `action`:

- **`CHAT`**
  Envia mensagem ao ChatGPT e devolve resposta em streaming.

- **`SYNC`**
  Faz scraping completo de um chat existente para alinhar o histórico local.

- **`GET_MENU`**
  Lê as opções do menu de contexto de um chat.

- **`EXEC_MENU`**
  Executa uma opção do menu (por exemplo excluir ou renomear).

- **`SEARCH`**
  Abre o Google, digita a busca, aguarda resultados e devolve resultados estruturados.

- **`STOP`**
  Encerra o loop principal do browser.

---

## Mecanismo de digitação e cola

O sistema segue **uma única regra para todos os pedidos** (remotos e Python):

### 1. Cola por clipboard (padrão para tudo)
Todo texto é colado via clipboard (`navigator.clipboard.writeText` + `Ctrl+V`). Isso acelera prompts longos e grandes blocos clínicos. Se o clipboard falhar, há fallback por injeção em chunks via JavaScript.

### 2. Digitação realista — somente para perguntas
Frases terminadas por `?` são digitadas caractere a caractere por `type_realistic()`, com atrasos aleatórios para parecerem humanas.

Além do delay base, o fluxo inclui:
- micro-pausas em pontuação (`. , ; : ! ?`);
- pausas de hesitação ocasionais (com probabilidade baixa);
- erros de digitação raros com correção imediata por `Backspace`.

Esse comportamento é configurável por variáveis `SIMULATOR_HUMAN_TYPING_*` no `config.py` (copiado de `Scripts/config.example.py`).

### 3. Texto misto (perguntas no meio)
Quando uma pergunta aparece no meio do texto, `smart_input()` divide o conteúdo em segmentos via `_split_by_questions()`:
1. Cola o trecho anterior via clipboard;
2. Digita a frase-pergunta realisticamente;
3. Retorna à cola para o restante.

O ciclo se repete para cada `?` encontrado, independentemente da origem da mensagem.

> **Nota:** os marcadores `[INICIO_TEXTO_COLADO]` / `[FIM_TEXTO_COLADO]` foram removidos do sistema. Referências legadas presentes em registros antigos do banco de dados são descartadas automaticamente na leitura.

---

## Persistência local

O simulador agora usa SQLite como persistência principal, com migração automática dos JSONs legados no primeiro boot.

### Arquivos principais
- **`db/app.db`** — histórico local de chats, mensagens, usuários e sessões
- **`db/history.json`** — legado (fonte de migração inicial, quando existir)
- **`db/users/users.json`** — legado/espelho para compatibilidade

### Papel do `storage.py` e `db.py`
`db.py` garante schema e migração inicial; `storage.py` mantém a API histórica do projeto (incluindo deduplicação/sync por chat) usando operações SQL transacionais, reduzindo risco de corrupção sob concorrência.

### Campo `chromium_profile` (rastreabilidade de perfil)
Cada chat persistido agora guarda `chromium_profile` (nome da pasta/perfil Chromium usado na execução, ex.: `chrome_profile`, `chrome_profile_segunda_chance`, `chrome_profile_whatsapp`).

Esse campo é usado para:
- retomar chats antigos no perfil correto;
- executar `/api/sync` no mesmo perfil em que a conversa foi criada;
- facilitar troubleshooting quando há múltiplas contas/perfis ativos.

No fluxo padrão:
- chats novos podem alternar automaticamente entre `default` e `segunda_chance` (round-robin), quando nenhum `browser_profile` explícito é enviado;
- se houver rate limit no `default`, o worker tenta fallback automático para `segunda_chance`.

---

## Frontend embutido

O frontend principal é um HTML gerado/garantido por `utils.setup_frontend()` e servido pelo Flask. A interface oferece:

- login;
- sidebar de chats;
- área de mensagens;
- envio de prompt;
- upload de arquivos;
- troca de senha e avatar;
- compartilhamento de preview;
- documentação interativa da API.

A UI usa o próprio backend do simulador como fonte de dados, especialmente:
- `/login`
- `/api/user/info`
- `/api/history`
- `/api/sync`
- `/api/delete`
- `/v1/chat/completions`
- `/api/web_search`

---

## Endpoints principais

### Autenticação
- `POST /login`
- `POST /logout`
- `GET /api/user/info`
- `POST /api/user/update_password`
- `POST /api/user/upload_avatar`
- `GET /api/user/avatar/<filename>`

### Operação de chats
- `GET /api/history`
- `POST /api/menu/options`
- `POST /api/menu/execute`
- `POST /api/sync`
- `POST /api/delete`
- `POST /v1/chat/completions`

### WhatsApp e notificações
- `POST /api/send_manual_whatsapp_reply` — repassa resposta manual de profissional/secretária ao `acompanhamento_whatsapp.py` para envio via WhatsApp Web

### Infraestrutura e pesquisa
- `GET /health`
- `GET /`
- `POST /api/web_search`
- `GET /api/web_search/test`

### Semântica do endpoint principal
O endpoint mais importante é:

- **`POST /v1/chat/completions`**

Ele é o equivalente “estilo OpenAI/Ollama” do simulador. Recebe prompt, anexos e chat alvo; enfileira uma tarefa `CHAT`; e pode responder em streaming ou em bloco.

---

## Pesquisa web

A pesquisa web é uma feature nativa do simulador.

### Como funciona
1. o cliente chama `POST /api/web_search` com uma lista de queries;
2. o `server.py` cria uma tarefa `SEARCH` por query;
3. o `browser.py` abre o Google em uma nova aba;
4. digita a busca de modo humano;
5. extrai resultados estruturados, com fallback por HTML bruto se necessário;
6. retorna uma lista com título, URL, snippet e tipo do resultado.

### Casos de uso
- enriquecimento de respostas da LLM;
- automação clínica no analisador de prontuários;
- integrações externas que querem “search via navegador real”.

---

## Integração com o analisador de prontuários

`Scripts/analisador_prontuarios.py` é um segundo sistema acoplado ao simulador.

### O que ele faz
- roda como daemon;
- consulta dados clínicos via um endpoint PHP externo (`chat.js.php`);
- chama `POST /v1/chat/completions` do simulador como backend LLM local;
- se o ChatGPT Simulator cair/ficar indisponível, **não encerra o processo**:
  permanece em loop de reconexão e retoma automaticamente quando o serviço volta;
- opcionalmente chama `POST /api/web_search` para buscar evidências;
- enriquece condutas clínicas com referências extraídas da web;
- grava/atualiza análises em uma tabela SQL remota via PHP.

### Variáveis de configuração do analisador

Todas as constantes configuráveis do analisador estão **centralizadas em `Scripts/config.py`** (prefixo `ANALISADOR_*`). O `analisador_prontuarios.py` importa de lá via `getattr(config, ..., fallback)` — se uma variável for removida por engano do `config.py`, o script continua funcionando com o valor padrão local.

**Para alterar qualquer parâmetro, edite apenas `config.py`.** A tabela abaixo lista as variáveis disponíveis:

| Variável (em config.py) | Padrão | Descrição |
|---|---|---|
| `ANALISADOR_PHP_URL` | URL do ConexaoVida | Endpoint PHP remoto |
| `ANALISADOR_LLM_URL` | `http://127.0.0.1:3003/v1/chat/completions` | URL do Simulator local |
| `ANALISADOR_LLM_MODEL` | `ChatGPT Simulator` | Nome do modelo LLM |
| `ANALISADOR_BROWSER_PROFILE` | `default` | Perfil Chromium enviado ao server (`browser_profile`) com fallback para `default` |
| `ANALISADOR_POLL_INTERVAL` | `30` | Segundos entre ciclos do loop principal |
| `ANALISADOR_MAX_TENTATIVAS` | `3` | Máximo de retentativas por análise com erro |
| `ANALISADOR_BATCH_SIZE` | `10` | Quantidade de registros processados por lote |
| `ANALISADOR_MIN_CHARS` | `80` | Tamanho mínimo de texto do prontuário após limpeza HTML |
| `ANALISADOR_TIMEOUT_PROCESSANDO_MIN` | `15` | Minutos antes de considerar uma análise travada |
| `ANALISADOR_PAUSA_MIN` / `_MAX` | `25` / `60` | Intervalo base (seg) anti-rate-limit aplicado a QUALQUER pedido Python; o `server.py` divide pelo número de perfis ChatGPT ativos em `config.CHROMIUM_PROFILES` |
| `ANALISADOR_FILTRO_HORARIO_UTIL_ATIVO` | `False` | `True` para bloquear em horário útil (seg-sex) |
| `ANALISADOR_HORARIO_UTIL_INICIO` | `7` | Hora de início do bloqueio (07:00, formato 24h) |
| `ANALISADOR_HORARIO_UTIL_FIM` | `19` | Hora de fim do bloqueio (19:00, exclusivo) |
| `ANALISADOR_SEARCH_HABILITADA` | `True` | `False` para desabilitar busca web |
| `ANALISADOR_EMBEDDING_MODEL_NAME` | `all-MiniLM-L6-v2` | Modelo de embeddings |
| `ANALISADOR_SIMILARIDADE_TOP_K` | `5` | Quantos casos semelhantes retornar |
| `ANALISADOR_LLM_THROTTLE_MIN` | `0` | Seg mínimos entre envios ao ChatGPT (0 = desativado) |
| `ANALISADOR_LLM_THROTTLE_MAX` | `0` | Seg máximos (aleatoriza entre MIN e MAX; 0 = desativado) |
| `ANALISADOR_LLM_RATE_LIMIT_RETRY_MAX` | `3` | Tentativas em rate limit antes de desistir |
| `ANALISADOR_LLM_RATE_LIMIT_RETRY_BASE_S` | `0` | Espera base (seg) no 1.º rate limit (0 = retry imediato) |

### Lógica de ordenação da fila de análises

A query de pendentes unitários divide a fila em duas faixas com base no campo `datetime_atendimento_inicio`:

1. **Atendimentos com menos de 30 dias** — ordenados **ASC** (mais antigos primeiro). São pacientes recentes cujas dúvidas o usuário pode precisar consultar em breve; os mais antigos dentro dessa janela têm maior chance de já terem gerado dúvidas.
2. **Atendimentos com 30+ dias** — ordenados **DESC** (mais novos primeiro). São prontuários antigos e pouco revisitados; a prioridade são os menos defasados.

Toda a lógica roda no SQL via `CASE WHEN` + `DATE_SUB(NOW(), INTERVAL 30 DAY)`, sem processamento local na máquina do usuário.

### Throttle e proteção contra rate limit

Cada análise envia 2-4 mensagens ao ChatGPT em sequência (análise principal + planejamento de queries + enriquecimento com evidências + refinamento opcional). Atualmente, o projeto está em modo de **pacing centralizado no Simulator**:

- **Throttle local do analisador**: desativado por padrão (`ANALISADOR_LLM_THROTTLE_MIN/MAX = 0`).
- **Cooldown local de rate limit**: desativado por padrão (`ANALISADOR_LLM_RATE_LIMIT_RETRY_BASE_S = 0`, retry imediato).
- **Intervalo anti-rate-limit entre pedidos Python**: enforçado no próprio `server.py` (não mais no analisador) para **qualquer** request cujo `request_source` seja um script Python — `analisador_prontuarios.py`, `acompanhamento_whatsapp.py`, `auto_dev_agent.py`, etc. A base é sorteada entre `ANALISADOR_PAUSA_MIN` e `ANALISADOR_PAUSA_MAX` e então **dividida pela quantidade de perfis Chromium ativos em `config.CHROMIUM_PROFILES`** (atualmente 2 perfis → intervalo cai pela metade). O call-site histórico `_aguardar_intervalo_entre_analises()` do analisador virou no-op.
- **Pedidos remotos não-Python**: UI local, frontend PHP e qualquer outro cliente que não seja `.py` **passam imediatamente**, sem intervalo anti-rate-limit.
- **Proteção no parse**: a detecção de rate limit continua dentro de `_parse_json_llm()` para evitar confundir erro de limite com “JSON inválido”.

Ou seja, a cadência passa a ser totalmente controlada pelo `server.py`/`browser.py` (incluindo alternância de perfis e fallback automático de perfil).

#### Como o servidor calcula o intervalo

No `server.py` (função `_wait_python_request_interval_if_needed`):

1. Se `request_source` **não** for Python → retorna imediatamente (sem espera).
2. Se for Python, sorteia `base = random.uniform(ANALISADOR_PAUSA_MIN, ANALISADOR_PAUSA_MAX)`.
3. Conta `N = len(config.CHROMIUM_PROFILES)` (atualmente 2: `default` + `analisador`/`segunda_chance`).
4. Calcula `alvo = base / N` e aguarda o tempo restante desde o último pedido Python liberado.
5. Atualiza o timestamp global (`_python_anti_rate_limit_last_ts`) ao liberar o pedido.

Durante a espera, o servidor publica eventos de `status` na stream com `phase = "python_anti_rate_limit_interval"` expondo `target_seconds`, `base_seconds` e `profile_count` para observabilidade.

### Filtro de horário útil

Por padrão, o analisador usa o perfil `default` (mesma conta/interface do usuário humano). Opcionalmente, pode usar um perfil dedicado via `ANALISADOR_BROWSER_PROFILE` (ex.: `segunda_chance`) para reduzir disputa de rate-limit. O plano Plus impõe um **limite de mensagens por janela de tempo**; se o analisador consumir esse limite durante o expediente, o usuário humano pode ficar temporariamente bloqueado.

Quando `FILTRO_HORARIO_UTIL_ATIVO = True`, o analisador entra em espera nos dias úteis (seg-sex) entre `HORARIO_UTIL_INICIO` e `HORARIO_UTIL_FIM`, reavaliando a cada 5 minutos. Fora desse horário (noites, madrugadas e fins de semana), roda normalmente.

### Por que isso importa
Outra LLM lendo este repositório deve entender que o simulador não é apenas um chat UI: ele é também um **serviço interno** usado por um pipeline clínico automatizado.

---

## Documentação modular (docs/)

Além deste README completo, o refactor criou documentos segmentados em `docs/` para facilitar navegação:
- `docs/arquitetura.md`
- `docs/analisador_prontuarios.md`
- `docs/whatsapp.md`
- `docs/agente_autonomo.md`
- `docs/sync_github.md`
- `docs/concurrency_per_profile.md`
- `docs/prompt_monitoramento_logs.md` — prompt pronto para acionar o Claude Code em sessões de monitoramento de logs, com comandos de referência rápida e descrição de cada sistema monitorado.

## Integração com PHP/proxy externo

O projeto também foi desenhado para ser consumido por um frontend/proxy PHP externo. Isso aparece nas referências do `server.py` e do `analisador_prontuarios.py` ao arquivo `chat.js.php` hospedado no ambiente do Conexão Vida.

Na prática, esse PHP parece funcionar como ponte entre a aplicação principal do site e o simulador, incluindo chamadas SQL e envio de prompts para a LLM via backend local.

Pontos importantes dessa ponte PHP para outra LLM:

- o proxy PHP encaminha chamadas de chat para o `server.py` na porta 3003;
- downloads de arquivos protegidos do ChatGPT podem precisar passar por proxy/autenticação compartilhada com o `browser.py`;
- no endpoint `execute_sql`, funções como `REPLACE(...)` e `REGEXP_REPLACE(...)` dentro de consultas `SELECT` devem ser tratadas como leitura, não como escrita SQL, desde que não exista comando real `REPLACE INTO`/`UPDATE`/etc. no início de uma instrução.

---

## Diretórios importantes

- **`Scripts/`** — backend Python principal
- **`frontend/`** — frontend estático servido pela aplicação
- **`db/`** — dados persistidos localmente
- **`db/users/`** — usuários e avatares
- **`certs/`** — certificado TLS autoassinado
- **`chrome_profile/`** — perfil persistente do Chromium / estado do ChatGPT
- **`logs/`** — logs de execução por sistema (um arquivo por sessão, prefixo = nome do processo)
- **`logs/html_dos_erros/`** — snapshots HTML do Chromium capturados automaticamente quando erros críticos ocorrem no `browser.py` (watchdog timeout, erro geral de chat, timeout 660 s). Cada arquivo segue o padrão `YYYYMMDD_HHMMSS_<tipo_erro>[_<chat_id>].html` e pode ser aberto diretamente no navegador para diagnóstico visual.
- **`temp/`** — arquivos temporários

---

## Arquivos de entrada para operação no Windows

- **`0. start.bat`**
  Inicializa o sistema principal completo.

- **`1. start_apenas_analisador_prontuarios.bat`**
  Sobe apenas o analisador de prontuários.

- **`DDNS_automatico.bat`**
  Executa o cliente PowerShell de DDNS.

- **`sync_github.bat`** / **`Scripts/sync_github.ps1`**
  Sincronizam o repositório no Windows, tentam mergear automaticamente o PR aberto mais recente, fecham PRs mais antigos, atualizam os arquivos locais e, quando houver mudanças, reiniciam em sequência o `Scripts/main.py` e o `Scripts/analisador_prontuarios.py`. Também aceitam `install-task` para registrar uma tarefa agendada no Windows a cada 10 minutos.

- **`Scripts\config.py`**
  Configuração local da instância (não versionada; gerada a partir de `Scripts/config.example.py` quando ausente).

- **`Scripts\config.example.py`**
  Template versionado e limpo, usado para bootstrap em novos ambientes.

- **`criar_pr.bat`** / **`Scripts/create_pr.ps1`**
  Cria um Pull Request no GitHub com todas as alterações locais. Clona o repositório em pasta temporária, detecta diferenças em relação ao `main`, faz commit e push em uma branch `claude-fixes-<timestamp>` e abre o PR via API GitHub. Aceita título, corpo e nome da branch como argumentos opcionais; sem argumentos, gera tudo automaticamente.
  ```
  criar_pr.bat                          → PR automático
  criar_pr.bat "meu título"             → título customizado
  criar_pr.bat "título" "descrição"     → com body
  criar_pr.bat "título" "desc" "branch" → tudo customizado
  ```

- **`abrir_cmd_nesta_pasta.bat`**
  Abre um CMD elevado com menu para executar os `.bat` do projeto.

---

## Sincronização automática com GitHub no Windows

Esta automação existe para manter a pasta `C:\chatgpt_simulator` alinhada com o GitHub sem intervenção manual. O fluxo pensado para outra LLM entender é este:

1. `sync_github.bat` chama `Scripts\sync_github.ps1`.
2. O PowerShell carrega as configurações a partir de `Scripts\config.py` (arquivo local não versionado, gerado via template quando necessário), com fallback para variáveis de ambiente `CHATGPT_SIMULATOR_*`.
3. O script cria um lock para evitar duas execuções simultâneas quando a tarefa agendada roda a cada 10 minutos.
4. Se houver token GitHub configurado, ele lista PRs abertos na branch alvo, fecha os mais antigos e tenta mergear o PR aberto mais recente.
5. Em seguida ele faz um clone temporário da branch principal, compara os arquivos rastreados e copia apenas os novos/alterados para `C:\chatgpt_simulator`, **ignorando artefatos de cache** (ex.: `__pycache__`, `.pyc`, `.pyo`, `.pyd`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`).
6. Se algo realmente mudou, ele encerra os processos correspondentes a `Scripts\main.py` e `Scripts\analisador_prontuarios.py` e os inicia novamente em sequência.
7. Se nada mudou, ele apenas registra em log e encerra sem reiniciar nada.

### Arquivos protegidos pelo sync automático

Para evitar perda de estado local, o sync **não deve sobrescrever** estes itens quando está atualizando a máquina Windows:

- `sync_github.bat`
- `Scripts\sync_github.ps1`
- toda a pasta `chrome_profile\`
- artefatos de cache Python/linters (`__pycache__`, `*.pyc`, `*.pyo`, `*.pyd`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`)

### Arquivos exatos desta automação no repositório

Se outra LLM ou um revisor humano estiver procurando os arquivos do sync no repositório, os caminhos versionados corretos são exatamente estes:

- `sync_github.bat`
- `Scripts\sync_github.ps1`
- `Scripts\config.py`

### Convenção recomendada para configuração local do sync

A convenção atual recomendada para qualquer operador humano ou outra LLM é:

- editar os campos de sync em `Scripts\config.py` (ou definir variáveis de ambiente `CHATGPT_SIMULATOR_*`)
- não versionar tokens reais
- manter `GH_USER`/`GITHUB_TOKEN` válidos antes de tentar processar PRs ou clonar repositório privado

### Agendamento

- `sync_github.bat install-task` registra a tarefa agendada do Windows.
- `sync_github.bat uninstall-task` remove a tarefa.
- a frequência padrão é de 10 minutos, configurável em `GITHUB_SYNC_INTERVAL_MINUTES` no `config.py`.
- quando executado com `--scheduled`, o próprio `sync_github.ps1` entra em modo persistente e repete automaticamente a conferência a cada intervalo configurado.

---

## Estado e dados sensíveis

Uma LLM que vá trabalhar neste projeto deve prestar atenção especial a estes pontos:

1. **`config.py` contém API key, caminhos absolutos Windows e TODAS as variáveis configuráveis do sistema (inclusive do analisador, prefixo `ANALISADOR_*`).**
   O código assume `C:\chatgpt_simulator` como diretório base. Os demais módulos importam daqui com fallback local.

2. **`chrome_profile/` é altamente stateful.**
   Ali vivem sessão do navegador, cache e estado do ChatGPT.

3. **seletores Playwright podem quebrar com mudanças no site do ChatGPT ou Google.**

4. **há forte acoplamento com o domínio `conexaovida.org` e com um IP específico (`151.106.97.30`).**

5. **o frontend local não é apenas uma demo; ele também documenta e exerce a API.**

---

## Como outra LLM deve raciocinar sobre este repositório

Se outra LLM ler este README para atuar no projeto, deve assumir o seguinte modelo mental:

- isto é um **orquestrador de navegador + API Flask**, não uma integração direta com provider LLM;
- o `server.py` é a porta de entrada de todas as integrações externas;
- o `browser.py` é a fonte real de comportamento operacional;
- a fila `browser_queue` é o ponto central de desacoplamento;
- `storage.py` e `auth.py` fornecem persistência simples, local e baseada em JSON;
- `analisador_prontuarios.py` é um cliente interno importante e deve ser considerado ao alterar contratos da API;
- mudanças em payloads, nomes de campos ou formato de resposta podem quebrar integrações PHP e o pipeline clínico;
- o sistema de notificações de pendência profissional (`notificacao_pendente`) conecta 4 camadas: detecção na resposta da LLM (`acompanhamento_whatsapp.py`), flag no banco (`chatgpt_chats`), polling no frontend (PHP/JS) e envio manual de resposta ao paciente (`server.py` → `acompanhamento_whatsapp.py` → WhatsApp Web). Alterar qualquer uma dessas camadas pode quebrar o fluxo completo.

---

## Resumo executivo

Em uma frase:

> **ChatGPT_Simulator é uma camada de automação do ChatGPT via navegador real, exposta como API Flask e usada tanto por uma UI local quanto por integrações externas e por um analisador clínico automatizado.**

Em termos práticos:

- `main.py` sobe tudo;
- `server.py` recebe chamadas HTTP;
- `shared.py` entrega tarefas ao browser;
- `browser.py` executa no Chromium;
- `storage.py` salva histórico local;
- `auth.py` controla acesso;
- `utils.py` cuida de infraestrutura;
- `analisador_prontuarios.py` usa o simulador como engine LLM para um fluxo médico;
- `acompanhamento_whatsapp.py` monitora respostas de pacientes, gera respostas via ChatGPT Simulator e detecta quando a LLM precisa de intervenção humana (médico ou secretária), criando notificações pendentes no banco e permitindo resposta manual via interface web.

---

## Agente autônomo de desenvolvimento contínuo (`auto_dev_agent.py`)

O script `Scripts/auto_dev_agent.py` é um **agente verdadeiramente autônomo** —
um desenvolvedor sênior virtual — que:

1. **Monitora continuamente** o ecossistema (main, browser worker, analisador,
   whatsapp) de forma **multiplataforma** (Linux via `/proc` ou `ps`, Windows via
   PowerShell/WMI).
2. **Detecta incidentes em tempo real** — varre os logs recentes em busca de
   `Traceback`, `ERROR`, `Exception`, `rate limit`, `timeout`, tracebacks do
   Python e padrões de falha conhecidos.
3. **Lê o código-fonte relevante** do próprio projeto — prioriza arquivos
   citados em tracebacks e também módulos sugeridos pelos próprios incidentes
   (ex.: linhas com `[browser.py]`, `[server.py]`, `[storage.py]`), depois os
   módulos core (`main.py`, `server.py`, `browser.py`, `shared.py`,
   `storage.py`, `auth.py`, `utils.py`) — e monta um **contexto estruturado**
   para o modelo.
4. **Consulta o ChatGPT via `browser.py`** usando o endpoint interno
   `/v1/chat/completions` em **modo streaming** (evita timeouts longos;
   tolera pausas até `AUTODEV_AGENT_STREAM_IDLE_SEC` entre eventos). A
   **mesma conversa** é reutilizada entre ciclos (`chat_id` persistido em
   `temp/auto_dev_agent_state.json`), dando memória de longo prazo ao agente.
   Se `AUTODEV_AGENT_CODEX_URL` estiver definida, a conversa acontece dentro
   do Codex no ChatGPT.
5. **Recebe um plano estruturado em JSON** do ChatGPT com:
   - `analysis` (diagnóstico/raciocínio);
   - `actions[]` contendo ações dos tipos:
     - `edit_file` (com `search`/`replace` exatos e contextuais);
     - `create_file` (novos módulos/arquivos);
     - `shell` (comandos utilitários — validados contra lista de bloqueio);
     - `note` (observação textual para humanos).
6. **Aplica as ações com segurança**:
   - **Snapshot/backup** de todos os arquivos afetados em
     `temp/agent_backups/<timestamp>/` antes de tocar em qualquer coisa.
   - **Lock de instância única** em `temp/auto_dev_agent.lock` para impedir
     duas instâncias do agente alterando o mesmo repositório ao mesmo tempo.
   - Bloqueio de caminhos sensíveis (`.git/`, `certs/`, `db/`, `logs/`,
     `chrome_profile/`, `__pycache__/`, `.venv/`, `node_modules/`).
   - Bloqueio de arquivos protegidos por negócio (`Scripts/config.py` e o
     próprio `Scripts/auto_dev_agent.py`, salvo com `AUTODEV_AGENT_SELF_EDIT=1`).
   - Bloqueio de comandos destrutivos via regex (`rm -rf`, `git reset --hard`,
     `git push --force`, `shutdown`, `mkfs`, `dd if=`, `DROP TABLE`,
     `chmod -R 777`, `kill -9 1`, fork-bomb, …).
   - Auto-edição do próprio agente só com `AUTODEV_AGENT_SELF_EDIT=1`.
   - Limite de ações por ciclo (`AUTODEV_AGENT_MAX_ACTIONS`).
7. **Valida as alterações** compilando **todos os `.py`** do projeto com
   `py_compile`. Se qualquer arquivo falhar, dispara **rollback atômico**
   restaurando os backups.
8. **Re-consulta o ChatGPT em caso de falha**, enviando o resultado da tentativa
   anterior como feedback, até `AUTODEV_AGENT_MAX_RETRIES + 1` tentativas.
9. **Opcionalmente faz commit automático** das alterações validadas (`AUTODEV_AGENT_AUTOCOMMIT`). Por padrão, o refactor deixou essa opção em dry-run (`0`) para exigir revisão humana antes de commit.
   com mensagem derivada da `analysis` + checklist das ações aplicadas, e
   **push opcional com retry exponencial** (`AUTODEV_AGENT_AUTOPUSH`).
10. **Mantém métricas de longo prazo** em `temp/auto_dev_agent_state.json`:
    ciclos totais, ciclos com erros, ciclos com correções, total de ações, e
    IDs da conversa ativa com o ChatGPT.
11. **Sempre que não há erros** e passou o intervalo `AUTODEV_AGENT_SUGGESTION_SEC`,
    entra em **modo proativo**: pergunta ao ChatGPT por uma melhoria pequena e
    segura (performance, robustez, observabilidade, qualidade de código) e
    executa se válida.

### Arquitetura do ciclo

```
┌────────────────────── CICLO ──────────────────────┐
│                                                   │
│  health-check   logs/incidentes   código-fonte    │
│        │              │                │          │
│        └──────────────┼────────────────┘          │
│                       ▼                           │
│            context + objective                    │
│                       │                           │
│                       ▼                           │
│   POST /v1/chat/completions (stream=True)         │
│       │                                           │
│       │   browser.py → ChatGPT (Codex)            │
│       ▼                                           │
│   plan JSON { analysis, actions[] }               │
│       │                                           │
│       ▼                                           │
│   snapshot → apply → py_compile                   │
│       │           │        │                      │
│       │           ├── OK → git commit (+ push)    │
│       │           └── FAIL → rollback → retry     │
│       ▼                                           │
│   persist state → sleep CYCLE_INTERVAL_SEC        │
│                                                   │
└───────────────────────────────────────────────────┘
```

### Como executar

```bash
python Scripts/auto_dev_agent.py
```

ou no Windows:

```bat
3. start_agente_autonomo.bat
```

O agente espera até `AUTODEV_AGENT_STARTUP_WAIT_SEC` segundos pelo Simulator
subir. Depois disso entra em modo monitor mesmo se o Simulator ainda não
estiver pronto — reavalia a saúde a cada ciclo.
Se ocorrer erro fatal fora do loop principal, o processo entra em
auto-restart com espera de 30 segundos e tenta retomar automaticamente.

Se `AUTODEV_AGENT_AUTOSTART_CMD` estiver configurada, o agente também tenta
**iniciar automaticamente o Simulator** quando o health-check falhar, com
cooldown para evitar loops agressivos de restart.

### Variáveis de ambiente

| Variável | Default | Descrição |
|---|---|---|
| `AUTODEV_AGENT_SIMULATOR_URL` | `http://127.0.0.1:3003/v1/chat/completions` | Endpoint do Simulator |
| `AUTODEV_AGENT_CODEX_URL` | `https://chatgpt.com/codex/cloud` | URL base da conversa do Codex no ChatGPT |
| `AUTODEV_AGENT_MODEL` | `ChatGPT Simulator` | Nome lógico do modelo (apenas label) |
| `AUTODEV_AGENT_API_KEY` | `config.API_KEY` | Bearer token para o Simulator |
| `AUTODEV_AGENT_CYCLE_SEC` | `120` | Intervalo entre ciclos (s) |
| `AUTODEV_AGENT_SUGGESTION_SEC` | `600` | Intervalo de sugestões proativas (s) |
| `AUTODEV_AGENT_REQUEST_TIMEOUT` | `900` | Timeout total por consulta ao ChatGPT (s) |
| `AUTODEV_AGENT_STREAM_IDLE_SEC` | `180` | Idle tolerado entre eventos streaming (s) |
| `AUTODEV_AGENT_CONTEXT_CHARS` | `28000` | Orçamento máximo do contexto (chars) |
| `AUTODEV_AGENT_MAX_ACTIONS` | `5` | Ações aplicadas por ciclo (hard cap) |
| `AUTODEV_AGENT_MAX_RETRIES` | `2` | Retentativas após falha de validação |
| `AUTODEV_AGENT_AUTOFIX` | `1` | Aplica edit_file/create_file |
| `AUTODEV_AGENT_AUTOCOMMIT` | `0` | Dry-run por padrão; `1` para permitir commit automático |
| `AUTODEV_AGENT_AUTOPUSH` | `0` | Faz git push após commit (com retry) |
| `AUTODEV_AGENT_SELF_EDIT` | `0` | Permite que o agente edite a si próprio |
| `AUTODEV_AGENT_BRANCH` | *(branch atual)* | Força commit/push em branch específico |
| `AUTODEV_AGENT_REMOTE` | `origin` | Remote Git alvo do push |
| `AUTODEV_AGENT_COMMIT_PREFIX` | `[auto-dev-agent]` | Prefixo da mensagem de commit |
| `AUTODEV_AGENT_REUSE_CHAT` | `1` | Mantém a mesma conversa entre ciclos |
| `AUTODEV_AGENT_USE_PASTE_MARKERS` | *(removido)* | ~~Encapsula mensagens em marcadores de cola~~ — variável mantida por compatibilidade; não tem efeito. `browser.py` agora cola tudo automaticamente via clipboard, sem marcadores. |
| `AUTODEV_AGENT_STARTUP_WAIT_SEC` | `30` | Espera inicial pelo Simulator (s) |
| `AUTODEV_AGENT_HEALTH_RETRIES` | `2` | Quantidade de tentativas por health-check antes de marcar indisponível |
| `AUTODEV_AGENT_HEALTH_RETRY_DELAY_SEC` | `2` | Intervalo entre tentativas de health-check (s) |
| `AUTODEV_AGENT_AUTOSTART_CMD` | *(vazio)* | Comando para subir o Simulator automaticamente quando indisponível |
| `AUTODEV_AGENT_AUTOSTART_COOLDOWN_SEC` | `180` | Cooldown mínimo entre tentativas de auto-start (s) |
| `AUTODEV_AGENT_EXIT_ON_FATAL` | `0` | `exit(1)` em erro fatal (para CI) |

### Envio rápido via paste (clipboard)

Para **evitar a digitação realista caractere-a-caractere** do `browser.py`
(que poderia levar minutos em prompts grandes), o agente envia mensagens
diretamente sem encapsulamento adicional.

O `browser.py` agora **cola tudo via clipboard + Ctrl+V** por padrão, sem
necessidade de marcadores. Apenas frases terminadas em `?` são digitadas
realisticamente — o que raramente ocorre em mensagens do agente, que
tipicamente não terminam em pergunta.

- Prompt do sistema e payload do agente são colados instantaneamente;
- A variável `AUTODEV_AGENT_USE_PASTE_MARKERS` foi mantida por
  compatibilidade, mas não tem efeito — pode ser ignorada.

### Contrato de resposta esperado do ChatGPT

O agente **instrui o ChatGPT** (system prompt) a responder estritamente com
JSON no seguinte formato (sem markdown, sem prosa extra):

```json
{
  "analysis": "raciocínio/diagnóstico",
  "actions": [
    {
      "type": "edit_file",
      "file": "Scripts/server.py",
      "description": "O que muda e por quê",
      "search": "trecho exato hoje no arquivo (contexto único)",
      "replace": "novo trecho que substituirá o search"
    },
    {
      "type": "create_file",
      "file": "Scripts/novo_modulo.py",
      "description": "motivo da criação",
      "content": "conteúdo completo"
    },
    {
      "type": "shell",
      "command": "python -m py_compile Scripts/server.py",
      "description": "validação"
    },
    {
      "type": "note",
      "content": "observação para humanos"
    }
  ]
}
```

O parser do agente (`_extract_json_object`) é tolerante a fences de código
(` ``` ` ou ` ```json `), marcadores como `RESPOSTA:` e prosa extra. Ele
varre múltiplos candidatos JSON e prioriza o objeto que mais se parece com o
schema de plano (`analysis`, `actions`, `should_forward_to_codex`), reduzindo
falsos negativos quando o modelo repete contexto antes da resposta final.

### Arquivos protegidos e caminhos bloqueados

**Nunca** são modificados pelo agente, mesmo se o ChatGPT sugerir:

- Arquivos: `Scripts/config.py`, e `Scripts/auto_dev_agent.py`
  (salvo com `SELF_EDIT=1`).
- Diretórios: `.git/`, `certs/`, `db/`, `logs/`, `chrome_profile/`, `__pycache__/`,
  `.venv/`, `node_modules/`, `temp/agent_backups/`.
- Extensões editáveis: `.py`, `.md`, `.bat`, `.txt`, `.json`, `.ini`, `.cfg`,
  `.yml`, `.yaml`. Outras são read-only.

### Observabilidade

Logs:
- `logs/auto_dev_agent-<timestamp>.log` — log detalhado do ciclo atual.
- `temp/auto_dev_agent_state.json` — estado persistido (chat_id, contadores).
- `temp/agent_backups/<timestamp>/` — backups atômicos de arquivos alterados.

Mensagens-chave emitidas:
- `🛰️ Serviços ativos:` — apenas quando a assinatura muda (não polui o log).
- `⏳ Simulator indisponível` — throttled a cada 30 s.
- `✅ Validação OK em N arquivo(s) alterado(s).`
- `🛑 Validação falhou` → `↩️ Rollback`.
- `📦 Commit efetuado` / `🚀 Push OK`.
- `💭 Análise sem ações` — ciclo em que o ChatGPT escolheu não agir.
- `ℹ️ should_forward_to_codex ausente` — fallback de contrato (informativo),
  sem elevar para warning.
- Em forwards Codex, o agente imprime também status de progresso do browser
  (`⏳`, `📝`, `🔧`) em linhas persistentes para facilitar auditoria passo a passo.

Detecção de incidentes:
- Quando a linha de log traz nível explícito (`[INFO]`, `[WARNING]`, `[ERROR]`),
  o agente prioriza esse nível para reduzir falso-positivo.
- Linhas informativas conhecidas do `sync_github` (ex.: branches sem commits
  novos) são ignoradas no classificador de incidentes.

## Monitoramento de logs e diagnóstico

### Scanner de logs (`Scripts/log_scanner.py`)

Ferramenta que identifica o **log mais recente de cada sistema** e extrai apenas os trechos relevantes (erros, avisos, exceções), sem ler o arquivo inteiro. Projetada para diagnóstico eficiente e para consumo por LLMs (evita gastar tokens com linhas de log normais).

O relatório separa automaticamente erros em duas categorias:
- **🆕 Erros novos** — nunca antes investigados; requerem atenção.
- **🔵 Erros conhecidos** — já catalogados em `Scripts/erros_conhecidos.json`; Claude **não** reinvestiga.

```bash
# Todos os sistemas
python Scripts/log_scanner.py

# Sistema específico
python Scripts/log_scanner.py --systems simulator analisador_prontuarios

# Mais contexto ao redor de cada ocorrência
python Scripts/log_scanner.py --context 8

# Salvar relatório em arquivo
python Scripts/log_scanner.py --output logs/relatorio_erros.md

# Registrar um bug como corrigido (evita reinvestigação futura)
python Scripts/log_scanner.py --add-known "trecho que aparece no log" \
  --status fixed --description "o que era" --fix "o que foi feito" \
  --files "Scripts/browser.py"

# Registrar falso positivo ou dado histórico de banco
python Scripts/log_scanner.py --add-known "padrão" \
  --status false_positive --description "por que não é erro de código"

# Listar todos os erros já catalogados
python Scripts/log_scanner.py --list-known
```

Sistemas monitorados automaticamente:

| Prefixo                   | Processo                              |
|---------------------------|---------------------------------------|
| `simulator`               | ChatGPT Simulator (`main.py`)         |
| `analisador_prontuarios`  | Analisador de prontuários             |
| `auto_dev_agent`          | Agente autônomo de desenvolvimento    |
| `cloudflared`             | Túnel Cloudflare                      |
| `ddns-client`             | Atualização de DNS dinâmico           |
| `sync_github`             | Sync automático com GitHub            |

### Banco de erros conhecidos (`Scripts/erros_conhecidos.json`)

Arquivo JSON versionado que persiste o histórico de erros já investigados entre sessões do Claude Code. Cada entrada contém: padrão de busca, status, descrição, fix aplicado e data de resolução.

Status disponíveis:

| Status | Ícone | Significado |
|--------|-------|-------------|
| `fixed` | 🔵 | Bug corrigido no código |
| `false_positive` | ⚪ | Linha de log normal que casa com keyword mas não é erro |
| `historico_db` | 🗄️ | Erro antigo gravado no banco, sem impacto no código atual |
| `suppressed` | 🔕 | Comportamento aceito/esperado, não requer ação |
| `monitoring` | 👁️ | Conhecido, ainda ativo — monitorar frequência |

### Snapshots HTML de erros (`logs/html_dos_erros/`)

O `browser.py` captura automaticamente o HTML renderizado da página do Chromium e salva em `logs/html_dos_erros/` nos seguintes eventos críticos:

- **Watchdog timeout** — aba não responde ao heartbeat (`page.evaluate("1")`)
- **Timeout externo 660 s** — tarefa de chat ultrapassa o limite total
- **Erro genérico de chat** — qualquer exceção não recuperável no fluxo principal

O nome de cada arquivo segue o padrão `YYYYMMDD_HHMMSS_<tipo>_<chat_id>.html`. Abrir no navegador mostra exatamente o estado visual do ChatGPT no momento da falha.

### Prompt de monitoramento para Claude Code

Para acionar uma sessão de revisão de logs com correção automática, envie ao Claude Code (ou configure como Routine):

> **Monitora os logs do projeto e corrija os erros encontrados.**

O fluxo seguido: rodar `log_scanner.py` → ignorar erros conhecidos → investigar apenas os 🆕 novos → corrigir → registrar com `--add-known`. Detalhes e referência completa em [`docs/prompt_monitoramento_logs.md`](docs/prompt_monitoramento_logs.md).

---

## Documentação detalhada
- [Arquitetura](docs/arquitetura.md)
- [Analisador de Prontuários](docs/analisador_prontuarios.md)
- [WhatsApp](docs/whatsapp.md)
- [Agente Autônomo](docs/agente_autonomo.md)
- [Sync GitHub](docs/sync_github.md)

## Refactor em andamento
Acompanhe o plano completo em [`REFACTOR_PROGRESS.md`](REFACTOR_PROGRESS.md).
