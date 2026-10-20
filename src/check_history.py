import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Запрашиваем количество занятых мест за одни сутки.
base_url = (
    "https://mobility.api.opendatahub.com/v2/flat/"
    "ParkingStation/occupied/2026-09-01/2026-09-02"
)

params = {
    "where": (
        'and(scode.in.("103","106","108"),'
        'sorigin.eq."FAMAS",mperiod.eq.300)'
    ),
    "timezone": "UTC",
    "limit": 10000,
}

request = Request(
    base_url + "?" + urlencode(params),
    headers={
        "Referer": "https://github.com/ldlddaa/parking-occupancy-forecast-system"
    },
)

print("Получаем историю за 1 сентября 2026 года...", flush=True)

with urlopen(request, timeout=45) as response:
    result = json.load(response)

rows = result["data"]

if len(rows) >= 10000:
    raise RuntimeError("Достигнут лимит ответа. Нужно проверить пагинацию.")

# Сохраняем исходный ответ API в папку проекта.
root = Path(__file__).resolve().parents[1]
folder = root / "data" / "raw" / "parking"
folder.mkdir(parents=True, exist_ok=True)

file_path = folder / "history_2026-09-01.json"
file_path.write_text(
    json.dumps(result, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

# Проверяем строго одни сутки: начало включаем, конец исключаем.
start = datetime(2026, 9, 1, tzinfo=timezone.utc)
end = datetime(2026, 9, 2, tzinfo=timezone.utc)

for code in ("103", "106", "108"):
    station_rows = [
        row for row in rows
        if row["scode"] == code
        and start <= datetime.fromisoformat(row["mvalidtime"]) < end
    ]

    print(f"\nПарковка {code}: наблюдений — {len(station_rows)}")

    if not station_rows:
        continue

    times = sorted({
        datetime.fromisoformat(row["mvalidtime"])
        for row in station_rows
    })

    intervals = Counter(
        (later - earlier).total_seconds() / 60
        for earlier, later in zip(times, times[1:])
    )

    print("Уникальных временных отметок:", len(times))
    print("Первая отметка:", times[0])
    print("Последняя отметка:", times[-1])
    print("Интервалы в минутах и их количество:", dict(intervals))
    print("Пример количества занятых мест:", station_rows[0]["mvalue"])

print(f"\nИсходный ответ сохранён: {file_path}")