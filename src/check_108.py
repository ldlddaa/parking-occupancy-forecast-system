import json
from collections import Counter
from urllib.parse import urlencode
from urllib.request import Request, urlopen

base_url = (
    "https://mobility.api.opendatahub.com/v2/flat/"
    "ParkingStation/occupied/2026-09-01/2026-09-02"
)

# Ограничиваем только кодом парковки.
params = {
    "where": 'scode.eq."108"',
    "timezone": "UTC",
    "limit": 10000,
}

request = Request(
    base_url + "?" + urlencode(params),
    headers={
        "Referer": "https://github.com/ldlddaa/parking-occupancy-forecast-system"
    },
)

print("Проверяем парковку 108 без фильтра поставщика и периода...")

with urlopen(request, timeout=45) as response:
    result = json.load(response)

rows = result["data"]
print("Получено записей:", len(rows))

series = Counter(
    (row.get("sorigin"), row.get("mperiod"))
    for row in rows
)

for (origin, period), count in series.items():
    print(
        f"Поставщик: {origin} | "
        f"Период в секундах: {period} | Записей: {count}"
    )

if rows:
    print("\nПример одной записи:")
    print(json.dumps(rows[0], ensure_ascii=False, indent=2))

if len(rows) >= 10000:
    print("Достигнут лимит: ответ может быть неполным.")