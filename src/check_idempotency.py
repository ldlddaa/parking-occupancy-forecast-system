import json
from datetime import datetime
from pathlib import Path
from merge_records import merge_records

root = Path(__file__).resolve().parents[1]


def parking_key(row):
    return (
        row["scode"],
        row["sorigin"],
        row["mperiod"],
        datetime.fromisoformat(row["mvalidtime"]),
    )


def weather_key(row):
    return datetime.fromisoformat(row["time"])


for source, key in (
    ("parking", parking_key),
    ("weather", weather_key),
):
    path = root / "data" / "storage" / f"{source}.json"
    storage = json.loads(path.read_text(encoding="utf-8"))
    original = storage["data"]

    first = merge_records(original, original, key)
    second = merge_records(first, original, key)

    assert first == original, (
        f"{source}: первый повтор изменил данные"
    )
    assert second == original, (
        f"{source}: второй повтор изменил данные"
    )

    print(f"\nИсточник: {source}")
    print("До повторной обработки:", len(original))
    print("После первого повтора:", len(first))
    print("После второго повтора:", len(second))
    print("Количество и содержимое не изменились — OK") 