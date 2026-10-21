import csv
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import sklearn

from prepare_forecast_targets import parse_utc, save_csv
from train_models import (
    DATA, FEATURE_REPORT, MODEL_ROOT, REPORT_DIR, ROOT,
    evaluate, file_hash, read_json, save_json,
)


def main():
    manifest_path = MODEL_ROOT / "forecast_manifest.json"
    manifest_hash = file_hash(manifest_path)
    manifest = read_json(manifest_path)

    if manifest["schema_version"] != 1 or manifest["status"] != "success":
        raise ValueError("Нет успешно завершённого обучения.")
    if (
        manifest["fit_split"] != "train"
        or manifest["selection_split"] != "validation"
    ):
        raise ValueError("Нарушен порядок обучения и выбора моделей.")
    if manifest["test_used_for_fit_or_selection"] is not False:
        raise ValueError(
            "Тестовая выборка уже использована при выборе моделей."
        )
    if manifest["versions"]["scikit_learn"] != sklearn.__version__:
        raise ValueError(
            "Версия scikit-learn отличается от версии при обучении."
        )

    for name, digest in manifest["input_files_sha256"].items():
        if file_hash(ROOT / name) != digest:
            raise ValueError(f"Файл изменился после обучения: {name}")

    columns = manifest["feature_columns"]
    feature_report = read_json(FEATURE_REPORT)
    if columns != feature_report["feature_columns"]:
        raise ValueError("Изменился порядок признаков.")
    if "target_occupancy_pct" in columns:
        raise ValueError("Целевое значение включено в признаки.")

    forecast = manifest["forecasting_settings"]
    start = parse_utc(forecast["validation_until_utc"])
    end = parse_utc(forecast["test_until_utc"])
    horizon = timedelta(minutes=forecast["forecast_horizon_minutes"])
    zone = ZoneInfo(manifest["features_settings"]["calendar_timezone"])
    lower = manifest["model_settings"]["prediction_min_pct"]
    upper = manifest["model_settings"]["prediction_max_pct"]
    methods = ("persistence", "linear_regression", "random_forest")

    groups = defaultdict(list)
    seen = set()

    with DATA.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        required = {
            "station_code", "station_name",
            "timestamp_utc", "target_timestamp_utc",
            "occupancy_now_pct", "target_occupancy_pct",
            "split", "capacity_history_verified", *columns,
        }
        header = reader.fieldnames or []
        if len(header) != len(set(header)) or set(header) != required:
            raise ValueError("Изменилась структура таблицы признаков.")

        for row in reader:
            if row["split"] not in {"train", "validation", "test"}:
                raise ValueError("Неизвестная выборка.")
            if row["split"] != "test":
                continue

            code = row["station_code"]
            if code not in manifest["stations"]:
                raise ValueError(
                    f"Для парковки {code} нет сохранённых моделей."
                )

            timestamp = parse_utc(row["timestamp_utc"])
            target_time = parse_utc(row["target_timestamp_utc"])
            if (
                not start <= timestamp < target_time < end
                or target_time - timestamp != horizon
            ):
                raise ValueError(
                    "Неверная тестовая граница или горизонт прогноза."
                )

            key = (code, timestamp)
            if key in seen:
                raise ValueError(f"Повтор тестового примера: {key}")
            seen.add(key)

            if (
                row["station_name"]
                != manifest["stations"][code]["station_name"]
            ):
                raise ValueError(f"Изменилось название парковки {code}.")
            if row["capacity_history_verified"] not in {"true", "false"}:
                raise ValueError(
                    "Некорректный признак исторической вместимости."
                )

            values = [float(row[field]) for field in columns]
            actual = float(row["target_occupancy_pct"])
            if (
                not np.isfinite(values).all()
                or not np.isfinite(actual)
                or not 0 <= actual <= 100
            ):
                raise ValueError("Некорректные тестовые значения.")

            groups[code].append({
                "timestamp": timestamp,
                "target_time": target_time,
                "features": values,
                "actual": actual,
                "capacity_history_verified":
                    row["capacity_history_verified"],
            })

    if set(groups) != set(manifest["stations"]):
        raise ValueError(
            "Состав парковок в тесте не соответствует обучению."
        )

    metric_rows = []
    prediction_rows = []
    stations = {}

    for code in sorted(groups):
        rows = sorted(groups[code], key=lambda row: row["timestamp"])
        expected = feature_report["split_counts"]["test"][code]["after"]
        if not rows or len(rows) != expected:
            raise ValueError(
                f"Не совпадает размер тестовой выборки для {code}."
            )

        station = manifest["stations"][code]
        selected = station["preferred_model_by_validation"]
        if selected not in methods:
            raise ValueError("Неизвестная выбранная модель.")

        x_test = np.asarray(
            [row["features"] for row in rows], dtype=float
        )
        y_test = np.asarray(
            [row["actual"] for row in rows], dtype=float
        )
        raw_predictions = {
            "persistence":
                x_test[:, columns.index("occupancy_now_pct")]
        }

        for model_name in ("linear_regression", "random_forest"):
            info = station["model_files"][model_name]
            path = ROOT / info["path"]
            if file_hash(path) != info["sha256"]:
                raise ValueError(
                    f"Изменён файл модели: {path.name}"
                )

            saved = joblib.load(path)
            if (
                saved["station_code"] != code
                or saved["model_name"] != model_name
                or saved["run_id"] != manifest["run_id"]
                or saved["feature_columns"] != columns
            ):
                raise ValueError(
                    "Сохранённая модель не соответствует описанию."
                )

            estimator = saved["estimator"]
            if estimator.n_features_in_ != len(columns):
                raise ValueError("Не совпадает число признаков модели.")

            raw_predictions[model_name] = estimator.predict(x_test)

        scores = {
            name: evaluate(
                y_test, raw_predictions[name], lower, upper
            )
            for name in methods
        }
        predictions = {
            name: np.clip(raw_predictions[name], lower, upper)
            for name in methods
        }
        baseline_mae = scores["persistence"]["mae"]
        improvements = {
            name: (
                100 * (baseline_mae - scores[name]["mae"]) / baseline_mae
                if baseline_mae > 0 else None
            )
            for name in methods
        }

        print(
            f"\nПарковка {code}: тестовых примеров — {len(rows)}"
        )
        for name in methods:
            score = scores[name]
            metric_rows.append({
                "station_code": code,
                "station_name": station["station_name"],
                "model": name,
                "test_rows": len(rows),
                **score,
                "selected_by_validation":
                    str(name == selected).lower(),
                "mae_improvement_vs_persistence_pct":
                    improvements[name],
            })
            print(
                f"  {name}: MAE = {score['mae']:.4f}; "
                f"RMSE = {score['rmse']:.4f} п.п."
            )

        print(f"  Выбрано заранее по валидации: {selected}")
        improvement = improvements[selected]
        if improvement is not None:
            print(
                "  Снижение MAE выбранной модели относительно "
                f"простого прогноза: {improvement:.2f}%"
            )

        for index, row in enumerate(rows):
            prediction_rows.append({
                "station_code": code,
                "station_name": station["station_name"],
                "timestamp_utc": row["timestamp"].isoformat(),
                "target_timestamp_utc":
                    row["target_time"].isoformat(),
                "target_timestamp_local":
                    row["target_time"].astimezone(zone).isoformat(),
                "actual_occupancy_pct": row["actual"],
                "persistence_pct":
                    float(predictions["persistence"][index]),
                "linear_regression_pct":
                    float(predictions["linear_regression"][index]),
                "random_forest_pct":
                    float(predictions["random_forest"][index]),
                "selected_model": selected,
                "selected_prediction_pct":
                    float(predictions[selected][index]),
                "capacity_history_verified":
                    row["capacity_history_verified"],
            })

        stations[code] = {
            "station_name": station["station_name"],
            "test_rows": len(rows),
            "selected_model_by_validation": selected,
            "test_metrics": scores,
            "selected_model_mae_improvement_pct": improvement,
        }

    if file_hash(manifest_path) != manifest_hash:
        raise RuntimeError(
            "Описание моделей изменилось во время проверки."
        )
    for name, digest in manifest["input_files_sha256"].items():
        if file_hash(ROOT / name) != digest:
            raise RuntimeError(
                f"Входной файл изменился во время проверки: {name}"
            )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    metrics_path = REPORT_DIR / "test_metrics.csv"
    predictions_path = REPORT_DIR / "test_predictions.csv"
    save_csv(metrics_path, metric_rows)
    save_csv(predictions_path, prediction_rows)

    report = {
        "status": "success",
        "training_run_id": manifest["run_id"],
        "evaluated_at_utc":
            datetime.now(timezone.utc).isoformat(),
        "model_manifest_sha256": manifest_hash,
        "test_start_utc": start.isoformat(),
        "test_end_utc_exclusive": end.isoformat(),
        "forecast_horizon_minutes":
            forecast["forecast_horizon_minutes"],
        "calendar_timezone": str(zone),
        "test_rows": len(prediction_rows),
        "selection_source": "validation",
        "models_refitted": False,
        "evaluation_mode":
            "sequential_one_hour_ahead_with_observed_history",
        "error_units": "percentage_points",
        "stations": stations,
        "artifacts": {
            "metrics": {
                "path": str(metrics_path.relative_to(ROOT)),
                "sha256": file_hash(metrics_path),
            },
            "predictions": {
                "path": str(predictions_path.relative_to(ROOT)),
                "sha256": file_hash(predictions_path),
            },
        },
    }
    save_json(REPORT_DIR / "test_report.json", report)

    print("\nИтоговая проверка завершена.")
    print("Метрики:", metrics_path)
    print("Все тестовые прогнозы:", predictions_path)
    print("Отчёт проверки:", REPORT_DIR / "test_report.json")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"Итоговая проверка остановлена: {error}")
        raise SystemExit(1)