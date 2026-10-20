import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def numeric_value(value):
    """Проверяем, можно ли использовать значение как число."""
    if isinstance(value, bool) or value is None:
        return None

    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


parking_path = ROOT / "data" / "storage" / "parking.json"
parking = json.loads(parking_path.read_text(encoding="utf-8"))

start = datetime(2026, 9, 1, tzinfo=timezone.utc)
end = datetime.fromisoformat(
    parking["loaded_until_exclusive"]
).replace(tzinfo=timezone.utc)

expected_times = {
    start + timedelta(minutes=5 * i)
    for i in range(int((end - start).total_seconds() / 300))
}

print("ПАРКОВКИ")

for code in ("103", "106"):
    rows = [
        row for row in parking["data"]
        if row["scode"] == code
    ]

    times = [
        datetime.fromisoformat(row["mvalidtime"])
        for row in rows
    ]
    time_counts = Counter(times)

    missing = sorted(expected_times - set(times))
    unexpected = set(times) - expected_times
    duplicates = sum(count - 1 for count in time_counts.values())

    values = [numeric_value(row.get("mvalue")) for row in rows]
    valid_values = [value for value in values if value is not None]

    print(f"\nПарковка {code}")
    print("Записей:", len(rows))
    print("Ожидаемых отметок:", len(expected_times))
    print("Пропущенных отметок:", len(missing))
    print("Повторов временных отметок:", duplicates)
    print("Отметок вне ожидаемой сетки:", len(unexpected))
    print("Пустых или нечисловых значений:", values.count(None))
    print("Отрицательных значений:", sum(v < 0 for v in valid_values))

    if valid_values:
        print("Минимум занятых мест:", min(valid_values))
        print("Максимум занятых мест:", max(valid_values))

    print("Первые пропущенные отметки:")
    for timestamp in missing[:10]:
        print(" ", timestamp.isoformat())


weather_path = ROOT / "data" / "storage" / "weather.json"
weather = json.loads(weather_path.read_text(encoding="utf-8"))
rows = weather["data"]

weather_start = datetime(2026, 9, 1)
weather_end = datetime.fromisoformat(
    weather["loaded_until_exclusive"]
)

expected_hours = {
    weather_start + timedelta(hours=i)
    for i in range(
        int((weather_end - weather_start).total_seconds() / 3600)
    )
}

times = [datetime.fromisoformat(row["time"]) for row in rows]

print("\nПОГОДА")
print("Записей:", len(rows))
print("Пропущенных часов:", len(expected_hours - set(times)))
print("Повторов временных отметок:", len(times) - len(set(times)))
print("Отметок вне ожидаемой сетки:", len(set(times) - expected_hours))

for column in rows[0]:
    if column == "time":
        continue

    values = [numeric_value(row.get(column)) for row in rows]
    valid_values = [value for value in values if value is not None]

    print(f"\n{column}")
    print("Пустых или нечисловых значений:", values.count(None))

    if valid_values:
        print("Минимум:", min(valid_values))
        print("Максимум:", max(valid_values))
        print("\nПАРКОВОЧНЫЕ ОТМЕТКИ ВНЕ СЕТКИ")

for row in parking["data"]:
    timestamp = datetime.fromisoformat(row["mvalidtime"])

    if timestamp in expected_times:
        continue

    seconds = (timestamp - start).total_seconds()
    nearest = start + timedelta(
        seconds=round(seconds / 300) * 300
    )

    print("\nПарковка:", row["scode"])
    print("Фактическое время:", timestamp.isoformat())
    print("Ближайшая отметка сетки:", nearest.isoformat())
    print(
        "Отклонение в секундах:",
        (timestamp - nearest).total_seconds(),
    )
    print("Занятых мест:", row["mvalue"])
    print("Поставщик:", row["sorigin"])
    print("Период измерения:", row["mperiod"])