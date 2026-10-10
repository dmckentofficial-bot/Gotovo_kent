"""4-значный PIN-код: хэширование PBKDF2 и подсчёт времени блокировки.

PIN — дополнительный замок панели внутри уже авторизованной через Telegram
сессии владельца. Хэш хранится в service_state (ключ pin_hash).
"""

import hashlib
import hmac
import secrets

PIN_PATTERN = r"^\d{4}$"
PIN_LEN = 4
PBKDF2_ITERATIONS = 120_000
LOCK_FREE_ATTEMPTS = 5
LOCK_BASE_SECONDS = 30
LOCK_MAX_SECONDS = 900


def hash_pin(pin):
    """Возвращает строку 'pbkdf2$iterations$salt_hex$hash_hex'."""
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt.hex()}${derived.hex()}"


def verify_pin(stored, pin):
    if not stored or not pin:
        return False
    try:
        _, iterations, salt_hex, expected_hex = stored.split("$")
        derived = hashlib.pbkdf2_hmac(
            "sha256", pin.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, AttributeError, TypeError):
        return False
    return hmac.compare_digest(derived.hex(), expected_hex)


def lockout_seconds(fail_count, free_attempts=LOCK_FREE_ATTEMPTS,
                    base=LOCK_BASE_SECONDS, maximum=LOCK_MAX_SECONDS):
    """0 до порога свободных попыток, далее экспоненциальная блокировка."""
    if fail_count < free_attempts:
        return 0
    return min(maximum, base * 2 ** (fail_count - free_attempts))