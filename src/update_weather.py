import argparse
import csv
import io
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from http_client import request_bytes
from load_log import run_logged
from merge_records import merge_records

ROOT = Path(__file__).resolve().parents[1]
STORAGE_PATH = ROOT / "data" / "storage" / "weather.json"


def parse_csv(text):
    lines = text.splitlines()

    header = next(
        (i for i, line in enumerate(lines) if line.startswith("time,")),
        None,
    )

    if header is None:
        raise ValueError("В ответе не найдена таблица погоды.")

    reader = csv.DictReader(io.StringIO("\n".join(lines[header:])))
    rows = [row for row in reader if row.get("time")]

    if not rows:
        raise ValueError("Таблица погоды пустая.")

    return rows


def save_storage(data):
    STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STORAGE_PATH.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(STORAGE_PATH)


def record_key(row):
    return datetime.fromisoformat(row["time"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--until", required=True, type=date.fromisoformat
    )
    until = parser.parse_args().until

    if STORAGE_PATH.exists():
        storage = json.loads(
            STORAGE_PATH.read_text(encoding="utf-8")
        )
    else:
        seed_path = (
            ROOT / "data" / "raw" / "weather"
            / "weather_2026-09.csv"
        )
        rows = parse_csv(
            seed_path.read_text(encoding="utf-8-sig")
        )

        storage = {
            "timezone": "UTC",
            "latitude": 46.4983,
            "longitude": 11.3548,
            "loaded_until_exclusive": "2026-10-01",
            "data": rows,
        }
        save_storage(storage)
        print("Хранилище создано из сентябрьского CSV.")

    current = date.fromisoformat(
        storage["loaded_until_exclusive"]
    )

    print("Уже загружено до, не включая:", current)
    print("Запрошена граница, не включая:", until)
    print("Записей до запуска:", len(storage["data"]))

    if current >= until:
        print("Новых периодов нет. Запросы к API не выполнялись.")

    while current < until:
        next_day = current + timedelta(days=1)

        params = {
            "latitude": storage["latitude"],
            "longitude": storage["longitude"],
            "start_date": current.isoformat(),
            "end_date": current.isoformat(),
            "hourly": (
                "temperature_2m,precipitation,"
                "cloud_cover,wind_speed_10m"
            ),
            "timezone": "GMT",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "format": "csv",
        }

        url = (
            "https://archive-api.open-meteo.com/v1/archive?"
            + urlencode(params)
        )

        print(f"Запрашиваем сутки: {current}...", flush=True)

        raw_bytes = request_bytes(url)
        rows = parse_csv(raw_bytes.decode("utf-8-sig"))

        expected_times = {
            datetime.combine(current, datetime.min.time())
            + timedelta(hours=hour)
            for hour in range(24)
        }
        actual_times = {record_key(row) for row in rows}

        if len(rows) != 24 or actual_times != expected_times:
            raise ValueError(
                "Неполные или повторяющиеся часы. "
                "Граница не обновлена."
            )

        expected_columns = set(storage["data"][0])

        for row in rows:
            if set(row) != expected_columns:
                raise ValueError(
                    "Изменился состав столбцов погоды."
                )

            for column, value in row.items():
                if column != "time":
                    if str(value).strip().lower() in (
                        "", "nan", "null", "none"
                    ):
                        raise ValueError(
                            "Есть пустые показатели. "
                            "Граница не обновлена."
                        )

        raw_path = (
            ROOT / "data" / "raw" / "weather"
            / f"weather_{current}.csv"
        )
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(raw_bytes)

        before = len(storage["data"])

        storage["data"] = merge_records(
            storage["data"], rows, record_key
        )
        storage["loaded_until_exclusive"] = next_day.isoformat()
        save_storage(storage)

        print("Получено строк за сутки:", len(rows))
        print(
            "Добавлено новых строк:",
            len(storage["data"]) - before,
        )

        current = next_day

    print("Записей после запуска:", len(storage["data"]))
    print("Граница загрузки:", storage["loaded_until_exclusive"])
    print("Хранилище:", STORAGE_PATH)


if __name__ == "__main__":
    try:
        run_logged("weather", STORAGE_PATH, main)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"\nЗагрузка остановлена: {error}", flush=True)
        print("Ранее сохранённые сутки остаются в хранилище.")
        raise SystemExit(1)