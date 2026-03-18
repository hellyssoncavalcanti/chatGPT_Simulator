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

### Por que isso importa
Outra LLM lendo este repositório deve entender que o simulador não é apenas um chat UI: ele é também um **serviço interno** usado por um pipeline clínico automatizado.

---

## Integração com PHP/proxy externo

O projeto também foi desenhado para ser consumido por um frontend/proxy PHP externo. Isso aparece nas referências do `server.py` e do `analisador_prontuarios.py` ao arquivo `chatgpt_integracao_criado_pelo_gemini.js.php` hospedado no ambiente do Conexão Vida.

Na prática, esse PHP parece funcionar como ponte entre a aplicação principal do site e o simulador, incluindo chamadas SQL e envio de prompts para a LLM via backend local.

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

- **`abrir_cmd_nesta_pasta.bat`**  
  Abre um CMD elevado com menu para executar os `.bat` do projeto.

---

## Estado e dados sensíveis

Uma LLM que vá trabalhar neste projeto deve prestar atenção especial a estes pontos:

1. **`config.py` contém API key e caminhos absolutos Windows.**  
   O código assume `C:\chatgpt_simulator` como diretório base.

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
- mudanças em payloads, nomes de campos ou formato de resposta podem quebrar integrações PHP e o pipeline clínico.

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
- `analisador_prontuarios.py` usa o simulador como engine LLM para um fluxo médico.
