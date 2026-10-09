# Telegram Forwarder Mini App

Private Telegram Mini App for forwarding new posts from up to five public Telegram channels into one destination channel. It preserves post text and links, splits long text to Telegram's message limits, and uploads supported photos and videos. Forwarding is off until the owner starts it in the Mini App. The bot token is read only from the server environment and is never returned to the Mini App.

The Mini App itself adapts to the Telegram theme, validates the form before saving, asks for confirmation before the forwarding runs, and shows live status with per-status statistics (forwarded / filtered / skipped / errors), auto-refreshing every minute.

## Reliability

- **Albums:** posts with several photos/videos are sent as one Telegram media group (`sendMediaGroup`), split into groups of 10 when larger.
- **Transient errors:** Telegram Bot API calls are retried on `429` (respecting `retry_after`) and `5xx` with exponential backoff; permanent errors (400/401/403) fail fast.
- **De-duplication:** a post is never forwarded twice, even after a restart that loses the cursor — the history table is checked before publishing.

## Deploy on Render

1. Push this project to a **private GitHub repository** and create a Render Blueprint from that repository. The included `render.yaml` creates one Docker web service on Render's free plan.
2. In Render, set `TELEGRAM_BOT_TOKEN` to a newly issued bot token, and set `ADMIN_TELEGRAM_ID` to the numeric Telegram user ID allowed to administer the Mini App.
3. Wait for the service URL to be assigned and verify `/health` returns `{"status":"ok"}`. Render provides `RENDER_EXTERNAL_URL`; the app uses it to add an “Открыть приложение” Web App button to the configured admin's bot chat.
4. Open the bot chat in Telegram and launch the Mini App from its menu button. Add the public source channels and destination; the bot must be an administrator in the destination with permission to publish.

**Free-plan limitations:** Render may spin down an inactive free web service. Forwarding only happens while the service is awake, so continuous forwarding is not guaranteed if it is left idle. The free filesystem is ephemeral; settings, cursors, and history can be lost after a restart or deploy, and previously forwarded posts might be seen again. Upgrade to a paid instance with a persistent disk for dependable unattended forwarding. The Mini App rejects requests that are not signed by Telegram or do not come from `ADMIN_TELEGRAM_ID`.

## Local checks

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

To run the web server locally, provide `TELEGRAM_BOT_TOKEN` and `ADMIN_TELEGRAM_ID`, then run:

```sh
uvicorn app.main:app --reload
```

The Mini App itself must be opened from Telegram using an HTTPS URL. A localhost browser tab is not a valid Telegram Mini App launch context.

### Full local check with ngrok (through Telegram)

To test real end-to-end forwarding on your machine, the app must be reachable over HTTPS. The helper script opens an ngrok tunnel, picks up the public URL as `APP_URL`, and starts uvicorn:

```sh
brew install ngrok
ngrok config add-authtoken <YOUR_NGROK_TOKEN>   # from dashboard.ngrok.com
cp .env.example .env                            # fill in the bot token and your ID
bash scripts/run_local.sh
```

Then open the bot chat in Telegram and tap **«Открыть приложение»** — the menu button is configured automatically. Add one source channel and the destination channel (the bot must be an administrator there), press **«Проверить подключение»**, then **«Запустить пересылку»**.
