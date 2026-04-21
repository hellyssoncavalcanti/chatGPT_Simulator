# REFACTOR_PROGRESS.md

## Contexto e requisitos obrigatórios do usuário

### Requisitos mandatórios (texto consolidado)
1. **Allowlist deve priorizar API key**, pois IP pode variar.
2. **Arquivos sensíveis locais** (`Scripts/config.py`, `Scripts/sync_github_settings.ps1`) devem ser recriados limpos quando ausentes.
3. **Credencial padrão** deve ser `admin/admin`, resetando apenas em instalação nova (quando `config.py` não existe).
4. **`0. start.bat`** deve cuidar do bootstrap/dependências.
5. **Analisador com perfil dedicado opcional**: campo `browser_profile` no payload até `browser.py`, com fallback para perfil default.
6. **sync_github** permanece autônomo.

---

## Plano por fases (modelo completo)

### Fase 1 — Segurança e bootstrap
- [x] Criar `Scripts/config.example.py` versionado (sem segredo de produção).
- [x] Criar `Scripts/sync_github_settings.example.ps1` versionado.
- [x] Atualizar `.gitignore` para ignorar `config.py`, `.env`, settings locais e bancos locais.
- [x] Implementar bootstrap no `0. start.bat` para copiar templates quando ausentes.
- [x] Aplicar reset de credenciais apenas em `fresh install`.

### Fase 2 — Controle de acesso
- [x] Consolidar política de acesso no `server.py` priorizando API key/sessão.
- [x] Manter origem/IP como camada secundária (defesa em profundidade).

### Fase 3 — Perfil Chromium por requisição
- [x] Definir `CHROMIUM_PROFILES` em config.
- [x] Propagar `browser_profile` em `server.py` -> `browser.py`.
- [x] Implementar fallback para `default` quando perfil inválido.
- [x] Atualizar `analisador_prontuarios.py` para enviar `browser_profile` em todos payloads LLM.

### Fase 4 — Migração de persistência para SQLite
- [x] Criar `Scripts/db.py` com schema (`chats`, `messages`, `users`, `sessions`).
- [x] Migrar automaticamente JSON legado para SQLite no primeiro init.
- [x] Migrar `storage.py` preservando API pública.
- [x] Migrar `auth.py` para sessões persistentes em SQLite com TTL.

### Fase 5 — Portabilidade e config hygiene
- [x] Derivar `BASE_DIR` de `Path(__file__).resolve().parent.parent`.
- [x] Adicionar suporte a `.env` (python-dotenv opcional).
- [x] Introduzir `requirements.txt` para runtime reprodutível.
- [x] Validar variáveis críticas (`ANALISADOR_*`, `AUTODEV_AGENT_*`) com Pydantic em startup.

### Fase 6 — Resiliência de fila e seletores
- [x] Implementar DLQ em `BrowserTaskQueue` (`mark_failed`, listagem e retry manual).
- [x] Expor DLQ em API (`GET /api/queue/failed`, `POST /api/queue/failed/retry`).
- [x] Centralizar seletores críticos em `Scripts/app_selectors.py` com metadados de validação.
- [x] Adicionar smoke test de seletores (`tests/test_selectors_smoke.py`).

### Fase 7 — Observabilidade
- [x] Adicionar SSE de logs (`GET /api/logs/stream`).
- [x] Adicionar endpoint Prometheus (`GET /metrics`).
- [x] Manter métricas JSON existentes (`GET /api/metrics`).

### Fase 8 — Qualidade/manutenção
- [x] Ajustar `AUTODEV_AGENT_AUTOCOMMIT` para dry-run por padrão.
- [x] Atualizar documentação técnica para refletir arquitetura atual.
- [x] Dividir documentação em `docs/` e manter README raiz como índice.

### Fase 9 — Encerramento do refactor
- [x] Rodar validações de sintaxe (`py_compile`) nos módulos alterados.
- [x] Rodar testes unitários de fila/storage/selectors.
- [x] Atualizar este arquivo com status final.

---

## Estado final

### Entregas concluídas
- Bootstrap seguro e replicável em novos ambientes.
- Persistência robusta em SQLite com migração automática.
- Sessões persistentes com TTL.
- Perfil Chromium dedicado opcional para analisador.
- DLQ com retry manual.
- Logs em SSE + métrica Prometheus.
- Seletores críticos centralizados e smoke-tested.
- README enxuto + docs segmentadas.

### Observações
- Itens de arquitetura de longo prazo (plugin de domínio e fila distribuída com Redis/RQ) foram **documentados como próximos passos**, mas não eram necessários para fechar este ciclo de refactor local.

---

## Prompt de retomada (se necessário)
"Leia `REFACTOR_PROGRESS.md`, valide que todos os itens marcados [x] continuam íntegros após merges recentes e prossiga apenas em melhorias incrementais (pluginização de integrações de domínio, fila distribuída, dashboards Grafana versionados e hardening de smoke tests end-to-end)."
