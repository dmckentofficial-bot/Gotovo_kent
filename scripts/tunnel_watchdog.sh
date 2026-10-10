#!/usr/bin/env bash
# Сторож туннеля: проверяет публичный URL раз в минуту, при падении
# поднимает новый и переписывает кнопку меню + APP_URL.
# Запуск: nohup ./scripts/tunnel_watchdog.sh >/tmp/tunnel_watchdog.log 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8005}"
LOG_PREFIX="[watchdog]"

current_url() {
  grep -E '^APP_URL=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' || true
}

tunnel_alive() {
  local url="$1"
  [ -n "$url" ] || return 1
  # Туннели мигают: живым считаем только при 2 успехах из 3 замеров.
  local ok=0
  for _ in 1 2 3; do
    if curl -s -m 10 "$url/health" >/dev/null 2>&1; then
      ok=$((ok + 1))
    fi
  done
  [ "$ok" -ge 2 ]
}

start_tunnel() {
  pkill -f "localhost.run" 2>/dev/null || true
  sleep 1
  rm -f /tmp/lhr_tunnel.log
  nohup ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
    -R 80:localhost:"$PORT" nokey@localhost.run > /tmp/lhr_tunnel.log 2>&1 &
  local url=""
  for _ in $(seq 1 20); do
    sleep 1
    url=$(grep -oE 'https://[a-z0-9.-]+\.lhr\.life' /tmp/lhr_tunnel.log 2>/dev/null | sort -u | head -1 || true)
    [ -n "$url" ] && break
  done
  echo "$url"
}

apply_url() {
  local url="$1"
  if grep -q '^APP_URL=' .env 2>/dev/null; then
    perl -i -pe "s#^APP_URL=.*#APP_URL=$url#" .env
  else
    echo "APP_URL=$url" >> .env
  fi
  set -a && [ -f .env ] && . ./.env && set +a
  export APP_URL="$url"
  .venv/bin/python - <<PY 2>/dev/null
import os
from app import worker, database
worker.set_app_url("$url")
# Только общая кнопка: лишние вызовы роняют флуд-лимит Telegram.
try:
    worker.configure_menu_button("$url", None)
    database.set_state("menu_url", "$url")
    print("$LOG_PREFIX кнопка меню -> $url")
except Exception as exc:
    print("$LOG_PREFIX кнопка меню не обновилась:", exc)
PY
}

# Кнопка могла протухнуть раньше адреса (флуд-лимит): добиваем её каждый тик.
ensure_menu() {
  local url="$1"
  set -a && [ -f .env ] && . ./.env && set +a
  export APP_URL="$url"
  .venv/bin/python - <<PY 2>/dev/null
import os
from app import worker, database
stored = ""
try:
    stored = database.get_state("menu_url", "")
except Exception:
    pass
if stored != "$url":
    try:
        worker.configure_menu_button("$url", None)
        database.set_state("menu_url", "$url")
        print("$LOG_PREFIX кнопка меню догнана -> $url")
    except Exception as exc:
        print("$LOG_PREFIX кнопка меню ждёт (флуд):", str(exc)[:80])
PY
}

echo "$LOG_PREFIX старт (порт $PORT)"
while true; do
  url="$(current_url)"
  if tunnel_alive "$url"; then
    echo "$LOG_PREFIX ok: $url"
    ensure_menu "$url"
  else
    echo "$LOG_PREFIX туннель упал (${url:-пустой}) — поднимаю новый"
    url="$(start_tunnel)"
    if tunnel_alive "$url"; then
      echo "$LOG_PREFIX новый туннель: $url"
      apply_url "$url"
    else
      echo "$LOG_PREFIX не удалось поднять туннель, повтор через минуту"
    fi
  fi
  sleep 60
done
