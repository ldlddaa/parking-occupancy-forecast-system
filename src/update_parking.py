import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request

from http_client import request_bytes
from load_log import run_logged
from merge_records import merge_records

ROOT = Path(__file__).resolve().parents[1]
STORAGE_PATH = ROOT / "data" / "storage" / "parking.json"


def save_storage(data):
    STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STORAGE_PATH.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(STORAGE_PATH)


def record_key(row):
    return (
        row["scode"],
        row["sorigin"],
        row["mperiod"],
        datetime.fromisoformat(row["mvalidtime"]),
    )


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
            ROOT / "data" / "raw" / "parking"
            / "parking_2026-09-01_2026-10-01.json"
        )
        seed = json.loads(seed_path.read_text(encoding="utf-8"))

        storage = {
            "loaded_until_exclusive": seed["end_utc_exclusive"],
            "data": seed["data"],
        }
        save_storage(storage)
        print("Хранилище создано из сентябрьской выгрузки.")

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

        base_url = (
            "https://mobility.api.opendatahub.com/v2/flat/"
            f"ParkingStation/occupied/{current}/{next_day}"
        )

        params = {
            "where": (
                'and(scode.in.("103","106"),'
                'sorigin.eq."FAMAS",mperiod.eq.300)'
            ),
            "timezone": "UTC",
            "limit": 5000,
        }

        request = Request(
            base_url + "?" + urlencode(params),
            headers={
                "Referer": (
                    "https://github.com/ldlddaa/"
                    "parking-occupancy-forecast-system"
                )
            },
        )

        print(f"Запрашиваем сутки: {current}...", flush=True)

        result = json.loads(request_bytes(request))

        if not isinstance(result, dict):
            raise ValueError("API вернул неожиданный формат ответа.")

        received = result.get("data")

        if not isinstance(received, list):
            raise ValueError("В ответе API отсутствует список data.")

        if len(received) >= 5000:
            raise RuntimeError(
                "Достигнут лимит ответа. Граница не обновлена."
            )

        start = datetime.combine(
            current, datetime.min.time(), tzinfo=timezone.utc
        )
        end = datetime.combine(
            next_day, datetime.min.time(), tzinfo=timezone.utc
        )

        rows = [
            row for row in received
            if start <= datetime.fromisoformat(row["mvalidtime"]) < end
        ]

        if {row["scode"] for row in rows} != {"103", "106"}:
            raise ValueError(
                "Получены данные не для обеих парковок. "
                "Граница не обновлена."
            )

        before = len(storage["data"])

        storage["data"] = merge_records(
            storage["data"], rows, record_key
        )
        storage["loaded_until_exclusive"] = next_day.isoformat()
        save_storage(storage)

        print("Получено записей за сутки:", len(rows))
        print(
            "Добавлено новых записей:",
            len(storage["data"]) - before,
        )

        current = next_day

    print("Записей после запуска:", len(storage["data"]))
    print("Граница загрузки:", storage["loaded_until_exclusive"])
    print("Хранилище:", STORAGE_PATH)


if __name__ == "__main__":
    try:
        run_logged("parking", STORAGE_PATH, main)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"\nЗагрузка остановлена: {error}", flush=True)
        print("Ранее сохранённые сутки остаются в хранилище.")
        raise SystemExit(1)