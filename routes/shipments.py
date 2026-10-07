"""
Shipments recorded in Access, by ship date — read-only.

  GET /recent?since=YYYY-MM-DD&until=YYYY-MM-DD[&full_orders=true]

In this database a shipment has no row of its own with a date: the warehouse
fills three columns on the ORDER LINES — [Order Details].ShippingID (the
OrderID, plus A/B/C… for each partial shipment), ShippingDate and
OrderShippedQty. So "shipments in a window" = order lines whose ShippingDate
falls in [since, until). Built for CRMReports' daily "shipped in Access, not in
Sage" alert.

Nothing here writes, locks or alters the .mdb: plain SELECTs, connection closed.
"""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from database.connection import get_db_connection
from middleware.auth import verify_api_key

router = APIRouter()


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _day(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    t = _s(v)
    return t[:10] or None


def _parse(d: str, name: str) -> datetime:
    try:
        return datetime.strptime(d.strip()[:10], "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM-DD, got {d!r}")


@router.get("/recent", dependencies=[Depends(verify_api_key)])
def recent_shipments(
    since: str = Query(..., description="First ship date included (YYYY-MM-DD)"),
    until: str = Query(..., description="Ship dates BEFORE this day (YYYY-MM-DD), exclusive"),
    full_orders: bool = Query(False, description="Also return every OTHER shipped line of the orders found (any date) — "
                              "a quantity check needs the order's whole shipped history, not just the window"),
):
    """
    Order lines shipped in Access between `since` (inclusive) and `until`
    (exclusive), with the item code (Products.ItemNo), customer and order date.
    Example: GET /api/shipments/recent?since=2026-08-20&until=2026-10-02
    """
    d0 = _parse(since, "since")
    d1 = _parse(until, "until")
    if d1 <= d0:
        raise HTTPException(status_code=400, detail="until must be after since")
    if (d1 - d0) > timedelta(days=400):
        raise HTTPException(status_code=400, detail="window is limited to 400 days")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT OrderID, ProductID, OrderQty, ShippingID, ShippingDate, OrderShippedQty, WarehouseID "
            "FROM [Order Details] "
            "WHERE ShippingID IS NOT NULL AND ShippingDate >= ? AND ShippingDate < ?",
            (d0, d1),
        )
        cols = [c[0] for c in cursor.description]
        lines = [dict(zip(cols, r)) for r in cursor.fetchall()]

        cursor.execute("SELECT ProductID, ItemNo FROM Products")
        item_by_pid = {_s(r[0]).upper(): _s(r[1]) for r in cursor.fetchall()}

        order_ids = sorted({_s(l.get("OrderID")) for l in lines if _s(l.get("OrderID"))})
        if full_orders and order_ids:
            whole: List[Dict[str, Any]] = []
            for i in range(0, len(order_ids), 200):
                chunk = order_ids[i:i + 200]
                cursor.execute(
                    "SELECT OrderID, ProductID, OrderQty, ShippingID, ShippingDate, OrderShippedQty, WarehouseID "
                    "FROM [Order Details] WHERE ShippingID IS NOT NULL AND OrderID IN ("
                    + ",".join("?" * len(chunk)) + ")",
                    chunk,
                )
                c2 = [c[0] for c in cursor.description]
                whole.extend(dict(zip(c2, r)) for r in cursor.fetchall())
            lines = whole
        orders: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(order_ids), 200):
            chunk = order_ids[i:i + 200]
            cursor.execute(
                "SELECT OrderID, CustomerID, OrderDate, CustomerOrderNumber FROM Orders WHERE OrderID IN ("
                + ",".join("?" * len(chunk)) + ")",
                chunk,
            )
            for r in cursor.fetchall():
                orders[_s(r[0])] = {"customerId": _s(r[1]) or None, "orderDate": _day(r[2]), "customerPo": _s(r[3]) or None}

        names: Dict[str, str] = {}
        try:
            cursor.execute("SELECT CustomerID, CompanyName FROM Customers")
            names = {_s(r[0]).upper(): _s(r[1]) for r in cursor.fetchall()}
        except Exception:
            pass  # names are context only

        out: List[Dict[str, Any]] = []
        for l in lines:
            oid = _s(l.get("OrderID"))
            o = orders.get(oid, {})
            cust = o.get("customerId")
            out.append({
                "orderId": oid,
                "shippingId": _s(l.get("ShippingID")),
                "shippingDate": _day(l.get("ShippingDate")),
                "productId": _s(l.get("ProductID")),
                "itemNo": item_by_pid.get(_s(l.get("ProductID")).upper(), ""),
                "orderQty": float(l.get("OrderQty") or 0),
                "shippedQty": float(l.get("OrderShippedQty") or 0),
                "warehouseId": _s(l.get("WarehouseID")) or None,
                "customerId": cust,
                "customerName": names.get((cust or "").upper()) or None,
                "orderDate": o.get("orderDate"),
                "customerPo": o.get("customerPo"),
            })
        out.sort(key=lambda x: (x["shippingDate"] or "", x["orderId"], x["shippingId"]))
        return {"since": d0.strftime("%Y-%m-%d"), "until": d1.strftime("%Y-%m-%d"), "count": len(out), "lines": out}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Access read failed: {e}")
    finally:
        cursor.close()
        conn.close()
