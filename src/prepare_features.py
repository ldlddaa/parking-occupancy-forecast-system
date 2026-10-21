import csv
import json
import math
from collections import Counter
from datetime import timedelta
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

from prepare_forecast_targets import on_grid, parse_utc, save_csv


ROOT = Path(__file__).resolve().parents[1]
MART = ROOT / "data" / "marts" / "parking_weather.csv"
TARGETS = ROOT / "data" / "modeling" / "forecast_targets.csv"
OUTPUT = ROOT / "data" / "modeling" / "forecast_features.csv"
REPORT = ROOT / "logs" / "quality" / "forecast_features.json"


def positive_minutes(values, field, period):
    if not isinstance(values, list) or not values:
        raise ValueError(f"Пустой или некорректный список: {field}")
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError(f"В {field} нужны положительные целые числа.")
    if len(set(values)) != len(values):
        raise ValueError(f"Повтор значений в {field}.")
    if any(value * 60 % period for value in values):
        raise ValueError(f"Значения {field} должны быть кратны шагу сетки.")


def build_features(code, timestamp, target_time, history, config, period, zone):
    features = {"occupancy_now_pct": history[(code, timestamp)]}

    for minutes in config["lag_minutes"]:
        past_time = timestamp - timedelta(minutes=minutes)
        value = history.get((code, past_time))
        if value is None:
            return None, "missing_lag"
        features[f"occupancy_lag_{minutes}m"] = value

    # Окно содержит отметки строго после левой границы,
    # до момента прогноза включительно.
    for minutes in config["rolling_window_minutes"]:
        values = []
        for step in range(minutes * 60 // period):
            past_time = timestamp - timedelta(seconds=step * period)
            value = history.get((code, past_time))
            if value is None:
                return None, "incomplete_window"
            values.append(value)
        features[f"occupancy_mean_{minutes}m"] = mean(values)

    local = target_time.astimezone(zone)
    hour = local.hour + local.minute / 60 + local.second / 3600
    features.update({
        "target_hour_sin": math.sin(2 * math.pi * hour / 24),
        "target_hour_cos": math.cos(2 * math.pi * hour / 24),
        "target_weekday_sin": math.sin(2 * math.pi * local.weekday() / 7),
        "target_weekday_cos": math.cos(2 * math.pi * local.weekday() / 7),
        "target_is_weekend": int(local.weekday() >= 5),
    })
    return features, None


def main():
    def read_config(name):
        return json.loads(
            (ROOT / "config" / name).read_text(encoding="utf-8")
        )

    config = read_config("features.json")
    forecasting = read_config("forecasting.json")
    processing = read_config("processing.json")

    period = processing["parking_period_seconds"]
    if type(period) is not int or period <= 0:
        raise ValueError("Некорректный шаг временной сетки.")

    expected = {
        "schema_version": 1,
        "rolling_include_current": True,
        "require_complete_windows": True,
        "missing_feature_policy": "drop",
        "calendar_timestamp": "target",
    }
    for field, value in expected.items():
        if config[field] != value:
            raise ValueError(f"Неподдерживаемая настройка: {field}")

    if config["calendar_timezone"] != processing["calendar_timezone"]:
        raise ValueError("Часовые пояса обработки и признаков различаются.")
    if forecasting["weather_features"] is not False:
        raise ValueError("Погодные признаки в этом варианте отключены.")

    positive_minutes(config["lag_minutes"], "lag_minutes", period)
    positive_minutes(
        config["rolling_window_minutes"], "rolling_window_minutes", period
    )
    zone = ZoneInfo(config["calendar_timezone"])

    minutes = forecasting["forecast_horizon_minutes"]
    if type(minutes) is not int or minutes <= 0 or minutes * 60 % period:
        raise ValueError("Некорректный горизонт прогноза.")
    horizon = timedelta(minutes=minutes)

    boundaries = [
        parse_utc(forecasting[field])
        for field in (
            "start_utc", "train_until_utc",
            "validation_until_utc", "test_until_utc",
        )
    ]
    if any(
        left >= right
        for left, right in zip(boundaries, boundaries[1:])
    ):
        raise ValueError("Нарушен порядок границ выборок.")

    splits = ("train", "validation", "test")
    intervals = dict(zip(splits, zip(boundaries, boundaries[1:])))

    # История фактических наблюдений на исходной сетке.
    history = {}
    seen = set()

    with MART.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        required = {
            "station_code", "timestamp_utc",
            "occupancy_pct", "is_on_grid",
        }
        if required - set(reader.fieldnames or []):
            raise ValueError("В витрине отсутствуют необходимые столбцы.")

        for row in reader:
            timestamp = parse_utc(row["timestamp_utc"])
            key = (row["station_code"], timestamp)

            if key in seen:
                raise ValueError(f"Повтор наблюдения: {key}")
            seen.add(key)

            if row["is_on_grid"] not in {"true", "false"}:
                raise ValueError("Некорректный признак временной сетки.")

            valid_grid = on_grid(timestamp, period)
            if valid_grid != (row["is_on_grid"] == "true"):
                raise ValueError(
                    "Признак временной сетки не соответствует времени."
                )

            value = float(row["occupancy_pct"])
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError(f"Некорректная загрузка: {key}")

            if valid_grid:
                history[key] = value

    results = []
    rejected = Counter()
    before = Counter()
    after = Counter()
    target_keys = set()
    feature_columns = None

    with TARGETS.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        required = {
            "station_code", "station_name",
            "timestamp_utc", "target_timestamp_utc",
            "occupancy_now_pct", "target_occupancy_pct",
            "split", "capacity_history_verified",
        }
        if set(reader.fieldnames or []) != required:
            raise ValueError("Изменилась структура таблицы целевых примеров.")

        for row in reader:
            code = row["station_code"]
            timestamp = parse_utc(row["timestamp_utc"])
            target_time = parse_utc(row["target_timestamp_utc"])
            key = (code, timestamp)
            split = row["split"]

            if key in target_keys:
                raise ValueError(f"Повтор целевого примера: {key}")
            target_keys.add(key)

            if split not in intervals:
                raise ValueError(f"Неизвестная выборка: {split}")

            start, end = intervals[split]
            if not start <= timestamp < target_time < end:
                raise ValueError(f"Нарушена граница выборки: {key}")
            if target_time - timestamp != horizon:
                raise ValueError(f"Неверный горизонт: {key}")

            # Проверяем согласованность таблицы целей с витриной.
            for moment, field in (
                (timestamp, "occupancy_now_pct"),
                (target_time, "target_occupancy_pct"),
            ):
                value = float(row[field])
                original = history.get((code, moment))

                if original is None or not math.isfinite(value):
                    raise ValueError(
                        f"Отсутствует допустимое наблюдение: {key}"
                    )
                if not math.isclose(
                    value, original, rel_tol=0, abs_tol=1e-6
                ):
                    raise ValueError(
                        "Таблица целей устарела относительно витрины."
                    )

            before[(split, code)] += 1

            features, reason = build_features(
                code, timestamp, target_time,
                history, config, period, zone,
            )
            if reason:
                rejected[reason] += 1
                continue

            if not all(
                math.isfinite(value) for value in features.values()
            ):
                raise ValueError("Обнаружен нечисловой признак.")

            feature_columns = list(features)
            results.append({**row, **features})
            after[(split, code)] += 1

    codes = sorted({code for _, code in before})
    if not results or any(
        after[(split, code)] == 0
        for split in splits
        for code in codes
    ):
        raise ValueError("После подготовки осталась пустая выборка.")

    if len(results) + sum(rejected.values()) != sum(before.values()):
        raise RuntimeError("Не сходится баланс примеров.")

    results.sort(
        key=lambda row: (row["station_code"], row["timestamp_utc"])
    )
    save_csv(OUTPUT, results)

    report = {
        "passed": True,
        "settings": config,
        "forecasting_settings": forecasting,
        "input_rows": sum(before.values()),
        "output_rows": len(results),
        "excluded_rows": dict(rejected),
        "exclusion_priority": ["missing_lag", "incomplete_window"],
        "feature_columns": feature_columns,
        "feature_count": len(feature_columns),
        "rolling_window_definition":
            "(issue_time - window, issue_time]",
        "evaluation_mode":
            "sequential_one_hour_ahead_with_observed_history",
        "split_counts": {
            split: {
                code: {
                    "before": before[(split, code)],
                    "after": after[(split, code)],
                }
                for code in codes
            }
            for split in splits
        },
    }

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            report, ensure_ascii=False, indent=2, allow_nan=False
        ),
        encoding="utf-8",
    )
    temporary.replace(REPORT)

    print("Примеров до формирования признаков:", report["input_rows"])
    print("Примеров после формирования признаков:", len(results))
    print("Исключено из-за отсутствия лагов:", rejected["missing_lag"])
    print(
        "Исключено из-за неполных окон:",
        rejected["incomplete_window"],
    )
    print("Количество признаков:", len(feature_columns))

    for split in splits:
        print(f"\nВыборка {split}:")
        for code in codes:
            print(
                f"  Парковка {code}: "
                f"{before[(split, code)]} → {after[(split, code)]}"
            )

    print(
        "\nИстория загрузки используется только "
        "до момента прогноза включительно."
    )
    print("Таблица признаков:", OUTPUT)
    print("Отчёт подготовки:", REPORT)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"Формирование признаков остановлено: {error}")
        raise SystemExit(1)