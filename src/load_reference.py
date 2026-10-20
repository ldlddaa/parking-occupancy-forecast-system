import csv
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_parkings():
    path = ROOT / "data" / "reference" / "parkings.csv"

    required_columns = {
        "station_code",
        "station_name",
        "capacity",
        "capacity_reference_date",
        "capacity_history_verified",
    }

    parkings = {}

    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        if set(reader.fieldnames or []) != required_columns:
            raise ValueError("Неверный состав столбцов справочника.")

        for row in reader:
            code = row["station_code"].strip()
            name = row["station_name"].strip()
            capacity = int(row["capacity"])
            verified = row["capacity_history_verified"].strip().lower()

            date.fromisoformat(row["capacity_reference_date"])

            if not code or not name:
                raise ValueError("Код и название не должны быть пустыми.")

            if code in parkings:
                raise ValueError(f"Повтор кода парковки: {code}")

            if capacity <= 0:
                raise ValueError(f"Некорректная вместимость: {code}")

            if verified not in {"true", "false"}:
                raise ValueError(f"Некорректный признак проверки: {code}")

            parkings[code] = {
                "station_code": code,
                "station_name": name,
                "capacity": capacity,
                "capacity_reference_date": row["capacity_reference_date"],
                "capacity_history_verified": verified == "true",
            }

    if not parkings:
        raise ValueError("Справочник пустой.")

    return parkings


if __name__ == "__main__":
    parkings = load_parkings()

    path = ROOT / "data" / "storage" / "parking.json"
    storage = json.loads(path.read_text(encoding="utf-8"))

    observation_codes = {
        str(row["scode"]) for row in storage["data"]
    }
    unknown_codes = observation_codes - set(parkings)

    if unknown_codes:
        raise ValueError(
            f"В справочнике отсутствуют коды: {sorted(unknown_codes)}"
        )

    print("Справочник прочитан успешно.")
    print("Количество парковок:", len(parkings))

    for code, parking in parkings.items():
        print(
            f"{code} | {parking['station_name']} | "
            f"Вместимость: {parking['capacity']}"
        )

    print("Проверено наблюдений:", len(storage["data"]))
    print("Все коды наблюдений найдены в справочнике — OK")
    