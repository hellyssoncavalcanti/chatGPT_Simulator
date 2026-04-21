# Refactor Progress — ChatGPT Simulator

> Arquivo de estado para retomar trabalho em caso de falha de chat.
> **Sempre releia este arquivo inteiro antes de continuar.**

## Objetivo geral

Executar refactor amplo do ChatGPT Simulator em várias fases. Escopo acordado com o usuário na conversa original (ver "Requisitos do usuário" abaixo). Trabalhar **por fase, sequencialmente**, atualizando este arquivo ao final de cada fase.

## Requisitos do usuário (literais — não reinterprete)

1. **Allowlist baseada em API key** (não em IP). O IP do usuário solicitante pode mudar; `api_key` é a fonte de verdade. Manter possibilidade de IP/origem como camada adicional *opcional*, mas não obrigatória.
2. **Dados sensíveis em `Scripts/config.py` e `Scripts/sync_github_settings.ps1`**: ambos são *excluídos* quando o sistema é enviado a outros devs. O `0. start.bat` deve **criar versões limpas** desses arquivos quando não os encontrar. Nunca sobrescrevê-los se existirem.
3. **Credenciais padrão**: `admin` / `admin`. Só reinicializar para o padrão quando o arquivo `config.py` **não for encontrado** (indicando instalação em novo local). Ao reabrir o `0. start.bat` em máquina já configurada, NÃO mexer em `users.json`.
4. **Dependências**: manter o `0. start.bat` cuidando de tudo como já faz; adicionar SQLite ao fluxo (biblioteca `sqlite3` é stdlib, então só precisamos garantir a migração).
5. **Conta Plus alternativa para `analisador_prontuarios.py`**: preparar infra para no futuro usar perfil Chromium distinto. Um campo opcional na requisição ao `server.py` → repassado ao `browser.py` → seleciona o perfil. Fallback para perfil padrão quando ausente/inválido.
6. **Sync GitHub**: manter autonomia total, sem aprovação humana de PR. Usuário confia, pois reversão via GitHub é simples.
7. **Executar todas as 19 sugestões originais** do Claude (ver §"Lista de 19 sugestões" abaixo).

## Arquitetura atual (descoberta na exploração)

- **NÃO é git repo** (`C:\chatgpt_simulator\.git` não existe). O sync clona em temp.
- **`.gitignore`** existe com 3 entradas. Nota: entrada `Scripts/sync_github.settings.ps1` está com typo (ponto em vez de underscore) — arquivo real é `Scripts/sync_github_settings.ps1`.
- **`Scripts/config.py`** linhas 96, 129, 142: `API_KEY`, `GITHUB_TOKEN`, `GITHUB_REMOTE_PHP_API_KEY` hardcoded.
- **`Scripts/sync_github_settings.ps1`** linhas 6, 18: PAT do GitHub e API key hardcoded.
- **`Scripts/auth.py:52`**: senha padrão `"&lt;senha antiga&gt;"` hardcoded.
- **`Scripts/main.py:496`**: `print("\n[ADMIN] User: admin | Pass: &lt;senha antiga&gt;")` — precisa atualizar texto.
- **`README.md:362`**: documenta `&lt;senha antiga&gt;` — precisa atualizar para `admin`.
- **`Scripts/server.py:640-662`**: `enforce_domain_origin()` hardcoded com `conexaovida.org`, `127.0.0.1`, `151.106.97.30`. Nenhuma referência a `api_key` como bypass.
- **`Scripts/browser.py:4716-4723`** e **`:4806-4812`**: `launch_persistent_context(config.DIRS["profile"], ...)` — perfil único, sem seleção por task.
- **`Scripts/analisador_prontuarios.py`**: 4 lugares (linhas 4371, 5131, 5232, 5420) constroem payload para `/v1/chat/completions` — precisam do novo campo `browser_profile`.
- **`Scripts/storage.py`**: JSON em `db/history.json`, thread-safe via `threading.Lock`. API pública: `load_chats`, `save_chat`, `append_message`, `update_full_history`, `find_chat_by_origin`, `delete_chat`, `delete_chats_by_origin`.
- **`Scripts/auth.py`**: JSON em `db/users/users.json`, `SESSIONS` em memória. API pública: `load_users`, `save_users`, `hash_password`, `verify_login`, `change_password`, `update_avatar`, `get_user_info`, `check_session`, `logout`.
- **`Scripts/shared.py`**: `BrowserTaskQueue` (prioridade + round-robin). Campos de task: `action`, `chat_id`, `url`, `origin_url`, `request_source`, `queue_priority`, `messages`, `stream_queue`, etc.

## Lista de 19 sugestões (controle de fases)

| #  | Sugestão                                                             | Fase | Status    |
|----|----------------------------------------------------------------------|------|-----------|
| 1  | Remover credencial padrão do README; forçar troca no 1º login        | 1    | pendente  |
| 2  | API key / tokens fora de git (start.bat cria templates limpos)       | 1    | pendente  |
| 3  | Sessões em memória → persistência (adiado: parte do SQLite)          | 4    | pendente  |
| 4  | Allowlist IP → allowlist via API key (IP/origem opcional)            | 2    | pendente  |
| 5  | Docs de TLS com mkcert                                               | 8    | DEFERIDO  |
| 6  | JSON → SQLite (history + users)                                      | 4    | pendente  |
| 7  | Centralizar seletores Playwright em `selectors.py`                   | 6    | pendente  |
| 8  | Dead-letter queue para tarefas falhas                                | 6    | pendente  |
| 9  | Perfil Chromium dedicado para analisador (por request)               | 3    | pendente  |
| 10 | Derivar paths de `__file__` (portabilidade)                          | 6    | pendente  |
| 11 | `requirements.txt`                                                   | 8    | pendente  |
| 12 | SSE `/api/logs/stream`                                               | 7    | pendente  |
| 13 | Prometheus `/metrics`                                                | 7    | pendente  |
| 14 | Suíte pytest                                                         | 8    | pendente  |
| 15 | Split do README em `docs/`                                           | 8    | pendente  |
| 16 | Auto-dev-agent dry-run default                                       | —    | REJEITADO (usuário confia) |
| 17 | Pydantic validando `config.py`                                       | 8    | pendente  |
| 18 | Isolar integração `conexaovida.org` como plugin                      | —    | DEFERIDO (refactor massivo fora do escopo imediato) |
| 19 | Redis/RQ para fila multi-host                                        | —    | DEFERIDO (usuário rejeitou) |

## Fases de execução

### Fase 1 — Bootstrap limpo + credenciais
**Status:** concluída ✅

- [x] `Scripts/config.example.py` (template limpo, sem sensíveis)
- [x] `Scripts/sync_github_settings.example.ps1` (template limpo)
- [x] `0. start.bat` copia os `.example.*` para os reais se não existirem, antes de invocar `main.py`
- [x] Se `config.py` não existir no momento do start, também deletar `db/users/users.json` para forçar admin/admin
- [x] `Scripts/auth.py` → `hash_password("admin")`
- [x] `Scripts/main.py` → imprime `"admin | Pass: admin (altere no primeiro login)"`
- [x] `README.md` → admin / admin (seção "Usuário padrão (instalação nova)")
- [x] `.gitignore` protege `config.py`, `sync_github_settings.ps1`, `db/*.db`, JSONs legados, perfis Chromium, .venv
- [x] `.gitignore` refeito (tinha typo `sync_github.settings.ps1`)
- [x] Adicionado em `config.py`: `ALLOWED_IPS`, `CHROMIUM_PROFILES`, `ANALISADOR_BROWSER_PROFILE`, `APP_DB_FILE`
- [x] `py_compile` passou nos arquivos tocados

### Fase 2 — Access control: API key primária
**Status:** pendente

- [ ] Em `server.py`, renomear `enforce_domain_origin()` → `enforce_access_policy()`
- [ ] Lógica: se `check_auth()` passa (API key OU sessão válida), aceita imediatamente. Só aplica origin/IP check quando *nenhuma* autenticação foi enviada e a rota exige uma (camada de defesa em profundidade contra bots não autenticados).
- [ ] Mover `allowed_domains` para `config.CORS_ALLOWED_ORIGINS` (já existe)
- [ ] Criar `config.SIMULATOR_ALLOWED_IPS` (nova, CSV env var)
- [ ] Remover literal `"151.106.97.30"` do código-fonte
- [ ] Garantir que `/login`, `/health`, `/`, `/robots.txt`, `/favicon.ico` continuem públicos

### Fase 3 — Perfil Chromium por request
**Status:** pendente

- [ ] Novo helper `browser._resolve_profile_dir(name)`: aceita string como `"default"`, `"analisador"`, ou caminho arbitrário. Mapa em `config.CHROMIUM_PROFILES = {"default": ..., "analisador": ...}`. Fallback para `default`.
- [ ] `server.py` (`/v1/chat/completions`) lê opcionalmente `browser_profile` do JSON e anexa à task.
- [ ] `browser.py` no loop principal: ao extrair a task, se `task.get("browser_profile")` diferente do perfil atualmente ativo, abre contexto Chromium secundário (lazy), ou fallback para default.
- [ ] `analisador_prontuarios.py` (4 lugares): adicionar `payload["browser_profile"] = config.ANALISADOR_BROWSER_PROFILE or "default"`.
- [ ] Nova constante `config.ANALISADOR_BROWSER_PROFILE = _env("ANALISADOR_BROWSER_PROFILE", "default")`.

### Fase 4 — Persistência SQLite
**Status:** pendente

- [ ] Novo módulo `Scripts/db.py`: abre `db/app.db`, cria tabelas `chats(chat_id PK, title, url, origin_url, created_at, updated_at)`, `messages(id PK AI, chat_id FK, role, content, ord)`, `users(username PK, password_hash, avatar)`, `sessions(token PK, username, created_at)`.
- [ ] Thread-safe via `threading.Lock` + `sqlite3.connect(..., check_same_thread=False, isolation_level=None)` em modo WAL.
- [ ] `Scripts/storage.py` — reescrever todas as funções públicas mantendo assinaturas idênticas. Internamente chama `db.py`.
- [ ] `Scripts/auth.py` — idem: `load_users`, `verify_login`, `change_password`, `update_avatar` passam a usar `db.py`. `SESSIONS` continua em memória (por enquanto).
- [ ] Migração on-boot: se `db/app.db` não existir mas `db/history.json` e/ou `db/users/users.json` existirem, importar conteúdo e renomear os JSONs para `.migrated.json`.

### Fase 5 — Polimento (fases 6–8)

#### Fase 6 — Resiliência & portabilidade
- [ ] `Scripts/selectors.py`: dicionário centralizado de seletores do ChatGPT e Google.
- [ ] `config.py`: `BASE_DIR` passa a derivar de `__file__` por padrão, env var `SIMULATOR_BASE_DIR` só sobrescreve.
- [ ] `/api/queue/failed`: endpoint que retorna snapshot de tarefas em DLQ. `shared.py` ganha `browser_queue.fail(task, reason)` e `browser_queue.snapshot_failed()`.

#### Fase 7 — Observabilidade
- [ ] `server.py` rota `/api/logs/stream` com SSE, gerador que faz tail do `config.LOG_PATH` via offset.
- [ ] `/metrics` em Prometheus exposition format usando `prometheus_client` (dependência opcional; fallback graceful se não instalado).

#### Fase 8 — Qualidade
- [ ] `requirements.txt` na raiz, baseado em `main.py::CORE_DEPENDENCIES` + `prometheus_client` opcional.
- [ ] `tests/` com `test_storage.py`, `test_auth.py`, `test_agent_parser.py`, `test_shared_queue.py`.
- [ ] `docs/` absorvendo seções detalhadas do README; README raiz vira índice.
- [ ] Validação leve via Pydantic em `config.py` (classe `SimulatorSettings` opcional; fallback se Pydantic não instalado).

### Fase 9 — Validação
- [ ] `py_compile` em todos os `.py` do projeto.
- [ ] `pytest tests/` passa.
- [ ] Leitura visual de `0. start.bat` e simulação mental do fluxo fresh-install.
- [ ] Atualizar este arquivo com status final.

## Convenções

- Nunca deletar `config.py`, `sync_github_settings.ps1`, `chrome_profile/`, `db/` sem autorização.
- Preservar assinaturas públicas de `storage.py`, `auth.py`, `shared.browser_queue` — muitos módulos dependem delas.
- Sempre que tocar `browser.py`, validar que o loop principal continua respondendo a `STOP`.
- Commit por fase, mensagens em português consistentes com `[auto-dev-agent]` do projeto.

## Log de execução

(Atualizar ao final de cada fase com hash curto do commit, data, e próximos passos.)

- _fase 0 — mapeamento concluído, arquivo de progresso criado_
