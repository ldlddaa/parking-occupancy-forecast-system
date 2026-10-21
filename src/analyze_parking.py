import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "marts" / "parking_weather.csv"
OUTPUT = ROOT / "reports" / "analysis"
FIGURES = OUTPUT / "figures"
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def save_csv(path, rows):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def average(values):
    return mean(values) if values else float("nan")


def save_figure(figure, name):
    figure.tight_layout()
    figure.savefig(FIGURES / name, dpi=200, bbox_inches="tight")
    plt.close(figure)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--until",
        type=date.fromisoformat,
        default=date(2026, 9, 21),
        help="Конец анализируемого периода, не включая; дата UTC.",
    )
    args = parser.parse_args()

    config = json.loads(
        (ROOT / "config" / "processing.json").read_text(encoding="utf-8")
    )
    start = datetime.combine(
        date.fromisoformat(config["start_date"]),
        time.min,
        tzinfo=timezone.utc,
    )
    end = datetime.combine(args.until, time.min, tzinfo=timezone.utc)
    local_zone = ZoneInfo(config["calendar_timezone"])
    interval = timedelta(seconds=int(config["parking_period_seconds"]))

    if end <= start:
        raise ValueError("Конец периода должен быть позже начала.")

    required = {
        "station_code", "station_name", "timestamp_utc",
        "occupancy_pct", "is_on_grid", "capacity_history_verified",
    }
    parking = defaultdict(list)
    names = {}
    seen = set()
    input_count = 0

    with INPUT.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Отсутствуют столбцы: {sorted(missing)}")

        for row in reader:
            input_count += 1
            timestamp = datetime.fromisoformat(row["timestamp_utc"])
            if timestamp.tzinfo is None:
                raise ValueError("У временной отметки отсутствует часовой пояс.")
            timestamp = timestamp.astimezone(timezone.utc)

            if not start <= timestamp < end:
                continue

            code = row["station_code"]
            key = (code, timestamp)
            if key in seen:
                raise ValueError(f"Повтор наблюдения: {key}")
            seen.add(key)

            value = float(row["occupancy_pct"])
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError(f"Некорректная загрузка: {key}")

            for field in ("is_on_grid", "capacity_history_verified"):
                if row[field] not in {"true", "false"}:
                    raise ValueError(f"Некорректный признак {field}: {key}")

            if code in names and names[code] != row["station_name"]:
                raise ValueError(f"Разные названия парковки {code}.")
            names[code] = row["station_name"]

            parking[code].append({
                "timestamp": timestamp,
                "local": timestamp.astimezone(local_zone),
                "value": value,
                "off_grid": row["is_on_grid"] == "false",
                "capacity_unverified":
                    row["capacity_history_verified"] == "false",
            })

    if not parking:
        raise ValueError("В выбранном периоде нет наблюдений.")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    FIGURES.mkdir(parents=True, exist_ok=True)
    codes = sorted(parking)
    hourly = defaultdict(list)
    weekdays = defaultdict(list)
    summary = []

    for code in codes:
        rows = sorted(parking[code], key=lambda row: row["timestamp"])
        parking[code] = rows

        for row in rows:
            local = row["local"]
            day_type = "weekend" if local.weekday() >= 5 else "weekday"
            hourly[(code, day_type, local.hour)].append(row["value"])
            weekdays[(code, local.weekday())].append(row["value"])

        values = [row["value"] for row in rows]
        summary.append({
            "station_code": code,
            "station_name": names[code],
            "observations": len(rows),
            "first_timestamp_utc": rows[0]["timestamp"].isoformat(),
            "last_timestamp_utc": rows[-1]["timestamp"].isoformat(),
            "mean_occupancy_pct": round(mean(values), 4),
            "median_occupancy_pct": round(median(values), 4),
            "min_occupancy_pct": min(values),
            "max_occupancy_pct": max(values),
            "off_grid_observations": sum(row["off_grid"] for row in rows),
            "capacity_unverified_observations": sum(
                row["capacity_unverified"] for row in rows
            ),
        })

    hourly_rows = []
    weekday_rows = []

    for code in codes:
        for day_type in ("weekday", "weekend"):
            for hour in range(24):
                values = hourly[(code, day_type, hour)]
                hourly_rows.append({
                    "station_code": code,
                    "day_type": day_type,
                    "hour_local": hour,
                    "observations": len(values),
                    "mean_occupancy_pct":
                        round(mean(values), 4) if values else "",
                })

        for weekday in range(7):
            values = weekdays[(code, weekday)]
            weekday_rows.append({
                "station_code": code,
                "day_of_week": weekday,
                "day_name": WEEKDAYS[weekday],
                "observations": len(values),
                "mean_occupancy_pct":
                    round(mean(values), 4) if values else "",
            })

    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
    })

    # 1. Фактическая загрузка. В пропусках линия разрывается.
    figure, axes = plt.subplots(
        len(codes), 1, figsize=(12, 3.2 * len(codes)),
        squeeze=False, sharex=True,
    )
    for axis, code in zip(axes[:, 0], codes):
        timestamps = []
        values = []
        previous = None

        for row in parking[code]:
            current = row["timestamp"]
            if previous is not None and current - previous > interval:
                timestamps.append(previous + interval)
                values.append(float("nan"))
            timestamps.append(current)
            values.append(row["value"])
            previous = current

        axis.plot(timestamps, values, linewidth=0.8, color="#2563eb")
        axis.set_title(f"{code} — {names[code]}")
        axis.set_ylabel("Загрузка, %")
        axis.set_ylim(0, 105)
        axis.grid(alpha=0.2)
        axis.xaxis.set_major_locator(mdates.AutoDateLocator(tz=local_zone))
        axis.xaxis.set_major_formatter(
            mdates.DateFormatter("%d.%m", tz=local_zone)
        )
    axes[-1, 0].set_xlabel("Дата по времени Europe/Rome")
    save_figure(figure, "01_occupancy_history.png")

    # 2. Средняя загрузка по часам, отдельно будни и выходные.
    figure, axes = plt.subplots(
        len(codes), 1, figsize=(10, 3.3 * len(codes)),
        squeeze=False, sharex=True,
    )
    for axis, code in zip(axes[:, 0], codes):
        for day_type, label, color in (
            ("weekday", "Будни", "#2563eb"),
            ("weekend", "Выходные", "#ea580c"),
        ):
            values = [
                average(hourly[(code, day_type, hour)])
                for hour in range(24)
            ]
            axis.plot(
                range(24), values, marker="o",
                markersize=3, label=label, color=color,
            )
        axis.set_title(f"{code} — {names[code]}")
        axis.set_ylabel("Средняя загрузка, %")
        axis.set_ylim(0, 105)
        axis.set_xticks(range(0, 24, 2))
        axis.grid(alpha=0.2)
        axis.legend()
    axes[-1, 0].set_xlabel("Час по времени Europe/Rome")
    save_figure(figure, "02_hourly_profile.png")

    # 3. Средняя загрузка по дням недели.
    figure, axis = plt.subplots(figsize=(11, 5))
    width = 0.8 / len(codes)
    for index, code in enumerate(codes):
        positions = [
            day - 0.4 + width / 2 + index * width
            for day in range(7)
        ]
        values = [
            average(weekdays[(code, day)])
            for day in range(7)
        ]
        axis.bar(
            positions, values, width=width,
            label=f"{code} — {names[code]}",
        )
    axis.set_xticks(range(7), WEEKDAYS)
    axis.set_ylim(0, 105)
    axis.set_ylabel("Средняя загрузка, %")
    axis.set_title("Загрузка по дням недели")
    axis.grid(axis="y", alpha=0.2)
    axis.set_axisbelow(True)
    axis.legend()
    save_figure(figure, "03_weekday_profile.png")

    save_csv(OUTPUT / "parking_summary.csv", summary)
    save_csv(OUTPUT / "hourly_profile.csv", hourly_rows)
    save_csv(OUTPUT / "weekday_profile.csv", weekday_rows)

    report = {
        "source_file": str(INPUT.relative_to(ROOT)),
        "start_utc": start.isoformat(),
        "end_utc_exclusive": end.isoformat(),
        "calendar_timezone": config["calendar_timezone"],
        "input_rows": input_count,
        "analyzed_rows": sum(len(rows) for rows in parking.values()),
        "averaging": "arithmetic_mean_of_available_observations",
        "missing_values": "not_filled",
        "matplotlib_version": matplotlib.__version__,
        "stations": summary,
    }
    report_path = OUTPUT / "analysis_report.json"
    temporary = report_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(report_path)

    print(f"Начало периода UTC: {start.isoformat()}")
    print(f"Конец периода UTC, не включая: {end.isoformat()}")
    print(f"Часовой пояс календарных признаков: {local_zone}")
    print(f"Строк в исходной витрине: {input_count}")
    print(f"Наблюдений для анализа: {report['analyzed_rows']}")

    for item in summary:
        print(f"\nПарковка {item['station_code']}: {item['station_name']}")
        print("Наблюдений:", item["observations"])
        print("Средняя загрузка, %:", item["mean_occupancy_pct"])
        print("Медианная загрузка, %:", item["median_occupancy_pct"])
        print("Минимальная загрузка, %:", item["min_occupancy_pct"])
        print("Максимальная загрузка, %:", item["max_occupancy_pct"])

    print("\nСредние рассчитаны по имеющимся наблюдениям.")
    print("Пропуски не заполнялись. Выходные — суббота и воскресенье.")
    print("Сохранены три графика и три CSV-таблицы.")
    print("Каталог результатов:", OUTPUT)
    print("Отчёт анализа:", report_path)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"Анализ остановлен: {error}")
        raise SystemExit(1)