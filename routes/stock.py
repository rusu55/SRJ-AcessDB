"""
Stock in Access Inventory - read-only.

  GET /by-location   on-hand qty per bin / location
  GET /lots          inventory lots by item, location, pallet or container
  GET /received      lots put away in a date window (receiving log)
  GET /reorder       items at or below their re-order level, with open PO / SO qty

On hand = WarehouseReceiptQty - OrderShippedQty on a lot (the same rule as
/api/inventory/all). Lots that are fully shipped are left out unless asked.
"""

from datetime import timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from middleware.auth import verify_api_key
from routes.report_utils import (
    day, db, dt, fetch, fetch_in, key, line_shipped, num, product_ids_for_item, products, s, today, window, window_out,
)

router = APIRouter()
AUTH = [Depends(verify_api_key)]

_LOT_COLS = ("ProductID, Location, VendorInvNo, MPackNo, UnitNo, WarehouseReceiptDate, WarehouseReceiptQty, "
             "WarehouseID, ShippingDate, OrderShippedQty, " + ", ".join(f"ProdLotNo{i}" for i in range(1, 41)))


def _lot(r: Dict[str, Any], prods: Dict[str, Any]) -> Dict[str, Any]:
    p = prods.get(key(r.get("ProductID")), {})
    rec, shp = num(r.get("WarehouseReceiptQty")), num(r.get("OrderShippedQty"))
    return {
        "productId": s(r.get("ProductID")), "itemNo": p.get("itemNo") or None, "itemName": p.get("name"),
        "location": s(r.get("Location")) or None, "warehouseId": s(r.get("WarehouseID")) or None,
        "masterPackNo": s(r.get("MPackNo")) or None, "unitNo": s(r.get("UnitNo")) or None,
        "vendorInvNo": s(r.get("VendorInvNo")) or None,
        "receiptDate": day(r.get("WarehouseReceiptDate")), "receiptQty": rec,
        "shippedQty": shp, "lastShipDate": day(r.get("ShippingDate")), "onHand": rec - shp,
        "lotNumbers": [s(r.get(f"ProdLotNo{i}")) for i in range(1, 41) if s(r.get(f"ProdLotNo{i}"))],
    }


@router.get("/by-location", dependencies=AUTH)
def by_location(
    q: Optional[str] = Query(None, description="Text in the location, e.g. '(IL) I2' or 'GA'"),
    warehouse_id: Optional[str] = None,
):
    """Example: GET /api/stock/by-location?q=(IL)"""
    with db() as cur:
        rows = fetch(cur, "SELECT ProductID, Location, WarehouseID, WarehouseReceiptQty, OrderShippedQty FROM Inventory")
        prods = products(cur)
    needle = (q or "").strip().lower()
    groups: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        on = num(r.get("WarehouseReceiptQty")) - num(r.get("OrderShippedQty"))
        if on == 0:
            continue
        loc = s(r.get("Location")) or "(no location)"
        if needle and needle not in loc.lower():
            continue
        if warehouse_id and key(r.get("WarehouseID")) != key(warehouse_id):
            continue
        g = groups.setdefault(loc, {"location": loc, "lots": 0, "items": set(), "qty": 0.0, "negativeLots": 0})
        g["lots"] += 1
        g["items"].add(key(prods.get(key(r.get("ProductID")), {}).get("itemNo")) or s(r.get("ProductID")))
        if on > 0:
            g["qty"] += on
        else:
            g["negativeLots"] += 1
    out = [{**g, "items": len(g["items"])} for g in groups.values()]
    out.sort(key=lambda g: g["location"])
    return {"count": len(out), "locations": out}


@router.get("/lots", dependencies=AUTH)
def lots(
    item_no: Optional[str] = None,
    location: Optional[str] = Query(None, description="Contains match"),
    pallet: Optional[str] = Query(None, description="Master pack no (MPackNo) or unit no (UnitNo), exact"),
    vendor_inv: Optional[str] = Query(None, description="Container / vendor invoice no (VendorInvNo), exact"),
    include_empty: bool = Query(False, description="Also lots already fully shipped"),
):
    """At least one filter is required. Example: GET /api/stock/lots?pallet=JL251022-49"""
    if not any((item_no, location, pallet, vendor_inv)):
        raise HTTPException(status_code=400, detail="give item_no, location, pallet or vendor_inv")
    with db() as cur:
        sql, params, where = f"SELECT {_LOT_COLS} FROM Inventory", [], []
        if pallet:
            where.append("(MPackNo = ? OR UnitNo = ?)")
            params += [pallet.strip(), pallet.strip()]
        if vendor_inv:
            where.append("VendorInvNo = ?")
            params.append(vendor_inv.strip())
        if location:
            where.append("Location LIKE ?")
            params.append(f"%{location.strip()}%")
        rows = fetch(cur, sql + (" WHERE " + " AND ".join(where) if where else ""), params)
        if item_no:
            pids = {key(p) for p in product_ids_for_item(cur, item_no)}
            rows = [r for r in rows if key(r.get("ProductID")) in pids]
        prods = products(cur)
    out = [_lot(r, prods) for r in rows]
    if not include_empty:
        out = [l for l in out if l["onHand"] != 0]
    out.sort(key=lambda l: (l["itemNo"] or "", l["receiptDate"] or ""))
    return {"count": len(out), "onHand": sum(l["onHand"] for l in out if l["onHand"] > 0), "lots": out}


@router.get("/received", dependencies=AUTH)
def received(
    since: Optional[str] = Query(None, description="Receipt date from (YYYY-MM-DD), default 30 days ago"),
    until: Optional[str] = Query(None, description="Receipt date before (YYYY-MM-DD), default tomorrow"),
):
    """Lots put away in the window, plus totals per container (VendorInvNo).
    Example: GET /api/stock/received?since=2026-09-01"""
    d0, d1 = window(since, until, default_days=30, max_days=800)
    with db() as cur:
        rows = fetch(cur, f"SELECT {_LOT_COLS} FROM Inventory WHERE WarehouseReceiptDate >= ? AND WarehouseReceiptDate < ?",
                     (d0, d1))
        prods = products(cur)
    out = [_lot(r, prods) for r in rows]
    out.sort(key=lambda l: (l["receiptDate"] or "", l["vendorInvNo"] or "", l["itemNo"] or ""))
    per: Dict[str, Dict[str, Any]] = {}
    for l in out:
        k = l["vendorInvNo"] or "(none)"
        g = per.setdefault(k, {"vendorInvNo": k, "lots": 0, "qty": 0.0, "items": set(), "firstDate": l["receiptDate"],
                               "lastDate": l["receiptDate"]})
        g["lots"] += 1
        g["qty"] += l["receiptQty"]
        g["items"].add(l["itemNo"] or l["productId"])
        g["lastDate"] = max(g["lastDate"] or "", l["receiptDate"] or "") or None
    per_out = sorted(({**g, "items": len(g["items"])} for g in per.values()), key=lambda g: g["lastDate"] or "",
                     reverse=True)
    return {**window_out(d0, d1), "count": len(out), "qty": sum(l["receiptQty"] for l in out),
            "byContainer": per_out, "lots": out}


@router.get("/reorder", dependencies=AUTH)
def reorder(all_items: bool = Query(False, description="Every item that has a re-order level, not only the short ones")):
    """Items whose stock + open PO qty - open SO qty is at or below the
    Products.ReOrderLevel (open SO lines on orders over a year old are not
    counted). Example: GET /api/stock/reorder"""
    with db() as cur:
        prods = products(cur)
        inv = fetch(cur, "SELECT ProductID, WarehouseReceiptQty, OrderShippedQty FROM Inventory")
        pol = fetch(cur, "SELECT ProductID, PurQty, PurShippedQty FROM [Purchase Order Details]")
        sol = fetch(cur, "SELECT OrderID, ProductID, OrderQty, ShippingID, ShippingDate, OrderShippedQty "
                         "FROM [Order Details] WHERE ShippingDate IS NULL")
        # open lines on orders older than a year are abandoned, not demand
        cutoff = today() - timedelta(days=365)
        old_orders = {key(o["OrderID"]) for o in fetch_in(cur, "SELECT OrderID, OrderDate FROM Orders WHERE OrderID IN",
                                                          [s(r["OrderID"]) for r in sol])
                      if dt(o.get("OrderDate")) and dt(o.get("OrderDate")) < cutoff}
    by: Dict[str, Dict[str, Any]] = {}

    def g(pid):
        p = prods.get(key(pid), {})
        code = key(p.get("itemNo")) or f"#{key(pid)}"
        return by.setdefault(code, {"itemNo": p.get("itemNo") or s(pid), "itemName": p.get("name"),
                                    "productLine": p.get("productLine"), "reorderLevel": 0.0, "onHand": 0.0,
                                    "onOrderPo": 0.0, "openSo": 0.0})
    for pid, p in prods.items():
        if num(p.get("reorderLevel")) > 0:
            x = g(pid)
            x["reorderLevel"] = max(x["reorderLevel"], num(p.get("reorderLevel")))
    for r in inv:
        on = num(r.get("WarehouseReceiptQty")) - num(r.get("OrderShippedQty"))
        if on > 0:
            g(r.get("ProductID"))["onHand"] += on
    for r in pol:
        op = num(r.get("PurQty")) - num(r.get("PurShippedQty"))
        if op > 0:
            g(r.get("ProductID"))["onOrderPo"] += op
    for r in sol:
        if not line_shipped(r) and key(r.get("OrderID")) not in old_orders:
            g(r.get("ProductID"))["openSo"] += num(r.get("OrderQty"))
    out = []
    for x in by.values():
        if x["reorderLevel"] <= 0:
            continue
        x["available"] = x["onHand"] + x["onOrderPo"] - x["openSo"]
        x["short"] = x["available"] <= x["reorderLevel"]
        x["suggestedQty"] = max(0.0, x["reorderLevel"] - x["available"]) if x["short"] else 0.0
        if all_items or x["short"]:
            out.append(x)
    out.sort(key=lambda x: (not x["short"], x["available"] - x["reorderLevel"]))
    return {"count": len(out), "short": sum(1 for x in out if x["short"]), "items": out}
