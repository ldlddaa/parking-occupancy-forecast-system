import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Адрес API со справочником парковок.
base_url = "https://mobility.api.opendatahub.com/v2/flat/ParkingStation"

# Выбираем только парковки Больцано.
params = {
    "where": 'smetadata.municipality.eq."Bolzano - Bozen"',
    "limit": -1,
}

url = base_url + "?" + urlencode(params)

request = Request(
    url,
    headers={
        "Referer": "https://github.com/ldlddaa/parking-occupancy-forecast-system"
    },
)

print("Запрашиваем список парковок...", flush=True)

with urlopen(request, timeout=45) as response:
    result = json.load(response)

stations = result["data"]

print(f"\nНайдено записей парковок: {len(stations)}\n")

for station in stations:
    code = station.get("scode")
    name = station.get("sname")
    metadata = station.get("smetadata") or {}
    capacity = metadata.get("capacity", "не указана")

    print(f"Код: {code} | Название: {name} | Вместимость: {capacity}")