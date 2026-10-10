import html
import json
import mimetypes
import os
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv


# Переменные окружения из .env (в облаке файла нет — load_dotenv просто ничего не сделает).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 49 * 1024 * 1024
MAX_MESSAGE_UNITS = 4096
MAX_CAPTION_UNITS = 1024
MEDIA_GROUP_SIZE = 10
MEDIA_HOSTS = ("telesco.pe", "telegram.org", "telegram-cdn.org")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

# Прерыватель флуда: пока не истёк retry_after от Telegram, запросы не уходят
# в сеть, а сразу падают с понятной ошибкой (иначе стартап виснет минутами).
_flood_until = 0.0


def _flood_wait_left():
    left = _flood_until - time.time()
    return int(left) if left > 0 else 0


def get_bot_token():
    """Текущий токен бота (может меняться из интерфейса без перезапуска сервера)."""
    return BOT_TOKEN


def set_bot_token(token):
    """Заменяет токен бота на лету и синхронизирует его с окружением."""
    global BOT_TOKEN
    BOT_TOKEN = (token or "").strip()
    os.environ["TELEGRAM_BOT_TOKEN"] = BOT_TOKEN
    return BOT_TOKEN


def _bot_request(method, data=None, files=None, timeout=30, max_attempts=4):
    global _flood_until
    if not get_bot_token():
        raise RuntimeError("На сервере не задан TELEGRAM_BOT_TOKEN.")
    wait_left = _flood_wait_left()
    if wait_left > 0:
        raise RuntimeError(f"Telegram Bot API (429): Too Many Requests: retry after {wait_left}")

    def sleep_backoff(attempt, retry_after=None):
        if retry_after:
            time.sleep(min(max(int(retry_after), 1), 30))
        else:
            time.sleep(min(2 ** attempt, 8))

    for attempt in range(max_attempts):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{get_bot_token()}/{method}",
                data=data or {},
                files=files,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            if attempt == max_attempts - 1:
                raise RuntimeError("Не удалось подключиться к Telegram Bot API.") from exc
            sleep_backoff(attempt)
            continue
        try:
            payload = response.json()
        except ValueError as exc:
            if response.status_code >= 500 and attempt < max_attempts - 1:
                sleep_backoff(attempt)
                continue
            raise RuntimeError(f"Telegram Bot API вернул HTTP {response.status_code}.") from exc
        if response.ok and payload.get("ok"):
            return payload["result"]
        code = payload.get("error_code", response.status_code)
        description = payload.get("description", "неизвестная ошибка")
        if code == 429:
            # По флуду не повторяем: сразу запоминаем паузу и выходим.
            try:
                retry_after = (payload.get("parameters") or {}).get("retry_after")
                _flood_until = time.time() + max(int(retry_after or 0), 1)
            except (TypeError, ValueError):
                pass
            raise RuntimeError(f"Telegram Bot API (429): {description}")
        if code >= 500 and attempt < max_attempts - 1:
            sleep_backoff(attempt)
            continue
        raise RuntimeError(f"Telegram Bot API ({code}): {description}")


def bot_request(method, data=None, files=None, timeout=30):
    return _bot_request(method, data=data, files=files, timeout=timeout)


def normalize_channel(value):
    value = value.strip()
    if value.startswith("@"):
        username = value[1:]
    else:
        parsed = urlparse(value if "://" in value else f"https://{value}")
        if parsed.hostname not in {"t.me", "www.t.me"}:
            raise ValueError("Укажите публичную ссылку t.me или @username.")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2 and parts[0] == "s":
            parts.pop(0)
        if len(parts) != 1:
            raise ValueError("Поддерживаются только публичные каналы с username.")
        username = parts[0]
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise ValueError("Некорректный username публичного канала.")
    return username


def normalize_destination(value):
    """Приводит канал назначения к виду, пригодному для chat_id Bot API.

    Публичные каналы: '@username', 'username', 't.me/username',
    'https://t.me/username' → '@username' (раньше сырой URL уходил в Bot API
    и вызывал ошибку 400 «chat not found»).
    Числовые ID (например, '8967870222' или '-100...') остаются как есть —
    тип чата затем проверяет Bot API через getChat.
    """
    value = value.strip()
    if value.lstrip("-").isdigit():
        return value
    username = normalize_channel(value)
    return f"@{username}"


class PublicFeedParser(HTMLParser):
    """Парсит публичную ленту t.me/s, сохраняя текст, HTML-форматирование и медиа.

    Новый способ пересылки в полном объёме: помимо plain-text (для фильтра
    и истории) собираем Telegram-совместимый HTML — жирный/курсив/ссылки/
    код/спойлеры/кастомные эмодзи. Отправка идёт с parse_mode=HTML, поэтому
    ссылки остаются кликабельными прямо в тексте, а не привеском в скобках.
    """

    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    # Тег вёрстки -> (открывающий HTML, закрывающий HTML). None = выкинуть тег,
    # но оставить его текстовое содержимое.
    HTML_TAG_MAP = {
        "b": ("<b>", "</b>"),
        "strong": ("<b>", "</b>"),
        "i": ("<i>", "</i>"),
        "em": ("<i>", "</i>"),
        "u": ("<u>", "</u>"),
        "ins": ("<u>", "</u>"),
        "s": ("<s>", "</s>"),
        "strike": ("<s>", "</s>"),
        "del": ("<s>", "</s>"),
        "code": ("<code>", "</code>"),
        "pre": ("<pre>", "</pre>"),
        "blockquote": ("<blockquote>", "</blockquote>"),
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.posts = []
        self.current = None
        self.wrap_depth = None
        self.text_depth = None
        self.anchor_stack = []
        self.html_stack = []

    def _html_open(self, tag, attr_map, depth):
        """Преобразует открывающий тег вёрстки в Telegram-HTML либо пропускает."""
        parts = self.current["html_parts"]
        if tag == "br":
            parts.append("\n")
            return
        if tag == "a":
            href = html.unescape((attr_map.get("href") or "").strip())
            if urlparse(href).scheme.lower() in {"http", "https"}:
                safe_href = html.escape(href, quote=True)
                parts.append(f'<a href="{safe_href}">')
                self.html_stack.append({"depth": depth, "close": "</a>"})
            return
        if tag == "tg-emoji":
            emoji_id = (attr_map.get("emoji-id") or "").strip()
            if emoji_id.isdigit():
                parts.append(f'<tg-emoji emoji-id="{emoji_id}">')
                self.html_stack.append({"depth": depth, "close": "</tg-emoji>"})
            return
        if tag == "span" and "tg-spoiler" in set((attr_map.get("class") or "").split()):
            parts.append('<span class="tg-spoiler">')
            self.html_stack.append({"depth": depth, "close": "</span>"})
            return
        mapped = self.HTML_TAG_MAP.get(tag)
        if mapped:
            parts.append(mapped[0])
            self.html_stack.append({"depth": depth, "close": mapped[1]})

    def _html_close(self, depth):
        """Закрывает HTML-теги, открытые на заданной глубине."""
        while self.html_stack and self.html_stack[-1]["depth"] == depth:
            self.current["html_parts"].append(self.html_stack.pop()["close"])

    def add_media(self, url, media_type):
        if not url or self.current is None:
            return
        url = html.unescape(url.strip())
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in MEDIA_HOSTS
        ):
            return
        item = {"type": media_type, "url": url}
        if item not in self.current["media"]:
            self.current["media"].append(item)

    def handle_starttag(self, tag, attrs):
        depth = len(self.stack) + 1
        attr_map = dict(attrs)
        classes = set(attr_map.get("class", "").split())

        if tag == "div" and "tgme_widget_message_wrap" in classes:
            self.current = {"id": "", "text_parts": [], "html_parts": [], "media": [], "links": []}
            self.wrap_depth = depth
            self.html_stack = []

        if self.current is not None:
            post_id = attr_map.get("data-post", "")
            if post_id:
                self.current["id"] = post_id.rsplit("/", 1)[-1]
            if tag == "div" and "tgme_widget_message_text" in classes:
                self.text_depth = depth
            if self.text_depth is not None and depth > self.text_depth:
                self._html_open(tag, attr_map, depth)
            if tag == "a" and self.text_depth is not None:
                href = html.unescape(attr_map.get("href", "").strip())
                if urlparse(href).scheme.lower() in {"http", "https"}:
                    self.anchor_stack.append({"depth": depth, "href": href, "text_parts": []})
            if tag == "br" and self.text_depth is not None:
                self.current["text_parts"].append("\n")
            if tag == "a" and "tgme_widget_message_photo_wrap" in classes:
                match = re.search(r"""url\(['"]?(.*?)['"]?\)""", attr_map.get("style", ""))
                if match:
                    self.add_media(match.group(1), "photo")
            if tag == "video":
                self.add_media(attr_map.get("src") or attr_map.get("data-src"), "video")
            elif tag == "source":
                source_url = attr_map.get("src") or attr_map.get("data-src")
                if source_url and "video" in attr_map.get("type", "").lower():
                    self.add_media(source_url, "video")
            for key in ("data-video", "data-video-src", "data-src"):
                value = attr_map.get(key)
                if value and (key.startswith("data-video") or tag in {"video", "source"}):
                    self.add_media(value, "video")

        if tag not in self.VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        depth = len(self.stack)
        if self.current is not None:
            if self.text_depth is not None and depth > self.text_depth:
                self._html_close(depth)
            if tag == "a" and self.anchor_stack and self.anchor_stack[-1]["depth"] == depth:
                anchor = self.anchor_stack.pop()
                href = anchor["href"]
                anchor_text = "".join(anchor["text_parts"]).strip()
                if href not in anchor_text:
                    self.current["text_parts"].append(f" ({href})")
                self.current["links"].append(href)
            if self.text_depth == depth:
                self.current["text_parts"].append("\n")
                self.text_depth = None
            if self.wrap_depth == depth:
                self.current["text"] = re.sub(
                    r"\n{3,}", "\n\n", "".join(self.current.pop("text_parts")).strip()
                )
                self.current["html"] = re.sub(
                    r"\n{3,}", "\n\n", "".join(self.current.pop("html_parts")).strip()
                )
                if self.current["id"].isdigit():
                    self.posts.append(self.current)
                self.current = None
                self.wrap_depth = None
                self.text_depth = None
                self.anchor_stack = []
                self.html_stack = []

        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.current is not None and self.text_depth is not None:
            self.current["text_parts"].append(data)
            self.current["html_parts"].append(html.escape(data))
            if self.anchor_stack:
                self.anchor_stack[-1]["text_parts"].append(data)


def fetch_public_posts(username):
    try:
        response = requests.get(
            f"https://t.me/s/{username}",
            headers={"User-Agent": "Mozilla/5.0 (compatible; TelegramMiniApp/1.0)"},
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Не удалось загрузить публичную ленту @{username}.") from exc
    parser = PublicFeedParser()
    parser.feed(response.text)
    if not parser.posts:
        raise RuntimeError(f"В публичной ленте @{username} не найдено доступных публикаций.")
    return parser.posts


def fetch_single_post(username, post_id):
    """Догружает один конкретный пост для полного текста и медиа.

    Страница списка t.me/s иногда отдаёт обрезанный snippet, а страница
    отдельного поста t.me/s/{username}/{post_id} содержит текст целиком.
    Используется как для точного фильтра, так и для fallback-пересылки.
    """
    try:
        response = requests.get(
            f"https://t.me/s/{username}/{post_id}",
            headers={"User-Agent": "Mozilla/5.0 (compatible; TelegramMiniApp/1.0)"},
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Не удалось загрузить пост @{username}/{post_id}.") from exc
    parser = PublicFeedParser()
    parser.feed(response.text)
    for post in parser.posts:
        if post.get("id") == str(post_id):
            return post
    if parser.posts:
        return parser.posts[0]
    raise RuntimeError(f"Пост @{username}/{post_id} не найден в публичной ленте.")


def text_units(text):
    return len(text.encode("utf-16-le")) // 2


def _media_file(media):
    """Готовит файл для отправки: метод, поле, имя, байты.

    Байты либо уже есть (точный MTProto-путь, оригиналы), либо качаются
    с CDN (скрапер). Тип определяет метод: кругляшки -> sendVideoNote,
    голосовые -> sendVoice, GIF -> sendAnimation, стикеры -> sendSticker.
    """
    if media.get("data") is not None:
        binary = media["data"]
        filename = media.get("filename") or "file.bin"
    else:
        filename, binary = download_media(media)
    kind = media.get("type")
    mapping = {
        "photo": ("sendPhoto", "photo"),
        "video": ("sendVideo", "video"),
        "gif": ("sendAnimation", "animation"),
        "voice": ("sendVoice", "voice"),
        "round": ("sendVideoNote", "video_note"),
        "sticker": ("sendSticker", "sticker"),
        "file": ("sendDocument", "document"),
    }
    method, field = mapping.get(kind, ("sendPhoto", "photo"))
    return method, field, filename, binary


def _split_with_entities(text, entities, limit):
    """Делит текст по лимиту (UTF-16), раздавая entities по частям без потерь."""
    if text_units(text) <= limit:
        return [(text, list(entities or []))]
    segments = []
    start = 0
    total = len(text)
    while start < total:
        end = start
        units = 0
        while end < total:
            width = 2 if ord(text[end]) > 0xFFFF else 1
            if units + width > limit:
                break
            units += width
            end += 1
        if end == start:
            raise ValueError("Не удалось разделить сообщение в допустимый лимит Telegram.")
        if end < total:
            boundary = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
            if boundary >= start + limit // 2:
                end = boundary + 1
        start_units = len(text[:start].encode("utf-16-le")) // 2
        end_units = len(text[:end].encode("utf-16-le")) // 2
        kept = [
            {**item, "offset": item.get("offset", 0) - start_units}
            for item in (entities or [])
            if item.get("offset", 0) >= start_units
            and item.get("offset", 0) + item.get("length", 0) <= end_units
        ]
        segments.append((text[start:end], kept))
        start = end
    return segments


def _send_poll(destination, poll, reply_markup, signature=""):
    options = poll.get("options") or []
    if not options:
        raise ValueError("Опрос без вариантов ответа.")
    payload = {
        "chat_id": destination,
        "question": poll.get("question", "")[:300],
        "options": json.dumps(options[:10]),
        "is_anonymous": poll.get("is_anonymous", True),
    }
    if poll.get("type") == "quiz":
        payload["type"] = "quiz"
        if poll.get("correct_option_id") is not None:
            payload["correct_option_id"] = poll["correct_option_id"]
        if poll.get("explanation"):
            payload["explanation"] = poll["explanation"][:200]
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    bot_request("sendPoll", payload, timeout=30)
    if (signature or "").strip():
        bot_request("sendMessage", {"chat_id": destination, "text": signature.strip()}, timeout=30)
    return True


TAG_RE = re.compile(r"<[^>]+>")


def visible_text(html_text):
    """Видимый текст без HTML-тегов (для подсчёта лимитов Telegram)."""
    return html.unescape(TAG_RE.sub("", html_text or ""))


def visible_units(html_text):
    return text_units(visible_text(html_text))


def post_html(post):
    """HTML поста: из парсера либо экранированный plain-text."""
    if post.get("html"):
        return post["html"]
    return html.escape(post.get("text", ""))


def split_text(text, limit):
    parts = []
    start = 0
    while start < len(text):
        end = start
        units = 0
        while end < len(text):
            width = 2 if ord(text[end]) > 0xFFFF else 1
            if units + width > limit:
                break
            units += width
            end += 1
        if end == start:
            raise ValueError("Не удалось разделить сообщение в допустимый лимит Telegram.")
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
            if boundary >= start + limit // 2:
                end = boundary + 1
        parts.append(text[start:end])
        start = end
    return parts


def download_media(media):
    media_type = media["type"]
    max_bytes = MAX_VIDEO_BYTES if media_type == "video" else MAX_PHOTO_BYTES
    try:
        response = requests.get(media["url"], stream=True, timeout=(10, 45))
    except requests.RequestException as exc:
        raise RuntimeError("Не удалось скачать медиа из публичной ленты.") from exc
    try:
        response.raise_for_status()
        parsed = urlparse(response.url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in MEDIA_HOSTS
        ):
            raise RuntimeError("Ссылка на медиа перенаправляет за пределы доменов Telegram.")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        expected = "video/" if media_type == "video" else "image/"
        if content_type and not content_type.startswith(expected):
            raise RuntimeError(f"Ссылка на {media_type} не вернула подходящий файл.")
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise RuntimeError(f"Файл {media_type} превышает лимит загрузки Telegram.")
        data = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            data.extend(chunk)
            if len(data) > max_bytes:
                raise RuntimeError(f"Файл {media_type} превышает лимит загрузки Telegram.")
        extension = mimetypes.guess_extension(content_type) or (".mp4" if media_type == "video" else ".jpg")
        return f"file{extension}", bytes(data)
    except requests.RequestException as exc:
        raise RuntimeError("Не удалось безопасно скачать медиа из Telegram.") from exc
    finally:
        response.close()


def _append_signature(text, signature):
    signature = (signature or "").strip()
    if not signature:
        return text
    return f"{text}\n\n{signature}" if text else signature


def _append_signature_html(html_text, signature):
    signature = (signature or "").strip()
    if not signature:
        return html_text
    safe_signature = html.escape(signature)
    return f"{html_text}\n\n{safe_signature}" if html_text else safe_signature


def _apply_signature(chat_id, copied, signature):
    """Добавляет подпись к уже скопированному сообщению.

    Пытается вписать подпись в текст/подпись правкой (одно сообщение),
    иначе отправляет подпись отдельным сообщением. Оригинал при этом
    уже скопирован целиком и не страдает.
    """
    signature = (signature or "").strip()
    if not signature or not isinstance(copied, dict):
        return
    copied_id = copied.get("message_id")
    if not copied_id:
        bot_request("sendMessage", {"chat_id": chat_id, "text": signature}, timeout=30)
        return
    try:
        if copied.get("text") is not None:
            new_text = f"{copied.get('text', '')}\n\n{signature}" if copied.get("text") else signature
            if text_units(new_text) > MAX_MESSAGE_UNITS:
                raise ValueError("too long")
            payload = {"chat_id": chat_id, "message_id": copied_id, "text": new_text}
            if copied.get("entities"):
                payload["entities"] = json.dumps(copied["entities"])
            bot_request("editMessageText", payload, timeout=30)
            return
        if copied.get("caption") is not None:
            new_caption = f"{copied.get('caption', '')}\n\n{signature}" if copied.get("caption") else signature
            if text_units(new_caption) > MAX_CAPTION_UNITS:
                raise ValueError("too long")
            payload = {"chat_id": chat_id, "message_id": copied_id, "caption": new_caption}
            if copied.get("caption_entities"):
                payload["caption_entities"] = json.dumps(copied["caption_entities"])
            bot_request("editMessageCaption", payload, timeout=30)
            return
    except (RuntimeError, ValueError):
        pass
    # Фото/видео без подписи, кругляшки, голосовые, стикеры, опросы:
    # подпись отдельным сообщением, оригинал уже на месте целиком.
    try:
        bot_request("sendMessage", {"chat_id": chat_id, "text": signature}, timeout=30)
    except RuntimeError:
        pass


def _russian_copy_error(description):
    """Переводит типовые ошибки копирования на понятный русский."""
    lowered = (description or "").lower()
    if "message to copy not found" in lowered or "message to forward not found" in lowered \
            or "message_id_invalid" in lowered or "message not found" in lowered:
        return "Пост не найден в источнике (удалён или неверный ID)."
    if "message can't be copied" in lowered or "message can't be forwarded" in lowered:
        return "Источник запретил копирование (защищённый контент)."
    if "chat not found" in lowered:
        return "Чат не найден: проверьте username источника и назначения."
    if "bot is not a member" in lowered or "bot was blocked" in lowered or "forbidden" in lowered:
        return "Бот не состоит в чате или заблокирован: добавьте его администратором в канал назначения."
    if "not enough rights" in lowered or "need administrator" in lowered or "can't post" in lowered:
        return "Боту не хватает прав: дайте право публиковать сообщения в канале назначения."
    return description or "неизвестная ошибка"


def forward_message(chat_id, from_chat_id, message_id, signature=""):
    """Пересылает сообщение серверным копированием — всё 1-в-1.

    copyMessage создаёт НОВОЕ сообщение без метки «переслано из», выглядит так,
    будто вы опубликовали его сами. Сервер Telegram копирует оригинал целиком:
    полный текст любой длины, все фото/видео/альбомы, форматирование, entities,
    ссылки, превью, кругляшки, голосовые — без скачивания и без сжатия.
    """
    try:
        message_id = int(message_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Некорректный ID поста: {message_id!r}.") from exc
    if isinstance(from_chat_id, str) and from_chat_id.lstrip("-").isdigit():
        source = from_chat_id.strip()
    else:
        source = str(from_chat_id).strip()
        if not source.startswith("@"):
            source = f"@{source}"
    copied = None
    copy_error = ""
    try:
        copied = bot_request(
            "copyMessage",
            data={
                "chat_id": chat_id,
                "from_chat_id": source,
                "message_id": message_id,
            },
            timeout=30,
        )
    except RuntimeError as exc:
        copy_error = str(exc)
    if copied is None and copy_error:
        # copyMessage не умеет редкие типы — пробуем обычный forward (с меткой,
        # но тоже целиком и без потерь).
        try:
            bot_request(
                "forwardMessage",
                data={
                    "chat_id": chat_id,
                    "from_chat_id": source,
                    "message_id": message_id,
                },
                timeout=30,
            )
            return True
        except RuntimeError as exc:
            raise RuntimeError(_russian_copy_error(str(exc) or copy_error)) from exc
    if copied is None:
        raise RuntimeError(_russian_copy_error(copy_error))
    _apply_signature(chat_id, copied, signature)
    return True


def publish_post(post, destination, signature=""):
    """Пересылает пост в полном объёме (текст + все медиа).

    Два пути, от лучшего к запасному:
    1. Серверное копирование copyMessage по (source, id) — всё 1-в-1 без
       скачивания и сжатия, выглядит как собственный пост. Работает, когда
       бот состоит в канале-источнике (например, ваши собственные каналы,
       куда бота добавили администратором).
    2. Полная перезаливка — для чужих публичных каналов, куда бота не
       добавили (Telegram не отдаёт их посты ботам напрямую). Тогда текст
       догружается со страницы отдельного поста целиком, все медиа качаются
       в оригинале с CDN Telegram и отправляются все до единого: альбомы
       группами по 10, длинные подписи и тексты — продолжением сообщений.
       Ничего не теряется, просто пост собирается из тех же частей.
    """
    source_username = post.get("source_username") or post.get("from_chat")
    message_id = post.get("id") or post.get("message_id")
    if source_username and message_id:
        try:
            return forward_message(destination, f"@{source_username}", message_id, signature)
        except RuntimeError as copy_exc:
            # Копия не удалась — всегда пробуем полную перезаливку, если есть
            # что перезаливать (текст/медиа уже есть в snippet, а полный текст
            # догружаем со страницы отдельного поста). Ошибки назначения
            # (нет прав, чат не найден) всплывут и из перезаливки тоже.
            enriched = dict(post)
            try:
                full = fetch_single_post(source_username.lstrip("@"), int(message_id))
                if full.get("text"):
                    enriched["text"] = full["text"]
                if full.get("html"):
                    enriched["html"] = full["html"]
                if full.get("media"):
                    enriched["media"] = full["media"]
            except (RuntimeError, ValueError):
                pass
            if not enriched.get("text", "").strip() and not enriched.get("media"):
                raise copy_exc from None
            post = enriched

    # Перезаливка в полном объёме (fallback и ручной вызов без привязки).
    # Точный путь MTProto: plain text + entities + кнопки + опросы + все типы
    # медиа. Путь скрапера: HTML + parse_mode (жирный/курсив/ссылки как в оригинале).
    entities = post.get("entities") or []
    reply_markup = post.get("reply_markup")
    media_items = post.get("media", [])

    def _markup():
        return {"reply_markup": json.dumps(reply_markup)} if reply_markup else {}

    if post.get("poll") and not media_items:
        return _send_poll(destination, post["poll"], reply_markup, signature)

    use_entities = bool(entities)
    if use_entities:
        full_text = _append_signature(post.get("text", "").strip(), signature)
    else:
        html_text = _append_signature_html(
            (post.get("html") or html.escape(post.get("text", "").strip())).strip(), signature
        )
    media_items = post.get("media", [])
    if len(media_items) > 1:
        if use_entities:
            parts = _split_with_entities(full_text, entities, MAX_CAPTION_UNITS)
            first_caption, first_ents, rest = parts[0][0], parts[0][1], parts[1:]
            first_is_html = False
        elif visible_units(html_text) <= MAX_CAPTION_UNITS:
            first_caption, first_ents, rest = html_text, [], []
            first_is_html = True
        else:
            plain_parts = split_text(visible_text(html_text), MAX_CAPTION_UNITS)
            first_caption, first_ents, rest = plain_parts.pop(0), [], [(p, []) for p in plain_parts]
            first_is_html = False
        for offset in range(0, len(media_items), MEDIA_GROUP_SIZE):
            chunk = media_items[offset:offset + MEDIA_GROUP_SIZE]
            media_json = []
            files = {}
            for index, media in enumerate(chunk):
                _method, _field, filename, binary_data = _media_file(media)
                field = f"media_{offset + index}"
                files[field] = (filename, binary_data)
                entry = {
                    "type": "photo" if media.get("type") == "photo" else "video",
                    "media": f"attach://{field}",
                }
                if offset == 0 and index == 0 and first_caption:
                    entry["caption"] = first_caption
                    if first_ents:
                        entry["caption_entities"] = json.dumps(first_ents)
                    elif first_is_html:
                        entry["parse_mode"] = "HTML"
                media_json.append(entry)
            bot_request(
                "sendMediaGroup",
                data={"chat_id": destination, "media": json.dumps(media_json)},
                files=files,
                timeout=60,
            )
        for part, part_ents in rest:
            payload = {"chat_id": destination, "text": part}
            if part_ents:
                payload["entities"] = json.dumps(part_ents)
            bot_request("sendMessage", payload)
        return True
    if media_items:
        if use_entities:
            caption_segments = _split_with_entities(full_text, entities, MAX_CAPTION_UNITS) if full_text else []
            is_html = False
        elif visible_units(html_text) <= MAX_CAPTION_UNITS:
            caption_segments = [(html_text, [])] if html_text else []
            is_html = True
        else:
            caption_segments = [(p, []) for p in split_text(visible_text(html_text), MAX_CAPTION_UNITS)] if html_text else []
            is_html = False
        for index, media in enumerate(media_items):
            method, field, filename, binary_data = _media_file(media)
            data = {"chat_id": destination, **(_markup() if index == 0 else {})}
            caption_allowed = method in {"sendPhoto", "sendVideo", "sendAnimation", "sendVoice", "sendDocument"}
            if index == 0 and caption_segments and caption_allowed:
                caption_text, caption_ents = caption_segments.pop(0)
                data["caption"] = caption_text
                if caption_ents:
                    data["caption_entities"] = json.dumps(caption_ents)
                elif is_html:
                    data["parse_mode"] = "HTML"
            bot_request(
                method,
                data=data,
                files={field: (filename, binary_data)},
                timeout=60,
            )
        for part, part_ents in caption_segments:
            payload = {"chat_id": destination, "text": part}
            if part_ents:
                payload["entities"] = json.dumps(part_ents)
            bot_request("sendMessage", payload)
        return True
    text_segments = (
        _split_with_entities(full_text, entities, MAX_MESSAGE_UNITS)
        if use_entities
        else ([(html_text, [])] if visible_units(html_text) <= MAX_MESSAGE_UNITS
              else [(p, []) for p in split_text(visible_text(html_text), MAX_MESSAGE_UNITS)])
    ) if (full_text if use_entities else html_text) else []
    for position, (part, part_ents) in enumerate(text_segments):
        payload = {"chat_id": destination, "text": part}
        if part_ents:
            payload["entities"] = json.dumps(part_ents)
        elif not use_entities:
            payload["parse_mode"] = "HTML"
        if position == 0:
            payload.update(_markup())
        bot_request("sendMessage", payload, timeout=30)
    return bool(text_segments)
