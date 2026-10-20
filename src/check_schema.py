import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_json(path, required, allow_extra):
    storage = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(storage, dict):
        raise ValueError("Ожидается объект JSON.")

    rows = storage.get("data")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Отсутствует непустой список data.")

    required = set(required)

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Запись {index} не является объектом.")

        missing = required - set(row)
        extra = set(row) - required

        if missing:
            raise ValueError(
                f"Запись {index}: отсутствуют поля {sorted(missing)}"
            )

        if extra and not allow_extra:
            raise ValueError(
                f"Запись {index}: лишние поля {sorted(extra)}"
            )

    return len(rows)


def check_csv(path, required, allow_extra):
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        columns = reader.fieldnames or []

        if len(columns) != len(set(columns)):
            raise ValueError("Повторяющиеся названия столбцов.")

        missing = set(required) - set(columns)
        extra = set(columns) - set(required)

        if missing:
            raise ValueError(f"Отсутствуют столбцы {sorted(missing)}")

        if extra and not allow_extra:
            raise ValueError(f"Лишние столбцы {sorted(extra)}")

        count = 0

        for index, row in enumerate(reader, start=1):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(
                    f"Строка {index}: число значений не соответствует заголовку."
                )
            count += 1

        if count == 0:
            raise ValueError("Таблица не содержит записей.")

    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stage",
        choices=["all", "raw", "prepared"],
        default="all",
    )
    stage = parser.parse_args().stage

    schema = json.loads(
        (ROOT / "config" / "schemas.json").read_text(encoding="utf-8")
    )

    if schema["schema_version"] != 1:
        raise ValueError("Неподдерживаемая версия описания структуры.")

    datasets = [
        (
            "raw",
            "data/storage/parking.json",
            schema["raw_parking_required_fields"],
        ),
        (
            "raw",
            "data/storage/weather.json",
            schema["raw_weather_required_fields"],
        ),
        (
            "prepared",
            "data/processed/parking_clean.csv",
            schema["parking_columns"],
        ),
        (
            "prepared",
            "data/processed/weather_clean.csv",
            schema["weather_columns"],
        ),
        (
            "prepared",
            "data/marts/parking_weather.csv",
            schema["parking_columns"] + schema["mart_extra_columns"],
        ),
    ]

    results = []

    for group, relative_path, required in datasets:
        if stage != "all" and group != stage:
            continue

        result = {
            "file": relative_path,
            "status": "error",
            "rows": None,
            "error": "",
        }

        try:
            if group == "raw":
                count = check_json(
                    ROOT / relative_path,
                    required,
                    schema["allow_extra_raw_fields"],
                )
            else:
                count = check_csv(
                    ROOT / relative_path,
                    required,
                    schema["allow_extra_prepared_columns"],
                )

            result["status"] = "passed"
            result["rows"] = count
            print(f"{relative_path}: {count} записей — OK")

        except (OSError, ValueError, TypeError, KeyError) as error:
            result["error"] = f"{type(error).__name__}: {error}"
            print(f"{relative_path}: ОШИБКА — {error}")

        results.append(result)

    passed = all(item["status"] == "passed" for item in results)

    report = {
        "schema_version": schema["schema_version"],
        "stage": stage,
        "passed": passed,
        "results": results,
    }

    path = ROOT / "logs" / "quality" / f"schema_{stage}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)

    print("\nОтчёт проверки:", path)

    if not passed:
        raise SystemExit("Обнаружено несоответствие структуры данных.")

    print("Все структуры соответствуют описанию — OK")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"Проверка остановлена: {error}")
        raise SystemExit(1)