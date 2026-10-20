import csv
import json
import math
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from load_reference import load_parkings

ROOT = Path(__file__).resolve().parents[1]


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def parse_utc(value):
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError("Не указан часовой пояс.")
    if timestamp.utcoffset().total_seconds() != 0:
        raise ValueError("Ожидается время UTC.")
    return timestamp.astimezone(timezone.utc)


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Нечисловое или бесконечное значение.")
    return result


def integer(value):
    result = number(value)
    if not result.is_integer():
        raise ValueError("Ожидается целое число.")
    return int(result)


def record_key(row):
    return (
        row["station_code"],
        row["source"],
        integer(row["period_seconds"]),
        parse_utc(row["timestamp_utc"]),
    )


def main():
    config = json.loads(
        (ROOT / "config" / "processing.json").read_text(encoding="utf-8")
    )
    schema = json.loads(
        (ROOT / "config" / "schemas.json").read_text(encoding="utf-8")
    )
    parkings = load_parkings()

    parking = read_csv(ROOT / "data/processed/parking_clean.csv")
    weather = read_csv(ROOT / "data/processed/weather_clean.csv")
    mart = read_csv(ROOT / "data/marts/parking_weather.csv")

    if not parking or not weather or not mart:
        raise ValueError("Одна из проверяемых таблиц пустая.")

    parking_by_key = {record_key(row): row for row in parking}
    if len(parking_by_key) != len(parking):
        raise ValueError("В подготовленных парковках есть дубли.")

    weather_by_time = {}
    weather_fields = [
        "temperature_c", "precipitation_mm",
        "cloud_cover_pct", "wind_speed_kmh",
    ]

    for row in weather:
        timestamp = parse_utc(row["timestamp_utc"])
        if timestamp in weather_by_time:
            raise ValueError("В подготовленной погоде есть дубли.")

        values = {field: number(row[field]) for field in weather_fields}
        if values["precipitation_mm"] < 0:
            raise ValueError("Отрицательные осадки.")
        if values["wind_speed_kmh"] < 0:
            raise ValueError("Отрицательная скорость ветра.")
        if not 0 <= values["cloud_cover_pct"] <= 100:
            raise ValueError("Недопустимая облачность.")

        weather_by_time[timestamp] = values

    weather_times = sorted(weather_by_time)
    local_zone = ZoneInfo(config["calendar_timezone"])
    start = parse_utc(config["start_date"] + "T00:00:00+00:00")

    errors = Counter()
    examples = {}
    seen = set()
    off_grid = 0
    unverified_capacity = 0

    def check(condition, name, detail):
        if not condition:
            errors[name] += 1
            examples.setdefault(name, [])
            if len(examples[name]) < 3:
                examples[name].append(detail)

    check(
        len(mart) == len(parking),
        "Количество строк",
        f"Парковки: {len(parking)}, витрина: {len(mart)}",
    )

    for index, row in enumerate(mart, start=1):
        detail = f"Строка {index}"

        try:
            key = record_key(row)
            timestamp = key[3]
            code = row["station_code"]
            occupied = integer(row["occupied"])
            capacity = integer(row["capacity"])
            occupancy = number(row["occupancy_pct"])

            if code not in parkings:
                raise ValueError(f"Неизвестная парковка: {code}")

            reference = parkings[code]

            check(key not in seen, "Уникальность ключей", detail)
            seen.add(key)

            check(
                row["source"] == config["parking_source"]
                and key[2] == config["parking_period_seconds"],
                "Поставщик и период", detail,
            )
            check(
                row["station_name"] == reference["station_name"]
                and capacity == reference["capacity"],
                "Связь со справочником", detail,
            )
            check(
                capacity > 0 and 0 <= occupied <= capacity,
                "Диапазон занятых мест", detail,
            )

            if capacity > 0:
                check(
                    0 <= occupancy <= 100
                    and math.isclose(
                        occupancy,
                        occupied / capacity * 100,
                        rel_tol=0,
                        abs_tol=0.000001,
                    ),
                    "Формула процента загрузки", detail,
                )

            expected_grid = (
                (timestamp - start).total_seconds()
                % config["parking_period_seconds"] == 0
            )
            check(
                row["is_on_grid"] == str(expected_grid).lower(),
                "Признак временной сетки", detail,
            )
            off_grid += not expected_grid

            check(
                row["capacity_history_verified"]
                == str(reference["capacity_history_verified"]).lower()
                and row["capacity_reference_date"]
                == reference["capacity_reference_date"],
                "Сведения о вместимости", detail,
            )
            unverified_capacity += not reference["capacity_history_verified"]

            original = parking_by_key.get(key)
            check(original is not None, "Наличие исходного наблюдения", detail)

            if original is not None:
                check(
                    all(
                        row[field] == original[field]
                        for field in schema["parking_columns"]
                    ),
                    "Сохранность парковочных полей", detail,
                )

            position = bisect_right(weather_times, timestamp) - 1
            weather_time = weather_times[position] if position >= 0 else None

            if weather_time is None:
                expected_status = "missing"
                check(
                    row["weather_timestamp_utc"] == ""
                    and row["weather_age_minutes"] == "",
                    "Отсутствующая погодная отметка", detail,
                )
            else:
                age = (timestamp - weather_time).total_seconds() / 60
                expected_status = (
                    "matched"
                    if age <= config["max_weather_age_minutes"]
                    else "stale"
                )

                check(
                    parse_utc(row["weather_timestamp_utc"]) == weather_time
                    and math.isclose(
                        number(row["weather_age_minutes"]),
                        age, rel_tol=0, abs_tol=0.000001,
                    ),
                    "Время и возраст погоды", detail,
                )

            check(
                row["weather_status"] == expected_status,
                "Статус погодного сопоставления", detail,
            )

            if expected_status == "matched":
                check(
                    all(
                        math.isclose(
                            number(row[field]),
                            weather_by_time[weather_time][field],
                            rel_tol=0, abs_tol=0.000001,
                        )
                        for field in weather_fields
                    ),
                    "Значения сопоставленной погоды", detail,
                )
            else:
                check(
                    all(row[field] == "" for field in weather_fields),
                    "Незаполненная погода при отсутствии соответствия",
                    detail,
                )

            local_time = timestamp.astimezone(local_zone)
            check(
                row["timestamp_local"] == local_time.isoformat()
                and row["date_local"] == local_time.date().isoformat()
                and integer(row["hour_local"]) == local_time.hour
                and integer(row["day_of_week"]) == local_time.weekday()
                and row["is_weekend"]
                == str(local_time.weekday() >= 5).lower(),
                "Календарные признаки", detail,
            )

        except (ValueError, TypeError, KeyError, OverflowError) as error:
            check(False, "Типы и обязательные значения", f"{detail}: {error}")

    check(
        seen == set(parking_by_key),
        "Полнота переноса наблюдений",
        "Наборы ключей парковок и витрины должны совпадать.",
    )

    report = {
        "passed": not errors,
        "mart_rows": len(mart),
        "error_counts": dict(errors),
        "error_examples": examples,
        "known_limitations": {
            "off_grid_records": off_grid,
            "capacity_history_unverified_records": unverified_capacity,
        },
    }

    path = ROOT / "logs/quality/mart_quality.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)

    print("Проверено строк витрины:", len(mart))
    print("Нарушений правил:", sum(errors.values()))
    print("Известных отметок вне сетки:", off_grid)
    print("Записей с неподтверждённой исторической вместимостью:", unverified_capacity)
    print("Отчёт качества:", path)

    for name, count in errors.items():
        print(f"ОШИБКА: {name} — {count}")

    if errors:
        raise SystemExit("Проверка качества не пройдена.")

    print("Все проверки качества витрины пройдены — OK")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"Проверка остановлена: {error}")
        raise SystemExit(1)