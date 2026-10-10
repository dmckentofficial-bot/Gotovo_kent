import json
import os
import re
import threading
import time

from . import database
from . import telegram


POLL_SECONDS = 60
DIRECT_TICK_SECONDS = 10
POLL_EVERY_N_TICKS = 6
DIRECT_SOURCE = "direct"

HELP_TEXT = (
    "Я принимаю пересланные посты и публикую их в канал назначения один в один: "
    "текст, фото и видео в оригинале, кнопки.\n\n"
    "Просто перешлите мне любой пост из любого канала.\n"
    "Команды: /start — помощь, /id — ваш Telegram ID."
)


def _owner_id():
    raw = (database.get_state("admin_telegram_id", "") or os.environ.get("ADMIN_TELEGRAM_ID", "")).strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _direct_media_label(message):
    kinds = []
    for key, label in (
        ("photo", "photo"), ("video", "video"), ("video_note", "round"),
        ("voice", "voice"), ("sticker", "sticker"), ("animation", "gif"),
        ("document", "file"), ("poll", "poll"), ("story", "story"),
    ):
        if key in message:
            kinds.append(label)
    return kinds


def _answer_private(chat_id, text):
    try:
        telegram.bot_request("sendMessage", {"chat_id": chat_id, "text": text}, timeout=30)
    except RuntimeError:
        pass


def drain_direct_messages():
    """Режим «бот-приёмник»: пересланное владельцем в личку — 1-в-1 в канал.

    Бот уже держит сообщение (его переслали ему), поэтому copyMessage идёт
    серверными file_id: оригиналы фото/видео, entities, кнопки — без потерь
    и без лимита 49 МБ на скачивание. Чужие сообщения игнорируются.
    """
    if not telegram.get_bot_token():
        return
    config = database.get_config()
    if not config.get("destination"):
        return
    try:
        destination = telegram.normalize_destination(config["destination"])
    except ValueError:
        return
    owner_id = _owner_id()

    params = {"timeout": 10, "allowed_updates": json.dumps(["message"])}
    try:
        offset = int(database.get_state("bot_updates_offset", "") or 0)
    except ValueError:
        offset = 0
    if offset:
        params["offset"] = offset
    try:
        updates = telegram.bot_request("getUpdates", params, timeout=25)
    except RuntimeError:
        return
    if not isinstance(updates, list):
        return
    for update in updates:
        try:
            update_id = int(update.get("update_id", 0))
        except (TypeError, ValueError):
            continue
        database.set_state("bot_updates_offset", update_id + 1)
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        message_id = message.get("message_id")
        if chat.get("type") != "private" or not message_id:
            continue
        text = message.get("text", "")
        if text.startswith("/start"):
            _answer_private(chat.get("id"), HELP_TEXT)
            continue
        if text.startswith("/id"):
            _answer_private(chat.get("id"), f"Ваш Telegram ID: {sender.get('id')}")
            continue
        if owner_id is not None and sender.get("id") != owner_id:
            continue
        preview = {
            "id": str(message_id),
            "text": text or message.get("caption", ""),
            "media": [{"type": kind} for kind in _direct_media_label(message)],
        }
        try:
            telegram.bot_request(
                "copyMessage",
                {"chat_id": destination, "from_chat_id": chat.get("id"), "message_id": message_id},
                timeout=30,
            )
            database.write_history(preview, DIRECT_SOURCE, "forwarded")
        except RuntimeError:
            try:
                telegram.bot_request(
                    "forwardMessage",
                    {"chat_id": destination, "from_chat_id": chat.get("id"), "message_id": message_id},
                    timeout=30,
                )
                database.write_history(preview, DIRECT_SOURCE, "forwarded", "Скопировать не вышло — переслано с меткой.")
            except RuntimeError as exc:
                database.write_history(preview, DIRECT_SOURCE, "error", str(exc))


def run(stop_event):
    # Приёмник лички опрашивается каждые ~10 секунд (почти мгновенно),
    # тяжёлый опрос источников — раз в минуту, как раньше.
    tick = 0
    while not stop_event.is_set():
        if tick % POLL_EVERY_N_TICKS == 0:
            try:
                poll_once()
            except Exception as exc:
                database.set_state("last_error", f"Ошибка цикла пересылки ({type(exc).__name__}).")
        try:
            drain_direct_messages()
        except Exception as exc:
            database.set_state("last_error", f"Ошибка приёма сообщений ({type(exc).__name__}).")
        tick += 1
        stop_event.wait(DIRECT_TICK_SECONDS)


def _matches_filter(text, config):
    if not config.get("filter_enabled"):
        return True
    terms = [item.strip().casefold() for item in re.split(r"[,\n;]+", config.get("filter_terms", "")) if item.strip()]
    if not terms:
        return False
    normalized = text.casefold()
    matches = [
        re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalized) is not None
        if term.startswith("#") else term in normalized
        for term in terms
    ]
    return all(matches) if config.get("filter_mode") == "all" else any(matches)


def poll_once():
    config = database.get_config()
    database.set_state("last_error", "")
    if not telegram.get_bot_token():
        database.set_state("last_error", "На сервере не задан токен бота.")
        return
    if not config.get("enabled"):
        database.set_state("last_checked", int(time.time()))
        return
    if not config["sources"] or not config["destination"]:
        database.set_state("last_error", "")
        database.set_state("last_checked", int(time.time()))
        return

    signature = config.get("signature_text", "").strip() if config.get("signature_enabled") else ""

    try:
        destination = telegram.normalize_destination(config["destination"])
    except ValueError as exc:
        database.set_state("last_error", f"Канал назначения: {exc}")
        return

    use_exact = False
    try:
        from . import mtproto as mt_reader
        use_exact = mt_reader.available()
    except Exception:  # noqa: BLE001 - без точного чтения работаем через скрапер
        use_exact = False

    for source in config["sources"]:
        try:
            if use_exact:
                try:
                    posts = mt_reader.fetch_recent(source, limit=20)
                    exact_mode = True
                    database.set_state("mt_last_ok", int(time.time()))
                except Exception as exc:
                    note = f"@{source}: точное чтение недоступно ({exc}), читаю превью."
                    database.set_state("last_error", note)
                    posts = telegram.fetch_public_posts(source)
                    exact_mode = False
            else:
                posts = telegram.fetch_public_posts(source)
                exact_mode = False
            cursor = database.get_cursor(source)
            if cursor is None:
                latest_id = max(int(post["id"]) for post in posts)
                database.set_cursor(source, latest_id)
                continue

            new_posts = sorted(
                (post for post in posts if int(post["id"]) > cursor),
                key=lambda post: int(post["id"]),
            )
            if exact_mode:
                try:
                    new_posts = mt_reader.group_posts(new_posts)
                except Exception:  # noqa: BLE001
                    pass
            for post in new_posts:
                post_id = int(post["id"])
                member_ids = [post["id"], *(post.get("_mt_group_ids") or [])]
                if any(database.already_forwarded(source, member) for member in member_ids):
                    database.set_cursor(source, post_id)
                    continue
                if not exact_mode:
                    # Для точного фильтра догружаем полный текст отдельного поста:
                    # в общей ленте иногда лежит обрезанный snippet.
                    try:
                        full = telegram.fetch_single_post(source, post_id)
                        if full.get("text"):
                            post["text"] = full["text"]
                        if full.get("html"):
                            post["html"] = full["html"]
                        if full.get("media"):
                            post["media"] = full["media"]
                    except RuntimeError:
                        pass
                else:
                    # Оригиналы медиа качаем только для новых постов.
                    try:
                        for item in post.get("media", []):
                            if item.get("data") is None and item.get("message_ref"):
                                filename, blob = mt_reader.download_media_bytes(
                                    source, int(item["message_ref"])
                                )
                                item["data"] = blob
                                item["filename"] = filename
                    except Exception as exc:
                        database.write_history(post, source, "error", f"Не удалось скачать медиа: {exc}")
                        database.set_state("last_error", f"@{source}, пост {post_id}: {exc}")
                        break
                if not _matches_filter(post.get("text", ""), config):
                    database.write_history(post, source, "filtered")
                    database.set_cursor(source, post_id)
                    continue
                try:
                    # Добавляем source_username для copyMessage
                    post["source_username"] = source
                    if telegram.publish_post(post, destination, signature):
                        database.write_history(post, source, "forwarded")
                    else:
                        database.write_history(post, source, "skipped", "Пустой пост без поддерживаемого медиа.")
                    database.set_cursor(source, post_id)
                except (RuntimeError, ValueError) as exc:
                    database.write_history(post, source, "error", str(exc))
                    database.set_state("last_error", f"@{source}, пост {post_id}: {exc}")
                    break
        except RuntimeError as exc:
            # Пустой канал — не ошибка, просто нечего забирать.
            if "не найдено доступных публикаций" in str(exc):
                continue
            database.set_state("last_error", f"@{source}: {exc}")
        except Exception as exc:
            database.set_state("last_error", f"@{source}: непредвиденная ошибка ({type(exc).__name__}).")
    database.set_state("last_checked", int(time.time()))


def validate_configuration():
    config = database.get_config()
    if not telegram.get_bot_token():
        raise RuntimeError("На сервере не задан TELEGRAM_BOT_TOKEN.")
    if not config["sources"]:
        raise ValueError("Добавьте хотя бы один канал-источник.")
    if not config["destination"]:
        raise ValueError("Укажите канал назначения.")
    if config["filter_enabled"] and not config["filter_terms"].strip():
        raise ValueError("Укажите слова или хэштеги для фильтра.")

    try:
        destination = telegram.normalize_destination(config["destination"])
    except ValueError as exc:
        raise ValueError(f"Канал назначения: {exc}") from exc

    results = []
    for source in config["sources"]:
        posts = telegram.fetch_public_posts(source)
        results.append({"source": source, "available": True, "posts": len(posts)})
    bot = telegram.bot_request("getMe")
    chat = telegram.bot_request("getChat", {"chat_id": destination})
    if chat.get("type") != "channel":
        raise RuntimeError(
            "Канал назначения должен быть настоящим Telegram-каналом. "
            "Укажите @username канала, а не личную переписку."
        )
    member = telegram.bot_request(
        "getChatMember", {"chat_id": destination, "user_id": bot["id"]}
    )
    if member.get("status") not in {"administrator", "creator"}:
        raise RuntimeError("Добавьте бота администратором в канал назначения.")
    if member.get("can_post_messages") is False:
        raise RuntimeError("Боту не дано право публиковать сообщения в канале назначения.")
    return {
        "sources": results,
        "bot": bot.get("username", ""),
        "destination": chat.get("title") or chat.get("username") or destination,
    }


def configure_menu_button(app_url, admin_id=None):
    if not app_url:
        return
    menu_button = {
        "type": "web_app",
        "text": "Открыть приложение",
        "web_app": {"url": app_url.rstrip("/") + "/"},
    }
    targets = [None]
    if admin_id is not None:
        targets.append(admin_id)
    for chat_id in targets:
        data = {"menu_button": json.dumps(menu_button)}
        if chat_id is not None:
            data["chat_id"] = chat_id
        telegram.bot_request("setChatMenuButton", data)
