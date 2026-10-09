# Деплой — пошаговая инструкция

Полный мини-гайд: как отправить код в GitHub и развернуть бота на Render.
Статус проекта на момент сохранения: см. раздел «Что сделано» внизу.

---

## 1. Пуш в GitHub

Локальная ветка `main` уже содержит все доработки (4 коммита). Зайди в папку проекта и выполни:

```sh
cd ~/Desktop/Телеграм\ проект\ 
git status                 # должно быть «working tree clean»
git push origin main
```

Если git потребует авторизацию (нет gh / ключей), поставь GitHub CLI:

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"  # если нет brew
brew install gh
gh auth login              # GitHub.com → HTTPS → Login with a web browser → Authorize
git push origin main
```

После пуша проверь: на github.com в репозитории `Telegram_app` должны появиться коммиты
`Improve Mini App UX…`, `Add client-side demo mode…`, `Add media albums…`.

> ⚠️ `.env` в репозиторий не пушится (в `.gitignore`) — это правильно. Токен бота хранится только локально и настройках Render.

## 2. Деплой на Render

1. Зарегистрируйся на **render.com** (бесплатный аккаунт).
2. **New → Blueprint** → подключи GitHub и выбери репозиторий `Telegram_app`.
   - В репозитории уже лежит `render.yaml`: создаст Docker-сервис, free plan, health check `/health`.
3. Render попросит задать переменные окружения:
   - `TELEGRAM_BOT_TOKEN` — токен из @BotFather
   - `ADMIN_TELEGRAM_ID` — твой числовой ID (узнать: @userinfobot)
4. Дождись деплоя, открой `https://<имя>.onrender.com/health` → должно быть `{"status":"ok"}`.
5. В Telegram: чат с ботом → кнопка **«Открыть приложение»** → добавь каналы → **Проверить подключение** → **Запустить пересылку**.

Кнопка меню бота настраивается автоматически (по `RENDER_EXTERNAL_URL`).

## 3. Безопасность токена

Токен, записанный в чат при разработке, считается «сгоревшим».
Перед боевым использованием перевыпусти его: в @BotFather нажми **/revoke** (или **/token**),
затем обнови `TELEGRAM_BOT_TOKEN` в настройках Render. Локальный `.env` тоже обнови.

## 4. Локальный запуск (для отладки)

```sh
cd ~/Desktop/Телеграм\ проект\ 
. .venv/bin/activate
python -m unittest discover -s tests -v      # 14 тестов
python -m pip install -r requirements.txt     # если окружения ещё нет
```

Проверка интерфейса в браузере без Telegram (демо-данные):

- тёмная: `python3 -m http.server 8000` → `http://127.0.0.1:8000/?demo=1`
- светлая: `...?demo=1&light=1`

Полная проверка пересылки через телефон (HTTPS нужен Telegram):

```sh
brew install ngrok
ngrok config add-authtoken <токен_ngrok>
bash scripts/run_local.sh   # поднимет ngrok + uvicorn, пропишет APP_URL
```

## Что сделано в этой версии

- **UI/UX Mini App**: тема Telegram (тёмная/светлая), валидация формы, модалка подтверждения запуска/остановки, статистика (чипы), живой статус, автообновление раз в минуту, хаптика, демо-режим `?demo=1`.
- **Надёжность**: альбомы через `sendMediaGroup` (пачками по 10), ретраи Bot API на 429/5xx с экспоненциальной паузой, дедупликация через историю отправки.
- **Инфраструктура**: скрипт `scripts/run_local.sh` для локального прогона через ngrok.

Известные ограничения бесплатного плана Render: сервис засыпает при неактивности (пересылка идёт только в бодром состоянии); диск эфемерный (курсоры сбрасываются при рестарте — повторных постов благодаря дедупликации не будет, но могут пропускаться новые). Для непрерывной работы — paid-план с persistent disk.