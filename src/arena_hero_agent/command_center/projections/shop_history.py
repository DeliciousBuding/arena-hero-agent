"""Shop price history projection (port of legacy ``shop-history.ts``).

The official shop changes prices/stock over time; every public products
snapshot is appended to ``runtime/shop-history.jsonl`` (deduplicated against
the previous snapshot by an ``id:cost:stock`` signature). This read path
aggregates the history into per-product trends: current cost/stock, delta vs
the previous snapshot containing the product, first/last seen, and snapshot
count. ``/api/shop/history``.

The write side (external fetch + append) is a P5-9 write route and lives at
the API layer; this module is the pure record/aggregate layer.
"""

from __future__ import annotations

import os
from typing import Any

from ..goal_store import iso_utc
from ..jsonl import load_jsonl_rows
from ..paths import validate_data_root
from ._common import current_epoch_ms, num

__all__ = [
    "aggregate_shop_history",
    "load_shop_history",
    "normalize_products",
    "should_append",
    "snapshot_signature",
]


def normalize_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map official shop product rows to the compact brief (TS parity)."""
    out: list[dict[str, Any]] = []
    for product in products or []:
        product_id = str(product.get("id") or "")
        if not product_id:
            continue
        out.append(
            {
                "id": product_id,
                "name": str(product.get("name") or ""),
                "resourceCost": num(product.get("resource_cost")),
                "availableStock": num(product.get("available_stock")),
                "purchaseLimit": num(product.get("purchase_limit")),
            }
        )
    return out


def snapshot_signature(products: list[dict[str, Any]]) -> str:
    """Sorted ``id:cost:stock`` signature; identical signature = no change (TS parity)."""
    return "|".join(
        sorted(f"{p['id']}:{p['resourceCost']}:{p['availableStock']}" for p in products)
    )


def should_append(prev: dict[str, Any] | None, products: list[dict[str, Any]]) -> bool:
    """Append the first snapshot or any snapshot differing from the previous one."""
    return prev is None or snapshot_signature(prev.get("products") or []) != snapshot_signature(
        products
    )


def aggregate_shop_history(
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate history snapshots into per-product trends (TS parity)."""
    by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        for product in entry.get("products") or []:
            product_id = str(product.get("id") or "")
            if not product_id:
                continue
            current = by_id.get(product_id)
            if current is None:
                by_id[product_id] = {
                    "name": product.get("name"),
                    "latest": dict(product),
                    "prev": None,
                    "firstAt": entry.get("at"),
                    "lastAt": entry.get("at"),
                    "count": 1,
                }
            else:
                current["prev"] = dict(current["latest"])
                current["latest"] = dict(product)
                current["lastAt"] = entry.get("at")
                current["count"] += 1

    trends: list[dict[str, Any]] = []
    for product_id, item in by_id.items():
        latest = item["latest"]
        prev = item["prev"]
        trends.append(
            {
                "id": product_id,
                "name": item["name"],
                "currentCost": latest["resourceCost"],
                "currentStock": latest["availableStock"],
                "costDelta": latest["resourceCost"] - prev["resourceCost"]
                if prev is not None
                else None,
                "stockDelta": (
                    latest["availableStock"] - prev["availableStock"] if prev is not None else None
                ),
                "firstSeenAt": item["firstAt"],
                "lastSeenAt": item["lastAt"],
                "snapshots": item["count"],
            }
        )
    trends.sort(key=lambda item: str(item["id"]))
    last = entries[-1] if entries else None
    return {
        "snapshots": len(entries),
        "productCount": len(last.get("products") or []) if last else 0,
        "lastSnapshotAt": last.get("at") if last else None,
        "trends": trends,
    }


def load_shop_history_entries(
    data_root: str | os.PathLike[str],
) -> list[dict[str, Any]]:
    """Read all history snapshots (the file is small; full read)."""
    root = validate_data_root(data_root)
    path = root / "runtime" / "shop-history.jsonl"
    return [entry for entry in load_jsonl_rows(path) if isinstance(entry.get("products"), list)]


def load_shop_history(
    data_root: str | os.PathLike[str],
    *,
    refreshed_at: str | None = None,
) -> dict[str, Any]:
    """Read history snapshots and aggregate (``/api/shop/history``)."""
    body = aggregate_shop_history(load_shop_history_entries(data_root))
    at = iso_utc(current_epoch_ms())
    return {
        "generatedAt": at,
        **body,
        "refreshedAt": refreshed_at,
        "cachedAt": at,
    }
