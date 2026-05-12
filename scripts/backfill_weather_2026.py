"""
One-time backfill: load historical weather data from Open-Meteo for
2026-01-01 to 2026-03-22 for store_id 1 (Matosinhos) and store_id 2 (Bolhão).

Run from the project root:
    python scripts/backfill_weather_2026.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
from weather_service import fetch_openmeteo_historical
from db.meteorologia import upsert_weather_data_batch
from db.connection import db_connection

STORES = [
    {"id": 1, "name": "Matosinhos", "latitude": 41.176617, "longitude": -8.689655},
    {"id": 2, "name": "Bolhão",     "latitude": 41.14906,  "longitude": -8.608581},
]

START = date(2026, 1, 1)
END   = date(2026, 3, 22)


EXPECTED_DAYS = (END - START).days + 1


def _db_count(store_id: int) -> int:
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM weather_data "
            "WHERE store_id = %s AND fonte = 'open-meteo' AND data BETWEEN %s AND %s",
            (store_id, START, END),
        )
        return cur.fetchone()[0]


def backfill():
    all_ok = True
    for store in STORES:
        store_id = store["id"]
        lat = store["latitude"]
        lon = store["longitude"]
        name = store["name"]

        print(f"Fetching historical data for store {store_id} ({name}) "
              f"{START} → {END} …")

        records = fetch_openmeteo_historical(lat, lon, START, END)

        if not records:
            print(f"  [WARN] No records returned for store {store_id}. "
                  "Check connectivity or date range.")
            all_ok = False
            continue

        for rec in records:
            rec["store_id"] = store_id

        upsert_weather_data_batch(records)
        print(f"  Upserted {len(records)} records for store {store_id} ({name}).")

        db_count = _db_count(store_id)
        if db_count < EXPECTED_DAYS:
            print(f"  [WARN] DB has only {db_count}/{EXPECTED_DAYS} days for store {store_id}. "
                  "Some dates may be missing — consider re-running.")
            all_ok = False
        else:
            print(f"  Completeness check passed: {db_count}/{EXPECTED_DAYS} days in DB.")

    if all_ok:
        print("Backfill complete — all stores fully covered.")
    else:
        print("Backfill finished with warnings — review output above.")
        sys.exit(1)


if __name__ == "__main__":
    backfill()
