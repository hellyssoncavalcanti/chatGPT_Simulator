#!/usr/bin/env bash
set -euo pipefail

# Executa uma sequência de ações sem pausas interativas.
# Uso:
#   ./Scripts/codex_autoflow.sh "cmd 1" "cmd 2" "cmd 3"
# Exemplo:
#   ./Scripts/codex_autoflow.sh \
#     "python3 -m pytest tests/test_server_helpers.py" \
#     "git status --short"

if [ "$#" -eq 0 ]; then
  echo "Uso: $0 \"cmd 1\" \"cmd 2\" ..."
  exit 1
fi

step=1
for cmd in "$@"; do
  echo "▶️ [${step}/$#] $cmd"
  bash -lc "$cmd"
  step=$((step + 1))
done

echo "✅ Fluxo concluído sem pausas."
