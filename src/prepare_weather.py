import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COLUMNS = {
    "temperature_c": "temperature_2m (°C)",
    "precipitation_mm": "precipitation (mm)",
    "cloud_cover_pct": "cloud_cover (%)",
    "wind_speed_kmh": "wind_speed_10m (km/h)",
}


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
    storage = json.loads(
        (ROOT / "data" / "storage" / "weather.json").read_text(
            encoding="utf-8"
        )
    )

    supported = {
        "schema_version": 1,
        "timezone": "UTC",
        "off_grid_policy": "keep_and_flag",
        "missing_policy": "leave_missing",
        "invalid_record_policy": "quarantine",
    }

    for field, expected in supported.items():
        if config.get(field) != expected:
            raise ValueError(f"Неподдерживаемая настройка: {field}")

    if storage["timezone"] != "UTC":
        raise ValueError("В хранилище погоды ожидается время UTC.")

    if not isinstance(storage["data"], list):
        raise ValueError("В хранилище отсутствует список наблюдений.")

    start = datetime.fromisoformat(
        config["start_date"]
    ).replace(tzinfo=timezone.utc)

    end = datetime.fromisoformat(
        storage["loaded_until_exclusive"]
    ).replace(tzinfo=timezone.utc)

    if end <= start:
        raise ValueError("Конец периода должен быть позже начала.")

    clean = {}
    rejected = []
    duplicates = 0

    for index, row in enumerate(storage["data"], start=1):
        try:
            timestamp = datetime.fromisoformat(row["time"])

            # В исходном CSV часовой пояс не записан в каждой строке.
            # Он задан для всего погодного хранилища как UTC.
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            timestamp = timestamp.astimezone(timezone.utc)

            if not start <= timestamp < end:
                raise ValueError("Время находится вне выбранного периода.")

            values = {}

            for target, original in COLUMNS.items():
                raw_value = row[original]

                if raw_value is None or isinstance(raw_value, bool):
                    raise ValueError(f"Некорректный показатель: {original}")

                value = float(raw_value)

                if not math.isfinite(value):
                    raise ValueError(f"Нечисловой показатель: {original}")

                values[target] = value

            if values["precipitation_mm"] < 0:
                raise ValueError("Отрицательное количество осадков.")

            if values["wind_speed_kmh"] < 0:
                raise ValueError("Отрицательная скорость ветра.")

            if not 0 <= values["cloud_cover_pct"] <= 100:
                raise ValueError("Облачность вне диапазона 0–100.")

            on_grid = (timestamp - start).total_seconds() % 3600 == 0

            prepared = {
                "timestamp_utc": timestamp.isoformat(),
                "timestamp_original": row["time"],
                **values,
                "is_on_grid": str(on_grid).lower(),
            }

        except (KeyError, ValueError, TypeError, OverflowError) as error:
            rejected.append({
                "input_row": index,
                "reason": f"{type(error).__name__}: {error}",
                "original_record": json.dumps(row, ensure_ascii=False),
            })
            continue

        if timestamp in clean:
            existing = clean[timestamp]

            if any(existing[field] != prepared[field] for field in COLUMNS):
                raise RuntimeError(
                    f"Разные погодные значения для времени {timestamp}"
                )

            duplicates += 1
            continue

        clean[timestamp] = prepared

    if not clean:
        raise ValueError("После проверки не осталось допустимых записей.")

    rows = [clean[timestamp] for timestamp in sorted(clean)]

    expected_times = {
        start + timedelta(hours=i)
        for i in range(int((end - start).total_seconds() / 3600))
    }

    missing = len(expected_times - set(clean))

    folder = ROOT / "data" / "processed"
    clean_path = folder / "weather_clean.csv"
    rejected_path = folder / "weather_rejected.csv"

    save_csv(clean_path, list(rows[0]), rows)
    save_csv(
        rejected_path,
        ["input_row", "reason", "original_record"],
        rejected,
    )

    report = {
        "input_records": len(storage["data"]),
        "clean_records": len(rows),
        "rejected_records": len(rejected),
        "removed_duplicates": duplicates,
        "off_grid_records": sum(
            row["is_on_grid"] == "false" for row in rows
        ),
        "missing_hours": missing,
        "requested_latitude": storage["latitude"],
        "requested_longitude": storage["longitude"],
        "start_utc": start.isoformat(),
        "end_utc_exclusive": end.isoformat(),
    }

    report_path = ROOT / "logs" / "quality" / "weather_cleaning.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(report_path)

    print("Прочитано записей:", report["input_records"])
    print("Сохранено допустимых:", report["clean_records"])
    print("Отклонено записей:", report["rejected_records"])
    print("Удалено дублей:", report["removed_duplicates"])
    print("Отметок вне часовой сетки:", report["off_grid_records"])
    print("Пропущенных часов:", missing)
    print("Подготовленные данные:", clean_path)
    print("Отклонённые записи:", rejected_path)
    print("Отчёт проверки:", report_path)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"Обработка остановлена: {error}")
        raise SystemExit(1)