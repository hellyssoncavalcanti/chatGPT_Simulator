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
  Camada HTTP/REST. Autentica, valida origem, recebe chamadas da UI/API, envia tarefas para o browser e consolida respostas em JSON ou streaming.

- **`Scripts/browser.py`**  
  Motor de automação com Playwright. É responsável por abrir o ChatGPT, digitar/colar mensagens, anexar arquivos, sincronizar histórico, pesquisar no Google e manipular menus de contexto.

- **`Scripts/shared.py`**  
  Define a fila `browser_queue`, que desacopla o Flask do loop assíncrono do Playwright.

- **`Scripts/storage.py`**  
  Persistência local em JSON do histórico de chats (`db/history.json`).

- **`Scripts/auth.py`**  
  Login, sessão em memória e gerenciamento simples de usuários/avatares em `db/users/users.json`.

- **`Scripts/utils.py`**  
  Infraestrutura auxiliar: geração de certificados TLS, logging e materialização do frontend HTML.

- **`Scripts/analisador_prontuarios.py`**  
  Serviço de automação clínica que usa o simulador como backend LLM local.

---

## Fluxo de inicialização

Ao iniciar pelo `0. start.bat`, o sistema segue, em essência, esta ordem:

1. prepara pastas e mata processos antigos;
2. ativa a virtualenv;
3. instala dependências Python e o Chromium do Playwright;
4. garante regra de firewall da porta 3002;
5. abre `https://localhost:3002` no navegador;
6. executa `Scripts/main.py`.

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
Frontend PHP (chatgpt_integracao_criado_pelo_gemini.js.php) faz polling a cada 30s
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

#### Handlers PHP (chatgpt_integracao_criado_pelo_gemini.js.php)

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
O `server.py` aplica validação de `Origin`, `Referer` e IP remoto. O sistema está preparado para aceitar chamadas vindas de:
- `https://conexaovida.org`
- `https://www.conexaovida.org`
- `127.0.0.1`
- `151.106.97.30`

### 4. Usuário padrão
Se `users.json` não existir, o sistema cria automaticamente o usuário:
- **usuário:** `admin`
- **senha:** `32713091`

> Observação importante para outra LLM: a autenticação é funcional, mas simples. As sessões vivem em memória no dict `SESSIONS`; portanto reiniciar o processo invalida sessões ativas.

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

O sistema distingue dois modos de entrada no ChatGPT:

### 1. Digitação realista
Textos comuns são enviados caractere a caractere por `type_realistic()`, com atrasos aleatórios pequenos para parecerem humanos.

### 2. Cola por clipboard
Blocos delimitados por:
- `[INICIO_TEXTO_COLADO]`
- `[FIM_TEXTO_COLADO]`

são colados via clipboard (`navigator.clipboard.writeText` + `Ctrl+V`). Isso acelera prompts longos e grandes blocos clínicos. Se o clipboard falhar, há um fallback por injeção em chunks.

---

## Persistência local

O simulador persiste histórico local em JSON, não em banco relacional.

### Arquivos principais
- **`db/history.json`** — histórico local dos chats
- **`db/users/users.json`** — usuários, hash de senha e avatar

### Papel do `storage.py`
`storage.py` faz leitura e escrita com `threading.Lock`, para evitar corrupção quando múltiplas threads acessam o mesmo arquivo. Ele também possui lógica de sincronização para atualizar mensagens locais quando a versão do navegador é mais completa.

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
- consulta dados clínicos via um endpoint PHP externo (`chatgpt_integracao_criado_pelo_gemini.js.php`);
- chama `POST /v1/chat/completions` do simulador como backend LLM local;
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
| `ANALISADOR_POLL_INTERVAL` | `30` | Segundos entre ciclos do loop principal |
| `ANALISADOR_MAX_TENTATIVAS` | `3` | Máximo de retentativas por análise com erro |
| `ANALISADOR_BATCH_SIZE` | `10` | Quantidade de registros processados por lote |
| `ANALISADOR_MIN_CHARS` | `80` | Tamanho mínimo de texto do prontuário após limpeza HTML |
| `ANALISADOR_TIMEOUT_PROCESSANDO_MIN` | `15` | Minutos antes de considerar uma análise travada |
| `ANALISADOR_PAUSA_MIN` / `_MAX` | `15` / `45` | Intervalo de pausa (seg) entre análises individuais |
| `ANALISADOR_FILTRO_HORARIO_UTIL_ATIVO` | `False` | `True` para bloquear em horário útil (seg-sex) |
| `ANALISADOR_HORARIO_UTIL_INICIO` | `7` | Hora de início do bloqueio (07:00, formato 24h) |
| `ANALISADOR_HORARIO_UTIL_FIM` | `19` | Hora de fim do bloqueio (19:00, exclusivo) |
| `ANALISADOR_SEARCH_HABILITADA` | `True` | `False` para desabilitar busca web |
| `ANALISADOR_EMBEDDING_MODEL_NAME` | `all-MiniLM-L6-v2` | Modelo de embeddings |
| `ANALISADOR_SIMILARIDADE_TOP_K` | `5` | Quantos casos semelhantes retornar |
| `ANALISADOR_LLM_THROTTLE_MIN` | `8` | Seg mínimos entre envios ao ChatGPT |
| `ANALISADOR_LLM_THROTTLE_MAX` | `15` | Seg máximos (aleatoriza entre MIN e MAX) |
| `ANALISADOR_LLM_RATE_LIMIT_RETRY_MAX` | `3` | Tentativas em rate limit antes de desistir |
| `ANALISADOR_LLM_RATE_LIMIT_RETRY_BASE_S` | `60` | Espera base (seg) no 1.º rate limit |

### Lógica de ordenação da fila de análises

A query de pendentes unitários divide a fila em duas faixas com base no campo `datetime_atendimento_inicio`:

1. **Atendimentos com menos de 30 dias** — ordenados **ASC** (mais antigos primeiro). São pacientes recentes cujas dúvidas o usuário pode precisar consultar em breve; os mais antigos dentro dessa janela têm maior chance de já terem gerado dúvidas.
2. **Atendimentos com 30+ dias** — ordenados **DESC** (mais novos primeiro). São prontuários antigos e pouco revisitados; a prioridade são os menos defasados.

Toda a lógica roda no SQL via `CASE WHEN` + `DATE_SUB(NOW(), INTERVAL 30 DAY)`, sem processamento local na máquina do usuário.

### Throttle e proteção contra rate limit

Cada análise envia 2-4 mensagens ao ChatGPT em sequência (análise principal + planejamento de queries + enriquecimento com evidências + refinamento opcional). Para evitar o bloqueio por "excesso de solicitações":

- **Throttle global**: antes de cada envio ao ChatGPT, o sistema aguarda um intervalo aleatório entre `ANALISADOR_LLM_THROTTLE_MIN` e `ANALISADOR_LLM_THROTTLE_MAX` segundos desde o último envio. Isso garante um ritmo "humano" mesmo entre mensagens internas de uma mesma análise.
- **Detecção de rate limit**: se o ChatGPT responder com texto indicando limite (ex: "Você chegou ao limite", "excesso de solicitações"), o sistema levanta `ChatGPTRateLimitError` e aguarda `ANALISADOR_LLM_RATE_LIMIT_RETRY_BASE_S` segundos antes de continuar o próximo item do lote.
- **Proteção no parse**: a detecção ocorre dentro de `_parse_json_llm()`, garantindo que rate limits não sejam confundidos com "JSON inválido" nem consumam tentativas do registro.

### Filtro de horário útil

O analisador compartilha a mesma conta e interface do ChatGPT Plus que o usuário humano. O plano Plus impõe um **limite de mensagens por janela de tempo** (estimado em ~160 mensagens / 3 horas para GPT-5.4 Thinking). Se o analisador consumir esse limite durante o expediente, o usuário ficará impossibilitado de usar o ChatGPT manualmente.

Quando `FILTRO_HORARIO_UTIL_ATIVO = True`, o analisador entra em espera nos dias úteis (seg-sex) entre `HORARIO_UTIL_INICIO` e `HORARIO_UTIL_FIM`, reavaliando a cada 5 minutos. Fora desse horário (noites, madrugadas e fins de semana), roda normalmente.

### Por que isso importa
Outra LLM lendo este repositório deve entender que o simulador não é apenas um chat UI: ele é também um **serviço interno** usado por um pipeline clínico automatizado.

---

## Integração com PHP/proxy externo

O projeto também foi desenhado para ser consumido por um frontend/proxy PHP externo. Isso aparece nas referências do `server.py` e do `analisador_prontuarios.py` ao arquivo `chatgpt_integracao_criado_pelo_gemini.js.php` hospedado no ambiente do Conexão Vida.

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
- **`logs/`** — logs de execução
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

- **`Scripts\sync_github_settings.ps1`** *(versionado com valores-base de exemplo)*
  Arquivo de configuração do sync automático. No repositório ele fica com parâmetros-base de exemplo; na máquina Windows ele pode ser personalizado localmente. **Esse arquivo é tratado como protegido pelo sync e não deve ser sobrescrito no Windows.**

- **`abrir_cmd_nesta_pasta.bat`**  
  Abre um CMD elevado com menu para executar os `.bat` do projeto.

---

## Sincronização automática com GitHub no Windows

Esta automação existe para manter a pasta `C:\chatgpt_simulator` alinhada com o GitHub sem intervenção manual. O fluxo pensado para outra LLM entender é este:

1. `sync_github.bat` chama `Scripts\sync_github.ps1`.
2. O PowerShell carrega primeiro `Scripts\sync_github_settings.ps1`; por compatibilidade, também aceita o nome antigo `Scripts\sync_github.settings.ps1`.
3. O script cria um lock para evitar duas execuções simultâneas quando a tarefa agendada roda a cada 10 minutos.
4. Se houver token GitHub configurado, ele lista PRs abertos na branch alvo, fecha os mais antigos e tenta mergear o PR aberto mais recente.
5. Em seguida ele faz um clone temporário da branch principal, compara os arquivos rastreados e copia apenas os novos/alterados para `C:\chatgpt_simulator`.
6. Se algo realmente mudou, ele encerra os processos correspondentes a `Scripts\main.py` e `Scripts\analisador_prontuarios.py` e os inicia novamente em sequência.
7. Se nada mudou, ele apenas registra em log e encerra sem reiniciar nada.

### Arquivos protegidos pelo sync automático

Para evitar perda de estado local, o sync **não deve sobrescrever** estes itens quando está atualizando a máquina Windows:

- `sync_github.bat`
- `Scripts\sync_github.ps1`
- `Scripts\sync_github_settings.ps1`
- `Scripts\sync_github.settings.ps1` *(compatibilidade com nome antigo)*
- toda a pasta `chrome_profile\`

### Arquivos exatos desta automação no repositório

Se outra LLM ou um revisor humano estiver procurando os arquivos do sync no repositório, os caminhos versionados corretos são exatamente estes:

- `sync_github.bat`
- `Scripts\sync_github.ps1`
- `Scripts\sync_github_settings.ps1`
- `.gitignore` *(mantendo apenas a compatibilidade com o nome legado `Scripts\sync_github.settings.ps1`)*

O arquivo `Scripts\sync_github_settings.ps1` agora faz parte do repositório com valores-base de exemplo, mas o sync continua tratando-o como protegido para não sobrescrever a versão personalizada existente na máquina Windows.

### Convenção recomendada para configuração local do sync

A convenção atual recomendada para qualquer operador humano ou outra LLM é:

- arquivo versionado/base: `Scripts\sync_github_settings.ps1`
- uso no Windows: personalize esse mesmo arquivo localmente
- não dependa que o sync substitua esse arquivo local, porque ele é protegido
- substitua os placeholders `COLE_SEU_TOKEN_AQUI` e `seu_usuario_ou_org` antes de tentar processar PRs ou clonar um repositório privado
- o nome antigo `Scripts\sync_github.settings.ps1` continua aceito apenas por compatibilidade

### Agendamento

- `sync_github.bat install-task` registra a tarefa agendada do Windows.
- `sync_github.bat uninstall-task` remove a tarefa.
- a frequência padrão é de 10 minutos, configurável em `syncIntervalMinutes`.
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

## Agente autônomo de melhoria contínua

Foi adicionado o script `Scripts/auto_dev_agent.py`, que atua como um orquestrador de operação contínua:

1. não inicia servidores automaticamente; detecta a cada ciclo quais processos do ecossistema estão ativos (main, analisador e browser worker) e passa a monitorá-los;
2. monitora logs em tempo real por ciclos;
3. detecta padrões de erro/warning;
4. consulta a LLM local (`/v1/chat/completions`, via Simulator/browser.py) para obter sugestões;
5. mesmo sem erro, entra em ciclo de melhoria contínua no intervalo configurado;
6. interpreta logs, envia contexto de erros para a LLM, aplica correções, reexecuta testes e tenta novamente até validar (máximo configurável de tentativas por rodada);
7. executa ações automáticas de shell/patch e validações rápidas (`py_compile`, `git status`).
8. registra snapshot de serviços ativos monitorados em cada mudança de estado (ON/OFF por processo alvo).

### Como executar

```bat
3. start_agente_autonomo.bat
```

ou diretamente:

```bash
python Scripts/auto_dev_agent.py
```

### Variáveis de ambiente úteis

- `AUTODEV_AGENT_SIMULATOR_URL` (default `http://127.0.0.1:3003/v1/chat/completions`)
- `AUTODEV_AGENT_CODEX_URL` (default `https://chatgpt.com/codex/cloud`)
- `AUTODEV_AGENT_MODEL` (default `ChatGPT Simulator`)
- `AUTODEV_AGENT_API_KEY` (opcional)
- `AUTODEV_AGENT_CYCLE_SEC` (default `60`)
- `AUTODEV_AGENT_SUGGESTION_SEC` (default `300`)
- `AUTODEV_AGENT_MAX_ATTEMPTS` (default `3`) → tentativas de correção por rodada
- `AUTODEV_AGENT_UNSAFE` (default `0`) → habilita auto-apply de patch

> Operação automática: por padrão, o agente valida e monitora em loop, mas **não aplica patch automaticamente** (`AUTODEV_AGENT_UNSAFE=0`), com bloqueio adicional para arquivo crítico `Scripts/analisador_prontuarios.py`.
> Autenticação: se `AUTODEV_AGENT_API_KEY` não for definida, o agente tenta reutilizar `config.API_KEY` automaticamente.
