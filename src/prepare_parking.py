import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from load_reference import load_parkings

ROOT = Path(__file__).resolve().parents[1]


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

    supported = {
        "schema_version": 1,
        "timezone": "UTC",
        "off_grid_policy": "keep_and_flag",
        "missing_policy": "leave_missing",
        "invalid_record_policy": "quarantine",
        "capacity_policy": "use_reference_and_flag",
    }

    for field, expected in supported.items():
        if config.get(field) != expected:
            raise ValueError(f"Неподдерживаемая настройка: {field}")

    period = config["parking_period_seconds"]
    if not isinstance(period, int) or isinstance(period, bool) or period <= 0:
        raise ValueError("Период должен быть положительным целым числом.")

    parkings = load_parkings()
    storage = json.loads(
        (ROOT / "data" / "storage" / "parking.json").read_text(
            encoding="utf-8"
        )
    )

    start = datetime.fromisoformat(
        config["start_date"]
    ).replace(tzinfo=timezone.utc)

    end = datetime.fromisoformat(
        storage["loaded_until_exclusive"]
    ).replace(tzinfo=timezone.utc)

    if end <= start:
        raise ValueError("Конец периода должен быть позже начала.")

    if not isinstance(storage["data"], list):
        raise ValueError("В хранилище отсутствует список наблюдений.")

    clean = {}
    rejected = []
    duplicates = 0

    for index, row in enumerate(storage["data"], start=1):
        try:
            code = str(row["scode"])
            reference = parkings[code]

            timestamp = datetime.fromisoformat(row["mvalidtime"])
            if timestamp.tzinfo is None:
                raise ValueError("У временной отметки не указан часовой пояс.")
            timestamp = timestamp.astimezone(timezone.utc)

            if not start <= timestamp < end:
                raise ValueError("Время находится вне выбранного периода.")

            if row["sorigin"] != config["parking_source"]:
                raise ValueError("Неожиданный поставщик данных.")

            if row["mperiod"] != period:
                raise ValueError("Неожиданный период измерения.")

            if row["mvalue"] is None or isinstance(row["mvalue"], bool):
                raise ValueError("Некорректное количество занятых мест.")

            occupied = float(row["mvalue"])
            capacity = reference["capacity"]

            if not math.isfinite(occupied) or not occupied.is_integer():
                raise ValueError("Количество мест должно быть целым числом.")

            if not 0 <= occupied <= capacity:
                raise ValueError("Количество мест вне диапазона вместимости.")

            occupied = int(occupied)
            on_grid = (timestamp - start).total_seconds() % period == 0

            prepared = {
                "station_code": code,
                "station_name": reference["station_name"],
                "timestamp_utc": timestamp.isoformat(),
                "timestamp_original": row["mvalidtime"],
                "source": row["sorigin"],
                "period_seconds": period,
                "occupied": occupied,
                "capacity": capacity,
                "occupancy_pct": round(occupied / capacity * 100, 6),
                "is_on_grid": str(on_grid).lower(),
                "capacity_reference_date": reference["capacity_reference_date"],
                "capacity_history_verified": str(
                    reference["capacity_history_verified"]
                ).lower(),
            }

        except (KeyError, ValueError, TypeError, OverflowError) as error:
            rejected.append({
                "input_row": index,
                "reason": f"{type(error).__name__}: {error}",
                "original_record": json.dumps(row, ensure_ascii=False),
            })
            continue

        key = (code, prepared["source"], period, timestamp)

        if key in clean:
            if clean[key]["occupied"] != occupied:
                raise RuntimeError(
                    f"Разные значения для одного наблюдения: {key}"
                )
            duplicates += 1
            continue

        clean[key] = prepared

    if not clean:
        raise ValueError("После проверки не осталось допустимых наблюдений.")

    rows = sorted(
        clean.values(),
        key=lambda row: (row["station_code"], row["timestamp_utc"]),
    )

    expected_times = {
        start + timedelta(seconds=period * i)
        for i in range(int((end - start).total_seconds() / period))
    }

    missing = {}
    for code in parkings:
        actual_times = {
            datetime.fromisoformat(row["timestamp_utc"])
            for row in rows
            if row["station_code"] == code
        }
        missing[code] = len(expected_times - actual_times)

    folder = ROOT / "data" / "processed"
    clean_path = folder / "parking_clean.csv"
    rejected_path = folder / "parking_rejected.csv"

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
        "missing_grid_points_by_station": missing,
        "capacity_history_unverified_records": sum(
            row["capacity_history_verified"] == "false" for row in rows
        ),
        "start_utc": start.isoformat(),
        "end_utc_exclusive": end.isoformat(),
    }

    report_path = ROOT / "logs" / "quality" / "parking_cleaning.json"
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
    print("Сохранено отметок вне сетки:", report["off_grid_records"])
    print("Пропущенных отметок по парковкам:", missing)
    print(
        "Записей с неподтверждённой исторической вместимостью:",
        report["capacity_history_unverified_records"],
    )
    print("Процент загрузки рассчитан.")
    print("Подготовленные данные:", clean_path)
    print("Отклонённые записи:", rejected_path)
    print("Отчёт проверки:", report_path)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"Обработка остановлена: {error}")
        raise SystemExit(1)