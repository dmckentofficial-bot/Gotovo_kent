"""Точное чтение публичных каналов через MTProto (пользовательская сессия).

Зачем нужен второй способ чтения:
- скрапер t.me/s видит только текст+фото/видео-превью: там физически нет
  кнопок, кругляшков, голосовых, стикеров, опросов и фото в оригинале;
- Telegram не отдаёт посты ботам из каналов, где бота нет;
- пользовательская MTProto-сессия (Telethon) читает ЛЮБОЙ публичный канал
  дословно: текст с entities, все типы медиа в оригинале, кнопки, опросы,
  группировку альбомов.

Приватность: api_id/api_hash/телефон/сессия хранятся только в локальной БД,
как и токен бота. Без настроенной сессии воркер молча использует скрапер.
"""

import asyncio
import logging
import mimetypes
import threading

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl import types as tl_types

from . import database


logger = logging.getLogger("app.mtproto")

MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 49 * 1024 * 1024
MAX_FILE_BYTES = 49 * 1024 * 1024

# Публичные тестовые ключи из примеров TDLib (опубликованы самим Telegram
# для разработчиков). Если my.telegram.org недоступен — их достаточно, чтобы
# войти со СВОЕГО номера и читать СВОИ каналы: данные сессии всё равно ваши.
# Минус: общие ключи иногда упираются в лимиты — тогда заведите личные.
# Встроенные ключи официального Desktop-клиента (публичны в open-source):
# работают без my.telegram.org, вход — по QR-коду из приложения Telegram.
TEST_API_ID = "2040"
TEST_API_HASH = "b18441a1ff607e10a989891a5462e627"


# ВАЖНО: одну сессию держит только долгоживущий клиент серверного процесса.
# Разовые скрипты/проверки с той же сессией Telegram может счесть угоном
# и отозвать её — для проверок используйте моки, а не живые подключения.

_loop = None
_thread = None
_client = None
_lock = threading.Lock()


def _ensure_loop():
    global _loop, _thread
    with _lock:
        if _loop is not None:
            return _loop
        loop = asyncio.new_event_loop()

        def runner():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=runner, name="mtproto-loop", daemon=True)
        thread.start()
        _loop, _thread = loop, thread
        return loop


def _run(coro_factory, timeout=90):
    """Выполняет корутину в потоке MTProto и ждёт результат."""
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
    return future.result(timeout)


# ---------- учётные данные ----------

def get_credentials():
    return {
        "api_id": database.get_state("mt_api_id", "").strip(),
        "api_hash": database.get_state("mt_api_hash", "").strip(),
        "phone": database.get_state("mt_phone", "").strip(),
        "has_session": bool(database.get_state("mt_session", "")),
    }


def is_configured():
    creds = get_credentials()
    return bool(creds["api_id"] and creds["api_hash"] and creds["phone"])


def fill_test_keys():
    """Подставляет встроенные ключи (без my.telegram.org).

    Дальше вход — по QR-коду из приложения Telegram (Настройки → Устройства →
    Привязать устройство). Номер телефона для QR-входа не нужен: аккаунт
    определяется тем, кто отсканировал.
    """
    database.set_state("mt_api_id", TEST_API_ID)
    database.set_state("mt_api_hash", TEST_API_HASH)
    return {"api_id": TEST_API_ID, "api_hash_hint": f"…{TEST_API_HASH[-4:]}"}


_qr_login = None


async def _start_qr():
    """Начинает QR-вход: возвращает ссылку для QR-картинки."""
    global _client, _qr_login
    database.set_state("mt_api_id", database.get_state("mt_api_id", "").strip() or TEST_API_ID)
    database.set_state("mt_api_hash", database.get_state("mt_api_hash", "").strip() or TEST_API_HASH)
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        _client = None
    creds = get_credentials()
    session = StringSession(database.get_state("mt_session", "") or None)
    _client = TelegramClient(session, int(creds["api_id"]), creds["api_hash"])
    await _client.connect()
    if await _client.is_user_authorized():
        _save_session(_client)
        return {"authorized": True}
    _qr_login = await _client.qr_login()
    return {"authorized": False, "qr": True}


async def _qr_status():
    """Проверяет, отсканирован ли QR (короткое ожидание для polling из UI)."""
    global _qr_login
    if _qr_login is None:
        return {"authorized": False, "waiting": False}
    try:
        user = await _qr_login.wait(timeout=20)
    except Exception:  # noqa: BLE001 - таймаут/истечение QR
        user = None
    if user:
        _save_session(_client)
        _qr_login = None
        me = await _client.get_me()
        return {"authorized": True, "user": getattr(me, "username", "") or getattr(me, "first_name", "")}
    return {"authorized": False, "waiting": True}


def _qr_png_bytes(url):
    import io

    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def _qr_png():
    global _qr_login
    if _qr_login is None:
        raise RuntimeError("Сначала нажмите «Войти по QR-коду».")
    # QR живёт около минуты: протухший обновляем, чтобы на экране всегда был свежий.
    try:
        import datetime as _dt

        expires = getattr(_qr_login, "expires", None)
        if expires is not None:
            now = _dt.datetime.now(_dt.timezone.utc)
            exp = expires if expires.tzinfo else expires.replace(tzinfo=_dt.timezone.utc)
            if (exp - now).total_seconds() < 45:
                await _qr_login.recreate()
    except Exception:  # noqa: BLE001 - старый код ещё валиден, отдаём как есть
        pass
    return _qr_png_bytes(_qr_login.url)


def start_qr():
    return _run(lambda: _start_qr(), timeout=60)


def qr_status():
    try:
        return _run(lambda: _qr_status(), timeout=40)
    except Exception as exc:  # noqa: BLE001
        return {"authorized": False, "waiting": True, "error": str(exc)}


def qr_png():
    return _run(lambda: _qr_png(), timeout=30)


async def _get_client():
    global _client
    if _client is not None and _client.is_connected():
        return _client
    creds = get_credentials()
    if not creds["api_id"] or not creds["api_hash"]:
        raise RuntimeError("Укажите API ID и API Hash с my.telegram.org.")
    try:
        api_id = int(creds["api_id"])
    except ValueError as exc:
        raise RuntimeError("API ID должен быть числом.") from exc
    session = StringSession(database.get_state("mt_session", "") or None)
    _client = TelegramClient(session, api_id, creds["api_hash"])
    await _client.connect()
    return _client


def _save_session(client):
    try:
        database.set_state("mt_session", client.session.save())
    except Exception as exc:  # noqa: BLE001 - сессия некритична для чтения
        logger.warning("Не удалось сохранить MTProto-сессию: %s", exc)


# ---------- авторизация (шаги выполняет пользователь из интерфейса) ----------

async def _start_login(phone, api_id, api_hash):
    global _client
    try:
        clean_api_id = int(str(api_id).strip())
    except ValueError as exc:
        raise RuntimeError("API ID должен быть числом.") from exc
    if not str(api_hash).strip() or not str(phone).strip():
        raise RuntimeError("Заполните телефон, API ID и API Hash.")
    database.set_state("mt_api_id", str(api_id).strip())
    database.set_state("mt_api_hash", str(api_hash).strip())
    database.set_state("mt_phone", str(phone).strip())
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        _client = None
    session = StringSession(database.get_state("mt_session", "") or None)
    _client = TelegramClient(session, clean_api_id, str(api_hash).strip())
    await _client.connect()
    if await _client.is_user_authorized():
        _save_session(_client)
        return {"authorized": True}
    try:
        sent = await _client.send_code_request(str(phone).strip())
    except FloodWaitError as exc:
        raise RuntimeError(f"Telegram просит подождать {exc.seconds} с.") from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Не удалось запросить код: {exc}") from exc
    database.set_state("mt_phone_hash", sent.phone_code_hash or "")
    return {"authorized": False, "code_sent": True}


async def _confirm_code(code):
    global _client
    client = await _get_client()
    phone = database.get_state("mt_phone", "").strip()
    phone_hash = database.get_state("mt_phone_hash", "").strip()
    try:
        await client.sign_in(phone, str(code).strip(), phone_code_hash=phone_hash or None)
    except SessionPasswordNeededError:
        return {"authorized": False, "need_password": True}
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
        raise RuntimeError("Неверный или просроченный код. Запросите новый.") from exc
    _save_session(client)
    database.set_state("mt_phone_hash", "")
    me = await client.get_me()
    return {"authorized": True, "user": getattr(me, "username", "") or getattr(me, "first_name", "")}


async def _submit_password(password):
    global _client
    client = await _get_client()
    try:
        await client.sign_in(password=str(password))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Неверный пароль двухэтапной аутентификации.") from exc
    _save_session(client)
    database.set_state("mt_phone_hash", "")
    me = await client.get_me()
    return {"authorized": True, "user": getattr(me, "username", "") or getattr(me, "first_name", "")}


async def _status():
    creds = get_credentials()
    result = {"configured": is_configured(), "connected": False, "authorized": False, "user": ""}
    last_ok = database.get_state("mt_last_ok", "")
    result["last_exact"] = int(last_ok) if last_ok.isdigit() else None
    if not creds["api_id"]:
        return result
    try:
        client = await _get_client()
        result["connected"] = bool(client.is_connected())
        if result["connected"] and await client.is_user_authorized():
            result["authorized"] = True
            try:
                me = await client.get_me()
                result["user"] = f"@{me.username}" if getattr(me, "username", "") else (getattr(me, "first_name", "") or creds["phone"])
            except Exception:  # noqa: BLE001
                result["user"] = creds["phone"]
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    return result


async def _disconnect():
    global _client
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        _client = None
    return {"ok": True}


def start_login(phone, api_id, api_hash):
    return _run(lambda: _start_login(phone, api_id, api_hash), timeout=60)


def confirm_code(code):
    return _run(lambda: _confirm_code(code), timeout=60)


def submit_password(password):
    return _run(lambda: _submit_password(password), timeout=60)


def status():
    try:
        return _run(lambda: _status(), timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"configured": is_configured(), "connected": False, "authorized": False, "error": str(exc)}


def disconnect():
    try:
        return _run(lambda: _disconnect(), timeout=30)
    except Exception:  # noqa: BLE001
        return {"ok": True}


def available():
    """True, если сессия настроена и авторизована (можно точно читать)."""
    if not is_configured():
        return False
    try:
        info = status()
    except Exception:  # noqa: BLE001
        return False
    return bool(info.get("authorized"))


# ---------- маппинг сущностей Telethon -> Bot API ----------

def map_entities(tl_entities):
    """Telethon MessageEntity -> список entities для Bot API."""
    out = []
    for entity in tl_entities or []:
        base = {"offset": entity.offset, "length": entity.length}
        if isinstance(entity, tl_types.MessageEntityBold):
            out.append({**base, "type": "bold"})
        elif isinstance(entity, tl_types.MessageEntityItalic):
            out.append({**base, "type": "italic"})
        elif isinstance(entity, tl_types.MessageEntityCode):
            out.append({**base, "type": "code"})
        elif isinstance(entity, tl_types.MessageEntityPre):
            item = {**base, "type": "pre"}
            if getattr(entity, "language", ""):
                item["language"] = entity.language
            out.append(item)
        elif isinstance(entity, tl_types.MessageEntityTextUrl):
            out.append({**base, "type": "text_link", "url": entity.url})
        elif isinstance(entity, tl_types.MessageEntityMention):
            out.append({**base, "type": "mention"})
        elif isinstance(entity, tl_types.MessageEntityHashtag):
            out.append({**base, "type": "hashtag"})
        elif isinstance(entity, tl_types.MessageEntityCashtag):
            out.append({**base, "type": "cashtag"})
        elif isinstance(entity, tl_types.MessageEntityBotCommand):
            out.append({**base, "type": "bot_command"})
        elif isinstance(entity, tl_types.MessageEntityUrl):
            out.append({**base, "type": "url"})
        elif isinstance(entity, tl_types.MessageEntityEmail):
            out.append({**base, "type": "email"})
        elif isinstance(entity, tl_types.MessageEntityPhone):
            out.append({**base, "type": "phone_number"})
        elif isinstance(entity, tl_types.MessageEntityStrike):
            out.append({**base, "type": "strikethrough"})
        elif isinstance(entity, tl_types.MessageEntityUnderline):
            out.append({**base, "type": "underline"})
        elif isinstance(entity, tl_types.MessageEntitySpoiler):
            out.append({**base, "type": "spoiler"})
        elif isinstance(entity, tl_types.MessageEntityBlockquote):
            out.append({**base, "type": "blockquote"})
        elif isinstance(entity, tl_types.MessageEntityCustomEmoji):
            out.append({**base, "type": "custom_emoji", "custom_emoji_id": str(entity.document_id)})
        # MentionName и прочие персональные сущности пропускаем (текст остаётся).
    return out


def map_buttons(reply_markup):
    """Inline-кнопки со ссылками -> reply_markup для Bot API.

    Поддерживает и новую модель кнопок Bot API 2.x (KeyboardButton.type),
    и старую (поле url прямо на кнопке) — какая придёт с серверов.
    """
    if not isinstance(reply_markup, tl_types.ReplyInlineMarkup):
        return None
    keyboard = []
    for row in reply_markup.rows or []:
        line = []
        for button in row.buttons or []:
            text = getattr(button, "text", "")
            url = getattr(button, "url", "") or ""
            if not url:
                btn_type = getattr(button, "type", None)
                url = getattr(btn_type, "url", "") or ""
            if url and text and url.startswith(("http://", "https://", "t.me", "telegram.me")):
                if url.startswith(("t.me", "telegram.me")):
                    url = f"https://{url}"
                line.append({"text": text, "url": url})
        if line:
            keyboard.append(line)
    return {"inline_keyboard": keyboard} if keyboard else None


def slice_entities(entities, start, end):
    """Сущности, целиком лежащие в окне [start, end) видимого текста."""
    return [
        dict(item)
        for item in entities or []
        if item.get("offset", 0) >= start and item.get("offset", 0) + item.get("length", 0) <= end
    ]


def shift_entities(entities, offset):
    return [
        {**item, "offset": item.get("offset", 0) - offset}
        for item in entities or []
    ]


# ---------- чтение каналов ----------

def _media_kind(message):
    media = message.media
    if isinstance(media, tl_types.MessageMediaPhoto):
        return "photo"
    if isinstance(media, tl_types.MessageMediaDocument):
        doc = media.document
        if isinstance(doc, tl_types.Document):
            for attr in doc.attributes or []:
                if isinstance(attr, tl_types.DocumentAttributeSticker):
                    return "sticker"
                if isinstance(attr, tl_types.DocumentAttributeAudio) and getattr(attr, "voice", False):
                    return "voice"
                if isinstance(attr, tl_types.DocumentAttributeVideo) and getattr(attr, "round_message", False):
                    return "round"
            mime = (doc.mime_type or "").lower()
            if mime.startswith("audio/"):
                return "voice"
            if getattr(doc, "gif", False):
                return "gif"
            for attr in doc.attributes or []:
                if isinstance(attr, tl_types.DocumentAttributeAnimated):
                    return "gif"
                if isinstance(attr, tl_types.DocumentAttributeVideo):
                    return "video"
            if mime.startswith("video/"):
                return "video"
            if mime.startswith("image/"):
                return "photo"
            return "file"
    if isinstance(media, tl_types.MessageMediaPoll):
        return "poll"
    return ""


def _poll_payload(message):
    media = message.media
    if not isinstance(media, tl_types.MessageMediaPoll):
        return None
    poll = media.poll
    options = []
    for answer in poll.answers or []:
        options.append(answer.text)
    if not options:
        return None
    payload = {
        "question": poll.question.text if hasattr(poll.question, "text") else str(poll.question),
        "options": options,
        "is_anonymous": not getattr(poll, "public_voters", False),
    }
    if getattr(poll, "quiz", False):
        payload["type"] = "quiz"
        results = getattr(media, "results", None)
        solution = getattr(results, "solution", "") if results else ""
        if solution:
            payload["explanation"] = solution
        correct = []
        try:
            for answer in results.results or []:
                if getattr(answer, "correct", False):
                    correct.append(answer.option)
        except Exception:  # noqa: BLE001
            pass
        if len(correct) == 1:
            payload["correct_option_id"] = correct[0]
    return payload


def _message_to_post(message, username):
    kind = _media_kind(message)
    post = {
        "id": str(message.id),
        "source_username": username,
        "text": message.message or "",
        "html": "",
        "entities": map_entities(message.entities),
        "media": [],
        "reply_markup": map_buttons(message.reply_markup),
        "poll": _poll_payload(message) if kind == "poll" else None,
        "via": "mtproto",
        "_mt_grouped": getattr(message, "grouped_id", None),
        "_mt_kind": kind,
        "_mt_id": message.id,
    }
    if kind in {"photo", "video", "gif", "voice", "round", "sticker", "file"}:
        post["media"] = [{"type": kind, "mime": "", "filename": "", "message_ref": message.id}]
    return post


async def _fetch_recent(username, limit=20):
    client = await _get_client()
    if not await client.is_user_authorized():
        raise RuntimeError("MTProto-сессия не авторизована. Пройдите вход по коду.")
    try:
        entity = await client.get_entity(username)
    except Exception as exc:
        raise RuntimeError(f"Не удалось открыть @{username}: {exc}") from exc
    try:
        messages = await client.get_messages(entity, limit=limit)
    except FloodWaitError as exc:
        raise RuntimeError(f"Telegram просит подождать {exc.seconds} с.") from exc
    posts = []
    for message in messages or []:
        if message is None or getattr(message, "id", 0) <= 0:
            continue
        posts.append(_message_to_post(message, username.lstrip("@")))
    return posts


async def _download_one(username, message_id):
    client = await _get_client()
    entity = await client.get_entity(username)
    message = await client.get_messages(entity, ids=message_id)
    if message is None:
        raise RuntimeError(f"Пост @{username}/{message_id} не найден.")
    data = await client.download_media(message, bytes)
    if not data:
        raise RuntimeError("Не удалось скачать медиа.")
    kind = _media_kind(message)
    limit = MAX_VIDEO_BYTES if kind in {"video", "gif", "round", "file"} else MAX_PHOTO_BYTES
    if len(data) > limit:
        raise RuntimeError(f"Файл превышает лимит загрузки Telegram ({limit // 1024 // 1024} МБ).")
    mime = ""
    media = message.media
    if isinstance(media, tl_types.MessageMediaDocument) and isinstance(media.document, tl_types.Document):
        mime = (media.document.mime_type or "").lower()
    ext = mimetypes.guess_extension(mime) or {
        "photo": ".jpg", "video": ".mp4", "gif": ".mp4", "voice": ".ogg",
        "round": ".mp4", "sticker": ".webp", "file": ".bin",
    }.get(kind, ".bin")
    return f"file{ext}", bytes(data)


def fetch_recent(username, limit=20):
    return _run(lambda: _fetch_recent(username, limit=limit), timeout=90)


def download_media_bytes(username, message_id):
    return _run(lambda: _download_one(username, message_id), timeout=120)


def group_posts(posts):
    """Склеивает альбомы (одно grouped_id) в единый пост для публикации."""
    singles = []
    groups = {}
    order = []
    for post in posts:
        key = post.get("_mt_grouped")
        if not key:
            singles.append(post)
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(post)
    merged = list(singles)
    for key in order:
        items = sorted(groups[key], key=lambda p: int(p["id"]))
        base = dict(items[0])
        media = []
        for item in items:
            media.extend(item.get("media", []))
        base["media"] = media
        if not base.get("text"):
            for item in items[1:]:
                if item.get("text"):
                    base["text"] = item["text"]
                    base["entities"] = item.get("entities", [])
                    break
        if not base.get("reply_markup"):
            for item in items:
                if item.get("reply_markup"):
                    base["reply_markup"] = item["reply_markup"]
                    break
        base["id"] = str(max(int(item["id"]) for item in items))
        base["_mt_group_ids"] = [item["id"] for item in items]
        merged.append(base)
    merged.sort(key=lambda p: int(p["id"]))
    return merged
