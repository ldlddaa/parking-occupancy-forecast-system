import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from check_schema import check_json, check_csv

ROOT = Path(__file__).resolve().parents[1]


def expect_error(title, operation):
    try:
        operation()
    except ValueError as error:
        print(f"{title} — OK")
        print("Обнаруженная проблема:", error)
    else:
        raise AssertionError(f"Изменение структуры не замечено: {title}")


def write_csv(path, fields, row):
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerow(row)


schema = json.loads(
    (ROOT / "config/schemas.json").read_text(encoding="utf-8")
)

storage = json.loads(
    (ROOT / "data/storage/parking.json").read_text(encoding="utf-8")
)
sample = dict(storage["data"][0])

with (ROOT / "data/processed/parking_clean.csv").open(
    encoding="utf-8-sig", newline=""
) as file:
    prepared_sample = next(csv.DictReader(file))

with TemporaryDirectory() as temporary:
    folder = Path(temporary)
    json_path = folder / "parking.json"
    csv_path = folder / "parking.csv"

    # Дополнительное поле API разрешено настройками.
    extra_raw = {**sample, "new_optional_field": "test"}
    json_path.write_text(
        json.dumps({"data": [extra_raw]}),
        encoding="utf-8",
    )

    assert check_json(
        json_path,
        schema["raw_parking_required_fields"],
        schema["allow_extra_raw_fields"],
    ) == 1
    print("Дополнительное необязательное поле API разрешено — OK")

    # Удаление обязательного поля должно быть замечено.
    missing_raw = dict(sample)
    del missing_raw["mvalue"]
    json_path.write_text(
        json.dumps({"data": [missing_raw]}),
        encoding="utf-8",
    )

    expect_error(
        "Удаление обязательного поля API",
        lambda: check_json(
            json_path,
            schema["raw_parking_required_fields"],
            schema["allow_extra_raw_fields"],
        ),
    )

    # Переименование столбца подготовленной таблицы.
    renamed = dict(prepared_sample)
    renamed["occupancy_percent"] = renamed.pop("occupancy_pct")
    write_csv(csv_path, list(renamed), renamed)

    expect_error(
        "Переименование обязательного столбца",
        lambda: check_csv(
            csv_path,
            schema["parking_columns"],
            schema["allow_extra_prepared_columns"],
        ),
    )

    # Добавление неожиданного столбца подготовленной таблицы.
    extra_prepared = {**prepared_sample, "unexpected_column": "test"}
    write_csv(csv_path, list(extra_prepared), extra_prepared)

    expect_error(
        "Неожиданный столбец подготовленной таблицы",
        lambda: check_csv(
            csv_path,
            schema["parking_columns"],
            schema["allow_extra_prepared_columns"],
        ),
    )

print("\nВсе проверки изменения структуры пройдены.")