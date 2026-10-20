import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

START = "2026-09-01"
END = "2026-10-01"
PAGE_SIZE = 5000

base_url = (
    "https://mobility.api.opendatahub.com/v2/flat/"
    f"ParkingStation/occupied/{START}/{END}"
)

params = {
    "where": (
        'and(scode.in.("103","106"),'
        'sorigin.eq."FAMAS",mperiod.eq.300)'
    ),
    "timezone": "UTC",
    "limit": PAGE_SIZE,
}

all_rows = []
offset = 0

while True:
    params["offset"] = offset

    request = Request(
        base_url + "?" + urlencode(params),
        headers={
            "Referer": (
                "https://github.com/ldlddaa/"
                "parking-occupancy-forecast-system"
            )
        },
    )

    print(f"Получаем данные: offset={offset}...", flush=True)

    with urlopen(request, timeout=45) as response:
        result = json.load(response)

    page = result["data"]
    all_rows.extend(page)

    print(f"Получено в этой части: {len(page)}", flush=True)

    if len(page) < PAGE_SIZE:
        break

    offset += len(page)

# Оставляем только сентябрь по UTC.
start = datetime.fromisoformat(START).replace(tzinfo=timezone.utc)
end = datetime.fromisoformat(END).replace(tzinfo=timezone.utc)

rows = [
    row for row in all_rows
    if start <= datetime.fromisoformat(row["mvalidtime"]) < end
]

if not rows:
    raise RuntimeError("Записей за выбранный период нет.")

root = Path(__file__).resolve().parents[1]
folder = root / "data" / "raw" / "parking"
folder.mkdir(parents=True, exist_ok=True)

file_path = folder / f"parking_{START}_{END}.json"

file_path.write_text(
    json.dumps(
        {
            "start_utc": START,
            "end_utc_exclusive": END,
            "data": rows,
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)

expected = (end - start).days * 24 * 12

for code in ("103", "106"):
    station_rows = [row for row in rows if row["scode"] == code]

    times = sorted({
        datetime.fromisoformat(row["mvalidtime"])
        for row in station_rows
    })

    print(f"\nПарковка {code}")
    print("Всего записей:", len(station_rows))
    print("Уникальных временных отметок:", len(times))
    print("Ожидается при полном пятиминутном ряде:", expected)

    if times:
        print("Первая отметка:", times[0])
        print("Последняя отметка:", times[-1])

print(f"\nФайл сохранён: {file_path}")