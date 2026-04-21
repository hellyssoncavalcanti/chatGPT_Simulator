# Refactor Progress (ChatGPT Simulator)

## Status geral
- [x] Bootstrap seguro com templates (`config.example.py` + `sync_github_settings.example.ps1`) no `0. start.bat`.
- [x] Credencial padrão alinhada para `admin/admin` apenas em instalação nova.
- [x] Allowlist priorizando API key (fallback por origem/IP apenas defesa em profundidade).
- [x] Suporte a perfil Chromium por request (`browser_profile`) no fluxo `server.py -> browser.py`.
- [x] `analisador_prontuarios.py` envia `browser_profile` em todos os payloads LLM.
- [x] Migração de persistência JSON para SQLite (`Scripts/db.py`, `storage.py`, `auth.py`).
- [x] README atualizado para refletir bootstrap seguro, SQLite/sessões persistentes e perfil dedicado do analisador.
- [ ] DLQ de fila (`/api/queue/failed`) pendente.
- [ ] SSE de logs (`/api/logs/stream`) pendente.
- [ ] Prometheus em `/metrics` pendente.
- [ ] Centralização completa de seletores Playwright em `selectors.py` pendente.
- [ ] Split completo do README em `docs/` pendente.

## Próximos passos sugeridos
1. Implementar DLQ no `BrowserTaskQueue` e endpoints de retry manual no `server.py`.
2. Adicionar SSE de logs reaproveitando leitura incremental de `LOG_PATH`.
3. Expor `/metrics` no formato Prometheus com `prometheus_client`.
4. Introduzir `Scripts/selectors.py` com hash/data de validação e smoke-test.
5. Quebrar README em `docs/` (arquitetura, whatsapp, analisador, agente autônomo, sync) e manter README raiz como índice.

## Prompt de retomada (copiar em novo chat)
"Continue o refactor do projeto `/workspace/chatGPT_Simulator` lendo `REFACTOR_PROGRESS.md` primeiro. Respeite os requisitos do usuário: API key como autenticação primária (allowlist apenas fallback), bootstrap de `Scripts/config.py` e `Scripts/sync_github_settings.ps1` via `0. start.bat` quando ausentes, reset para `admin/admin` apenas em fresh install, suporte de `browser_profile` end-to-end com fallback para perfil default, e manter `sync_github` autônomo. Finalize os itens pendentes (DLQ, SSE logs, Prometheus, selectors centralizados e split docs), rode testes, atualize `REFACTOR_PROGRESS.md`, faça commit e abra PR."
