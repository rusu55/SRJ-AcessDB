"""
Data health checks on the Access DB - read-only.

  GET /    every check: how many rows are affected plus a few examples

Built for CRMReports' health page: problems here are why a report and Sage
disagree (a duplicated line doubles a shipment, an order dated 2031 never
shows in "this year", a customer typed twice splits its sales).
Nothing is fixed here - each check says what to look at.
"""

import re
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query

from middleware.auth import verify_api_key
from routes.report_utils import day, db, dt, fetch, key, line_amount, line_shipped, money, num, s, today

router = APIRouter()
AUTH = [Depends(verify_api_key)]

_NAME = re.compile(r"[^a-z0-9]+")
_SUFFIX = re.compile(r"\b(inc|llc|ltd|co|corp|corporation|company|the)\b")


def _norm_name(v: Any) -> str:
    return _NAME.sub(" ", _SUFFIX.sub(" ", s(v).lower())).strip()


def _check(cid: str, title: str, hint: str, rows: List[Dict[str, Any]], n: int, **extra) -> Dict[str, Any]:
    return {"id": cid, "title": title, "hint": hint, "count": len(rows), "examples": rows[:n], **extra}


@router.get("/", dependencies=AUTH)
def data_health(samples: int = Query(20, ge=0, le=1000, description="Examples per check")):
    """Example: GET /api/data-health/?samples=10"""
    now = today()
    with db() as cur:
        orders = fetch(cur, "SELECT OrderID, CustomerID, OrderDate FROM Orders")
        lines = fetch(cur, "SELECT OrderID, ProductID, OrderQty, OrderUnitPrice, RequiredDate, ShippingID, ShippingDate, "
                           "OrderShippedQty, WarehouseID, ProdLotNoID, SkidOrPalletNo, UnitNo FROM [Order Details]")
        prods = fetch(cur, "SELECT ProductID, ItemNo FROM Products")
        custs = fetch(cur, "SELECT CustomerID, CompanyName FROM Customers")
        inv = fetch(cur, "SELECT ProductID, Location, MPackNo, UnitNo, WarehouseReceiptQty, OrderShippedQty FROM Inventory")
        pol = fetch(cur, "SELECT PurchaseOrderID, ProductID, PurQty, PurShippedQty FROM [Purchase Order Details]")

    order_by_id = {key(o["OrderID"]): o for o in orders}
    pids = {key(p["ProductID"]) for p in prods}
    item_of = {key(p["ProductID"]): s(p.get("ItemNo")) for p in prods}
    cust_ids = {key(c["CustomerID"]) for c in custs}
    checks = []

    # 1. identical order lines
    # every column that tells two rows apart, pallet / unit / lot included -
    # one row per pallet of the same item is normal, an exact copy is not
    cols = ("OrderID", "ProductID", "OrderQty", "OrderUnitPrice", "ShippingID", "ShippingDate", "OrderShippedQty",
            "RequiredDate", "WarehouseID", "ProdLotNoID", "SkidOrPalletNo", "UnitNo")
    sig = Counter(tuple(s(l.get(c)) for c in cols) for l in lines)
    dups = [{"orderId": k[0], "itemNo": item_of.get(key(k[1])) or k[1], "qty": k[2], "shippingId": k[4] or None,
             "copies": n} for k, n in sig.items() if n > 1]
    dups.sort(key=lambda x: x["orderId"], reverse=True)
    checks.append(_check("duplicate-lines", "Order lines entered more than once",
                         "Every field the same (order, item, qty, price, shipment, pallet, unit, lot) on 2+ rows - "
                         "shipped qty and sales count twice.",
                         dups, samples, extraRows=sum(d["copies"] - 1 for d in dups)))

    # 2. orders with no lines
    with_lines = {key(l["OrderID"]) for l in lines}
    empty = [{"orderId": s(o["OrderID"]), "orderDate": day(o.get("OrderDate")), "customerId": s(o.get("CustomerID")) or None}
             for o in orders if key(o["OrderID"]) not in with_lines]
    empty.sort(key=lambda x: x["orderId"], reverse=True)
    checks.append(_check("orders-without-lines", "Orders with no lines", "Started and never filled in, or lines deleted.",
                         empty, samples))

    # 3. lines pointing at a missing product
    orphan = [{"orderId": s(l["OrderID"]), "productId": s(l.get("ProductID"))}
              for l in lines if key(l.get("ProductID")) not in pids]
    checks.append(_check("lines-unknown-product", "Order lines with a ProductID not in Products",
                         "The item code can't be shown, so the line drops out of item reports.", orphan, samples))

    # 4. odd order dates
    odd = []
    for o in orders:
        d = dt(o.get("OrderDate"))
        if d is None or d > now + timedelta(days=60) or d.year < 1995:
            odd.append({"orderId": s(o["OrderID"]), "orderDate": day(d), "customerId": s(o.get("CustomerID")) or None})
    odd.sort(key=lambda x: x["orderDate"] or "", reverse=True)
    checks.append(_check("odd-order-dates", "Orders with no date, a far-future date or a very old date",
                         "Usually a typo in the year (e.g. 2031 for 2021); the order lands in the wrong period.", odd, samples))

    # 4b. required dates that can't be right (typo in the year)
    odd_req = []
    for l in lines:
        rd = dt(l.get("RequiredDate"))
        od = dt(order_by_id.get(key(l["OrderID"]), {}).get("OrderDate"))
        if rd and od and (rd < od - timedelta(days=60) or rd > od + timedelta(days=730)):
            odd_req.append({"orderId": s(l["OrderID"]), "orderDate": day(od), "requiredDate": day(rd),
                            "itemNo": item_of.get(key(l.get("ProductID"))) or s(l.get("ProductID"))})
    odd_req.sort(key=lambda x: x["orderDate"] or "", reverse=True)
    checks.append(_check("odd-required-dates", "Lines whose required date is far before or after the order date",
                         "Usually a year typo (2002 for 2026); the line shows thousands of days late in on-time reports.",
                         odd_req, samples))

    # 5. orders for a customer that doesn't exist
    no_cust = [{"orderId": s(o["OrderID"]), "customerId": s(o.get("CustomerID")) or None, "orderDate": day(o.get("OrderDate"))}
               for o in orders if key(o.get("CustomerID")) not in cust_ids]
    no_cust.sort(key=lambda x: x["orderId"], reverse=True)
    checks.append(_check("orders-unknown-customer", "Orders whose CustomerID is not in Customers",
                         "Sales for these orders have no customer name.", no_cust, samples))

    # 6. shipped more than ordered
    over = [{"orderId": s(l["OrderID"]), "itemNo": item_of.get(key(l.get("ProductID"))) or s(l.get("ProductID")),
             "orderQty": num(l.get("OrderQty")), "shippedQty": num(l.get("OrderShippedQty")),
             "shippingDate": day(l.get("ShippingDate"))}
            for l in lines if num(l.get("OrderShippedQty")) > num(l.get("OrderQty"))]
    over.sort(key=lambda x: x["shippingDate"] or "", reverse=True)
    checks.append(_check("shipped-over-ordered", "Lines that shipped more than was ordered",
                         "Either the order qty or the shipped qty was typed wrong.", over, samples))

    # 7. half-recorded shipments
    half = []
    for l in lines:
        has_id, has_date, qty = bool(s(l.get("ShippingID"))), bool(dt(l.get("ShippingDate"))), num(l.get("OrderShippedQty"))
        if line_shipped(l) and not (has_id and has_date and qty > 0):
            half.append({"orderId": s(l["OrderID"]), "itemNo": item_of.get(key(l.get("ProductID"))) or s(l.get("ProductID")),
                         "shippingId": s(l.get("ShippingID")) or None, "shippingDate": day(l.get("ShippingDate")),
                         "shippedQty": qty,
                         "missing": ", ".join(x for x, ok in (("ShippingID", has_id), ("ShippingDate", has_date),
                                                              ("OrderShippedQty", qty > 0)) if not ok)})
    half.sort(key=lambda x: x["orderId"], reverse=True)
    checks.append(_check("half-recorded-shipments", "Shipments with the ID, date or qty missing",
                         "The line counts as shipped but one of the three shipping fields is empty.", half, samples))

    # 8. stale open lines
    year_ago = now - timedelta(days=365)
    stale = []
    for l in lines:
        if line_shipped(l):
            continue
        d = dt(order_by_id.get(key(l["OrderID"]), {}).get("OrderDate"))
        if d and d < year_ago:
            stale.append({"orderId": s(l["OrderID"]), "orderDate": day(d),
                          "itemNo": item_of.get(key(l.get("ProductID"))) or s(l.get("ProductID")),
                          "qty": num(l.get("OrderQty")), "value": money(line_amount(l))})
    stale.sort(key=lambda x: x["orderDate"] or "", reverse=True)
    checks.append(_check("stale-open-lines", "Open order lines on orders more than a year old",
                         "Never shipped, never cancelled. Close or delete them so backlog reports are real.",
                         stale, samples, value=money(sum(x["value"] for x in stale))))

    # 9. customers typed twice
    by_name = defaultdict(list)
    for c in custs:
        n = _norm_name(c.get("CompanyName"))
        if n:
            by_name[n].append(s(c["CustomerID"]))
    twins = [{"name": next((s(c.get("CompanyName")) for c in custs if s(c["CustomerID"]) == ids[0]), n),
              "customerIds": ids} for n, ids in by_name.items() if len(ids) > 1]
    twins.sort(key=lambda x: x["name"].lower())
    checks.append(_check("duplicate-customers", "Customers that look like the same company",
                         "Same name (ignoring Inc/LLC/punctuation) under different IDs - sales are split.", twins, samples))

    # 10. item code on several products
    by_code = defaultdict(list)
    for p in prods:
        if key(p.get("ItemNo")):
            by_code[key(p.get("ItemNo"))].append(s(p["ProductID"]))
    dup_items = [{"itemNo": k, "productIds": v} for k, v in by_code.items() if len(v) > 1]
    dup_items.sort(key=lambda x: x["itemNo"])
    checks.append(_check("duplicate-item-codes", "Item codes on more than one Products row",
                         "Stock and sales for the item are split across ProductIDs.", dup_items, samples))

    # 11. negative inventory lots
    neg = [{"itemNo": item_of.get(key(r.get("ProductID"))) or s(r.get("ProductID")), "location": s(r.get("Location")) or None,
            "masterPackNo": s(r.get("MPackNo")) or None, "unitNo": s(r.get("UnitNo")) or None,
            "receiptQty": num(r.get("WarehouseReceiptQty")), "shippedQty": num(r.get("OrderShippedQty"))}
           for r in inv if num(r.get("OrderShippedQty")) > num(r.get("WarehouseReceiptQty"))]
    checks.append(_check("negative-lots", "Inventory lots that shipped more than was received",
                         "On-hand goes below zero; the lot was picked from the wrong row or received short.", neg, samples))

    # 12. PO lines received over ordered
    po_over = [{"poId": s(l.get("PurchaseOrderID")), "itemNo": item_of.get(key(l.get("ProductID"))) or s(l.get("ProductID")),
                "orderedQty": num(l.get("PurQty")), "shippedQty": num(l.get("PurShippedQty"))}
               for l in pol if num(l.get("PurShippedQty")) > num(l.get("PurQty"))]
    po_over.sort(key=lambda x: x["poId"], reverse=True)
    checks.append(_check("po-shipped-over-ordered", "PO lines where the vendor shipped more than ordered",
                         "Over-shipment or a typo in PurShippedQty.", po_over, samples))

    return {"asOf": day(now), "problems": sum(1 for c in checks if c["count"]),
            "checks": checks}
