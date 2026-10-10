#!/usr/bin/env bash
# Перезапуск сервера и HTTPS-туннеля локально.
# Использование: ./scripts/restart.sh
set -euo pipefail
cd "$(dirname "$0")/.."

start_server() {
  if curl -s -m 3 http://localhost:8000/health >/dev/null 2>&1; then
    echo "✓ uvicorn уже работает."
    return
  fi
  echo "Запускаю uvicorn…"
  set -a && [ -f .env ] && . ./.env && set +a
  nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > /tmp/uvicorn_forwarder.log 2>&1 &
  sleep 3
  if ! curl -s -m 3 http://localhost:8000/health >/dev/null 2>&1; then
    echo "✗ uvicorn не поднялся. Лог: /tmp/uvicorn_forwarder.log"
    exit 1
  fi
  echo "✓ uvicorn работает на порту 8000."
}

current_url() {
  grep -E '^APP_URL=' .env | head -1 | cut -d= -f2- | tr -d '\r' || true
}

restart_tunnel() {
  echo "Перезапускаю туннель…"
  pkill -f "localhost.run" 2>/dev/null || true
  sleep 1
  nohup ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
    -R 80:localhost:8000 nokey@localhost.run > /tmp/lhr_tunnel.log 2>&1 &
  local url=""
  for _ in $(seq 1 15); do
    sleep 1
    url=$(grep -oE 'https://[a-z0-9]+\.lhr\.life' /tmp/lhr_tunnel.log | sort -u | head -1 || true)
    [ -n "$url" ] && break
  done
  if [ -n "$url" ] && curl -s -m 10 "$url/health" >/dev/null 2>&1; then
    echo "✓ URL туннеля: $url"
  else
    echo "✗ Не удалось получить рабочий URL туннеля."
    exit 1
  fi
  if grep -q '^APP_URL=' .env; then
    perl -i -pe "s#^APP_URL=.*#APP_URL=$url#" .env
  else
    echo "APP_URL=$url" >> .env
  fi
}

ensure_tunnel() {
  local url
  url="$(current_url)"
  if [ -n "$url" ] && curl -s -m 10 "$url/health" >/dev/null 2>&1; then
    echo "✓ Туннель работает: $url"
  else
    echo "Туннель недоступен (${url:-пустой}) — перезапускаю."
    restart_tunnel
    url="$(current_url)"
  fi
  echo "Обновляю кнопку меню на $url"
  set -a && [ -f .env ] && . ./.env && set +a
  export APP_URL="$url"
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    .venv/bin/python - <<PY
import os
from dotenv import load_dotenv
load_dotenv(".env")
os.environ.setdefault("DATABASE_PATH", "data/app.sqlite3")
from app import worker
try:
    admin_chat = int(os.environ.get("ADMIN_TELEGRAM_ID", "") or 0) or None
except ValueError:
    admin_chat = None
worker.configure_menu_button(os.environ["APP_URL"], admin_chat)
print("✓ Кнопка меню →", os.environ["APP_URL"])
PY
  else
    echo "TELEGRAM_BOT_TOKEN не задан — пропускаю обновление кнопки."
  fi
}

start_server
ensure_tunnel
echo "Готово. Локально: http://localhost:8000 · Туннель: $(current_url)"