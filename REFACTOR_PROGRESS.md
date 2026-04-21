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

## Próximos passos sugeridos
1. Evoluir DLQ para aceitar retry por ID estável (além do índice) e endpoint de purge seguro.
2. Expandir métricas Prometheus para labels por origem/prioridade da fila.
3. Adicionar testes unitários de simulação humana (delay/hesitação/typo) e ativá-los no CI.
4. Opcional: consolidar README raiz como índice mais curto e mover detalhes operacionais para `docs/`.

## Prompt de retomada (copiar em novo chat)
"Continue o refactor do projeto `/workspace/chatGPT_Simulator` lendo `REFACTOR_PROGRESS.md` primeiro. Preserve prioridade máxima para simulação humana no browser (digitação realista e comportamento não robótico), mantendo API key como autenticação primária (allowlist apenas fallback), bootstrap de `Scripts/config.py` e `Scripts/sync_github_settings.ps1` via `0. start.bat` quando ausentes, reset para `admin/admin` apenas em fresh install, suporte de `browser_profile` end-to-end com fallback para perfil default, e `sync_github` autônomo. Foque agora em hardening, métricas e testes adicionais; rode testes possíveis no ambiente, atualize `REFACTOR_PROGRESS.md`, faça commit e abra PR."
