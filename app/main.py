import logging
import os
import re
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

from . import auth, database, mtproto, pin, telegram, worker


logger = logging.getLogger("app.forwarder")


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
stop_event = threading.Event()
worker_thread = None


class ConfigPayload(BaseModel):
    sources: list[str] = Field(default_factory=list, max_length=5)
    destination: str = Field(default="", max_length=100)
    destinations: list[str] = Field(default_factory=list, max_length=5)
    filter_enabled: bool = False
    filter_terms: str = Field(default="", max_length=2000)
    filter_mode: Literal["any", "all"] = "any"


class ServicePayload(BaseModel):
    enabled: bool


class BotPayload(BaseModel):
    token: str = Field(default="", max_length=200)
    admin_id: str = Field(default="", max_length=64)


class MtStartPayload(BaseModel):
    phone: str = Field(default="", max_length=32)
    api_id: str = Field(default="", max_length=16)
    api_hash: str = Field(default="", max_length=128)


class MtCodePayload(BaseModel):
    code: str = Field(default="", max_length=16)


class MtPasswordPayload(BaseModel):
    password: str = Field(default="", max_length=256)


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


def _owner_id():
    raw = (database.get_state("admin_telegram_id", "") or os.environ.get("ADMIN_TELEGRAM_ID", "")).strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _password_user():
    admin_id = _owner_id()
    return {
        "id": admin_id if admin_id is not None else "admin",
        "username": "admin",
        "first_name": "Владелец",
        "auth_method": "password",
        "role": "owner",
    }


def require_identity(
    x_telegram_init_data: str | None = Header(default=None),
):
    """Только подпись Telegram без проверки доступа (для входа по инвайту)."""
    if not x_telegram_init_data:
        raise HTTPException(
            status_code=401, detail="Откройте приложение через Telegram.",
            headers={"X-Requires-Auth": "1"},
        )
    try:
        person = auth.verify_init_data(x_telegram_init_data)
    except PermissionError as exc:
        raise HTTPException(
            status_code=401, detail=str(exc), headers={"X-Requires-Auth": "1"}
        ) from exc
    database.upsert_user(
        person.get("id"), person.get("username", ""), person.get("first_name", "")
    )
    return person


def require_user(
    x_telegram_init_data: str | None = Header(default=None),
    x_admin_key: str | None = Header(default=None),
):
    """Любой допущенный: владелец (пароль/Telegram) или включённый по инвайту."""
    key = (x_admin_key or "").strip()
    if key and _authorize_key(key):
        # Правильный пароль работает всегда и снимает блокировку.
        database.set_state("key_fails", "0")
        database.set_state("key_locked_until", "0")
        owner = _owner_id()
        if owner is not None:
            database.ensure_owner(owner)
        return _password_user()
    if x_telegram_init_data:
        try:
            person = auth.verify_init_data(x_telegram_init_data)
        except PermissionError as exc:
            logger.warning("Авторизация отклонена: %s", exc)
            raise HTTPException(
                status_code=401, detail=str(exc), headers={"X-Requires-Auth": "1"}
            ) from exc
        user_id = person.get("id")
        owner = _owner_id()
        if owner is not None and int(user_id) == owner:
            database.ensure_owner(user_id, person.get("username", ""), person.get("first_name", ""))
            return {**person, "role": "owner"}
        user = database.upsert_user(user_id, person.get("username", ""), person.get("first_name", ""))
        if not user["enabled"]:
            raise HTTPException(
                status_code=403,
                detail="Нет доступа. Попросите у владельца ссылку-приглашение.",
            )
        return {**person, "role": user["role"]}
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


def require_admin(
    x_telegram_init_data: str | None = Header(default=None),
    x_admin_key: str | None = Header(default=None),
):
    user = require_user(x_telegram_init_data, x_admin_key)
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Раздел только для владельца.")
    return user


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

    # Многопользовательность: строка владельца в users + перенос старых
    # общих настроек/курсоров/истории на его ключ (идемпотентно).
    try:
        if admin_id and admin_id.lstrip("-").isdigit():
            owner_id = int(admin_id)
            database.ensure_owner(owner_id)
            database.migrate_legacy_to(database.user_key_for(owner_id))
    except Exception as exc:  # noqa: BLE001 - без миграции работаем как раньше
        logger.warning("Миграция данных владельца не удалась: %s", exc)

    app_url = os.environ.get("APP_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
    if telegram.BOT_TOKEN and app_url and admin_id:
        try:
            bot = telegram.bot_request("getMe")
            database.set_state("bot_username", bot.get("username", ""))
            # Кнопка по умолчанию обязательна; личная кнопка владельца —
            # best-effort (ID может принадлежать удалённому аккаунту).
            worker.configure_menu_button(app_url, None)
            try:
                admin_chat = int(admin_id)
            except ValueError:
                admin_chat = None
            if admin_chat is not None:
                try:
                    worker.configure_menu_button(app_url, admin_chat)
                except RuntimeError as exc:
                    logger.warning("Личная кнопка меню не обновлена: %s", exc)
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


@app.get("/api/mt/status")
def mt_status(_user=Depends(require_admin)):
    return mtproto.status()


@app.post("/api/mt/start")
def mt_start(payload: MtStartPayload, _user=Depends(require_admin)):
    try:
        return mtproto.start_login(payload.phone, payload.api_id, payload.api_hash)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/mt/confirm")
def mt_confirm(payload: MtCodePayload, _user=Depends(require_admin)):
    if not payload.code.strip():
        raise HTTPException(status_code=422, detail="Введите код из Telegram.")
    try:
        return mtproto.confirm_code(payload.code)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/mt/password")
def mt_password(payload: MtPasswordPayload, _user=Depends(require_admin)):
    if not payload.password:
        raise HTTPException(status_code=422, detail="Введите пароль двухэтапной аутентификации.")
    try:
        return mtproto.submit_password(payload.password)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/mt/disconnect")
def mt_disconnect(_user=Depends(require_admin)):
    return mtproto.disconnect()


@app.post("/api/mt/testkeys")
def mt_testkeys(_user=Depends(require_admin)):
    return mtproto.fill_test_keys()


@app.post("/api/mt/qr/start")
def mt_qr_start(_user=Depends(require_admin)):
    try:
        return mtproto.start_qr()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/mt/qr/image")
def mt_qr_image(_user=Depends(require_admin)):
    from fastapi.responses import Response

    try:
        png = mtproto.qr_png()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(content=png, media_type="image/png")


@app.get("/api/mt/qr/status")
def mt_qr_status(_user=Depends(require_admin)):
    return mtproto.qr_status()


@app.put("/api/bot")
def put_bot(payload: BotPayload, _user=Depends(require_admin)):
    new_token = payload.token.strip()
    new_admin = payload.admin_id.strip()

    if new_token and not re.fullmatch(r"\d+:[\w-]{30,}", new_token):
        raise HTTPException(
            status_code=422,
            detail="Токен похож на неверный: обычно это цифры, двоеточие и длинный код от @BotFather.",
        )

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
            worker.configure_menu_button(app_url, None)
            admin_chat = int(new_admin) if new_admin.lstrip("-").isdigit() else None
            if admin_chat is not None:
                try:
                    worker.configure_menu_button(app_url, admin_chat)
                except RuntimeError as exc:
                    logger.warning("Личная кнопка меню не обновлена: %s", exc)
            database.set_state("menu_error", "")
        except RuntimeError as exc:
            database.set_state("menu_error", str(exc))

    return {
        "bot_username": database.get_state("bot_username", ""),
        "admin_id": database.get_state("admin_telegram_id", ""),
        "token_hint": f"…{telegram.get_bot_token()[-4:]}" if len(telegram.get_bot_token()) >= 4 else "",
    }


def _ukey(user):
    try:
        return database.user_key_for(int(user.get("id")))
    except (TypeError, ValueError, AttributeError):
        return None


@app.get("/api/me")
def me(_user=Depends(require_user)):
    return {
        "id": _user.get("id"),
        "username": _user.get("username", ""),
        "first_name": _user.get("first_name", ""),
        "role": _user.get("role", "user"),
    }


class InviteRedeemPayload(BaseModel):
    token: str = Field(default="", max_length=128)


class InviteCreatePayload(BaseModel):
    max_uses: int = Field(default=0, ge=0, le=1000)
    expires_days: int = Field(default=0, ge=0, le=365)


class UserEnabledPayload(BaseModel):
    enabled: bool


@app.post("/api/invites/redeem")
def redeem_invite(payload: InviteRedeemPayload, _user=Depends(require_identity)):
    try:
        user_id = int(_user.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Войдите через Telegram.")
    owner = _owner_id()
    if owner is not None and user_id == owner:
        return {"ok": True, "message": "У владельца доступ уже есть."}
    ok, message = database.redeem_invite(payload.token, user_id)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message}


@app.get("/api/admin/users")
def admin_users(_user=Depends(require_admin)):
    result = []
    for user in database.list_users():
        key = database.user_key_for(user["user_id"])
        config = database.get_config(key)
        stats = database.history_stats(key)
        result.append({
            "id": user["user_id"],
            "username": user["username"],
            "first_name": user["first_name"],
            "role": user["role"],
            "enabled": bool(user["enabled"]),
            "sources": len(config.get("sources", [])),
            "destinations": len(config.get("destinations") or ([config["destination"]] if config.get("destination") else [])),
            "forwarding": bool(config.get("enabled")),
            "forwarded": stats["forwarded"],
            "errors": stats["errors"],
            "last_error": database.get_state(database.user_state_key(key, "last_error"), ""),
        })
    return result


@app.put("/api/admin/users/{user_id}/enabled")
def admin_user_enabled(user_id: int, payload: UserEnabledPayload, _user=Depends(require_admin)):
    target = database.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден.")
    if target["role"] == "owner":
        raise HTTPException(status_code=400, detail="Владельца отключить нельзя.")
    database.set_user_enabled(user_id, payload.enabled)
    return {"ok": True, "enabled": payload.enabled}


@app.delete("/api/admin/users/{user_id}")
def admin_user_delete(user_id: int, _user=Depends(require_admin)):
    target = database.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден.")
    if target["role"] == "owner":
        raise HTTPException(status_code=400, detail="Владельца удалить нельзя.")
    database.delete_user_everything(user_id)
    return {"ok": True}


@app.get("/api/admin/invites")
def admin_invites(_user=Depends(require_admin)):
    return database.list_invites()


@app.post("/api/admin/invites")
def admin_invite_create(payload: InviteCreatePayload, _user=Depends(require_admin)):
    return database.create_invite(payload.max_uses, payload.expires_days)


@app.delete("/api/admin/invites/{token}")
def admin_invite_revoke(token: str, _user=Depends(require_admin)):
    if not database.get_invite(token):
        raise HTTPException(status_code=404, detail="Приглашение не найдено.")
    database.revoke_invite(token)
    return {"ok": True}


@app.get("/api/config")
def get_config(_user=Depends(require_user)):
    return database.get_config(_ukey(_user))


@app.put("/api/config")
def put_config(payload: ConfigPayload, _user=Depends(require_user)):
    key = _ukey(_user)
    raw_destinations = [value for value in payload.destinations if value.strip()]
    if not raw_destinations and payload.destination.strip():
        raw_destinations = [payload.destination]
    if not raw_destinations:
        raise HTTPException(status_code=422, detail="Укажите хотя бы один свой канал.")
    normalized_destinations = []
    for destination in raw_destinations:
        try:
            value = telegram.normalize_destination(destination)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if value.casefold() not in {item.casefold() for item in normalized_destinations}:
            normalized_destinations.append(value)
    if len(normalized_destinations) > database.MAX_DESTINATIONS:
        raise HTTPException(
            status_code=422, detail=f"Больше {database.MAX_DESTINATIONS} своих каналов нельзя."
        )
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
        **database.get_config(key),
        "sources": normalized_sources,
        "destination": normalized_destinations[0],
        "destinations": normalized_destinations,
        "filter_enabled": payload.filter_enabled,
        "filter_terms": payload.filter_terms.strip(),
        "filter_mode": payload.filter_mode,
    }
    database.set_config(config, key)
    return {"saved": True, "config": config}


@app.post("/api/test")
def test_connection(_user=Depends(require_user)):
    try:
        return worker.validate_configuration(database.get_config(_ukey(_user)))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/status")
def status(_user=Depends(require_user)):
    key = _ukey(_user) or ""
    config = database.get_config(_ukey(_user))
    namespaced_checked = database.get_state(database.user_state_key(key, "last_checked"), "") if key else ""
    last_checked = namespaced_checked or database.get_state("last_checked")
    destinations = list(config.get("destinations") or [])
    if not destinations and config.get("destination"):
        destinations = [config["destination"]]
    display = []
    for destination in destinations:
        try:
            display.append(telegram.normalize_destination(destination))
        except ValueError:
            display.append(destination)
    return {
        "running": bool(config.get("enabled") and telegram.BOT_TOKEN and config["sources"] and display),
        "bot": database.get_state("bot_username"),
        "sources": config["sources"],
        "destination": display[0] if display else "",
        "destinations": display,
        "last_checked": int(last_checked) if last_checked.isdigit() else None,
        "last_error": database.get_state(database.user_state_key(key, "last_error"), "") if key else database.get_state("last_error"),
        "menu_error": database.get_state("menu_error"),
    }


@app.get("/api/stats")
def stats(_user=Depends(require_user)):
    return database.history_stats(_ukey(_user))


@app.put("/api/service")
def set_service(payload: ServicePayload, _user=Depends(require_user)):
    key = _ukey(_user)
    config = database.get_config(key)
    if payload.enabled:
        try:
            worker.validate_configuration(config)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    config["enabled"] = payload.enabled
    database.set_config(config, key)
    database.set_state(database.user_state_key(key, "last_error") if key else "last_error", "")
    if payload.enabled:
        # Сразу проверяем источники, не дожидаясь следующего тика воркера.
        threading.Thread(target=worker.poll_once, daemon=True).start()
    return {"running": payload.enabled}


@app.put("/api/signature")
def put_signature(payload: SignaturePayload, _user=Depends(require_user)):
    text = payload.text.strip()
    if payload.enabled and not text:
        raise HTTPException(status_code=422, detail="Введите текст подписи или отключите её.")
    key = _ukey(_user)
    config = database.get_config(key)
    config["signature_enabled"] = payload.enabled
    config["signature_text"] = text
    database.set_config(config, key)
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
def history(_user=Depends(require_user)):
    return database.recent_history(user_key=_ukey(_user))


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
