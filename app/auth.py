import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl


MAX_INIT_DATA_AGE = 24 * 60 * 60


def validate_init_data(init_data):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    admin_id = os.environ.get("ADMIN_TELEGRAM_ID", "").strip()
    if not token or not admin_id:
        raise PermissionError("На сервере не настроены TELEGRAM_BOT_TOKEN и ADMIN_TELEGRAM_ID.")
    if not init_data or len(init_data) > 8192:
        raise PermissionError("Откройте приложение через Telegram.")

    try:
        values = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError as exc:
        raise PermissionError("Данные авторизации Telegram повреждены.") from exc
    received_hash = values.pop("hash", "")
    if not received_hash or "auth_date" not in values or "user" not in values:
        raise PermissionError("Telegram не передал данные авторизации.")

    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise PermissionError("Не удалось проверить подпись Telegram.")

    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = str(user["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise PermissionError("Данные авторизации Telegram повреждены.") from exc
    if auth_date > int(time.time()) + 60 or int(time.time()) - auth_date > MAX_INIT_DATA_AGE:
        raise PermissionError("Срок действия данных Telegram истёк. Закройте и снова откройте Mini App.")
    if not hmac.compare_digest(user_id, admin_id):
        raise PermissionError("Доступ к приложению разрешён только владельцу бота.")
    return user
