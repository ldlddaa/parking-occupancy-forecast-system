import io
import json
from datetime import date, timedelta
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import http_client
import update_parking
import update_weather


def check_request(
    title,
    responses,
    expected_calls,
    should_fail=False,
    expected_wait=None,
):
    with (
        patch("http_client.urlopen", side_effect=responses) as opening,
        patch("http_client.time.sleep") as sleeping,
        patch("http_client.ATTEMPTS", 3),
    ):
        failed = False

        try:
            result = http_client.request_bytes(
                "https://example.invalid/test"
            )
            assert result == b"OK"
        except RuntimeError:
            failed = True

        assert failed == should_fail, title
        assert opening.call_count == expected_calls, title

        if expected_wait is not None:
            sleeping.assert_called_once_with(expected_wait)

    print(f"{title} — OK")


# Ответы API здесь имитируются: настоящих сетевых запросов нет.
check_request(
    "Таймаут, затем успешный ответ",
    [TimeoutError("Тестовый таймаут"), io.BytesIO(b"OK")],
    expected_calls=2,
)

check_request(
    "Ошибка соединения, затем успешный ответ",
    [URLError("Тестовая ошибка соединения"), io.BytesIO(b"OK")],
    expected_calls=2,
)

check_request(
    "HTTP 429: учтено ожидание Retry-After",
    [
        HTTPError(
            "https://example.invalid/test",
            429,
            "Too Many Requests",
            {"Retry-After": "5"},
            None,
        ),
        io.BytesIO(b"OK"),
    ],
    expected_calls=2,
    expected_wait=5,
)

check_request(
    "HTTP 503, затем успешный ответ",
    [
        HTTPError(
            "https://example.invalid/test",
            503,
            "Service Unavailable",
            {},
            None,
        ),
        io.BytesIO(b"OK"),
    ],
    expected_calls=2,
)

check_request(
    "Постоянный таймаут: остановка после трёх попыток",
    [TimeoutError("Тестовый таймаут") for _ in range(3)],
    expected_calls=3,
    should_fail=True,
)

check_request(
    "HTTP 400: остановка без повторов",
    [
        HTTPError(
            "https://example.invalid/test",
            400,
            "Bad Request",
            {},
            None,
        )
    ],
    expected_calls=1,
    should_fail=True,
)

# Проверяем реальные загрузчики при недоступности источника.
for module in (update_parking, update_weather):
    path = module.STORAGE_PATH
    before = path.read_bytes()

    storage = json.loads(before)
    boundary = date.fromisoformat(
        storage["loaded_until_exclusive"]
    )
    until = boundary + timedelta(days=1)

    with (
        patch(
            "sys.argv",
            [module.__name__, "--until", until.isoformat()],
        ),
        patch.object(
            module,
            "request_bytes",
            side_effect=RuntimeError("Тестовая недоступность API"),
        ) as requesting,
    ):
        try:
            module.main()
        except RuntimeError as error:
            assert str(error) == "Тестовая недоступность API"
        else:
            raise AssertionError("Загрузчик не сообщил о сбое.")

        requesting.assert_called_once()

    assert path.read_bytes() == before, (
        f"{module.__name__}: хранилище изменилось при сбое"
    )

    print(
        f"{module.__name__}: данные и граница "
        "загрузки при сбое не изменились — OK"
    )

print("\nВсе проверки устойчивости пройдены.")