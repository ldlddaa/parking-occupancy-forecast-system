import csv
import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "marts" / "parking_weather.csv"
OUTPUT = ROOT / "data" / "modeling" / "forecast_targets.csv"
REPORT = ROOT / "logs" / "quality" / "forecast_targets.json"


def parse_utc(value):
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError(f"Не указан часовой пояс: {value}")
    return timestamp.astimezone(timezone.utc)


def on_grid(timestamp, period_seconds):
    return (
        timestamp.microsecond == 0
        and int(timestamp.timestamp()) % period_seconds == 0
    )


def save_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    temporary.replace(path)


def main():
    config = json.loads(
        (ROOT / "config" / "forecasting.json").read_text(encoding="utf-8")
    )
    processing = json.loads(
        (ROOT / "config" / "processing.json").read_text(encoding="utf-8")
    )

    expected_settings = {
        "schema_version": 1,
        "target_column": "occupancy_pct",
        "target_join": "exact_timestamp",
        "off_grid_policy": "exclude",
        "missing_target_policy": "drop",
    }
    for field, expected in expected_settings.items():
        if config[field] != expected:
            raise ValueError(f"Неподдерживаемая настройка: {field}")

    if config["weather_features"] is not False:
        raise ValueError("Этот вариант подготовки не использует погоду.")

    minutes = config["forecast_horizon_minutes"]
    if type(minutes) is not int or minutes <= 0:
        raise ValueError("Горизонт должен быть положительным целым числом.")

    period = processing["parking_period_seconds"]
    if type(period) is not int or period <= 0:
        raise ValueError("Некорректный период наблюдений.")

    horizon = timedelta(minutes=minutes)
    if horizon.total_seconds() % period:
        raise ValueError("Горизонт должен быть кратен периоду наблюдений.")

    boundaries = [
        parse_utc(config[field])
        for field in (
            "start_utc",
            "train_until_utc",
            "validation_until_utc",
            "test_until_utc",
        )
    ]

    if any(left >= right for left, right in zip(boundaries, boundaries[1:])):
        raise ValueError("Границы выборок должны строго возрастать.")

    if not all(on_grid(value, period) for value in boundaries):
        raise ValueError("Границы должны находиться на временной сетке.")

    split_names = ("train", "validation", "test")
    intervals = list(zip(split_names, boundaries, boundaries[1:]))

    required = {
        "station_code", "station_name", "timestamp_utc",
        "source", "period_seconds", "occupancy_pct",
        "is_on_grid", "capacity_history_verified",
    }

    observations = {}
    input_count = 0

    with INPUT.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Отсутствуют столбцы: {sorted(missing)}")

        for row in reader:
            input_count += 1
            code = row["station_code"]
            timestamp = parse_utc(row["timestamp_utc"])
            key = (code, timestamp)

            if key in observations:
                raise ValueError(f"Повтор наблюдения: {key}")

            if row["source"] != processing["parking_source"]:
                raise ValueError(f"Неожиданный источник: {key}")
            if int(row["period_seconds"]) != period:
                raise ValueError(f"Неожиданный период: {key}")

            value = float(row["occupancy_pct"])
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError(f"Некорректная загрузка: {key}")

            for field in ("is_on_grid", "capacity_history_verified"):
                if row[field] not in {"true", "false"}:
                    raise ValueError(f"Некорректный признак {field}: {key}")

            actual_grid = on_grid(timestamp, period)
            if actual_grid != (row["is_on_grid"] == "true"):
                raise ValueError(f"Неверный признак временной сетки: {key}")

            observations[key] = {
                "name": row["station_name"],
                "value": value,
                "on_grid": actual_grid,
                "capacity_verified":
                    row["capacity_history_verified"] == "true",
            }

    if not observations:
        raise ValueError("Витрина не содержит наблюдений.")

    result = []
    skipped = Counter()
    counts = Counter()
    station_codes = set()

    for (code, timestamp), current in sorted(observations.items()):
        if not boundaries[0] <= timestamp < boundaries[-1]:
            skipped["outside_period"] += 1
            continue

        station_codes.add(code)

        if not current["on_grid"]:
            skipped["off_grid"] += 1
            continue

        split, split_start, split_end = next(
            interval
            for interval in intervals
            if interval[1] <= timestamp < interval[2]
        )
        target_time = timestamp + horizon

        # Цель должна принадлежать той же выборке, что и момент прогноза.
        if target_time >= split_end:
            skipped["target_outside_split"] += 1
            continue

        target = observations.get((code, target_time))
        if target is None:
            skipped["missing_target"] += 1
            continue

        if not target["on_grid"]:
            raise ValueError("Целевая отметка неожиданно оказалась вне сетки.")

        if current["name"] != target["name"]:
            raise ValueError(f"Изменилось название парковки {code}.")

        result.append({
            "station_code": code,
            "station_name": current["name"],
            "timestamp_utc": timestamp.isoformat(),
            "target_timestamp_utc": target_time.isoformat(),
            "occupancy_now_pct": current["value"],
            "target_occupancy_pct": target["value"],
            "split": split,
            "capacity_history_verified": str(
                current["capacity_verified"] and target["capacity_verified"]
            ).lower(),
        })
        counts[(split, code)] += 1

    if not result:
        raise ValueError("Не удалось сформировать ни одного примера.")

    for split in split_names:
        for code in station_codes:
            if counts[(split, code)] == 0:
                raise ValueError(f"Пустая выборка {split} для парковки {code}.")

    if len(result) + sum(skipped.values()) != input_count:
        raise RuntimeError("Не сходится баланс входных и выходных записей.")

    save_csv(OUTPUT, result)

    report = {
        "source_file": str(INPUT.relative_to(ROOT)),
        "output_file": str(OUTPUT.relative_to(ROOT)),
        "settings": config,
        "input_rows": input_count,
        "output_rows": len(result),
        "excluded_rows": dict(skipped),
        "split_counts": {
            split: {
                code: counts[(split, code)]
                for code in sorted(station_codes)
            }
            for split in split_names
        },
        "capacity_unverified_rows": sum(
            row["capacity_history_verified"] == "false"
            for row in result
        ),
        "target_matching": "same_station_at_exact_horizon",
        "boundary_rule": "issue_and_target_in_same_split",
    }

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(REPORT)

    print("Строк в исходной витрине:", input_count)
    print("Горизонт прогноза, мин:", minutes)
    print("Сформировано примеров:", len(result))
    print("Исключено вне заданного периода:", skipped["outside_period"])
    print("Исключено отметок вне сетки:", skipped["off_grid"])
    print(
        "Исключено из-за выхода цели за границу выборки:",
        skipped["target_outside_split"],
    )
    print("Исключено из-за отсутствия цели:", skipped["missing_target"])

    labels = {
        "train": "Обучение",
        "validation": "Проверка настроек",
        "test": "Итоговое тестирование",
    }
    for split in split_names:
        print(f"\n{labels[split]}:")
        for code in sorted(station_codes):
            print(f"  Парковка {code}: {counts[(split, code)]}")

    print("\nВсе цели найдены по точному времени.")
    print("Момент прогноза и цель находятся в одной выборке.")
    print("Таблица примеров:", OUTPUT)
    print("Отчёт подготовки:", REPORT)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"Подготовка остановлена: {error}")
        raise SystemExit(1)