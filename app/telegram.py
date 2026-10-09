import html
import mimetypes
import os
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests


MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 49 * 1024 * 1024
MAX_MESSAGE_UNITS = 4096
MAX_CAPTION_UNITS = 1024
MEDIA_HOSTS = ("telesco.pe", "telegram.org", "telegram-cdn.org")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _bot_request(method, data=None, files=None, timeout=30):
    if not BOT_TOKEN:
        raise RuntimeError("На сервере не задан TELEGRAM_BOT_TOKEN.")
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            data=data or {},
            files=files,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError("Не удалось подключиться к Telegram Bot API.") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Telegram Bot API вернул HTTP {response.status_code}.") from exc
    if not response.ok or not payload.get("ok"):
        description = payload.get("description", "неизвестная ошибка")
        raise RuntimeError(f"Telegram Bot API ({payload.get('error_code', response.status_code)}): {description}")
    return payload["result"]


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


class PublicFeedParser(HTMLParser):
    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.posts = []
        self.current = None
        self.wrap_depth = None
        self.text_depth = None
        self.anchor_stack = []

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
            self.current = {"id": "", "text_parts": [], "media": [], "links": []}
            self.wrap_depth = depth

        if self.current is not None:
            post_id = attr_map.get("data-post", "")
            if post_id:
                self.current["id"] = post_id.rsplit("/", 1)[-1]
            if tag == "div" and "tgme_widget_message_text" in classes:
                self.text_depth = depth
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
                if self.current["id"].isdigit():
                    self.posts.append(self.current)
                self.current = None
                self.wrap_depth = None
                self.text_depth = None
                self.anchor_stack = []

        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.current is not None and self.text_depth is not None:
            self.current["text_parts"].append(data)
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


def text_units(text):
    return len(text.encode("utf-16-le")) // 2


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


def publish_post(post, destination):
    text = post.get("text", "").strip()
    media_items = post.get("media", [])
    if media_items:
        caption_parts = split_text(text, MAX_CAPTION_UNITS) if text else []
        for index, media in enumerate(media_items):
            filename, binary_data = download_media(media)
            method = "sendVideo" if media["type"] == "video" else "sendPhoto"
            field = "video" if media["type"] == "video" else "photo"
            data = {"chat_id": destination}
            if index == 0 and caption_parts:
                data["caption"] = caption_parts.pop(0)
            bot_request(
                method,
                data=data,
                files={field: (filename, binary_data)},
                timeout=60,
            )
        for part in caption_parts:
            bot_request("sendMessage", {"chat_id": destination, "text": part})
    elif text:
        for part in split_text(text, MAX_MESSAGE_UNITS):
            bot_request("sendMessage", {"chat_id": destination, "text": part})
    else:
        return False
    return True
