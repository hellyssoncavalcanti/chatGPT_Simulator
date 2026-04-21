# ChatGPT Simulator

Sistema híbrido (Flask + Playwright + Chromium persistente) para operar o ChatGPT via navegador real com API HTTP/HTTPS.

## Início rápido (Windows)
1. Execute `0. start.bat`.
2. O script cria `Scripts/config.py` e `Scripts/sync_github_settings.ps1` a partir dos templates caso não existam.
3. Em instalação nova, reseta credenciais para `admin/admin`.
4. Cria/ativa `.venv`, instala dependências e sobe `Scripts/main.py`.

## Segurança e autenticação
- Autenticação primária: API key (`Authorization: Bearer`, body `api_key` ou query `api_key`).
- Sessão web com cookie `session_token` (persistida em SQLite com TTL).
- Fallback de defesa em profundidade com validação de origem/IP quando não há credencial válida.

## Persistência
- Banco principal: `db/app.db` (chats, mensagens, usuários, sessões).
- Migração automática de JSON legado na primeira inicialização.

## Observabilidade
- `GET /api/metrics` (JSON)
- `GET /metrics` (Prometheus)
- `GET /api/logs/tail` (polling)
- `GET /api/logs/stream` (SSE)
- `GET /api/queue/status`
- `GET /api/queue/failed` + `POST /api/queue/failed/retry`

## Documentação detalhada
- [Arquitetura](docs/arquitetura.md)
- [Analisador de Prontuários](docs/analisador_prontuarios.md)
- [WhatsApp](docs/whatsapp.md)
- [Agente Autônomo](docs/agente_autonomo.md)
- [Sync GitHub](docs/sync_github.md)

## Refactor em andamento
Acompanhe o plano completo em [`REFACTOR_PROGRESS.md`](REFACTOR_PROGRESS.md).
