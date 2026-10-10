import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Переменные окружения из .env (локально), в облаке — из настроек сервиса.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from . import auth, database, pin, telegram, worker


logger = logging.getLogger("app.forwarder")


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
stop_event = threading.Event()
worker_thread = None


class ConfigPayload(BaseModel):
    sources: list[str] = Field(default_factory=list, max_length=5)
    destination: str = Field(default="", max_length=100)
    filter_enabled: bool = False
    filter_terms: str = Field(default="", max_length=2000)
    filter_mode: Literal["any", "all"] = "any"


class ServicePayload(BaseModel):
    enabled: bool


class BotPayload(BaseModel):
    token: str = Field(default="", max_length=200)
    admin_id: str = Field(default="", max_length=64)


class SignaturePayload(BaseModel):
    enabled: bool
    text: str = Field(default="", max_length=1000)


class PinPayload(BaseModel):
    pin: str = Field(pattern=pin.PIN_PATTERN)


class PinChangePayload(BaseModel):
    current_pin: str = Field(pattern=pin.PIN_PATTERN)
    pin: str = Field(pattern=pin.PIN_PATTERN)


class AccessPayload(BaseModel):
    password: str = Field(min_length=4, max_length=8, pattern=r"^\d{4,8}$")


def _key_fail_state():
    count = int(database.get_state("key_fails", "0") or "0")
    until = float(database.get_state("key_locked_until", "0") or "0")
    return count, until


def _key_locked():
    _, until = _key_fail_state()
    return time.time() < until


def _register_key_fail():
    count, _ = _key_fail_state()
    count += 1
    if count >= 5:
        database.set_state("key_fails", "0")
        database.set_state("key_locked_until", int(time.time()) + 60)
    else:
        database.set_state("key_fails", str(count))


def _authorize_key(key):
    """Ключ считается верным, если он совпадает с резервным ADMIN_KEY или с паролем доступа."""
    if auth.check_admin_key(key):
        return True
    stored = database.get_state("access_hash", "")
    return bool(stored and pin.verify_pin(stored, key))


def _password_user():
    admin_id = os.environ.get("ADMIN_TELEGRAM_ID", "").strip()
    return {
        "id": int(admin_id) if admin_id.isdigit() else admin_id,
        "username": "admin",
        "first_name": "Владелец",
        "auth_method": "password",
    }


def require_admin(
    x_telegram_init_data: str | None = Header(default=None),
    x_admin_key: str | None = Header(default=None),
):
    key = (x_admin_key or "").strip()
    if key and _authorize_key(key):
        # Правильный пароль работает всегда и снимает блокировку.
        database.set_state("key_fails", "0")
        database.set_state("key_locked_until", "0")
        return _password_user()
    if x_telegram_init_data:
        try:
            return auth.authenticate(x_telegram_init_data, "")
        except PermissionError as exc:
            logger.warning("Авторизация отклонена: %s", exc)
            raise HTTPException(
                status_code=401, detail=str(exc), headers={"X-Requires-Auth": "1"}
            ) from exc
    if key:
        was_locked = _key_locked()
        _register_key_fail()
        if was_locked:
            _, until = _key_fail_state()
            wait = max(0, int(until - time.time()))
            raise HTTPException(status_code=429, detail=f"Слишком много неверных попыток. Подождите {wait} с.")
        raise HTTPException(status_code=401, detail="Неверный пароль доступа.", headers={"X-Requires-Auth": "1"})
    raise HTTPException(
        status_code=401,
        detail="Введите пароль доступа или откройте приложение через Telegram.",
        headers={"X-Requires-Auth": "1"},
    )


def _pin_gate():
    locked_until = database.get_state("pin_locked_until", "0")
    try:
        wait = int(locked_until) - int(time.time())
    except ValueError:
        wait = 0
    if wait > 0:
        raise HTTPException(status_code=429, detail=f"Слишком много попыток. Подождите {wait} с.")


def _pin_fail():
    fails = int(database.get_state("pin_fails", "0") or "0") + 1
    database.set_state("pin_fails", fails)
    seconds = pin.lockout_seconds(fails)
    if seconds:
        database.set_state("pin_locked_until", int(time.time()) + seconds)
    return fails


def _pin_reset():
    database.set_state("pin_fails", "0")
    database.set_state("pin_locked_until", "0")


@asynccontextmanager
async def lifespan(_app):
    global worker_thread
    database.initialize()

    # Токен и ID владельца можно менять из интерфейса — храним в БД,
    # при первом старте переносим значения из окружения (.env).
    stored_token = database.get_state("bot_token", "")
    telegram.set_bot_token(stored_token or os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    if stored_token:
        database.set_state("bot_token", stored_token)

    stored_admin = database.get_state("admin_telegram_id", "").strip()
    admin_id = stored_admin or os.environ.get("ADMIN_TELEGRAM_ID", "").strip()
    if stored_admin:
        os.environ["ADMIN_TELEGRAM_ID"] = stored_admin

    app_url = os.environ.get("APP_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
    if telegram.BOT_TOKEN and app_url and admin_id:
        try:
            bot = telegram.bot_request("getMe")
            database.set_state("bot_username", bot.get("username", ""))
            try:
                admin_chat = int(admin_id)
            except ValueError:
                admin_chat = None
            worker.configure_menu_button(app_url, admin_chat)
            database.set_state("menu_error", "")
        except RuntimeError as exc:
            database.set_state("menu_error", str(exc))
    else:
        database.set_state("menu_error", "Укажите токен бота, Telegram ID владельца и URL приложения.")

    stop_event.clear()
    worker_thread = threading.Thread(target=worker.run, args=(stop_event,), daemon=True)
    worker_thread.start()
    yield
    stop_event.set()
    if worker_thread:
        worker_thread.join(timeout=5)


app = FastAPI(title="Telegram Forwarder Mini App", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/bot")
def get_bot(_user=Depends(require_admin)):
    token = telegram.get_bot_token()
    return {
        "bot_username": database.get_state("bot_username", ""),
        "admin_id": database.get_state("admin_telegram_id", "") or os.environ.get("ADMIN_TELEGRAM_ID", ""),
        "token_set": bool(token),
        "token_hint": f"…{token[-4:]}" if len(token) >= 4 else "",
    }


@app.put("/api/bot")
def put_bot(payload: BotPayload, _user=Depends(require_admin)):
    new_token = payload.token.strip()
    new_admin = payload.admin_id.strip()

    if new_token:
        # Сначала проверяем токен через getMe — применяем только рабочий.
        previous_token = telegram.get_bot_token()
        telegram.set_bot_token(new_token)
        try:
            bot = telegram.bot_request("getMe")
        except RuntimeError as exc:
            telegram.set_bot_token(previous_token)
            raise HTTPException(status_code=400, detail=f"Токен не принят: {exc}") from exc
        database.set_state("bot_token", telegram.get_bot_token())
        database.set_state("bot_username", bot.get("username", ""))

    if new_admin:
        if not new_admin.lstrip("-").isdigit():
            raise HTTPException(status_code=422, detail="ID владельца должен быть числом.")
        database.set_state("admin_telegram_id", new_admin)
        os.environ["ADMIN_TELEGRAM_ID"] = new_admin

    # Обновляем кнопку меню под нового бота/владельца.
    app_url = os.environ.get("APP_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
    if telegram.get_bot_token() and app_url:
        try:
            worker.configure_menu_button(app_url, int(new_admin) if new_admin.lstrip("-").isdigit() else None)
            database.set_state("menu_error", "")
        except RuntimeError as exc:
            database.set_state("menu_error", str(exc))

    return {
        "bot_username": database.get_state("bot_username", ""),
        "admin_id": database.get_state("admin_telegram_id", ""),
        "token_hint": f"…{telegram.get_bot_token()[-4:]}" if len(telegram.get_bot_token()) >= 4 else "",
    }


@app.get("/api/config")
def get_config(_user=Depends(require_admin)):
    return database.get_config()


@app.put("/api/config")
def put_config(payload: ConfigPayload, _user=Depends(require_admin)):
    if not payload.destination.strip():
        raise HTTPException(status_code=422, detail="Укажите канал назначения.")
    try:
        destination = telegram.normalize_destination(payload.destination)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    normalized_sources = []
    for source in payload.sources:
        if not source.strip():
            continue
        try:
            username = telegram.normalize_channel(source)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if username.casefold() not in {value.casefold() for value in normalized_sources}:
            normalized_sources.append(username)
    if not normalized_sources:
        raise HTTPException(status_code=422, detail="Добавьте хотя бы один публичный канал-источник.")
    if payload.filter_enabled and not payload.filter_terms.strip():
        raise HTTPException(status_code=422, detail="Укажите слова или хэштеги для фильтра.")

    config = {
        **database.get_config(),
        "sources": normalized_sources,
        "destination": destination,
        "filter_enabled": payload.filter_enabled,
        "filter_terms": payload.filter_terms.strip(),
        "filter_mode": payload.filter_mode,
    }
    database.set_config(config)
    return {"saved": True, "config": config}


@app.post("/api/test")
def test_connection(_user=Depends(require_admin)):
    try:
        return worker.validate_configuration()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/status")
def status(_user=Depends(require_admin)):
    config = database.get_config()
    last_checked = database.get_state("last_checked")
    try:
        destination = telegram.normalize_destination(config["destination"]) if config["destination"] else ""
    except ValueError:
        destination = config["destination"]
    return {
        "running": bool(config.get("enabled") and telegram.BOT_TOKEN and config["sources"] and config["destination"]),
        "bot": database.get_state("bot_username"),
        "sources": config["sources"],
        "destination": destination,
        "last_checked": int(last_checked) if last_checked.isdigit() else None,
        "last_error": database.get_state("last_error"),
        "menu_error": database.get_state("menu_error"),
    }


@app.get("/api/stats")
def stats(_user=Depends(require_admin)):
    return database.history_stats()


@app.put("/api/service")
def set_service(payload: ServicePayload, _user=Depends(require_admin)):
    config = database.get_config()
    if payload.enabled:
        try:
            worker.validate_configuration()
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    config["enabled"] = payload.enabled
    database.set_config(config)
    database.set_state("last_error", "")
    if payload.enabled:
        # Сразу проверяем источники, не дожидаясь следующего тика воркера.
        threading.Thread(target=worker.poll_once, daemon=True).start()
    return {"running": payload.enabled}


@app.put("/api/signature")
def put_signature(payload: SignaturePayload, _user=Depends(require_admin)):
    text = payload.text.strip()
    if payload.enabled and not text:
        raise HTTPException(status_code=422, detail="Введите текст подписи или отключите её.")
    config = database.get_config()
    config["signature_enabled"] = payload.enabled
    config["signature_text"] = text
    database.set_config(config)
    return {"saved": True, "signature_enabled": payload.enabled, "signature_text": text}


@app.get("/api/pin/status")
def pin_status(_user=Depends(require_admin)):
    return {"enabled": bool(database.get_state("pin_hash", ""))}


@app.post("/api/pin/setup")
def pin_setup(payload: PinPayload, _user=Depends(require_admin)):
    if database.get_state("pin_hash", ""):
        raise HTTPException(status_code=409, detail="PIN-код уже установлен. Смените его в настройках.")
    database.set_state("pin_hash", pin.hash_pin(payload.pin))
    _pin_reset()
    return {"enabled": True}


@app.post("/api/pin/verify")
def pin_verify(payload: PinPayload, _user=Depends(require_admin)):
    _pin_gate()
    stored = database.get_state("pin_hash", "")
    if not stored:
        return {"ok": True, "enabled": False}
    if not pin.verify_pin(stored, payload.pin):
        _pin_fail()
        raise HTTPException(status_code=401, detail="Неверный PIN-код.")
    _pin_reset()
    return {"ok": True, "enabled": True}


@app.post("/api/pin/change")
def pin_change(payload: PinChangePayload, _user=Depends(require_admin)):
    _pin_gate()
    stored = database.get_state("pin_hash", "")
    if not stored:
        raise HTTPException(status_code=400, detail="PIN-код ещё не установлен.")
    if not pin.verify_pin(stored, payload.current_pin):
        _pin_fail()
        raise HTTPException(status_code=401, detail="Неверный текущий PIN-код.")
    if payload.pin == payload.current_pin:
        raise HTTPException(status_code=400, detail="Новый PIN-код должен отличаться от текущего.")
    database.set_state("pin_hash", pin.hash_pin(payload.pin))
    _pin_reset()
    return {"enabled": True}


@app.post("/api/pin/disable")
def pin_disable(payload: PinPayload, _user=Depends(require_admin)):
    _pin_gate()
    stored = database.get_state("pin_hash", "")
    if not stored:
        return {"enabled": False}
    if not pin.verify_pin(stored, payload.pin):
        _pin_fail()
        raise HTTPException(status_code=401, detail="Неверный PIN-код.")
    database.set_state("pin_hash", "")
    _pin_reset()
    return {"enabled": False}


@app.get("/api/access/status")
def access_status():
    return {"configured": bool(database.get_state("access_hash", ""))}


@app.post("/api/access/setup")
def access_setup(
    payload: AccessPayload,
    x_telegram_init_data: str | None = Header(default=None),
    x_admin_key: str | None = Header(default=None),
):
    if database.get_state("access_hash", ""):
        # Пароль уже задан: менять его разрешено только авторизованному владельцу.
        require_admin(x_telegram_init_data, x_admin_key)
    database.set_state("access_hash", pin.hash_pin(payload.password))
    database.set_state("key_fails", "0")
    database.set_state("key_locked_until", "0")
    return {"ok": True}


@app.post("/api/access/verify")
def access_verify(payload: AccessPayload):
    stored = database.get_state("access_hash", "")
    if stored and pin.verify_pin(stored, payload.password):
        database.set_state("key_fails", "0")
        database.set_state("key_locked_until", "0")
        return {"ok": True}
    was_locked = _key_locked()
    _register_key_fail()
    if was_locked:
        _, until = _key_fail_state()
        wait = max(0, int(until - time.time()))
        raise HTTPException(status_code=429, detail=f"Слишком много неверных попыток. Подождите {wait} с.")
    raise HTTPException(status_code=401, detail="Неверный пароль доступа.")


@app.get("/api/history")
def history(_user=Depends(require_admin)):
    return database.recent_history()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
