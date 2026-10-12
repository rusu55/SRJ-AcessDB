"""
Quality / ISO 9001 data from Access - read-only.

  GET /on-time            customer on-time shipping: ship date vs required date (ISO 9.1)
  GET /serial/{serial}    traceability for one serial / lot number: vendor,
                          container, put-away lot, and the order it went out on (ISO 8.5.2)

On time = the line shipped on or before its RequiredDate. Lines without a
required date are counted apart and left out of the percentage.
"""

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from middleware.auth import verify_api_key
from routes.report_utils import (
    customers, day, db, dt, fetch, fetch_by, fetch_in, key, num, products, s, suppliers, warehouses, window, window_out,
)

router = APIRouter()
AUTH = [Depends(verify_api_key)]

OTD_GROUPS = ("month", "customer", "item", "warehouse")


@router.get("/on-time", dependencies=AUTH)
def on_time(
    since: Optional[str] = Query(None, description="Ship date from (YYYY-MM-DD), default 365 days ago"),
    until: Optional[str] = Query(None, description="Ship date before (YYYY-MM-DD), default tomorrow"),
    group_by: str = Query("month", description="|".join(OTD_GROUPS)),
    late_lines: bool = Query(False, description="Also list every late line"),
):
    """Example: GET /api/quality/on-time?since=2026-01-01&group_by=customer"""
    by = (group_by or "").lower()
    if by not in OTD_GROUPS:
        raise HTTPException(status_code=400, detail=f"group_by must be one of: {', '.join(OTD_GROUPS)}")
    d0, d1 = window(since, until, default_days=365, max_days=3700)
    with db() as cur:
        lines = fetch(cur, "SELECT OrderID, ProductID, RequiredDate, ShippingID, ShippingDate, OrderShippedQty, WarehouseID "
                           "FROM [Order Details] WHERE ShippingDate >= ? AND ShippingDate < ?", (d0, d1))
        cust_of = {key(o["OrderID"]): s(o.get("CustomerID")) or None for o in fetch_by(
            cur, "SELECT OrderID, CustomerID FROM Orders", "OrderID", [s(l["OrderID"]) for l in lines])}
        names, prods, whs = customers(cur), products(cur), warehouses(cur)

    def blank():
        return {"lines": 0, "onTime": 0, "late": 0, "noDate": 0, "daysLateSum": 0}

    total, groups, ships, late_list = blank(), {}, {}, []
    for l in lines:
        sd, rd = dt(l.get("ShippingDate")), dt(l.get("RequiredDate"))
        cust = cust_of.get(key(l["OrderID"]))
        item = prods.get(key(l.get("ProductID")), {}).get("itemNo") or s(l.get("ProductID"))
        if by == "month":
            gk, label = sd.strftime("%Y-%m"), None
        elif by == "customer":
            gk, label = cust or "?", names.get(key(cust))
        elif by == "item":
            gk, label = item, None
        else:
            gk, label = s(l.get("WarehouseID")) or "(none)", whs.get(key(l.get("WarehouseID")))
        g = groups.setdefault(gk, {"key": gk, "label": label, **blank()})
        sid = s(l.get("ShippingID")) or f"{s(l['OrderID'])}@{day(sd)}"
        for b in (total, g):
            b["lines"] += 1
        if not rd:
            total["noDate"] += 1
            g["noDate"] += 1
            continue
        late_by = (sd.date() - rd.date()).days
        ok = late_by <= 0
        ships[sid] = ships.get(sid, True) and ok
        for b in (total, g):
            if ok:
                b["onTime"] += 1
            else:
                b["late"] += 1
                b["daysLateSum"] += late_by
        if not ok and late_lines:
            late_list.append({"orderId": s(l["OrderID"]), "shippingId": s(l.get("ShippingID")) or None,
                              "customerId": cust, "customerName": names.get(key(cust)), "itemNo": item,
                              "requiredDate": day(rd), "shippingDate": day(sd), "daysLate": late_by,
                              "qty": num(l.get("OrderShippedQty"))})

    def fin(b):
        rated = b["onTime"] + b["late"]
        out = {k: v for k, v in b.items() if k != "daysLateSum"}
        out["onTimePct"] = round(100 * b["onTime"] / rated, 1) if rated else None
        out["avgDaysLate"] = round(b["daysLateSum"] / b["late"], 1) if b["late"] else None
        return out
    rows = [fin(g) for g in groups.values()]
    rows.sort(key=(lambda r: r["key"]) if by == "month" else (lambda r: -r["lines"]))
    res = {**window_out(d0, d1), "groupBy": by, "totals": fin(total),
           "shipments": {"count": len(ships), "onTime": sum(1 for v in ships.values() if v),
                         "onTimePct": round(100 * sum(1 for v in ships.values() if v) / len(ships), 1) if ships else None},
           "rows": rows}
    if late_lines:
        late_list.sort(key=lambda x: -x["daysLate"])
        res["lateLines"] = late_list
    return res


_SPLIT = re.compile(r"[\s,;/]+")


@router.get("/serial/{serial}", dependencies=AUTH)
def serial_trace(serial: str):
    """Where a serial / production lot number came from and where it went.
    Example: GET /api/quality/serial/2510123CN"""
    sn = serial.strip()
    if len(sn) < 4:
        raise HTTPException(status_code=400, detail="serial is too short")
    target = sn.upper()
    with db() as cur:
        try:
            src = fetch(cur, "SELECT ProdSerialNoID, SupplierID, SupplierName, SupplierInvoiceNo, ProductID, ItemNo, "
                             "SupplierShippedDate, SRJReceiptDate FROM [Product Serial Numbers] WHERE ProdSerialNoID = ?",
                        (sn,))
        except Exception:  # noqa: BLE001 - optional table
            src = []
        lot_where = " OR ".join(f"ProdLotNo{i} = ?" for i in range(1, 41))
        lots = fetch(cur, "SELECT ProductID, Location, VendorInvNo, MPackNo, UnitNo, WarehouseReceiptDate, "
                          f"WarehouseReceiptQty, OrderShippedQty, ShippingDate, WarehouseID FROM Inventory WHERE {lot_where}",
                     [sn] * 40)
        sold = [l for l in fetch(cur, "SELECT OrderID, ProductID, ProdLotNoID, ShippingID, ShippingDate, OrderShippedQty, "
                                      "UnitNo, MCMake, MCModel FROM [Order Details] WHERE ProdLotNoID LIKE ?", (f"%{sn}%",))
                if target in {t.upper() for t in _SPLIT.split(s(l.get("ProdLotNoID"))) if t}]
        orders = {key(o["OrderID"]): o for o in fetch_in(
            cur, "SELECT OrderID, CustomerID, OrderDate, CustomerOrderNumber, ShipCity, ShipRegion FROM Orders "
                 "WHERE OrderID IN", [s(l["OrderID"]) for l in sold])}
        names, prods, sups = customers(cur), products(cur), suppliers(cur)
        containers = []
        inv_nos = {s(r.get("SupplierInvoiceNo")) for r in src} | {s(r.get("VendorInvNo")) for r in lots}
        inv_nos.discard("")
        if inv_nos:
            containers = fetch_in(cur, "SELECT VendorShippingID, SupplierID, ContainerNo, BLNo, ShippingDate, "
                                       "ETAVesselArrivalDate, ContainerReceiptDate FROM [Vendor Shipment] "
                                       "WHERE VendorShippingID IN", list(inv_nos))
    if not (src or lots or sold):
        raise HTTPException(status_code=404, detail=f"Serial {sn} not found in Access")
    return {
        "serial": sn,
        "source": [{"supplierId": r.get("SupplierID"), "supplier": s(r.get("SupplierName")) or sups.get(key(r.get("SupplierID"))),
                    "vendorInvoiceNo": s(r.get("SupplierInvoiceNo")) or None, "itemNo": s(r.get("ItemNo")) or None,
                    "vendorShippedDate": day(r.get("SupplierShippedDate")), "receivedDate": day(r.get("SRJReceiptDate"))}
                   for r in src],
        "containers": [{"vendorShippingId": s(c.get("VendorShippingID")), "supplier": sups.get(key(c.get("SupplierID"))),
                        "containerNo": s(c.get("ContainerNo")) or None, "blNo": s(c.get("BLNo")) or None,
                        "shippedDate": day(c.get("ShippingDate")), "etaPort": day(c.get("ETAVesselArrivalDate")),
                        "receiptDate": day(c.get("ContainerReceiptDate"))} for c in containers],
        "lots": [{"itemNo": prods.get(key(r.get("ProductID")), {}).get("itemNo") or None,
                  "location": s(r.get("Location")) or None, "masterPackNo": s(r.get("MPackNo")) or None,
                  "unitNo": s(r.get("UnitNo")) or None, "vendorInvNo": s(r.get("VendorInvNo")) or None,
                  "receiptDate": day(r.get("WarehouseReceiptDate")), "receiptQty": num(r.get("WarehouseReceiptQty")),
                  "shippedQty": num(r.get("OrderShippedQty")), "warehouseId": s(r.get("WarehouseID")) or None}
                 for r in lots],
        "soldOn": [{"orderId": s(l["OrderID"]),
                    "orderDate": day(orders.get(key(l["OrderID"]), {}).get("OrderDate")),
                    "customerId": s(orders.get(key(l["OrderID"]), {}).get("CustomerID")) or None,
                    "customerName": names.get(key(orders.get(key(l["OrderID"]), {}).get("CustomerID"))),
                    "customerPo": s(orders.get(key(l["OrderID"]), {}).get("CustomerOrderNumber")) or None,
                    "itemNo": prods.get(key(l.get("ProductID")), {}).get("itemNo") or None,
                    "shippingId": s(l.get("ShippingID")) or None, "shippingDate": day(l.get("ShippingDate")),
                    "qty": num(l.get("OrderShippedQty")), "unitNo": s(l.get("UnitNo")) or None,
                    "machine": " ".join(x for x in (s(l.get("MCMake")), s(l.get("MCModel"))) if x) or None}
                   for l in sold],
    }
