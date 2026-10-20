import csv
import json
import math
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]

WEATHER_FIELDS = [
    "temperature_c",
    "precipitation_mm",
    "cloud_cover_pct",
    "wind_speed_kmh",
]


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        fields = list(reader.fieldnames or [])
        rows = list(reader)

    if not rows:
        raise ValueError(f"Пустая таблица: {path}")

    return fields, rows


def parse_utc(value):
    timestamp = datetime.fromisoformat(value)

    if timestamp.tzinfo is None:
        raise ValueError("В подготовленных данных не указан часовой пояс.")

    return timestamp.astimezone(timezone.utc)


def save_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    temporary.replace(path)


def main():
    config = json.loads(
        (ROOT / "config" / "processing.json").read_text(encoding="utf-8")
    )

    if config["schema_version"] != 1:
        raise ValueError("Неподдерживаемая версия настроек.")

    if config["mart_grain"] != "parking_observation":
        raise ValueError("Неожиданная детализация витрины.")

    if config["weather_join"] != "latest_at_or_before":
        raise ValueError("Неожиданное правило объединения.")

    max_age = float(config["max_weather_age_minutes"])
    if not math.isfinite(max_age) or max_age <= 0:
        raise ValueError("Предельный возраст погоды должен быть положительным.")

    local_zone = ZoneInfo(config["calendar_timezone"])

    parking_fields, parking = read_csv(
        ROOT / "data" / "processed" / "parking_clean.csv"
    )
    weather_fields, weather = read_csv(
        ROOT / "data" / "processed" / "weather_clean.csv"
    )

    required_parking = {
        "station_code", "station_name",
        "timestamp_utc", "timestamp_original",
        "source", "period_seconds",
        "occupied", "capacity", "occupancy_pct",
        "is_on_grid", "capacity_reference_date",
        "capacity_history_verified",
    }

    required_weather = {
        "timestamp_utc", "timestamp_original",
        "is_on_grid", *WEATHER_FIELDS,
    }

    if set(parking_fields) != required_parking:
        raise ValueError("Изменилась структура подготовленных парковок.")

    if set(weather_fields) != required_weather:
        raise ValueError("Изменилась структура подготовленной погоды.")

    weather_by_time = {}

    for row in weather:
        timestamp = parse_utc(row["timestamp_utc"])

        if timestamp in weather_by_time:
            raise ValueError("Повтор временной отметки погоды.")

        values = {}
        for field in WEATHER_FIELDS:
            value = float(row[field])
            if not math.isfinite(value):
                raise ValueError(f"Некорректный показатель погоды: {field}")
            values[field] = value

        weather_by_time[timestamp] = values

    weather_times = sorted(weather_by_time)
    result = []
    seen = set()
    statuses = Counter()
    maximum_matched_age = 0

    for row in parking:
        timestamp = parse_utc(row["timestamp_utc"])

        key = (
            row["station_code"],
            row["source"],
            int(row["period_seconds"]),
            timestamp,
        )

        if key in seen:
            raise ValueError(f"Повтор парковочного наблюдения: {key}")
        seen.add(key)

        # Ищем последнюю погодную отметку не позже наблюдения.
        position = bisect_right(weather_times, timestamp) - 1

        joined = {
            "weather_timestamp_utc": "",
            "weather_age_minutes": "",
            "weather_status": "missing",
            **{field: "" for field in WEATHER_FIELDS},
        }

        if position >= 0:
            weather_time = weather_times[position]
            age = (timestamp - weather_time).total_seconds() / 60

            joined["weather_timestamp_utc"] = weather_time.isoformat()
            joined["weather_age_minutes"] = round(age, 6)

            if age <= max_age:
                joined.update(weather_by_time[weather_time])
                joined["weather_status"] = "matched"
                maximum_matched_age = max(maximum_matched_age, age)
            else:
                joined["weather_status"] = "stale"

        local_time = timestamp.astimezone(local_zone)

        prepared = {
            **row,
            **joined,
            "timestamp_local": local_time.isoformat(),
            "date_local": local_time.date().isoformat(),
            "hour_local": local_time.hour,
            "day_of_week": local_time.weekday(),
            "is_weekend": str(local_time.weekday() >= 5).lower(),
        }

        statuses[joined["weather_status"]] += 1
        result.append(prepared)

    result.sort(
        key=lambda row: (row["station_code"], row["timestamp_utc"])
    )

    mart_path = ROOT / "data" / "marts" / "parking_weather.csv"
    save_csv(mart_path, list(result[0]), result)

    report = {
        "parking_input_rows": len(parking),
        "weather_input_rows": len(weather),
        "mart_rows": len(result),
        "weather_matched": statuses["matched"],
        "weather_missing": statuses["missing"],
        "weather_stale": statuses["stale"],
        "maximum_matched_weather_age_minutes": maximum_matched_age,
        "parking_off_grid_rows": sum(
            row["is_on_grid"] == "false" for row in result
        ),
        "calendar_timezone": config["calendar_timezone"],
        "day_of_week_encoding": "Monday=0, Sunday=6",
        "max_weather_age_minutes": max_age,
    }

    report_path = ROOT / "logs" / "quality" / "mart_building.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(report_path)

    print("Парковочных записей на входе:", len(parking))
    print("Погодных записей на входе:", len(weather))
    print("Строк в витрине:", len(result))
    print("Успешно сопоставлена погода:", statuses["matched"])
    print("Не найдена предшествующая погода:", statuses["missing"])
    print("Погода старше допустимого:", statuses["stale"])
    print("Максимальный возраст сопоставленной погоды, мин:", maximum_matched_age)
    print("Сохранено парковочных отметок вне сетки:", report["parking_off_grid_rows"])
    print("Календарный часовой пояс:", config["calendar_timezone"])
    print("День недели: понедельник = 0, воскресенье = 6.")
    print("Витрина:", mart_path)
    print("Отчёт сборки:", report_path)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"Построение витрины остановлено: {error}")
        raise SystemExit(1)