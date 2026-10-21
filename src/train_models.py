import csv
import hashlib
import json
import platform
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from uuid import uuid4

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, root_mean_squared_error
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from prepare_forecast_targets import parse_utc, save_csv


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "modeling" / "forecast_features.csv"
FEATURE_REPORT = ROOT / "logs" / "quality" / "forecast_features.json"
MODEL_ROOT = ROOT / "models"
REPORT_DIR = ROOT / "reports" / "models"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(actual, raw_predictions, lower=0, upper=100):
    raw = np.asarray(raw_predictions, dtype=float)
    if raw.shape != actual.shape or not np.isfinite(raw).all():
        raise ValueError("Модель вернула некорректные прогнозы.")

    predictions = np.clip(raw, lower, upper)
    return {
        "mae": float(mean_absolute_error(actual, predictions)),
        "rmse": float(root_mean_squared_error(actual, predictions)),
        "clipped_predictions": int(np.count_nonzero(raw != predictions)),
    }


def main():
    started = datetime.now(timezone.utc).isoformat()
    config = read_json(ROOT / "config" / "models.json")
    forecasting = read_json(ROOT / "config" / "forecasting.json")
    features_config = read_json(ROOT / "config" / "features.json")
    feature_report = read_json(FEATURE_REPORT)

    if config["schema_version"] != 1 or config["selection_metric"] != "mae":
        raise ValueError("Неподдерживаемые настройки моделей.")

    lower = config["prediction_min_pct"]
    upper = config["prediction_max_pct"]
    if (lower, upper) != (0, 100):
        raise ValueError("Ожидается диапазон прогнозов от 0 до 100%.")

    if feature_report.get("passed") is not True:
        raise ValueError("Подготовка признаков не подтверждена отчётом.")
    if feature_report["settings"] != features_config:
        raise ValueError(
            "Настройки признаков изменились. Пересоберите признаки."
        )
    if feature_report["forecasting_settings"] != forecasting:
        raise ValueError(
            "Настройки прогноза изменились. Пересоберите данные."
        )

    columns = (
        ["occupancy_now_pct"]
        + [
            f"occupancy_lag_{value}m"
            for value in features_config["lag_minutes"]
        ]
        + [
            f"occupancy_mean_{value}m"
            for value in features_config["rolling_window_minutes"]
        ]
        + [
            "target_hour_sin", "target_hour_cos",
            "target_weekday_sin", "target_weekday_cos",
            "target_is_weekend",
        ]
    )
    if columns != feature_report["feature_columns"]:
        raise ValueError("Список признаков не соответствует настройкам.")
    if len(set(columns)) != len(columns):
        raise ValueError("В списке признаков есть повторы.")

    forest_config = config["random_forest"]
    for field in ("max_depth_candidates", "min_samples_leaf_candidates"):
        values = forest_config[field]
        if (
            not isinstance(values, list)
            or not values
            or any(type(value) is not int or value <= 0 for value in values)
        ):
            raise ValueError(f"Некорректные кандидаты: {field}")

    if (
        type(forest_config["n_estimators"]) is not int
        or forest_config["n_estimators"] <= 0
    ):
        raise ValueError("Некорректное количество деревьев.")
    if (
        type(forest_config["n_jobs"]) is not int
        or forest_config["n_jobs"] == 0
    ):
        raise ValueError("Некорректное количество рабочих процессов.")
    if (
        type(config["random_state"]) is not int
        or not 0 <= config["random_state"] < 2**32
    ):
        raise ValueError("Некорректное начальное состояние генератора.")
    if type(config["linear_regression"]["fit_intercept"]) is not bool:
        raise ValueError("fit_intercept должен быть логическим значением.")

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

    intervals = dict(zip(
        ("train", "validation", "test"),
        zip(boundaries, boundaries[1:]),
    ))
    horizon = timedelta(
        minutes=forecasting["forecast_horizon_minutes"]
    )
    groups = defaultdict(lambda: defaultdict(list))
    counts = Counter()
    seen = set()
    names = {}

    base_columns = {
        "station_code", "station_name",
        "timestamp_utc", "target_timestamp_utc",
        "occupancy_now_pct", "target_occupancy_pct",
        "split", "capacity_history_verified",
    }

    with DATA.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        header = reader.fieldnames or []
        if (
            len(header) != len(set(header))
            or set(header) != base_columns | set(columns)
        ):
            raise ValueError("Изменилась структура таблицы признаков.")

        for row in reader:
            split = row["split"]
            code = row["station_code"]

            if split not in intervals:
                raise ValueError(f"Неизвестная выборка: {split}")
            counts[(split, code)] += 1

            # Тестовые значения не участвуют в обучении и выборе настроек.
            if split == "test":
                continue

            timestamp = parse_utc(row["timestamp_utc"])
            target_time = parse_utc(row["target_timestamp_utc"])
            start, end = intervals[split]

            if (
                not start <= timestamp < target_time < end
                or target_time - timestamp != horizon
            ):
                raise ValueError(
                    "Нарушены границы выборки или горизонт прогноза."
                )

            key = (code, timestamp)
            if key in seen:
                raise ValueError(f"Повтор примера: {key}")
            seen.add(key)

            if code in names and names[code] != row["station_name"]:
                raise ValueError(f"Разные названия парковки {code}.")
            names[code] = row["station_name"]

            values = [float(row[field]) for field in columns]
            target = float(row["target_occupancy_pct"])
            if (
                not np.isfinite(values).all()
                or not np.isfinite(target)
                or not 0 <= target <= 100
            ):
                raise ValueError(f"Некорректные значения: {key}")

            groups[code][split].append((timestamp, values, target))

    codes = sorted({code for _, code in counts})
    if not codes or sum(counts.values()) != feature_report["output_rows"]:
        raise ValueError(
            "Количество строк не соответствует отчёту признаков."
        )

    for code in codes:
        for split in intervals:
            expected = feature_report["split_counts"][split][code]["after"]
            if counts[(split, code)] != expected or expected == 0:
                raise ValueError(
                    f"Не совпадает размер выборки {split}, парковка {code}."
                )

    source_paths = [DATA, FEATURE_REPORT] + [
        ROOT / "config" / name
        for name in ("models.json", "forecasting.json", "features.json")
    ]
    source_hashes = {
        str(path.relative_to(ROOT)): file_hash(path)
        for path in source_paths
    }

    run_id = uuid4().hex
    run_dir = MODEL_ROOT / run_id
    run_dir.mkdir(parents=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    candidates = []
    summary = []
    stations = {}

    def metric_row(code, model_name, candidate, parameters, metrics, size):
        return {
            "station_code": code,
            "model": model_name,
            "candidate": candidate,
            "parameters": json.dumps(
                parameters, ensure_ascii=False, sort_keys=True
            ),
            "validation_rows": size,
            **metrics,
        }

    for code in codes:
        def arrays(split):
            rows = sorted(
                groups[code][split], key=lambda item: item[0]
            )
            return (
                np.asarray([row[1] for row in rows], dtype=float),
                np.asarray([row[2] for row in rows], dtype=float),
            )

        x_train, y_train = arrays("train")
        x_valid, y_valid = arrays("validation")
        print(
            f"\nПарковка {code}: обучение — {len(y_train)}, "
            f"валидация — {len(y_valid)}",
            flush=True,
        )

        persistence = evaluate(
            y_valid,
            x_valid[:, columns.index("occupancy_now_pct")],
            lower,
            upper,
        )
        baseline_row = metric_row(
            code, "persistence", "persistence",
            {}, persistence, len(y_valid),
        )
        candidates.append(baseline_row)
        summary.append(baseline_row)

        # Масштабирование обучается вместе с регрессией только на train.
        linear_parameters = config["linear_regression"]
        linear = make_pipeline(
            StandardScaler(),
            LinearRegression(**linear_parameters),
        )
        linear.fit(x_train, y_train)
        linear_score = evaluate(
            y_valid, linear.predict(x_valid), lower, upper
        )
        linear_row = metric_row(
            code, "linear_regression", "linear_regression",
            linear_parameters, linear_score, len(y_valid),
        )
        candidates.append(linear_row)
        summary.append(linear_row)
        print(
            f"  Линейная регрессия: "
            f"MAE = {linear_score['mae']:.4f} п.п.",
            flush=True,
        )

        best_forest = None
        best_row = None
        best_parameters = None
        combinations = list(product(
            forest_config["max_depth_candidates"],
            forest_config["min_samples_leaf_candidates"],
        ))

        for index, (depth, leaf) in enumerate(combinations, start=1):
            parameters = {
                "n_estimators": forest_config["n_estimators"],
                "max_depth": depth,
                "min_samples_leaf": leaf,
                "random_state": config["random_state"],
                "n_jobs": forest_config["n_jobs"],
            }
            print(
                f"  Случайный лес {index}/{len(combinations)}: "
                f"глубина {depth}, лист {leaf}...",
                flush=True,
            )

            forest = RandomForestRegressor(**parameters)
            forest.fit(x_train, y_train)
            score = evaluate(
                y_valid, forest.predict(x_valid), lower, upper
            )
            row = metric_row(
                code, "random_forest", f"random_forest_{index}",
                parameters, score, len(y_valid),
            )
            candidates.append(row)
            print(
                f"    MAE = {score['mae']:.4f} п.п.",
                flush=True,
            )

            if best_row is None or score["mae"] < best_row["mae"]:
                best_forest = forest
                best_row = row
                best_parameters = parameters

        summary.append(best_row)

        model_files = {}
        for model_name, estimator in (
            ("linear_regression", linear),
            ("random_forest", best_forest),
        ):
            path = run_dir / f"{code}_{model_name}.joblib"
            temporary = path.with_suffix(".tmp")
            joblib.dump(
                {
                    "estimator": estimator,
                    "station_code": code,
                    "model_name": model_name,
                    "feature_columns": columns,
                    "run_id": run_id,
                },
                temporary,
                compress=3,
            )
            temporary.replace(path)
            model_files[model_name] = {
                "path": str(path.relative_to(ROOT)),
                "sha256": file_hash(path),
            }

        scores = {
            "persistence": persistence,
            "linear_regression": linear_score,
            "random_forest": {
                field: best_row[field]
                for field in persistence
            },
        }
        preferred = min(
            scores, key=lambda name: scores[name]["mae"]
        )
        stations[code] = {
            "station_name": names[code],
            "train_rows": len(y_train),
            "validation_rows": len(y_valid),
            "validation_metrics": scores,
            "preferred_model_by_validation": preferred,
            "selected_random_forest_parameters": best_parameters,
            "model_files": model_files,
        }
        print(
            f"  Простой прогноз: "
            f"MAE = {persistence['mae']:.4f} п.п."
        )
        print(
            f"  Лучший вариант по валидации: {preferred}",
            flush=True,
        )

    if any(
        file_hash(ROOT / name) != digest
        for name, digest in source_hashes.items()
    ):
        raise RuntimeError(
            "Входные файлы изменились во время обучения. Повторите запуск."
        )

    manifest = {
        "schema_version": 1,
        "status": "success",
        "run_id": run_id,
        "started_at_utc": started,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "versions": {
            "python": platform.python_version(),
            "scikit_learn": sklearn.__version__,
            "numpy": np.__version__,
            "joblib": joblib.__version__,
        },
        "feature_columns": columns,
        "model_settings": config,
        "forecasting_settings": forecasting,
        "features_settings": features_config,
        "input_files_sha256": source_hashes,
        "fit_split": "train",
        "selection_split": "validation",
        "test_used_for_fit_or_selection": False,
        "refit_on_validation": False,
        "tie_break": (
            "first_candidate_in_config_order; "
            "persistence_then_linear_then_forest"
        ),
        "stations": stations,
    }

    save_csv(REPORT_DIR / "validation_candidates.csv", candidates)
    save_csv(REPORT_DIR / "validation_metrics.csv", summary)
    save_json(run_dir / "manifest.json", manifest)
    save_json(MODEL_ROOT / "forecast_manifest.json", manifest)

    print("\nОбучение завершено. Сохранены четыре обученные модели.")
    print(
        "Метрики валидации:",
        REPORT_DIR / "validation_metrics.csv",
    )
    print(
        "Все проверенные настройки:",
        REPORT_DIR / "validation_candidates.csv",
    )
    print(
        "Описание сохранённых моделей:",
        MODEL_ROOT / "forecast_manifest.json",
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"Обучение остановлено: {error}")
        raise SystemExit(1)