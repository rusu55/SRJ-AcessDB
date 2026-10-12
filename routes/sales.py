"""
Sales from Access orders - read-only.

  GET /lines      order lines in a date window, with customer / item / rep
  GET /summary    the same lines totalled by month, week, customer, item,
                  product line, salesman, order source, ship-to state,
                  warehouse or ship-via
  GET /compare    month by month: this year against last year

basis=ordered (default) puts a line in the window by its ORDER date and
values it at OrderQty x OrderUnitPrice (bookings). basis=shipped uses the
line's ShippingDate and OrderShippedQty x OrderUnitPrice (what went out).
Freight and tax are not included.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from middleware.auth import verify_api_key
from routes.report_utils import (
    customers, day, db, dt, fetch, fetch_by, key, line_amount, line_shipped, money, num,
    order_sources, product_ids_for_item, products, s, salesmen, shippers, warehouses, window, window_out,
)

router = APIRouter()
AUTH = [Depends(verify_api_key)]

_ORDER_COLS = ("OrderID, CustomerID, OrderDate, CustomerOrderNumber, SalesmanID, OrderSourceID, ShipVia, "
               "ShipRegion, ShipCity")
_LINE_COLS = ("OrderID, ProductID, OrderQty, OrderUnitPrice, RequiredDate, ShippingID, ShippingDate, "
              "OrderShippedQty, WarehouseID")

GROUPS = ("month", "week", "customer", "item", "product_line", "salesman", "source", "state", "warehouse", "ship_via")


def _basis(b: str) -> str:
    b = (b or "ordered").lower()
    if b not in ("ordered", "shipped"):
        raise HTTPException(status_code=400, detail="basis must be 'ordered' or 'shipped'")
    return b


def collect_lines(cur, d0: datetime, d1: datetime, basis: str, customer_id: Optional[str] = None,
                  item_no: Optional[str] = None, salesman_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Order lines in [d0, d1) on the chosen basis, each with its order header
    and lookups already filled in."""
    if basis == "ordered":
        sql = f"SELECT {_ORDER_COLS} FROM Orders WHERE OrderDate >= ? AND OrderDate < ?"
        params: List[Any] = [d0, d1]
        if customer_id:
            sql += " AND CustomerID = ?"
            params.append(customer_id.strip())
        if salesman_id is not None:
            sql += " AND SalesmanID = ?"
            params.append(salesman_id)
        orders = {key(o["OrderID"]): o for o in fetch(cur, sql, params)}
        lines = fetch_by(cur, f"SELECT {_LINE_COLS} FROM [Order Details]", "OrderID", list(orders.keys()))
    else:
        lines = [l for l in fetch(cur, f"SELECT {_LINE_COLS} FROM [Order Details] "
                                       "WHERE ShippingDate >= ? AND ShippingDate < ?", (d0, d1))]
        orders = {key(o["OrderID"]): o for o in fetch_by(cur, f"SELECT {_ORDER_COLS} FROM Orders", "OrderID",
                                                         [s(l["OrderID"]) for l in lines])}
        if customer_id:
            orders = {k: o for k, o in orders.items() if key(o.get("CustomerID")) == key(customer_id)}
        if salesman_id is not None:
            orders = {k: o for k, o in orders.items() if o.get("SalesmanID") is not None
                      and int(num(o.get("SalesmanID"))) == salesman_id}
        lines = [l for l in lines if key(l["OrderID"]) in orders]

    if item_no:
        pids = {key(p) for p in product_ids_for_item(cur, item_no)}
        lines = [l for l in lines if key(l.get("ProductID")) in pids]

    prods, names, reps, srcs, whs = products(cur), customers(cur), salesmen(cur), order_sources(cur), warehouses(cur)
    ships = shippers(cur)
    out = []
    for l in lines:
        o = orders.get(key(l["OrderID"]), {})
        p = prods.get(key(l.get("ProductID")), {})
        cust = s(o.get("CustomerID")) or None
        out.append({
            "orderId": s(l["OrderID"]),
            "orderDate": day(o.get("OrderDate")),
            "customerId": cust,
            "customerName": names.get(key(cust)),
            "customerPo": s(o.get("CustomerOrderNumber")) or None,
            "salesmanId": o.get("SalesmanID"),
            "salesman": reps.get(key(o.get("SalesmanID"))),
            "sourceId": s(o.get("OrderSourceID")) or None,
            "source": srcs.get(key(o.get("OrderSourceID"))),
            "shipVia": s(o.get("ShipVia")) or None,
            "carrier": ships.get(key(o.get("ShipVia"))),
            "shipState": s(o.get("ShipRegion")).upper() or None,
            "shipCity": s(o.get("ShipCity")) or None,
            "productId": s(l.get("ProductID")),
            "itemNo": p.get("itemNo") or None,
            "itemName": p.get("name"),
            "productLine": p.get("productLine"),
            "qty": num(l.get("OrderQty")),
            "shippedQty": num(l.get("OrderShippedQty")),
            "unitPrice": num(l.get("OrderUnitPrice")),
            "amount": money(line_amount(l, basis)),
            "requiredDate": day(l.get("RequiredDate")),
            "shippingDate": day(l.get("ShippingDate")),
            "shippingId": s(l.get("ShippingID")) or None,
            "warehouseId": s(l.get("WarehouseID")) or None,
            "warehouse": whs.get(key(l.get("WarehouseID"))),
            "shipped": line_shipped(l),
        })
    out.sort(key=lambda x: ((x["shippingDate"] if basis == "shipped" else x["orderDate"]) or "", x["orderId"]))
    return out


def _group_key(l: Dict[str, Any], by: str, basis: str):
    d = dt(l["shippingDate"] if basis == "shipped" else l["orderDate"])
    if by == "month":
        return (d.strftime("%Y-%m") if d else "?"), None
    if by == "week":
        if not d:
            return "?", None
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}", None
    if by == "customer":
        return l["customerId"] or "?", l["customerName"]
    if by == "item":
        return l["itemNo"] or f"#{l['productId']}", l["itemName"]
    if by == "product_line":
        return l["productLine"] or "(none)", None
    if by == "salesman":
        return (str(l["salesmanId"]) if l["salesmanId"] is not None else "(none)"), l["salesman"]
    if by == "source":
        return l["sourceId"] or "(none)", l["source"]
    if by == "state":
        return l["shipState"] or "(none)", None
    if by == "warehouse":
        return l["warehouseId"] or "(none)", l["warehouse"]
    return l["shipVia"] or "(none)", l["carrier"]


@router.get("/lines", dependencies=AUTH)
def sales_lines(
    since: Optional[str] = Query(None, description="YYYY-MM-DD, default 90 days ago"),
    until: Optional[str] = Query(None, description="YYYY-MM-DD exclusive, default tomorrow"),
    basis: str = Query("ordered", description="ordered (order date) | shipped (ship date)"),
    customer_id: Optional[str] = None,
    item_no: Optional[str] = None,
    salesman_id: Optional[int] = None,
    limit: int = Query(20000, ge=1, le=100000),
):
    """Example: GET /api/sales/lines?since=2026-09-01&until=2026-10-01&basis=shipped"""
    b = _basis(basis)
    d0, d1 = window(since, until, default_days=90, max_days=800)
    with db() as cur:
        lines = collect_lines(cur, d0, d1, b, customer_id, item_no, salesman_id)
        return {**window_out(d0, d1), "basis": b, "total": len(lines), "count": len(lines[:limit]),
                "amount": money(sum(l["amount"] for l in lines)), "lines": lines[:limit]}


@router.get("/summary", dependencies=AUTH)
def sales_summary(
    group_by: str = Query("month", description="|".join(GROUPS)),
    since: Optional[str] = Query(None, description="YYYY-MM-DD, default 365 days ago"),
    until: Optional[str] = Query(None, description="YYYY-MM-DD exclusive, default tomorrow"),
    basis: str = Query("ordered", description="ordered | shipped"),
    customer_id: Optional[str] = None,
    item_no: Optional[str] = None,
    salesman_id: Optional[int] = None,
    limit: int = Query(500, ge=1, le=10000, description="Rows kept (biggest first for non-date groups)"),
):
    """Totals per group: orders, lines, qty, amount and share of the total.
    Example: GET /api/sales/summary?group_by=customer&since=2026-01-01&limit=20"""
    by = (group_by or "").lower()
    if by not in GROUPS:
        raise HTTPException(status_code=400, detail=f"group_by must be one of: {', '.join(GROUPS)}")
    b = _basis(basis)
    d0, d1 = window(since, until, default_days=365, max_days=3700)
    with db() as cur:
        lines = collect_lines(cur, d0, d1, b, customer_id, item_no, salesman_id)
    groups: Dict[str, Dict[str, Any]] = {}
    for l in lines:
        k, label = _group_key(l, by, b)
        g = groups.setdefault(k, {"key": k, "label": label, "orders": set(), "customers": set(), "lines": 0,
                                  "qty": 0.0, "amount": 0.0})
        g["orders"].add(l["orderId"])
        if l["customerId"]:
            g["customers"].add(l["customerId"])
        g["lines"] += 1
        g["qty"] += l["shippedQty"] if b == "shipped" else l["qty"]
        g["amount"] += l["amount"]
    total = sum(g["amount"] for g in groups.values())
    rows = []
    for g in groups.values():
        rows.append({**g, "orders": len(g["orders"]), "customers": len(g["customers"]), "amount": money(g["amount"]),
                     "sharePct": round(100 * g["amount"] / total, 1) if total else 0.0})
    if by in ("month", "week"):
        rows.sort(key=lambda r: r["key"])
    else:
        rows.sort(key=lambda r: -r["amount"])
    return {**window_out(d0, d1), "basis": b, "groupBy": by, "groups": len(rows),
            "totals": {"orders": len({l["orderId"] for l in lines}), "lines": len(lines), "amount": money(total)},
            "rows": rows[:limit]}


@router.get("/compare", dependencies=AUTH)
def sales_compare(
    year: Optional[int] = Query(None, description="Default: this year"),
    basis: str = Query("ordered", description="ordered | shipped"),
):
    """Twelve months of `year` next to the same months of the year before:
    orders and amount, plus year-to-date for both (YTD stops at today's date
    so a part-year compares fairly). Example: GET /api/sales/compare?year=2026"""
    b = _basis(basis)
    y = year or datetime.now().year
    with db() as cur:
        lines = collect_lines(cur, datetime(y - 1, 1, 1), datetime(y + 1, 1, 1), b)
    months = {m: {"month": m, "thisYear": 0.0, "lastYear": 0.0, "ordersThisYear": set(), "ordersLastYear": set()}
              for m in range(1, 13)}
    now = datetime.now()
    ytd = {"thisYear": 0.0, "lastYear": 0.0}
    for l in lines:
        d = dt(l["shippingDate"] if b == "shipped" else l["orderDate"])
        if not d:
            continue
        m = months[d.month]
        side = "thisYear" if d.year == y else "lastYear"
        m[side] += l["amount"]
        m["orders" + side[0].upper() + side[1:]].add(l["orderId"])
        if (d.month, d.day) <= (now.month, now.day) or y < now.year:
            ytd[side] += l["amount"]
    rows = []
    for m in months.values():
        diff = m["thisYear"] - m["lastYear"]
        rows.append({"month": m["month"], "thisYear": money(m["thisYear"]), "lastYear": money(m["lastYear"]),
                     "ordersThisYear": len(m["ordersThisYear"]), "ordersLastYear": len(m["ordersLastYear"]),
                     "change": money(diff),
                     "changePct": round(100 * diff / m["lastYear"], 1) if m["lastYear"] else None})
    return {"year": y, "basis": b, "months": rows,
            "ytd": {"thisYear": money(ytd["thisYear"]), "lastYear": money(ytd["lastYear"]),
                    "changePct": round(100 * (ytd["thisYear"] - ytd["lastYear"]) / ytd["lastYear"], 1)
                    if ytd["lastYear"] else None}}
