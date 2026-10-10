import json
import re
import threading
import time

from . import database
from . import telegram


POLL_SECONDS = 60


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
    if not telegram.BOT_TOKEN:
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

    for source in config["sources"]:
        try:
            posts = telegram.fetch_public_posts(source)
            cursor = database.get_cursor(source)
            if cursor is None:
                latest_id = max(int(post["id"]) for post in posts)
                database.set_cursor(source, latest_id)
                continue

            new_posts = sorted(
                (post for post in posts if int(post["id"]) > cursor),
                key=lambda post: int(post["id"]),
            )
            for post in new_posts:
                post_id = int(post["id"])
                if database.already_forwarded(source, post.get("id", "")):
                    database.set_cursor(source, post_id)
                    continue
                if not _matches_filter(post.get("text", ""), config):
                    database.write_history(post, source, "filtered")
                    database.set_cursor(source, post_id)
                    continue
                try:
                    if telegram.publish_post(post, config["destination"], signature):
                        database.write_history(post, source, "forwarded")
                    else:
                        database.write_history(post, source, "skipped", "Пустой пост без поддерживаемого медиа.")
                    database.set_cursor(source, post_id)
                except (RuntimeError, ValueError) as exc:
                    database.write_history(post, source, "error", str(exc))
                    database.set_state("last_error", f"@{source}, пост {post_id}: {exc}")
                    break
        except (RuntimeError, ValueError) as exc:
            database.set_state("last_error", f"@{source}: {exc}")
        except Exception as exc:
            database.set_state("last_error", f"@{source}: непредвиденная ошибка ({type(exc).__name__}).")
    database.set_state("last_checked", int(time.time()))


def run(stop_event):
    while not stop_event.is_set():
        try:
            poll_once()
        except Exception as exc:
            database.set_state("last_error", f"Ошибка цикла пересылки ({type(exc).__name__}).")
        stop_event.wait(POLL_SECONDS)


def validate_configuration():
    config = database.get_config()
    if not telegram.BOT_TOKEN:
        raise RuntimeError("На сервере не задан TELEGRAM_BOT_TOKEN.")
    if not config["sources"]:
        raise ValueError("Добавьте хотя бы один канал-источник.")
    if not config["destination"]:
        raise ValueError("Укажите канал назначения.")
    if config["filter_enabled"] and not config["filter_terms"].strip():
        raise ValueError("Укажите слова или хэштеги для фильтра.")

    results = []
    for source in config["sources"]:
        posts = telegram.fetch_public_posts(source)
        results.append({"source": source, "available": True, "posts": len(posts)})
    bot = telegram.bot_request("getMe")
    chat = telegram.bot_request("getChat", {"chat_id": config["destination"]})
    member = telegram.bot_request(
        "getChatMember", {"chat_id": config["destination"], "user_id": bot["id"]}
    )
    if member.get("status") not in {"administrator", "creator"}:
        raise RuntimeError("Добавьте бота администратором в канал назначения.")
    if member.get("can_post_messages") is False:
        raise RuntimeError("Боту не дано право публиковать сообщения в канале назначения.")
    return {
        "sources": results,
        "bot": bot.get("username", ""),
        "destination": chat.get("title") or chat.get("username") or config["destination"],
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
