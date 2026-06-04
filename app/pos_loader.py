"""Loads pos_transactions.csv into the baskets table.

The real CSV is per-LINE-ITEM (order_id, order_date, order_time, store_id,
product_id, brand_name, total_amount). A single purchase = one (store, date, time)
group, so 101 line-items collapse to 24 baskets for ST1008. Conversion logic keys
off baskets, not raw rows."""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from datetime import datetime

from app.storage import Storage


def _parse_ts(date_str: str, time_str: str) -> str:
    # CSV uses DD-MM-YYYY + HH:MM:SS, local store time. Stored as naive ISO
    # (treated as store-local); the analytics layer compares within the same store
    # so tz offset cancels. Documented in CHOICES.md.
    dt = datetime.strptime(f"{date_str} {time_str}", "%d-%m-%Y %H:%M:%S")
    return dt.isoformat()


def load_pos_csv(path: str, storage: Storage) -> int:
    """Returns number of baskets loaded."""
    if not os.path.exists(path):
        return 0
    groups: dict[tuple, list[float]] = defaultdict(list)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            store = row["store_id"].strip()
            if store.lower().startswith("store_"):
                store = "ST" + store.split("_", 1)[1]
            key = (store, row["order_date"].strip(), row["order_time"].strip())
            try:
                groups[key].append(float(row["total_amount"]))
            except (ValueError, KeyError):
                groups[key].append(0.0)

    count = 0
    for (store, date_s, time_s), values in groups.items():
        basket_key = f"{store}|{date_s}|{time_s}"
        storage.upsert_basket(
            basket_key=basket_key,
            store_id=store,
            ts=_parse_ts(date_s, time_s),
            basket_value=round(sum(values), 2),
            item_count=len(values),
        )
        count += 1
    return count
