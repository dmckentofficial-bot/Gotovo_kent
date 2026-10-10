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
    "Просто перешлите мне любой пост из любого канала.\n\n"
    "Кнопки меню делают то же, что вкладки приложения:\n"
    "▶️/⏹ — запустить/остановить, 📊 — статус, 📈 — статистика,\n"
    "📋 — копируемые каналы, 📢 — свои каналы, ➕/➖ — добавить/убрать источник,\n"
    "🎯 — назначение, ✅ — проверка, ❓ — помощь.\n\n"
    "Команды:\n"
    "/status — состояние пересылки\n"
    "/stats — счётчики\n"
    "/on и /off — включить/выключить\n"
    "/list — копируемые каналы и свои\n"
    "/add канал — добавить источник\n"
    "/del канал — убрать источник\n"
    "/dest канал — заменить свои каналы одним\n"
    "/mydest канал — добавить свой канал\n"
    "/delm канал — убрать свой канал\n"
    "/test — проверить подключение\n"
    "/id — ваш Telegram ID"
)

MAX_SOURCES = 5

# Кнопки меню (текст кнопок приходит обычным сообщением).
BTN_RUN = "▶️ Запустить"
BTN_STOP = "⏹ Остановить"
BTN_STATUS = "📊 Статус"
BTN_STATS = "📈 Статистика"
BTN_LIST = "📋 Каналы (Cop)"
BTN_MY = "📢 Мои каналы"
BTN_ADD = "➕ Добавить"
BTN_DEL = "➖ Убрать"
BTN_DEST = "🎯 Назначение"
BTN_TEST = "✅ Проверка"
BTN_HELP = "❓ Помощь"
BTN_APP = "📱 Открыть приложение"


def _owner_id():
    raw = (database.get_state("admin_telegram_id", "") or os.environ.get("ADMIN_TELEGRAM_ID", "")).strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _app_url():
    stored = database.get_state("app_url", "").strip().rstrip("/")
    if stored:
        return stored
    return (os.environ.get("APP_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).strip().rstrip("/")


def set_app_url(url):
    """Сохраняет публичный URL для кнопки Mini App (клавиатура + меню)."""
    clean = (url or "").strip().rstrip("/")
    database.set_state("app_url", clean)
    if clean:
        os.environ["APP_URL"] = clean
    return clean


def _mini_app_keyboard():
    """Клавиатура с кнопкой Mini App (рядом с полем ввода)."""
    url = _app_url()
    if not url:
        return None
    return {"keyboard": [[{"text": BTN_APP, "web_app": {"url": f"{url}/"}}]],
            "resize_keyboard": True, "is_persistent": True}


def _main_keyboard():
    """Полное меню как в Mini App: управление + кнопка приложения."""
    config = database.get_config()
    toggle = BTN_STOP if config.get("enabled") else BTN_RUN
    rows = [
        [{"text": toggle}, {"text": BTN_STATUS}],
        [{"text": BTN_STATS}, {"text": BTN_TEST}],
        [{"text": BTN_LIST}, {"text": BTN_MY}],
        [{"text": BTN_ADD}, {"text": BTN_DEL}],
        [{"text": BTN_DEST}, {"text": BTN_HELP}],
    ]
    url = _app_url()
    if url:
        rows.insert(0, [{"text": BTN_APP, "web_app": {"url": f"{url}/"}}])
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}


def _answer_private(chat_id, text, keyboard=False):
    try:
        payload = {"chat_id": chat_id, "text": text}
        if keyboard == "main":
            payload["reply_markup"] = json.dumps(_main_keyboard())
        elif keyboard:
            markup = _mini_app_keyboard()
            if markup:
                payload["reply_markup"] = json.dumps(markup)
        telegram.bot_request("sendMessage", payload, timeout=30)
        database.set_state("bot_answers_sent", str(int(database.get_state("bot_answers_sent", "0") or 0) + 1))
    except RuntimeError as exc:
        # Ответы не должны умирать молча: иначе кнопки «не работают» без причин.
        database.set_state("bot_answer_error", str(exc)[:300])


def _pending_key():
    return "bot_pending"


def _set_pending(chat_id, action):
    database.set_state(_pending_key(), json.dumps({"chat": chat_id, "action": action}))


def _take_pending(chat_id):
    raw = database.get_state(_pending_key(), "")
    database.set_state(_pending_key(), "")
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(data, dict) or data.get("chat") != chat_id:
        return ""
    return data.get("action", "")


def _clear_pending():
    database.set_state(_pending_key(), "")


PENDING_ASK = {
    "add": "Пришлите @канал или ссылку t.me — добавлю в источники.",
    "del": "Пришлите @канал или номер из списка — уберу из источников.",
    "dest": "Пришлите @канал или ссылку t.me — станет назначением.",
    "mydest": "Пришлите @канал или ссылку t.me — добавлю в свои каналы.",
}


def _consume_pending(action, argument):
    if action == "add":
        return _cmd_add(argument)
    if action == "del":
        return _cmd_delete(argument)
    if action == "dest":
        return _cmd_destination(argument)
    if action == "mydest":
        return _cmd_my_destination(argument)
    return ""


def _my_channels_text():
    config = database.get_config()
    destinations = list(config.get("destinations") or [])
    if not destinations and config.get("destination"):
        destinations = [config["destination"]]
    if not destinations:
        return "Мои каналы: пока пусто. Пришлите @канал или ссылку t.me — добавлю."
    lines = ["Мои каналы (сюда копируется всё):"]
    lines.extend(f"{index}. {name}" for index, name in enumerate(destinations, 1))
    lines.append("Пришлите ещё @канал, чтобы добавить. Убрать: /delm @канал.")
    return "\n".join(lines)


def _save_sources(sources):
    config = database.get_config()
    config["sources"] = sources
    database.set_config(config)
    return config


def _cmd_status():
    config = database.get_config()
    state = "включена" if config.get("enabled") else "выключена"
    last_error = database.get_state("last_error", "")
    destinations = list(config.get("destinations") or [])
    if not destinations and config.get("destination"):
        destinations = [config["destination"]]
    lines = [
        f"Пересылка: {state}.",
        f"Источников: {len(config.get('sources', []))}.",
        f"Мои каналы: {', '.join(destinations) if destinations else 'не заданы'}.",
    ]
    if last_error:
        lines.append(f"Ошибка: {last_error}")
    else:
        lines.append("Ошибок нет.")
    return "\n".join(lines)


def _cmd_stats():
    stats = database.history_stats()
    return (
        f"Переслано: {stats['forwarded']}\n"
        f"Отфильтровано: {stats['filtered']}\n"
        f"Пропущено: {stats['skipped']}\n"
        f"Ошибок: {stats['errors']}\n"
        f"Всего событий: {stats['total']}"
    )


def _cmd_set_enabled(enabled):
    config = database.get_config()
    if enabled and (not config.get("sources") or not (config.get("destinations") or config.get("destination"))):
        return "Сначала задайте источники и хотя бы один свой канал: /add, /mydest."
    config["enabled"] = enabled
    database.set_config(config)
    return "Пересылка включена." if enabled else "Пересылка остановлена."


def _cmd_list():
    config = database.get_config()
    sources = config.get("sources") or []
    destinations = list(config.get("destinations") or [])
    if not destinations and config.get("destination"):
        destinations = [config["destination"]]
    lines = ["Копируемые каналы (Cop):"]
    lines.extend(f"{index}. @{name}" for index, name in enumerate(sources, 1))
    if not sources:
        lines.append("— пусто —")
    lines.append("Мои каналы (куда копировать):")
    lines.extend(f"{index}. {name}" for index, name in enumerate(destinations, 1))
    if not destinations:
        lines.append("— пусто —")
    return "\n".join(lines)


def _cmd_add(argument):
    if not argument:
        return "Использование: /add @канал или ссылку t.me."
    config = database.get_config()
    sources = list(config.get("sources") or [])
    raw = argument.strip().lstrip("@").casefold()
    if raw in {value.casefold() for value in sources}:
        return f"@{argument.strip().lstrip('@')} уже в списке."
    try:
        username = telegram.normalize_channel(argument)
    except ValueError as exc:
        return str(exc)
    if username.casefold() in {value.casefold() for value in sources}:
        return f"@{username} уже в списке."
    if len(sources) >= MAX_SOURCES:
        return f"Больше {MAX_SOURCES} источников нельзя."
    sources.append(username)
    _save_sources(sources)
    return f"@{username} добавлен. Всего: {len(sources)}."


def _cmd_delete(argument):
    if not argument:
        return "Использование: /del @канал или номер из /list."
    config = database.get_config()
    sources = list(config.get("sources") or [])
    target = argument.strip()
    if target.isdigit():
        index = int(target) - 1
        if 0 <= index < len(sources):
            removed = sources.pop(index)
            _save_sources(sources)
            return f"@{removed} убран."
        return "Нет источника с таким номером."
    try:
        username = telegram.normalize_channel(target)
    except ValueError as exc:
        return str(exc)
    for value in sources:
        if value.casefold() == username.casefold():
            sources.remove(value)
            _save_sources(sources)
            return f"@{username} убран."
    return f"@{username} нет в списке."


def _cmd_destination(argument):
    if not argument:
        return "Использование: /dest @канал или ссылку t.me."
    try:
        destination = telegram.normalize_destination(argument)
    except ValueError as exc:
        return str(exc)
    config = database.get_config()
    config["destination"] = destination
    config["destinations"] = [destination]
    database.set_config(config)
    return f"Свои каналы заменены одним: {destination}."


def _cmd_my_destination(argument):
    if not argument:
        return "Использование: /mydest @канал или ссылку t.me."
    try:
        destination = telegram.normalize_destination(argument)
    except ValueError as exc:
        return str(exc)
    config = database.get_config()
    destinations = list(config.get("destinations") or [])
    if not destinations and config.get("destination"):
        destinations = [config["destination"]]
    if destination.casefold() in {value.casefold() for value in destinations}:
        return f"{destination} уже в списке."
    if len(destinations) >= database.MAX_DESTINATIONS:
        return f"Больше {database.MAX_DESTINATIONS} своих каналов нельзя."
    destinations.append(destination)
    config["destination"] = destinations[0]
    config["destinations"] = destinations
    database.set_config(config)
    return f"{destination} добавлен. Всего: {len(destinations)}."


def _cmd_delete_destination(argument):
    if not argument:
        return "Использование: /delm @канал или номер из /list."
    config = database.get_config()
    destinations = list(config.get("destinations") or [])
    if not destinations and config.get("destination"):
        destinations = [config["destination"]]
    target = argument.strip()
    if target.isdigit():
        index = int(target) - 1
        if 0 <= index < len(destinations):
            removed = destinations.pop(index)
            config["destination"] = destinations[0] if destinations else ""
            config["destinations"] = destinations
            database.set_config(config)
            return f"{removed} убран."
        return "Нет своего канала с таким номером."
    try:
        username = telegram.normalize_destination(target)
    except ValueError as exc:
        return str(exc)
    for value in destinations:
        if value.casefold() == username.casefold():
            destinations.remove(value)
            config["destination"] = destinations[0] if destinations else ""
            config["destinations"] = destinations
            database.set_config(config)
            return f"{username} убран."
    return f"{username} нет в списке."


def _cmd_test():
    try:
        result = validate_configuration()
    except (RuntimeError, ValueError) as exc:
        return f"Проверка не пройдена: {exc}"
    lines = [f"Бот: @{result.get('bot')}", f"Назначение: {result.get('destination')}"]
    for item in result.get("sources", []):
        lines.append(f"@{item['source']}: доступен ({item['posts']} постов)")
    return "\n".join(lines)


def _handle_owner_command(text):
    """Команда владельца -> текст ответа (пусто = не команда)."""
    parts = text.strip().split(None, 1)
    command = parts[0].split("@")[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""
    handlers = {
        "/help": lambda: HELP_TEXT,
        "/status": _cmd_status,
        "/stats": _cmd_stats,
        "/on": lambda: _cmd_set_enabled(True),
        "/off": lambda: _cmd_set_enabled(False),
        "/list": _cmd_list,
        "/add": lambda: _cmd_add(argument),
        "/del": lambda: _cmd_delete(argument),
        "/dest": lambda: _cmd_destination(argument),
        "/mydest": lambda: _cmd_my_destination(argument),
        "/delm": lambda: _cmd_delete_destination(argument),
        "/test": _cmd_test,
    }
    handler = handlers.get(command)
    return handler() if handler else ""


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


def drain_direct_messages():
    """Режим «бот-приёмник»: пересланное владельцем в личку — 1-в-1 в канал.

    Бот уже держит сообщение (его переслали ему), поэтому copyMessage идёт
    серверными file_id: оригиналы фото/видео, entities, кнопки — без потерь
    и без лимита 49 МБ на скачивание. Чужие сообщения игнорируются.
    """
    if not telegram.get_bot_token():
        return
    config = database.get_config()
    raw_destinations = list(config.get("destinations") or [])
    if not raw_destinations and config.get("destination"):
        raw_destinations = [config["destination"]]
    if not raw_destinations:
        return
    destinations = []
    for destination in raw_destinations:
        try:
            normalized = telegram.normalize_destination(destination)
        except ValueError:
            continue
        if normalized.casefold() not in {value.casefold() for value in destinations}:
            destinations.append(normalized)
    if not destinations:
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
        try:
            _handle_private_update(update, owner_id, destinations)
        except Exception as exc:
            # Одно битое обновление не должно ронять весь приёмник.
            database.set_state("bot_answer_error", f"update {update_id}: {type(exc).__name__}: {exc}"[:300])


def _handle_private_update(update, owner_id, destinations):
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    message_id = message.get("message_id")
    if chat.get("type") != "private" or not message_id:
        return
    text = message.get("text", "")
    if text.startswith("/start"):
        _answer_private(chat.get("id"), HELP_TEXT, keyboard="main")
        return
    if text.startswith("/id"):
        _answer_private(chat.get("id"), f"Ваш Telegram ID: {sender.get('id')}")
        return
    if owner_id is not None and sender.get("id") != owner_id:
        return
    if text in (BTN_RUN, BTN_STOP, BTN_STATUS, BTN_STATS, BTN_LIST, BTN_MY,
                BTN_TEST, BTN_HELP, BTN_ADD, BTN_DEL, BTN_DEST):
        _clear_pending()
        if text == BTN_RUN:
            _answer_private(chat.get("id"), _cmd_set_enabled(True), keyboard="main")
        elif text == BTN_STOP:
            _answer_private(chat.get("id"), _cmd_set_enabled(False), keyboard="main")
        elif text == BTN_STATUS:
            _answer_private(chat.get("id"), _cmd_status(), keyboard="main")
        elif text == BTN_STATS:
            _answer_private(chat.get("id"), _cmd_stats(), keyboard="main")
        elif text == BTN_LIST:
            _answer_private(chat.get("id"), _cmd_list(), keyboard="main")
        elif text == BTN_MY:
            _answer_private(chat.get("id"), _my_channels_text(), keyboard="main")
            _set_pending(chat.get("id"), "mydest")
        elif text == BTN_TEST:
            _answer_private(chat.get("id"), _cmd_test(), keyboard="main")
        elif text == BTN_HELP:
            _answer_private(chat.get("id"), HELP_TEXT, keyboard="main")
        else:
            action = {BTN_ADD: "add", BTN_DEL: "del"}.get(text, "dest")
            _set_pending(chat.get("id"), action)
            _answer_private(chat.get("id"), PENDING_ASK[action], keyboard="main")
        return
    if text.startswith("/"):
        _clear_pending()
        answer = _handle_owner_command(text)
        if answer:
            _answer_private(chat.get("id"), answer, keyboard="main")
        return
    pending_action = _take_pending(chat.get("id"))
    if pending_action:
        _answer_private(chat.get("id"), _consume_pending(pending_action, text), keyboard="main")
        return
    preview = {
        "id": str(message_id),
        "text": text or message.get("caption", ""),
        "media": [{"type": kind} for kind in _direct_media_label(message)],
    }
    for destination in destinations:
        try:
            telegram.bot_request(
                "copyMessage",
                {"chat_id": destination, "from_chat_id": chat.get("id"), "message_id": message_id},
                timeout=30,
            )
            database.write_history(preview, DIRECT_SOURCE, "forwarded", "", destination)
        except RuntimeError:
            try:
                telegram.bot_request(
                    "forwardMessage",
                    {"chat_id": destination, "from_chat_id": chat.get("id"), "message_id": message_id},
                    timeout=30,
                )
                database.write_history(preview, DIRECT_SOURCE, "forwarded", "Скопировать не вышло — переслано с меткой.", destination)
            except RuntimeError as exc:
                database.write_history(preview, DIRECT_SOURCE, "error", str(exc), destination)


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
    if not config["sources"] or not (config.get("destinations") or config.get("destination")):
        database.set_state("last_error", "")
        database.set_state("last_checked", int(time.time()))
        return

    signature = config.get("signature_text", "").strip() if config.get("signature_enabled") else ""

    destinations = list(config.get("destinations") or [])
    if not destinations and config.get("destination"):
        destinations = [config["destination"]]
    normalized_destinations = []
    for destination in destinations:
        try:
            normalized = telegram.normalize_destination(destination)
        except ValueError as exc:
            database.set_state("last_error", f"Канал назначения {destination}: {exc}")
            return
        if normalized.casefold() not in {value.casefold() for value in normalized_destinations}:
            normalized_destinations.append(normalized)
    if not normalized_destinations:
        database.set_state("last_error", "")
        database.set_state("last_checked", int(time.time()))
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
                    errors = []
                    delivered = False
                    for destination in normalized_destinations:
                        if database.already_forwarded(source, post.get("id", ""), destination):
                            delivered = True
                            continue
                        try:
                            if telegram.publish_post(post, destination, signature):
                                database.write_history(post, source, "forwarded", "", destination)
                            else:
                                database.write_history(
                                    post, source, "skipped", "Пустой пост без поддерживаемого медиа.", destination
                                )
                            delivered = True
                        except (RuntimeError, ValueError) as exc:
                            database.write_history(post, source, "error", str(exc), destination)
                            errors.append(f"{destination}: {exc}")
                    database.set_cursor(source, post_id)
                    if not delivered and errors:
                        database.set_state("last_error", f"@{source}, пост {post_id}: {'; '.join(errors)}")
                        break
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
    destinations = list(config.get("destinations") or [])
    if not destinations and config.get("destination"):
        destinations = [config["destination"]]
    if not destinations:
        raise ValueError("Укажите хотя бы один свой канал.")
    if config["filter_enabled"] and not config["filter_terms"].strip():
        raise ValueError("Укажите слова или хэштеги для фильтра.")

    normalized = []
    for destination in destinations:
        try:
            value = telegram.normalize_destination(destination)
        except ValueError as exc:
            raise ValueError(f"Канал назначения {destination}: {exc}") from exc
        if value.casefold() not in {item.casefold() for item in normalized}:
            normalized.append(value)

    results = []
    for source in config["sources"]:
        posts = telegram.fetch_public_posts(source)
        results.append({"source": source, "available": True, "posts": len(posts)})
    bot = telegram.bot_request("getMe")
    checked = []
    for destination in normalized:
        chat = telegram.bot_request("getChat", {"chat_id": destination})
        if chat.get("type") != "channel":
            raise RuntimeError(
                f"{destination}: нужен настоящий Telegram-канал. "
                "Укажите @username канала, а не личную переписку."
            )
        member = telegram.bot_request(
            "getChatMember", {"chat_id": destination, "user_id": bot["id"]}
        )
        if member.get("status") not in {"administrator", "creator"}:
            raise RuntimeError(f"{destination}: добавьте бота администратором.")
        if member.get("can_post_messages") is False:
            raise RuntimeError(f"{destination}: дайте боту право публиковать сообщения.")
        checked.append(chat.get("title") or chat.get("username") or destination)
    return {
        "sources": results,
        "bot": bot.get("username", ""),
        "destination": ", ".join(checked),
        "destinations": checked,
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
