"""
Raw on-hand Inventory rows, one per row, with a row identity.

READ-ONLY. Built for CRMReports /inventory-audit -> Pallets & Locations ->
"Preview Access fix", which plans how to correct the Access DB from Sage 100.

Why a separate endpoint and not /api/inventory/all:
  /all groups lots by item and drops the columns a correction must target.
  A correction needs to point at ONE physical row, and the Inventory table has
  no primary key and no unique column (857 rows are exact copies of another
  row as of 2026-10-02). So every row carries:

    fingerprint  sha1 over every Inventory column, in table order. Two rows
                 with the same fingerprint are indistinguishable to SQL.
    dup_count    how many rows in the WHOLE table share that fingerprint.
                 Anything > 1 cannot be updated one row at a time - the
                 planner refuses those and says so.

Only rows where WarehouseReceiptQty <> OrderShippedQty are returned (the same
"on hand" rule as /api/inventory/all), but dup_count is counted across the
whole table so a fully-shipped twin still counts.

Nothing here writes, locks or alters the .mdb: one SELECT per table, fetched
in full, connection closed.
"""

import hashlib
from collections import Counter
from datetime import datetime, date
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException

from database.connection import get_db_connection
from middleware.auth import verify_api_key

router = APIRouter()


def _fp_value(v: Any) -> str:
    """Stable text for hashing: dates as ISO, None as empty."""
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


def _fingerprint(values: List[Any]) -> str:
    joined = "\x1f".join(_fp_value(v) for v in values)
    return hashlib.sha1(joined.encode("utf-8", errors="replace")).hexdigest()


def _date(v: Any):
    if isinstance(v, (datetime, date)):
        return v.strftime("%m/%d/%Y")
    return v


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


@router.get("/on-hand", dependencies=[Depends(verify_api_key)])
def inventory_rows_on_hand():
    """
    Every Inventory row with stock (receipt <> shipped), with its fingerprint.
    Example: GET /api/inventory-rows/on-hand
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Inventory columns only, in table order - the fingerprint must not
        # depend on a join. Products is read separately for the item code.
        cursor.execute("SELECT * FROM Inventory")
        inv_cols = [c[0] for c in cursor.description]
        raw_rows = cursor.fetchall()

        cursor.execute("SELECT ProductID, ItemNo FROM Products")
        item_by_pid = {
            ("" if r[0] is None else str(r[0]).strip().upper()): ("" if r[1] is None else str(r[1]).strip())
            for r in cursor.fetchall()
        }

        prints = [_fingerprint(list(r)) for r in raw_rows]
        counts = Counter(prints)
        idx = {name: i for i, name in enumerate(inv_cols)}

        def col(r, name):
            i = idx.get(name)
            return None if i is None else r[i]

        out: List[Dict[str, Any]] = []
        for r, fp in zip(raw_rows, prints):
            receipt = _num(col(r, "WarehouseReceiptQty"))
            shipped = _num(col(r, "OrderShippedQty"))
            if receipt == shipped:
                continue
            pid = col(r, "ProductID")
            pid_key = "" if pid is None else str(pid).strip().upper()
            out.append({
                "fingerprint": fp,
                "dup_count": counts[fp],
                "product_id": pid,
                "item_no": item_by_pid.get(pid_key, ""),
                "location": col(r, "Location"),
                "warehouse_id": col(r, "WarehouseID"),
                "mpack_no": col(r, "MPackNo"),
                "unit_no": col(r, "UnitNo"),
                "vendor_inv_no": col(r, "VendorInvNo"),
                "receipt_date": _date(col(r, "WarehouseReceiptDate")),
                "receipt_qty": receipt,
                "shipped_qty": shipped,
                "qty_on_hand": receipt - shipped,
                "shipping_date": _date(col(r, "ShippingDate")),
            })

        return {
            "count": len(out),
            "table_rows": len(raw_rows),
            "duplicate_rows": sum(n for n in counts.values() if n > 1),
            "rows": out,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()
