"""
Inventory rows with a row identity, and the ONE guarded way to change them.

Built for CRMReports /inventory-audit -> Pallets & Locations -> "Access fix",
which corrects the Access DB from Sage 100 (pallet id, location, quantity).

  GET  /on-hand   read-only: every row with stock + fingerprint + dup_count
  POST /backup    copy the .mdb to a timestamped file before a live run
  POST /update    change ONE row, picked by fingerprint (dry run by default)

Live writes (backup + update with dry_run=false) need
ACCESS_INVENTORY_WRITES_ENABLED=1 in .env. Separate from ACCESS_WRITES_ENABLED
(order writes) on purpose: turning one on must not turn on the other.

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
import os
import shutil
import time
from collections import Counter
from datetime import datetime, date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from config.settings import settings

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
                "starting_qty": _num(col(r, "StartingQty")),
                "receipt_qty": receipt,
                "shipped_qty": shipped,
                "qty_on_hand": receipt - shipped,
                "shipping_date": _date(col(r, "ShippingDate")),
                "recorder_shippg": col(r, "RecorderShippg"),
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


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _inventory_writes_enabled() -> bool:
    return os.getenv("ACCESS_INVENTORY_WRITES_ENABLED", "") == "1"


def _require_writes():
    if not _inventory_writes_enabled():
        raise HTTPException(
            status_code=403,
            detail="Live Access inventory writes are DISABLED on this instance "
                   "(set ACCESS_INVENTORY_WRITES_ENABLED=1 in .env and restart).",
        )


# The only columns this endpoint will ever change, with their widths.
# Received qty, dates, item, lots: never.
WRITABLE: Dict[str, Optional[int]] = {
    "Location": 30,
    "WarehouseID": 6,
    "MPackNo": 25,
    "UnitNo": 30,
    "RecorderShippg": 20,
    "OrderShippedQty": None,  # integer
}


@router.post("/backup", dependencies=[Depends(verify_api_key)])
def backup_database():
    """
    Copy the live .mdb to <db folder>\\_backups\\<name>-YYYYMMDD-HHMMSS.mdb
    (or ACCESS_BACKUP_DIR). Run before every live batch; refused while
    inventory writes are disabled. Read-only for the source file.
    """
    _require_writes()
    src = settings.ACCESS_DB_PATH
    dest_dir = os.getenv("ACCESS_BACKUP_DIR") or os.path.join(os.path.dirname(src), "_backups")
    try:
        os.makedirs(dest_dir, exist_ok=True)
        stem, ext = os.path.splitext(os.path.basename(src))
        dest = os.path.join(dest_dir, f"{stem}-{datetime.now():%Y%m%d-%H%M%S}{ext}")
        t0 = time.time()
        shutil.copy2(src, dest)
        size_src = os.path.getsize(src)
        size_dest = os.path.getsize(dest)
        if size_dest != size_src:
            raise HTTPException(status_code=500, detail=f"Backup size mismatch ({size_dest} vs {size_src} bytes) - not trusted.")
        return {"path": dest, "bytes": size_dest, "seconds": round(time.time() - t0, 1)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {e}")


class RowUpdateIn(BaseModel):
    fingerprint: str
    product_id: str
    # field -> new value. Only WRITABLE fields.
    set: Dict[str, Any]
    # field -> value the caller saw. Re-checked against the row before writing.
    expect: Dict[str, Any] = {}
    dry_run: bool = True


def _norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    return v


def _same(a: Any, b: Any) -> bool:
    a, b = _norm(a), _norm(b)
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        try:
            return abs(float(a) - float(b)) < 1e-6
        except (TypeError, ValueError):
            return False
    return str(a).upper() == str(b).upper()


@router.post("/update", dependencies=[Depends(verify_api_key)])
def update_row(body: RowUpdateIn):
    """
    Change ONE Inventory row. The row is found by fingerprint inside its
    ProductID and must be the only row with that fingerprint. The UPDATE's
    WHERE names every column of the row as read, so it cannot touch any other
    row, and it is rolled back unless exactly one row changed.

    Refuses (409) when the row changed since it was read, when it has an
    identical twin, or when the change would make it identical to another row.
    """
    # -- validate the request ------------------------------------------------
    if not body.set:
        raise HTTPException(status_code=400, detail="Nothing to set.")
    bad = [k for k in body.set if k not in WRITABLE]
    if bad:
        raise HTTPException(status_code=400, detail=f"Not writable: {', '.join(bad)}")
    new_vals: Dict[str, Any] = {}
    for k, v in body.set.items():
        width = WRITABLE[k]
        if k == "OrderShippedQty":
            try:
                iv = int(v)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="OrderShippedQty must be a whole number.")
            if iv != float(v) or iv < 0:
                raise HTTPException(status_code=400, detail="OrderShippedQty must be a whole number >= 0.")
            new_vals[k] = iv
        else:
            sv = "" if v is None else str(v).strip()
            if not sv:
                raise HTTPException(status_code=400, detail=f"{k} can't be blank.")
            if width and len(sv) > width:
                raise HTTPException(status_code=400, detail=f"{k} is longer than {width} characters.")
            new_vals[k] = sv

    if not body.dry_run:
        _require_writes()

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM Inventory WHERE ProductID = ?", (body.product_id,))
        cols = [c[0] for c in cursor.description]
        rows = cursor.fetchall()
        prints = [_fingerprint(list(r)) for r in rows]
        hits = [r for r, fp in zip(rows, prints) if fp == body.fingerprint]
        if not hits:
            raise HTTPException(status_code=409, detail="changed: the row is not there any more (edited since it was read). Re-run the preview.")
        if len(hits) > 1:
            raise HTTPException(status_code=409, detail=f"duplicate: {len(hits)} identical rows - one can't be targeted. Fix by hand.")
        row = list(hits[0])
        idx = {name: i for i, name in enumerate(cols)}

        for k, v in body.expect.items():
            if k not in idx:
                raise HTTPException(status_code=400, detail=f"Unknown column in expect: {k}")
            if not _same(row[idx[k]], v):
                raise HTTPException(status_code=409, detail=f"changed: {k} is {row[idx[k]]!r}, expected {v!r}. Re-run the preview.")

        if "OrderShippedQty" in new_vals:
            receipt = _num(row[idx["WarehouseReceiptQty"]])
            if new_vals["OrderShippedQty"] > receipt:
                raise HTTPException(status_code=400, detail=f"OrderShippedQty {new_vals['OrderShippedQty']} is more than received ({receipt:g}).")

        before = {k: row[idx[k]] for k in new_vals}
        after_row = list(row)
        for k, v in new_vals.items():
            after_row[idx[k]] = v
        fp_after = _fingerprint(after_row)
        if fp_after != body.fingerprint and fp_after in prints:
            raise HTTPException(status_code=409, detail="would-duplicate: after this change the row would be identical to another row. Fix by hand.")

        changed = {k: v for k, v in new_vals.items() if not _same(before[k], v)}
        result = {
            "fingerprint_before": body.fingerprint,
            "fingerprint_after": fp_after,
            "before": {k: _date(v) for k, v in before.items()},
            "after": new_vals,
            "changed_fields": list(changed),
        }
        if not changed:
            return {"status": "nothing", "dry_run": body.dry_run, **result}
        if body.dry_run:
            return {"status": "dry-ok", "dry_run": True, **result}

        # -- the write: one row, every column pinned ---------------------------
        set_sql = ", ".join(f"[{k}] = ?" for k in changed)
        where, params = [], list(changed.values())
        for name, val in zip(cols, row):
            if val is None:
                where.append(f"[{name}] IS NULL")
            else:
                where.append(f"[{name}] = ?")
                params.append(val)
        sql = f"UPDATE Inventory SET {set_sql} WHERE " + " AND ".join(where)
        cursor.execute(sql, params)
        if cursor.rowcount != 1:
            conn.rollback()
            raise HTTPException(status_code=409, detail=f"refused: the update matched {cursor.rowcount} rows, rolled back.")
        conn.commit()

        # -- read back ---------------------------------------------------------
        cursor.execute("SELECT * FROM Inventory WHERE ProductID = ?", (body.product_id,))
        back = [_fingerprint(list(r)) for r in cursor.fetchall()]
        verified = fp_after in back and body.fingerprint not in back
        return {"status": "written" if verified else "unverified", "dry_run": False, "verified": verified, **result}
    except HTTPException:
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"update failed: {e}")
    finally:
        cursor.close()
        conn.close()
