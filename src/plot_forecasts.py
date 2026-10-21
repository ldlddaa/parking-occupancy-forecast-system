import csv
import math
from collections import defaultdict
from datetime import timedelta
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from prepare_forecast_targets import parse_utc
from train_models import (
    MODEL_ROOT, REPORT_DIR, ROOT,
    file_hash, read_json, save_json,
)


LABELS = {
    "persistence": "Простой прогноз",
    "linear_regression": "Линейная регрессия",
    "random_forest": "Случайный лес",
}


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    if not rows:
        raise ValueError(f"Пустая таблица: {path}")
    return rows


def with_gaps(rows, field, interval):
    times, values = [], []
    previous = None

    for row in rows:
        current = row["time"]
        if previous is not None and current - previous > interval:
            times.append(previous + interval)
            values.append(float("nan"))

        times.append(current)
        values.append(row[field])
        previous = current

    return times, values


def main():
    report_path = REPORT_DIR / "test_report.json"
    report_hash = file_hash(report_path)
    report = read_json(report_path)

    if report["status"] != "success":
        raise ValueError("Нет успешной итоговой проверки моделей.")

    manifest_path = MODEL_ROOT / "forecast_manifest.json"
    if file_hash(manifest_path) != report["model_manifest_sha256"]:
        raise ValueError(
            "Модели изменились. Сначала повторите итоговую проверку."
        )

    manifest = read_json(manifest_path)
    for name, digest in manifest["input_files_sha256"].items():
        if file_hash(ROOT / name) != digest:
            raise ValueError(
                f"Изменились входные данные или настройки: {name}"
            )

    paths = {}
    for name in ("metrics", "predictions"):
        info = report["artifacts"][name]
        paths[name] = ROOT / info["path"]
        if file_hash(paths[name]) != info["sha256"]:
            raise ValueError(
                f"Изменён файл результатов: {paths[name].name}"
            )

    processing = read_json(ROOT / "config" / "processing.json")
    interval = timedelta(
        seconds=processing["parking_period_seconds"]
    )
    zone = ZoneInfo(report["calendar_timezone"])

    groups = defaultdict(list)
    seen = set()

    for row in read_csv(paths["predictions"]):
        code = row["station_code"]
        station = report["stations"][code]
        selected = station["selected_model_by_validation"]

        if row["selected_model"] != selected:
            raise ValueError(
                "Выбранная модель не совпадает с отчётом."
            )

        timestamp = parse_utc(row["target_timestamp_utc"])
        if (code, timestamp) in seen:
            raise ValueError("Повтор временной отметки прогноза.")
        seen.add((code, timestamp))

        values = {
            field: float(row[field])
            for field in (
                "actual_occupancy_pct",
                "persistence_pct",
                "selected_prediction_pct",
            )
        }
        if not all(
            math.isfinite(value) and 0 <= value <= 100
            for value in values.values()
        ):
            raise ValueError("Некорректные значения для графика.")

        if not math.isclose(
            values["selected_prediction_pct"],
            float(row[selected + "_pct"]),
            rel_tol=0,
            abs_tol=1e-8,
        ):
            raise ValueError(
                "Прогноз выбранной модели не совпадает с её столбцом."
            )

        groups[code].append({"time": timestamp, **values})

    if set(groups) != set(report["stations"]):
        raise ValueError("Не совпадает состав парковок.")
    if sum(map(len, groups.values())) != report["test_rows"]:
        raise ValueError(
            "Не совпадает общее количество тестовых примеров."
        )

    metrics = {}
    for row in read_csv(paths["metrics"]):
        key = (row["station_code"], row["model"])
        if key in metrics:
            raise ValueError("Повтор строки метрик.")

        values = {
            name: float(row[name])
            for name in ("mae", "rmse")
        }
        if not all(
            math.isfinite(value) and value >= 0
            for value in values.values()
        ):
            raise ValueError("Некорректная метрика.")

        for name, value in values.items():
            expected = report["stations"][key[0]][
                "test_metrics"
            ][key[1]][name]
            if not math.isclose(
                value, expected, rel_tol=0, abs_tol=1e-8
            ):
                raise ValueError("Метрики не совпадают с отчётом.")

        metrics[key] = values

    codes = sorted(groups)
    expected_keys = {
        (code, method)
        for code in codes
        for method in LABELS
    }
    if set(metrics) != expected_keys:
        raise ValueError(
            "Не хватает метрик для сравнения методов."
        )

    output = REPORT_DIR / "figures"
    output.mkdir(parents=True, exist_ok=True)
    saved = []

    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
    })

    def save_figure(figure, filename):
        path = output / filename
        temporary = path.with_suffix(".tmp")
        figure.tight_layout()
        figure.savefig(
            temporary,
            format="png",
            dpi=200,
            bbox_inches="tight",
        )
        plt.close(figure)
        temporary.replace(path)
        saved.append({
            "path": str(path.relative_to(ROOT)),
            "sha256": file_hash(path),
        })

    # Факт, выбранная модель и простой прогноз для каждой парковки.
    for code in codes:
        rows = sorted(
            groups[code], key=lambda row: row["time"]
        )
        station = report["stations"][code]
        if len(rows) != station["test_rows"]:
            raise ValueError(
                f"Не совпало число прогнозов для парковки {code}."
            )

        selected = station["selected_model_by_validation"]
        figure, axis = plt.subplots(figsize=(12, 5))

        for field, label, color, style, width in (
            (
                "persistence_pct", "Простой прогноз",
                "#ea580c", "--", 0.9,
            ),
            (
                "selected_prediction_pct", LABELS[selected],
                "#2563eb", "-", 1.1,
            ),
            (
                "actual_occupancy_pct", "Фактическая загрузка",
                "#111827", "-", 1.0,
            ),
        ):
            times, values = with_gaps(rows, field, interval)
            axis.plot(
                times, values,
                label=label,
                color=color,
                linestyle=style,
                linewidth=width,
                alpha=0.9,
            )

        axis.set_title(
            f"{code} — {station['station_name']}\n"
            f"Прогноз на {report['forecast_horizon_minutes']} "
            "минут вперёд"
        )
        axis.set_ylabel("Загрузка, %")
        axis.set_xlabel(
            "Время, на которое сделан прогноз (Europe/Rome)"
        )
        axis.set_ylim(0, 105)
        axis.grid(alpha=0.2)
        axis.xaxis.set_major_locator(
            mdates.AutoDateLocator(
                tz=zone, minticks=5, maxticks=9
            )
        )
        axis.xaxis.set_major_formatter(
            mdates.DateFormatter(
                "%d.%m\n%H:%M", tz=zone
            )
        )
        axis.legend(
            loc="upper left", ncol=3, fontsize=9
        )
        save_figure(
            figure, f"test_forecast_{code}.png"
        )

    # Сравнение ошибок всех методов.
    figure, axes = plt.subplots(
        1, len(codes),
        figsize=(6 * len(codes), 5),
        squeeze=False,
    )
    methods = list(LABELS)

    for axis, code in zip(axes[0], codes):
        for offset, name, label, color in (
            (-0.19, "mae", "MAE", "#2563eb"),
            (0.19, "rmse", "RMSE", "#ea580c"),
        ):
            values = [
                metrics[(code, method)][name]
                for method in methods
            ]
            bars = axis.bar(
                [
                    index + offset
                    for index in range(len(methods))
                ],
                values,
                width=0.36,
                label=label,
                color=color,
            )
            axis.bar_label(
                bars, fmt="%.2f", padding=3, fontsize=9
            )

        selected = report["stations"][code][
            "selected_model_by_validation"
        ]
        labels = [
            LABELS[method].replace(" ", "\n", 1)
            + ("\n(выбран)" if method == selected else "")
            for method in methods
        ]
        axis.set_xticks(
            range(len(methods)), labels
        )
        axis.set_title(f"Парковка {code}")
        axis.set_ylabel("Ошибка, п.п.")

        maximum = max(
            value
            for method in methods
            for value in metrics[(code, method)].values()
        )
        axis.set_ylim(0, max(1, maximum * 1.2))
        axis.grid(axis="y", alpha=0.2)
        axis.set_axisbelow(True)
        axis.legend()

    figure.suptitle(
        "Сравнение ошибок на тестовой выборке"
    )
    save_figure(
        figure, "test_metrics_comparison.png"
    )

    if file_hash(report_path) != report_hash:
        raise RuntimeError(
            "Отчёт проверки изменился во время построения графиков."
        )

    save_json(REPORT_DIR / "plot_report.json", {
        "status": "success",
        "training_run_id": report["training_run_id"],
        "test_report_sha256": report_hash,
        "test_rows": report["test_rows"],
        "calendar_timezone": str(zone),
        "displayed_period": "entire_test_period",
        "figures": saved,
    })

    print("Графики построены по всему тестовому периоду.")
    print(
        "Количество тестовых примеров:",
        report["test_rows"],
    )
    print("Создано графиков:", len(saved))
    for item in saved:
        print(ROOT / item["path"])
    print(
        "Отчёт построения:",
        REPORT_DIR / "plot_report.json",
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"Построение графиков остановлено: {error}")
        raise SystemExit(1)