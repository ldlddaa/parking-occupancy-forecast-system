import csv
import io
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

# Одна опорная точка в центре Больцано.
params = {
    "latitude": 46.4983,
    "longitude": 11.3548,
    "start_date": "2026-09-01",
    "end_date": "2026-09-30",
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

print("Получаем погоду за сентябрь...", flush=True)

with urlopen(url, timeout=45) as response:
    raw_bytes = response.read()

text = raw_bytes.decode("utf-8-sig")
lines = text.splitlines()

# Перед таблицей в CSV могут находиться сведения о координатах.
header_index = next(
    (
        index for index, line in enumerate(lines)
        if line.startswith("time,")
    ),
    None,
)

if header_index is None:
    raise ValueError(
        "В ответе не найдена таблица с колонкой time. "
        f"Начало ответа: {text[:300]}"
    )

table_text = "\n".join(lines[header_index:])
reader = csv.DictReader(io.StringIO(table_text))

rows = [
    row for row in reader
    if row.get("time")
]

if not rows:
    raise ValueError("Таблица погоды пустая.")

root = Path(__file__).resolve().parents[1]
folder = root / "data" / "raw" / "weather"
folder.mkdir(parents=True, exist_ok=True)

file_path = folder / "weather_2026-09.csv"
file_path.write_bytes(raw_bytes)

times = sorted(row["time"] for row in rows)

print("Получено строк:", len(rows))
print("Уникальных временных отметок:", len(set(times)))
print("Ожидается часов за сентябрь:", 30 * 24)
print("Первая отметка:", times[0])
print("Последняя отметка:", times[-1])
print("Часовой пояс запроса: GMT / UTC")

print("\nСтолбцы:", list(rows[0].keys()))
print("Пример первой строки:", rows[0])

missing_values = sum(
    str(value).strip().lower() in ("", "nan", "null", "none")
    for row in rows
    for column, value in row.items()
    if column != "time"
)

print("\nПустых значений погодных показателей:", missing_values)
print(f"Исходный CSV сохранён: {file_path}")