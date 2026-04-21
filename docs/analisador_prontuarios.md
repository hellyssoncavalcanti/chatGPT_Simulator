# Analisador de Prontuários

## Fluxo
1. Busca pendências no backend PHP.
2. Monta prompt clínico.
3. Chama `POST /v1/chat/completions` local.
4. Persiste resultado no backend remoto.

## Variáveis principais
- `ANALISADOR_POLL_INTERVAL`
- `ANALISADOR_BATCH_SIZE`
- `ANALISADOR_BROWSER_PROFILE`
- `ANALISADOR_LLM_THROTTLE_MIN/MAX`

## Perfil dedicado
O payload enviado ao simulator inclui `browser_profile`.
Se ausente/inválido, o `browser.py` cai para perfil `default`.
