#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-8080}"
DSH_HOST="127.0.0.1"
DSH_PORT="3080"

# --- Senha: se DSH_UI_PASSWORD não veio, gera uma e persiste no volume ---
PASSWORD_FILE="/data/.dashboard-password"

if [ -n "${DSH_UI_PASSWORD:-}" ]; then
    PLAIN_PASSWORD="$DSH_UI_PASSWORD"
else
    if [ -f "$PASSWORD_FILE" ]; then
        PLAIN_PASSWORD="$(cat "$PASSWORD_FILE")"
    else
        PLAIN_PASSWORD="$(head -c 32 /dev/urandom | base64 | tr -d '=+/' | head -c 32)"
        mkdir -p /data
        echo "$PLAIN_PASSWORD" > "$PASSWORD_FILE"
        chmod 600 "$PASSWORD_FILE"
        echo "=================================================="
        echo " SENHA GERADA (guarde isto): $PLAIN_PASSWORD"
        echo "=================================================="
    fi
fi

export DSH_UI_USERNAME="${DSH_UI_USERNAME:-admin}"

# Gera o hash bcrypt que o Caddy espera
DSH_UI_PASSWORD_HASH="$(caddy hash-password --plaintext "$PLAIN_PASSWORD")"
export DSH_UI_PASSWORD_HASH

# --- Workspace e home do DSH no volume ---
export DSH_WORKSPACE="${DSH_WORKSPACE:-/data/workspace}"
export DSH_HOME="${DSH_HOME:-/data/.dsh}"
mkdir -p "$DSH_WORKSPACE" "$DSH_HOME"

# --- Inicia o DSH em loopback ---
echo "[entrypoint] Iniciando DSH em ${DSH_HOST}:${DSH_PORT}..."
npx --yes @deepseek-ai/dsh --profile web \
    --no-open \
    --host "$DSH_HOST" \
    --port "$DSH_PORT" &

DSH_PID=$!

# --- Inicia o Caddy na porta pública ---
echo "[entrypoint] Iniciando Caddy em :${PORT}..."
caddy run --config /app/Caddyfile --adapter caddyfile &
CADDY_PID=$!

# --- Supervisão: se qualquer um cair, derruba o container ---
wait -n "$DSH_PID" "$CADDY_PID"
echo "[entrypoint] Um dos processos morreu. Encerrando container."
kill "$DSH_PID" "$CADDY_PID" 2>/dev/null || true
exit 1
