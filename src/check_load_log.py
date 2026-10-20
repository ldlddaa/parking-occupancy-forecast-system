import csv
import json
from datetime import date, timedelta
from unittest.mock import patch

import load_log
import update_parking
import update_weather


def read_log():
    with load_log.LOG_PATH.open(
        encoding="utf-8", newline=""
    ) as file:
        return list(csv.DictReader(file))


for source, module in (
    ("parking", update_parking),
    ("weather", update_weather),
):
    before_bytes = module.STORAGE_PATH.read_bytes()
    storage = json.loads(before_bytes)

    boundary = storage["loaded_until_exclusive"]
    until = date.fromisoformat(boundary) + timedelta(days=1)
    previous_log = read_log()

    message = "Тестовая недоступность API: проверка журнала"

    with (
        patch(
            "sys.argv",
            [module.__name__, "--until", until.isoformat()],
        ),
        patch.object(
            module,
            "request_bytes",
            side_effect=RuntimeError(message),
        ) as requesting,
    ):
        try:
            load_log.run_logged(
                source, module.STORAGE_PATH, module.main
            )
        except RuntimeError as error:
            assert str(error) == message
        else:
            raise AssertionError("Ожидаемая ошибка не возникла.")

        requesting.assert_called_once()

    updated_log = read_log()
    last = updated_log[-1]

    assert len(updated_log) == len(previous_log) + 1
    assert last["source"] == source
    assert last["status"] == "error"
    assert message in last["error"]
    assert last["record_count_change"] == "0"
    assert last["boundary_before"] == boundary
    assert last["boundary_after"] == boundary

    assert module.STORAGE_PATH.read_bytes() == before_bytes

    print(f"\n{source}: ошибка записана в журнал — OK")
    print("Статус:", last["status"])
    print("Текст ошибки:", last["error"])
    print("Данные и граница загрузки не изменились — OK")

print("\nПроверка регистрации ошибок пройдена.")