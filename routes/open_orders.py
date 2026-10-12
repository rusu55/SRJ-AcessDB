"""
Open (not yet shipped) sales and purchase order lines - read-only.

  GET /so-lines    unshipped customer order lines, late first, with stock cover
  GET /po-lines    PO lines the vendor still owes, overdue first
  GET /summary     the counts and values of both, for a dashboard tile

An SO line is open while it has no ship date, no ShippingID and nothing
shipped (the orders_write "locked" rule turned around). A PO line is open
while PurQty > PurShippedQty.

Very old open lines are almost always abandoned rather than real backlog, so
they are left out unless include_stale=true - the response still counts them
so nothing hides. Age is the order / PO date, or the required / due date when
the header has no date.
"""

from datetime import timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from middleware.auth import verify_api_key
from routes.report_utils import (
    customers, day, db, dt, fetch, fetch_by, key, line_shipped, money, num, on_hand_by_product,
    product_ids_for_item, products, s, suppliers, today, warehouses,
)

router = APIRouter()
AUTH = [Depends(verify_api_key)]


def _status(due, now) -> str:
    if not due:
        return "no date"
    if due < now:
        return "late"
    if due < now + timedelta(days=7):
        return "due this week"
    if due < now + timedelta(days=31):
        return "due this month"
    return "later"


def open_so_lines(cur, include_stale: bool, stale_days: int) -> Dict[str, Any]:
    raw = [l for l in fetch(cur, "SELECT OrderID, ProductID, OrderQty, OrderUnitPrice, RequiredDate, ShippingID, "
                                 "ShippingDate, OrderShippedQty, WarehouseID FROM [Order Details] "
                                 "WHERE ShippingDate IS NULL") if not line_shipped(l) and num(l.get("OrderQty")) > 0]
    orders = {key(o["OrderID"]): o for o in fetch_by(
        cur, "SELECT OrderID, CustomerID, OrderDate, CustomerOrderNumber, ShipCity, ShipRegion FROM Orders", "OrderID",
        [s(l["OrderID"]) for l in raw])}
    now = today()
    cutoff = now - timedelta(days=stale_days)
    prods, names, whs = products(cur), customers(cur), warehouses(cur)
    stock_pid = on_hand_by_product(cur)
    # stock is per item CODE: the same code can live on several ProductIDs
    stock_item: Dict[str, float] = {}
    for pid, q in stock_pid.items():
        code = key(prods.get(pid, {}).get("itemNo")) or f"#{pid}"
        stock_item[code] = stock_item.get(code, 0.0) + q
    lines, stale = [], 0
    for l in raw:
        o = orders.get(key(l["OrderID"]), {})
        od = dt(o.get("OrderDate"))
        # no order header / date (old orphans) -> judge age on the required date
        age = od or dt(l.get("RequiredDate"))
        is_stale = bool(age and age < cutoff)
        if is_stale:
            stale += 1
            if not include_stale:
                continue
        p = prods.get(key(l.get("ProductID")), {})
        code = key(p.get("itemNo")) or f"#{key(l.get('ProductID'))}"
        due = dt(l.get("RequiredDate"))
        qty = num(l.get("OrderQty"))
        cust = s(o.get("CustomerID")) or None
        lines.append({
            "orderId": s(l["OrderID"]),
            "orderDate": day(od),
            "customerId": cust,
            "customerName": names.get(key(cust)),
            "customerPo": s(o.get("CustomerOrderNumber")) or None,
            "shipTo": ", ".join(x for x in (s(o.get("ShipCity")), s(o.get("ShipRegion"))) if x) or None,
            "productId": s(l.get("ProductID")),
            "itemNo": p.get("itemNo") or None,
            "itemName": p.get("name"),
            "openQty": qty,
            "unitPrice": num(l.get("OrderUnitPrice")),
            "openValue": money(qty * num(l.get("OrderUnitPrice"))),
            "requiredDate": day(due),
            "daysLate": (now - due).days if due and due < now else 0,
            "status": _status(due, now),
            "warehouseId": s(l.get("WarehouseID")) or None,
            "warehouse": whs.get(key(l.get("WarehouseID"))),
            "stale": is_stale,
            "_code": code,
            "_due": due,
        })
    # Stock cover: hand the on-hand qty of each item to its open lines in
    # required-date order. "full" = this line can ship from stock today if the
    # earlier-due lines go first.
    lines.sort(key=lambda x: (x["_due"] is None, x["_due"] or now, x["orderId"]))
    left = dict(stock_item)
    for x in lines:
        have = left.get(x["_code"], 0.0)
        x["onHand"] = stock_item.get(x["_code"], 0.0)
        x["stockCover"] = "full" if have >= x["openQty"] else "partial" if have > 0 else "none"
        left[x["_code"]] = max(0.0, have - x["openQty"])
        del x["_code"], x["_due"]
    return {"lines": lines, "stale": stale}


def open_po_lines(cur, include_stale: bool, stale_days: int) -> Dict[str, Any]:
    raw = [l for l in fetch(cur, "SELECT PurchaseOrderID, ProductID, PurDueShip, PurQty, PurUnitPrice, PurShippedDate, "
                                 "PurShippedQty, VendorShippingID FROM [Purchase Order Details]")
           if num(l.get("PurQty")) - num(l.get("PurShippedQty")) > 0]
    heads = {key(h["PurchaseOrderID"]): h for h in fetch(
        cur, "SELECT PurchaseOrderID, SupplierID, PODate, WarehouseID, ShipVia FROM [Purchase Orders]")}
    now = today()
    cutoff = now - timedelta(days=stale_days)
    prods, sups = products(cur), suppliers(cur)
    lines, stale = [], 0
    for l in raw:
        h = heads.get(key(l.get("PurchaseOrderID")), {})
        pd = dt(h.get("PODate"))
        age = pd or dt(l.get("PurDueShip"))
        is_stale = bool(age and age < cutoff)
        if is_stale:
            stale += 1
            if not include_stale:
                continue
        p = prods.get(key(l.get("ProductID")), {})
        due = dt(l.get("PurDueShip"))
        open_qty = num(l.get("PurQty")) - num(l.get("PurShippedQty"))
        lines.append({
            "poId": s(l.get("PurchaseOrderID")),
            "poDate": day(pd),
            "supplierId": h.get("SupplierID"),
            "supplier": sups.get(key(h.get("SupplierID"))),
            "warehouseId": s(h.get("WarehouseID")) or None,
            "productId": s(l.get("ProductID")),
            "itemNo": p.get("itemNo") or None,
            "itemName": p.get("name"),
            "orderedQty": num(l.get("PurQty")),
            "shippedQty": num(l.get("PurShippedQty")),
            "openQty": open_qty,
            "unitPrice": num(l.get("PurUnitPrice")),
            "openValue": money(open_qty * num(l.get("PurUnitPrice"))),
            "dueShipDate": day(due),
            "daysOverdue": (now - due).days if due and due < now else 0,
            "status": _status(due, now),
            "lastShippedDate": day(l.get("PurShippedDate")),
            "vendorShippingId": s(l.get("VendorShippingID")) or None,
            "stale": is_stale,
        })
    lines.sort(key=lambda x: (x["dueShipDate"] is None, x["dueShipDate"] or "", x["poId"]))
    return {"lines": lines, "stale": stale}


@router.get("/so-lines", dependencies=AUTH)
def so_lines(
    customer_id: Optional[str] = None,
    item_no: Optional[str] = None,
    late_only: bool = Query(False, description="Only lines past their required date"),
    include_stale: bool = Query(False, description="Also lines on orders older than stale_days"),
    stale_days: int = Query(365, ge=30, le=5000),
):
    """Example: GET /api/open-orders/so-lines?late_only=true"""
    with db() as cur:
        res = open_so_lines(cur, include_stale, stale_days)
        lines = res["lines"]
        if customer_id:
            lines = [l for l in lines if key(l["customerId"]) == key(customer_id)]
        if item_no:
            pids = {key(p) for p in product_ids_for_item(cur, item_no)}
            lines = [l for l in lines if key(l["productId"]) in pids]
    if late_only:
        lines = [l for l in lines if l["status"] == "late"]
    return {"count": len(lines), "openValue": money(sum(l["openValue"] for l in lines)),
            "staleLinesLeftOut": 0 if include_stale else res["stale"], "lines": lines}


@router.get("/po-lines", dependencies=AUTH)
def po_lines(
    supplier_id: Optional[int] = None,
    item_no: Optional[str] = None,
    po_id: Optional[str] = None,
    overdue_only: bool = Query(False, description="Only lines past the promised ship date"),
    include_stale: bool = Query(False, description="Also lines on POs older than stale_days"),
    stale_days: int = Query(730, ge=30, le=5000),
):
    """All open PO lines in ONE call (instead of /api/po/all + one
    /api/po/details per PO). Example: GET /api/open-orders/po-lines?overdue_only=true"""
    with db() as cur:
        res = open_po_lines(cur, include_stale, stale_days)
        lines = res["lines"]
        if supplier_id is not None:
            lines = [l for l in lines if l["supplierId"] is not None and int(num(l["supplierId"])) == supplier_id]
        if po_id:
            lines = [l for l in lines if key(l["poId"]) == key(po_id)]
        if item_no:
            pids = {key(p) for p in product_ids_for_item(cur, item_no)}
            lines = [l for l in lines if key(l["productId"]) in pids]
    if overdue_only:
        lines = [l for l in lines if l["status"] == "late"]
    return {"count": len(lines), "openValue": money(sum(l["openValue"] for l in lines)),
            "staleLinesLeftOut": 0 if include_stale else res["stale"], "lines": lines}


def _tally(lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"lines": len(lines), "value": money(sum(l["openValue"] for l in lines))}
    for st in ("late", "due this week", "due this month", "later", "no date"):
        sub = [l for l in lines if l["status"] == st]
        out[st.replace(" ", "_")] = {"lines": len(sub), "value": money(sum(l["openValue"] for l in sub))}
    return out


@router.get("/summary", dependencies=AUTH)
def open_summary():
    """Example: GET /api/open-orders/summary"""
    with db() as cur:
        so = open_so_lines(cur, False, 365)
        po = open_po_lines(cur, False, 730)
    so_t = _tally(so["lines"])
    so_t["notCoveredByStock"] = sum(1 for l in so["lines"] if l["stockCover"] != "full")
    so_t["staleLinesLeftOut"] = so["stale"]
    po_t = _tally(po["lines"])
    po_t["staleLinesLeftOut"] = po["stale"]
    return {"asOf": day(today()), "salesOrders": so_t, "purchaseOrders": po_t}
