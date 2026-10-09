#!/usr/bin/env bash
#
# Локальный запуск Telegram Forwarder Mini App для реальной проверки.
#
# Что делает:
#   1. Создаёт виртуальное окружение и ставит зависимости.
#   2. Поднимает ngrok-туннель до локального FastAPI (HTTPS, нужен Telegram).
#   3. Прописывает APP_URL из ngrok, чтобы у бота появилась кнопка «Открыть приложение».
#   4. Запускает uvicorn с авто-перезагрузкой.
#
# Требования: python3, ngrok (brew install ngrok), токен из @BotFather.
# Перед первым запуском:  ngrok config add-authtoken <ТОКЕН_С_ngrok_DASHBOARD>
#
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v ngrok >/dev/null 2>&1; then
  echo "ngrok не найден. Установите:  brew install ngrok"
  echo "Затем:  ngrok config add-authtoken <ваш токен>  (берётся на dashboard.ngrok.com)"
  exit 1
fi

# --- виртуальное окружение --------------------------------------------------
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q -r requirements.txt

# --- настройки --------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "Создан файл .env. Заполните его и запустите скрипт снова:"
  echo "  TELEGRAM_BOT_TOKEN  — токен из @BotFather"
  echo "  ADMIN_TELEGRAM_ID   — ваш числовой ID (узнать можно у @userinfobot)"
  exit 1
fi
set -a
# shellcheck disable=SC1091
. ./.env
set +a

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [[ "$TELEGRAM_BOT_TOKEN" == *replace_with* ]]; then
  echo "TELEGRAM_BOT_TOKEN не задан в .env. Отредактируйте файл и повторите."
  exit 1
fi
if [ -z "${ADMIN_TELEGRAM_ID:-}" ] || [ "$ADMIN_TELEGRAM_ID" = "123456789" ]; then
  echo "ADMIN_TELEGRAM_ID не задан в .env. Отредактируйте файл и повторите."
  exit 1
fi

# --- ngrok ----------------------------------------------------------------
echo "Запускаю ngrok для порта 8000…"
ngrok http 8000 --log=stdout >/tmp/telegram_app_ngrok.log 2>&1 &
NGROK_PID=$!
trap 'kill "$NGROK_PID" 2>/dev/null || true' EXIT

tunnel_url=""
for _ in $(seq 1 40); do
  tunnel_url=$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(next((t['public_url'] for t in d.get('tunnels',[]) if t.get('proto')=='https'),''))" 2>/dev/null || true)
  [ -n "$tunnel_url" ] && break
  sleep 1
done
if [ -z "$tunnel_url" ]; then
  echo "Не удалось получить URL туннеля. Проверьте авторизацию ngrok:"
  echo "  ngrok config add-authtoken <ваш токен>"
  tail -5 /tmp/telegram_app_ngrok.log || true
  exit 1
fi
export APP_URL="$tunnel_url"
echo "🔗 Туннель (HTTPS): $tunnel_url"
echo "Откройте в Telegram чат с ботом и нажмите кнопку «Открыть приложение»."

# --- запуск ---------------------------------------------------------------
echo "Запускаю uvicorn на http://0.0.0.0:8000"
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload