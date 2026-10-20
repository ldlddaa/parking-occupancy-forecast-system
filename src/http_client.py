import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

# Настройки можно менять через переменные окружения.
TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "45"))
ATTEMPTS = int(os.getenv("HTTP_ATTEMPTS", "3"))
MAX_DELAY = 60

RETRY_CODES = {408, 429, 500, 502, 503, 504}


def retry_delay(value, default):
    """Читаем время ожидания из заголовка Retry-After."""
    if not value:
        return default

    try:
        return max(default, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)

            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)

            seconds = (
                retry_at - datetime.now(timezone.utc)
            ).total_seconds()

            return max(default, seconds)
        except (ValueError, TypeError, OverflowError):
            return default


def request_bytes(request):
    """Получаем ответ API с ограничением ожидания и повторами."""
    if TIMEOUT <= 0 or ATTEMPTS < 1:
        raise ValueError(
            "HTTP_TIMEOUT должен быть положительным, "
            "HTTP_ATTEMPTS — не меньше 1."
        )

    for attempt in range(1, ATTEMPTS + 1):
        delay = 2 ** attempt

        try:
            with urlopen(request, timeout=TIMEOUT) as response:
                return response.read()

        except HTTPError as error:
            code = error.code
            retry_after = error.headers.get("Retry-After")
            error.close()

            if code not in RETRY_CODES:
                raise RuntimeError(
                    f"HTTP {code}: запрос отклонён. "
                    "Автоматический повтор не выполняется."
                ) from error

            reason = f"HTTP {code}"
            delay = retry_delay(retry_after, delay)

        except (URLError, TimeoutError, ConnectionError) as error:
            reason = f"Ошибка соединения: {error}"

        if attempt == ATTEMPTS:
            raise RuntimeError(
                f"{reason}. Исчерпаны попытки: {ATTEMPTS}."
            )

        if delay > MAX_DELAY:
            raise RuntimeError(
                f"{reason}. Сервер просит подождать "
                f"{delay:.0f} секунд. Повторите запуск позже."
            )

        print(
            f"{reason}. Попытка {attempt}/{ATTEMPTS}. "
            f"Повтор через {delay:.0f} секунд.",
            flush=True,
        )
        time.sleep(delay)