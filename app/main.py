import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, database, telegram, worker


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


def require_admin(x_telegram_init_data: str | None = Header(default=None)):
    try:
        return auth.validate_init_data(x_telegram_init_data or "")
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@asynccontextmanager
async def lifespan(_app):
    global worker_thread
    database.initialize()
    app_url = os.environ.get("APP_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
    admin_id = os.environ.get("ADMIN_TELEGRAM_ID", "").strip()
    if telegram.BOT_TOKEN and app_url and admin_id:
        try:
            worker.configure_menu_button(app_url)
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


@app.get("/api/config")
def get_config(_user=Depends(require_admin)):
    return database.get_config()


@app.put("/api/config")
def put_config(payload: ConfigPayload, _user=Depends(require_admin)):
    if not payload.destination.strip():
        raise HTTPException(status_code=422, detail="Укажите канал назначения.")
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
        "sources": normalized_sources,
        "destination": payload.destination.strip(),
        "filter_enabled": payload.filter_enabled,
        "filter_terms": payload.filter_terms.strip(),
        "filter_mode": payload.filter_mode,
        "enabled": database.get_config().get("enabled", False),
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
    return {
        "running": bool(config.get("enabled") and telegram.BOT_TOKEN and config["sources"] and config["destination"]),
        "sources": config["sources"],
        "destination": config["destination"],
        "last_checked": int(last_checked) if last_checked.isdigit() else None,
        "last_error": database.get_state("last_error"),
        "menu_error": database.get_state("menu_error"),
    }


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
    return {"running": payload.enabled}


@app.get("/api/history")
def history(_user=Depends(require_admin)):
    return database.recent_history()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
