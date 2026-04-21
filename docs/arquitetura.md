# Arquitetura

## Componentes
- `Scripts/main.py`: bootstrap de threads HTTP/HTTPS + worker browser.
- `Scripts/server.py`: API Flask, autenticação, fila e streaming.
- `Scripts/browser.py`: automação Playwright/Chromium.
- `Scripts/shared.py`: `browser_queue` com prioridade + DLQ.
- `Scripts/db.py`: schema/migração SQLite.
- `Scripts/storage.py`: persistência de chats/mensagens.
- `Scripts/auth.py`: usuários e sessões com TTL.

## Persistência
- Principal: `db/app.db` (SQLite).
- Legado/migração: `db/history.json` e `db/users/users.json`.

## Observabilidade
- `GET /api/metrics`: métricas operacionais em JSON.
- `GET /metrics`: formato Prometheus.
- `GET /api/logs/tail`: últimas linhas (polling).
- `GET /api/logs/stream`: SSE de logs.
