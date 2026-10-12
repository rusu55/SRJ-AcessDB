"""
Customers - read-only.

  GET /                          search / list customers (optionally with sales stats)
  GET /inactive                  bought before, nothing for N months (win-back list)
  GET /new                       first-ever order inside a date window
  GET /{customer_id}             one customer: contact, totals, recent orders, equipment
  GET /{customer_id}/items       what the customer buys: qty, sales, last price, last date

Money is order-line value (OrderQty x OrderUnitPrice) - freight and tax are
left out so customers compare on product sales. Built for CRMReports
(client pages, sales stats, win-back lists).
"""

from datetime import timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from middleware.auth import verify_api_key
from routes.report_utils import (
    day, db, dt, fetch, fetch_in, key, line_amount, line_shipped,
    money, num, parse_day, products, s, salesmen, today, window, window_out,
)

router = APIRouter()
AUTH = [Depends(verify_api_key)]


def _contact(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "customerId": s(r.get("CustomerID")),
        "name": s(r.get("CompanyName")) or None,
        "contact": s(r.get("ContactName")) or None,
        "title": s(r.get("ContactTitle")) or None,
        "email": s(r.get("Email Address")) or None,
        "phone": s(r.get("PhoneNumber")) or None,
        "address1": s(r.get("Address1")) or None,
        "address2": s(r.get("Address2")) or None,
        "city": s(r.get("City")) or None,
        "state": s(r.get("StateOrProvince")) or None,
        "postalCode": s(r.get("PostalCode")) or None,
        "country": s(r.get("Country")) or None,
        "categoryId": s(r.get("CategoryID")) or None,
        "vendorNo": s(r.get("VendorNo")) or None,
        "creditPass": bool(r.get("CreditPass")) if r.get("CreditPass") is not None else None,
        "creditLimit": r.get("CreditLimit"),
    }


_CUSTOMER_COLS = ("CustomerID, CompanyName, ContactName, ContactTitle, [Email Address], PhoneNumber, "
                  "Address1, Address2, City, StateOrProvince, PostalCode, Country, CategoryID, VendorNo, "
                  "CreditPass, CreditLimit")


def _order_stats(cur) -> Dict[str, Dict[str, Any]]:
    """CustomerID -> orders, first/last order date, sales, sales last 12 months."""
    orders = fetch(cur, "SELECT OrderID, CustomerID, OrderDate FROM Orders")
    cust_of = {key(o["OrderID"]): key(o.get("CustomerID")) for o in orders}
    year_ago = today() - timedelta(days=365)
    stats: Dict[str, Dict[str, Any]] = {}
    date_of: Dict[str, Any] = {}
    for o in orders:
        c = key(o.get("CustomerID"))
        if not c:
            continue
        d = dt(o.get("OrderDate"))
        date_of[key(o["OrderID"])] = d
        st = stats.setdefault(c, {"orders": 0, "firstOrder": None, "lastOrder": None, "sales": 0.0, "sales12m": 0.0})
        st["orders"] += 1
        if d and (st["firstOrder"] is None or d < st["firstOrder"]):
            st["firstOrder"] = d
        if d and (st["lastOrder"] is None or d > st["lastOrder"]):
            st["lastOrder"] = d
    for l in fetch(cur, "SELECT OrderID, OrderQty, OrderUnitPrice FROM [Order Details]"):
        oid = key(l.get("OrderID"))
        c = cust_of.get(oid)
        if not c or c not in stats:
            continue
        amt = line_amount(l)
        stats[c]["sales"] += amt
        d = date_of.get(oid)
        if d and d >= year_ago:
            stats[c]["sales12m"] += amt
    return stats


def _stats_out(st: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    st = st or {"orders": 0, "firstOrder": None, "lastOrder": None, "sales": 0.0, "sales12m": 0.0}
    return {"orders": st["orders"], "firstOrder": day(st["firstOrder"]), "lastOrder": day(st["lastOrder"]),
            "sales": money(st["sales"]), "salesLast12Months": money(st["sales12m"])}


@router.get("/", dependencies=AUTH)
def list_customers(
    q: Optional[str] = Query(None, description="Text in customer id, name, contact, city, state or email"),
    with_stats: bool = Query(False, description="Add orders, first/last order and sales (reads all orders - slower)"),
    limit: int = Query(200, ge=1, le=10000),
):
    """Example: GET /api/customers/?q=united&with_stats=true&limit=50"""
    with db() as cur:
        rows = fetch(cur, f"SELECT {_CUSTOMER_COLS} FROM Customers")
        needle = (q or "").strip().lower()
        out = []
        for r in rows:
            c = _contact(r)
            if needle and not any(needle in (c.get(f) or "").lower()
                                  for f in ("customerId", "name", "contact", "city", "state", "email")):
                continue
            out.append(c)
        out.sort(key=lambda c: ((c["name"] or "").lower(), c["customerId"]))
        total = len(out)
        out = out[:limit]
        if with_stats:
            stats = _order_stats(cur)
            for c in out:
                c.update(_stats_out(stats.get(key(c["customerId"]))))
        return {"total": total, "count": len(out), "customers": out}


@router.get("/inactive", dependencies=AUTH)
def inactive_customers(
    months: int = Query(12, ge=1, le=120, description="No order for at least this many months"),
    min_orders: int = Query(2, ge=1, description="Ignore one-off buyers below this many orders"),
    min_sales: float = Query(0, ge=0, description="Lifetime sales at least this much"),
    limit: int = Query(500, ge=1, le=10000),
):
    """Customers worth a call: they bought before, nothing lately. Biggest
    lifetime sales first. Example: GET /api/customers/inactive?months=12&min_orders=3"""
    cutoff = today() - timedelta(days=round(months * 30.44))
    with db() as cur:
        stats = _order_stats(cur)
        rows = {key(r["CustomerID"]): r for r in fetch(cur, f"SELECT {_CUSTOMER_COLS} FROM Customers")}
        out = []
        for c, st in stats.items():
            last = st["lastOrder"]
            if not last or last >= cutoff or st["orders"] < min_orders or st["sales"] < min_sales:
                continue
            base = _contact(rows[c]) if c in rows else {"customerId": c, "name": None}
            base.update(_stats_out(st))
            base["daysSinceLastOrder"] = (today() - last).days
            out.append(base)
        out.sort(key=lambda x: -x["sales"])
        return {"cutoff": day(cutoff), "count": len(out[:limit]), "total": len(out), "customers": out[:limit]}


@router.get("/new", dependencies=AUTH)
def new_customers(
    since: Optional[str] = Query(None, description="YYYY-MM-DD, default 90 days ago"),
    until: Optional[str] = Query(None, description="YYYY-MM-DD exclusive, default tomorrow"),
):
    """Customers whose FIRST order falls in the window, with what that first
    order was worth. Example: GET /api/customers/new?since=2026-01-01"""
    d0, d1 = window(since, until, default_days=90, max_days=3700)
    with db() as cur:
        stats = _order_stats(cur)
        rows = {key(r["CustomerID"]): r for r in fetch(cur, f"SELECT {_CUSTOMER_COLS} FROM Customers")}
        out = []
        for c, st in stats.items():
            first = st["firstOrder"]
            if not first or not (d0 <= first < d1):
                continue
            base = _contact(rows[c]) if c in rows else {"customerId": c, "name": None}
            base.update(_stats_out(st))
            out.append(base)
        out.sort(key=lambda x: x["firstOrder"] or "", reverse=True)
        return {**window_out(d0, d1), "count": len(out), "customers": out}


@router.get("/{customer_id}", dependencies=AUTH)
def customer_detail(customer_id: str, recent: int = Query(20, ge=1, le=500, description="How many recent orders")):
    """Contact, lifetime totals, open lines, the last `recent` orders and the
    equipment list. Example: GET /api/customers/EASHAR"""
    with db() as cur:
        rows = fetch(cur, f"SELECT {_CUSTOMER_COLS} FROM Customers WHERE CustomerID = ?", (customer_id.strip(),))
        if not rows:
            raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found")
        cust = _contact(rows[0])
        orders = fetch(cur, "SELECT OrderID, OrderDate, CustomerOrderNumber, BuyerName, SalesmanID, ShipVia, "
                            "ShipCity, ShipRegion, Freight, SalesTax, HandlingCharge FROM Orders WHERE CustomerID = ?",
                       (cust["customerId"],))
        lines_by_order: Dict[str, List[Dict[str, Any]]] = {}
        if orders:
            for l in fetch_in(cur, "SELECT OrderID, ProductID, OrderQty, OrderUnitPrice, RequiredDate, ShippingID, "
                                   "ShippingDate, OrderShippedQty FROM [Order Details] WHERE OrderID IN",
                              [s(o["OrderID"]) for o in orders]):
                lines_by_order.setdefault(key(l["OrderID"]), []).append(l)
        reps = salesmen(cur)
        year_ago = today() - timedelta(days=365)
        sales = sales12 = open_amt = 0.0
        open_lines = 0
        shaped = []
        for o in orders:
            ls = lines_by_order.get(key(o["OrderID"]), [])
            sub = sum(line_amount(l) for l in ls)
            shipped = [l for l in ls if line_shipped(l)]
            d = dt(o.get("OrderDate"))
            sales += sub
            if d and d >= year_ago:
                sales12 += sub
            for l in ls:
                if not line_shipped(l):
                    open_lines += 1
                    open_amt += line_amount(l)
            tax = sub * num(o.get("SalesTax"))
            shaped.append({
                "orderId": s(o["OrderID"]),
                "orderDate": day(d),
                "customerPo": s(o.get("CustomerOrderNumber")) or None,
                "buyer": s(o.get("BuyerName")) or None,
                "salesman": reps.get(key(o.get("SalesmanID"))),
                "shipTo": ", ".join(x for x in (s(o.get("ShipCity")), s(o.get("ShipRegion"))) if x) or None,
                "lines": len(ls),
                "subtotal": money(sub),
                "total": money(sub + num(o.get("Freight")) + tax + num(o.get("HandlingCharge"))),
                "status": "no lines" if not ls else "shipped" if len(shipped) == len(ls)
                          else "open" if not shipped else "partly shipped",
                "lastShipDate": max((day(l.get("ShippingDate")) for l in shipped if l.get("ShippingDate")), default=None),
            })
        shaped.sort(key=lambda x: (x["orderDate"] or "", x["orderId"]), reverse=True)
        dates = [x["orderDate"] for x in shaped if x["orderDate"]]
        try:
            equipment = [{"rentalStoreId": s(e.get("RentalStoreID")) or None, "make": s(e.get("MachineMake")) or None,
                          "model": s(e.get("MachineModel")) or None, "qty": e.get("MCQty"),
                          "itemNo": s(e.get("ItemNo")) or None}
                         for e in fetch(cur, "SELECT * FROM [Equipment List by Customer] WHERE CustomerID = ?",
                                        (cust["customerId"],))]
        except Exception:  # noqa: BLE001 - optional table
            equipment = []
        return {
            "customer": cust,
            "totals": {
                "orders": len(orders),
                "firstOrder": min(dates) if dates else None,
                "lastOrder": max(dates) if dates else None,
                "sales": money(sales),
                "salesLast12Months": money(sales12),
                "openLines": open_lines,
                "openValue": money(open_amt),
            },
            "recentOrders": shaped[:recent],
            "equipment": equipment,
        }


@router.get("/{customer_id}/items", dependencies=AUTH)
def customer_items(
    customer_id: str,
    since: Optional[str] = Query(None, description="Only orders from this day (YYYY-MM-DD); default all history"),
):
    """Every item the customer has ordered: total qty, sales, how many orders,
    last price paid and when. Most recent first.
    Example: GET /api/customers/EASHAR/items?since=2024-01-01"""
    with db() as cur:
        params: List[Any] = [customer_id.strip()]
        sql = "SELECT OrderID, OrderDate FROM Orders WHERE CustomerID = ?"
        if since:
            sql += " AND OrderDate >= ?"
            params.append(parse_day(since, "since"))
        orders = {key(o["OrderID"]): dt(o.get("OrderDate")) for o in fetch(cur, sql, params)}
        if not orders:
            return {"customerId": customer_id, "count": 0, "items": []}
        lines = fetch_in(cur, "SELECT OrderID, ProductID, OrderQty, OrderUnitPrice, OrderShippedQty, ShippingDate, "
                              "ShippingID FROM [Order Details] WHERE OrderID IN", list(orders.keys()))
        prods = products(cur)
        items: Dict[str, Dict[str, Any]] = {}
        for l in lines:
            p = prods.get(key(l.get("ProductID")), {})
            code = key(p.get("itemNo")) or f"#{s(l.get('ProductID'))}"
            d = orders.get(key(l["OrderID"]))
            it = items.setdefault(code, {"itemNo": p.get("itemNo") or s(l.get("ProductID")), "name": p.get("name"),
                                         "productLine": p.get("productLine"), "orders": set(), "qty": 0.0,
                                         "shippedQty": 0.0, "sales": 0.0, "_last": None, "lastPrice": None,
                                         "lastOrderId": None})
            it["orders"].add(key(l["OrderID"]))
            it["qty"] += num(l.get("OrderQty"))
            it["shippedQty"] += num(l.get("OrderShippedQty"))
            it["sales"] += line_amount(l)
            if d and (it["_last"] is None or d >= it["_last"]):
                it["_last"] = d
                it["lastPrice"] = num(l.get("OrderUnitPrice"))
                it["lastOrderId"] = s(l.get("OrderID"))
        out = []
        for it in items.values():
            it["orders"] = len(it["orders"])
            it["sales"] = money(it["sales"])
            it["lastOrderDate"] = day(it.pop("_last"))
            out.append(it)
        out.sort(key=lambda x: (x["lastOrderDate"] or ""), reverse=True)
        return {"customerId": customer_id, "count": len(out), "items": out}
