"""
Purchasing - suppliers, PO lines and containers (Vendor Shipment) - read-only.

  GET /suppliers                       suppliers with PO and container totals
  GET /po-lines                        every PO line (open and closed) for POs dated in a window
  GET /containers                      containers by vendor ship date, with status and paperwork
  GET /containers/{vendor_shipping_id} one container: lines, received qty, serials

A "container" is a [Vendor Shipment] row (VendorShippingID, e.g.
JLLM26260B). Its lines are [Vendor Shipment Details] (PO, item, qty, price);
what was put away is Inventory rows whose VendorInvNo is that id.
Note: [Vendor Shipment Details].ItemNo holds the ProductID (e.g. "4700"),
not the item code - _item() turns it into the code.
"""

from datetime import timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from middleware.auth import verify_api_key
from routes.report_utils import (
    day, db, dt, fetch, fetch_in, key, money, num, products, s, suppliers, today, window, window_out,
)

router = APIRouter()
AUTH = [Depends(verify_api_key)]


@router.get("/suppliers", dependencies=AUTH)
def supplier_list(q: Optional[str] = Query(None, description="Text in name, city, country or contact")):
    """Example: GET /api/purchasing/suppliers?q=jiangxi"""
    with db() as cur:
        sups = fetch(cur, "SELECT SupplierID, SupplierName, City, Province, Country, ContactPerson, EmailAddress, "
                          "PhoneNumber FROM Suppliers")
        heads = fetch(cur, "SELECT PurchaseOrderID, SupplierID, PODate FROM [Purchase Orders]")
        lines = fetch(cur, "SELECT PurchaseOrderID, PurQty, PurShippedQty, PurUnitPrice FROM [Purchase Order Details]")
        conts = fetch(cur, "SELECT VendorShippingID, SupplierID, ShippingDate FROM [Vendor Shipment]")
    sup_of_po = {key(h["PurchaseOrderID"]): key(h.get("SupplierID")) for h in heads}
    st: Dict[str, Dict[str, Any]] = {}

    def g(k):
        return st.setdefault(k, {"pos": 0, "lastPoDate": None, "poValue": 0.0, "openLines": 0, "openValue": 0.0,
                                 "containers": 0, "lastContainerShipped": None})
    for h in heads:
        x = g(key(h.get("SupplierID")))
        x["pos"] += 1
        d = dt(h.get("PODate"))
        if d and (x["lastPoDate"] is None or d > x["lastPoDate"]):
            x["lastPoDate"] = d
    for l in lines:
        x = g(sup_of_po.get(key(l.get("PurchaseOrderID")), ""))
        x["poValue"] += num(l.get("PurQty")) * num(l.get("PurUnitPrice"))
        open_qty = num(l.get("PurQty")) - num(l.get("PurShippedQty"))
        if open_qty > 0:
            x["openLines"] += 1
            x["openValue"] += open_qty * num(l.get("PurUnitPrice"))
    for c in conts:
        x = g(key(c.get("SupplierID")))
        x["containers"] += 1
        d = dt(c.get("ShippingDate"))
        if d and (x["lastContainerShipped"] is None or d > x["lastContainerShipped"]):
            x["lastContainerShipped"] = d
    needle = (q or "").strip().lower()
    out = []
    for r in sups:
        row = {"supplierId": r.get("SupplierID"), "name": s(r.get("SupplierName")) or None,
               "city": s(r.get("City")) or None, "province": s(r.get("Province")) or None,
               "country": s(r.get("Country")) or None, "contact": s(r.get("ContactPerson")) or None,
               "email": s(r.get("EmailAddress")) or None, "phone": s(r.get("PhoneNumber")) or None}
        if needle and not any(needle in (row.get(f) or "").lower() for f in ("name", "city", "country", "contact")):
            continue
        x = g(key(r.get("SupplierID")))
        row.update({"pos": x["pos"], "lastPoDate": day(x["lastPoDate"]), "poValue": money(x["poValue"]),
                    "openLines": x["openLines"], "openValue": money(x["openValue"]), "containers": x["containers"],
                    "lastContainerShipped": day(x["lastContainerShipped"])})
        out.append(row)
    out.sort(key=lambda r: (r["lastPoDate"] or ""), reverse=True)
    return {"count": len(out), "suppliers": out}


@router.get("/po-lines", dependencies=AUTH)
def po_lines_window(
    since: Optional[str] = Query(None, description="PO date from (YYYY-MM-DD), default 365 days ago"),
    until: Optional[str] = Query(None, description="PO date before (YYYY-MM-DD), default tomorrow"),
    supplier_id: Optional[int] = None,
):
    """Every line of every PO dated in the window, header fields included -
    one call instead of /api/po/all + /api/po/details per PO.
    Example: GET /api/purchasing/po-lines?since=2026-01-01&supplier_id=155"""
    d0, d1 = window(since, until, default_days=365, max_days=3700)
    with db() as cur:
        sql = ("SELECT PurchaseOrderID, SupplierID, PODate, WarehouseID, ShipVia, PriceTerm, PaymentTerm "
               "FROM [Purchase Orders] WHERE PODate >= ? AND PODate < ?")
        params: List[Any] = [d0, d1]
        if supplier_id is not None:
            sql += " AND SupplierID = ?"
            params.append(supplier_id)
        heads = {key(h["PurchaseOrderID"]): h for h in fetch(cur, sql, params)}
        lines = fetch_in(cur, "SELECT PurchaseOrderID, ProductID, PurDueShip, PurQty, PurUnitPrice, PackID, "
                              "PurShippedDate, PurShippedQty, VendorShippingID FROM [Purchase Order Details] "
                              "WHERE PurchaseOrderID IN", [h["PurchaseOrderID"] for h in heads.values()])
        prods, sups = products(cur), suppliers(cur)
    out = []
    for l in lines:
        h = heads.get(key(l.get("PurchaseOrderID")), {})
        p = prods.get(key(l.get("ProductID")), {})
        open_qty = max(0.0, num(l.get("PurQty")) - num(l.get("PurShippedQty")))
        out.append({
            "poId": s(l.get("PurchaseOrderID")), "poDate": day(h.get("PODate")),
            "supplierId": h.get("SupplierID"), "supplier": sups.get(key(h.get("SupplierID"))),
            "warehouseId": s(h.get("WarehouseID")) or None, "priceTerm": s(h.get("PriceTerm")) or None,
            "productId": s(l.get("ProductID")), "itemNo": p.get("itemNo") or None, "itemName": p.get("name"),
            "orderedQty": num(l.get("PurQty")), "shippedQty": num(l.get("PurShippedQty")), "openQty": open_qty,
            "unitPrice": num(l.get("PurUnitPrice")), "lineValue": money(num(l.get("PurQty")) * num(l.get("PurUnitPrice"))),
            "dueShipDate": day(l.get("PurDueShip")), "shippedDate": day(l.get("PurShippedDate")),
            "vendorShippingId": s(l.get("VendorShippingID")) or None, "packId": s(l.get("PackID")) or None,
            "open": open_qty > 0,
        })
    out.sort(key=lambda x: (x["poDate"] or "", x["poId"]), reverse=True)
    return {**window_out(d0, d1), "pos": len(heads), "count": len(out),
            "value": money(sum(x["lineValue"] for x in out)), "lines": out}


_CONTAINER_COLS = ("VendorShippingID, SupplierID, InvAmount, PackingCost, NoPacks, ShippingLine, FreightForwarderName, "
                   "VesselName, BLNo, ContainerNo, ContainerSize, FreightCharge, FreightHandlingCharge, "
                   "FreightOtherCharges, LoadingPort, ShippingDate, UnloadingPort, ETAVesselArrivalDate, "
                   "ContainerDischargedDate, OnRailDate, FinalDestination, ContainerReceiptDate, "
                   "NewInventoryInput, NewInventoryDate, NameNewInventoryInput, QuickBooksInput, QuickBooksInputDate, "
                   "NameQuickBooksInput, ProdLotNoInput, ProdLotNoInputDate, NameProdLotNoInput, POUpdate, "
                   "POUpdateDate, NamePOUpdate")


def _item(v: Any, prods: Dict[str, Any]) -> Optional[str]:
    """[Vendor Shipment Details].ItemNo is really a ProductID."""
    return prods.get(key(v), {}).get("itemNo") or s(v) or None


def _container(r: Dict[str, Any], sups: Dict[str, Any]) -> Dict[str, Any]:
    now = today()
    shipped, eta, received = dt(r.get("ShippingDate")), dt(r.get("ETAVesselArrivalDate")), dt(r.get("ContainerReceiptDate"))
    if received and received <= now:
        status = "received"
    elif eta and eta <= now:
        status = "at port / inland"
    elif shipped and shipped <= now:
        status = "on the water"
    else:
        status = "booked"

    def step(flag, d, who):
        return {"done": bool(r.get(flag)), "date": day(r.get(d)), "by": s(r.get(who)) or None}
    freight = num(r.get("FreightCharge")) + num(r.get("FreightHandlingCharge")) + num(r.get("FreightOtherCharges"))
    return {
        "vendorShippingId": s(r.get("VendorShippingID")),
        "supplierId": r.get("SupplierID"), "supplier": sups.get(key(r.get("SupplierID"))),
        "status": status,
        "containerNo": s(r.get("ContainerNo")) or None, "containerSize": s(r.get("ContainerSize")) or None,
        "blNo": s(r.get("BLNo")) or None, "vessel": s(r.get("VesselName")) or None,
        "shippingLine": s(r.get("ShippingLine")) or None, "forwarder": s(r.get("FreightForwarderName")) or None,
        "loadingPort": s(r.get("LoadingPort")) or None, "unloadingPort": s(r.get("UnloadingPort")) or None,
        "finalDestination": s(r.get("FinalDestination")) or None,
        "packs": s(r.get("NoPacks")) or None,
        "shippedDate": day(shipped), "etaPort": day(eta), "dischargedDate": day(r.get("ContainerDischargedDate")),
        "onRailDate": day(r.get("OnRailDate")), "receiptDate": day(received),
        "transitDays": (received - shipped).days if received and shipped and received <= now else None,
        "daysAtSea": (now - shipped).days if status == "on the water" and shipped else None,
        "invoiceAmount": money(num(r.get("InvAmount"))), "packingCost": money(num(r.get("PackingCost"))),
        "freight": money(freight),
        "paperwork": {
            "newInventory": step("NewInventoryInput", "NewInventoryDate", "NameNewInventoryInput"),
            "quickBooks": step("QuickBooksInput", "QuickBooksInputDate", "NameQuickBooksInput"),
            "lotNumbers": step("ProdLotNoInput", "ProdLotNoInputDate", "NameProdLotNoInput"),
            "poUpdate": step("POUpdate", "POUpdateDate", "NamePOUpdate"),
        },
    }


@router.get("/containers", dependencies=AUTH)
def containers(
    since: Optional[str] = Query(None, description="Vendor ship date from (YYYY-MM-DD), default 180 days ago"),
    until: Optional[str] = Query(None, description="Vendor ship date before (YYYY-MM-DD), default 120 days ahead"),
    status: Optional[str] = Query(None, description="booked | on the water | at port / inland | received"),
    supplier_id: Optional[int] = None,
    with_lines: bool = Query(False, description="Include each container's PO / item lines"),
):
    """Example: GET /api/purchasing/containers?status=on%20the%20water"""
    d0, d1 = window(since or (today() - timedelta(days=180)).strftime("%Y-%m-%d"),
                    until or (today() + timedelta(days=120)).strftime("%Y-%m-%d"), max_days=3700)
    with db() as cur:
        sql = f"SELECT {_CONTAINER_COLS} FROM [Vendor Shipment] WHERE ShippingDate >= ? AND ShippingDate < ?"
        params: List[Any] = [d0, d1]
        if supplier_id is not None:
            sql += " AND SupplierID = ?"
            params.append(supplier_id)
        rows = fetch(cur, sql, params)
        sups, prods = suppliers(cur), products(cur)
        det = fetch_in(cur, "SELECT VendorShippingID, WarehouseID, PurchaseOrderID, ItemNo, InvShippedQty, InvUnitPrice "
                            "FROM [Vendor Shipment Details] WHERE VendorShippingID IN",
                       [s(r["VendorShippingID"]) for r in rows])
    by_c: Dict[str, List[Dict[str, Any]]] = {}
    for d in det:
        by_c.setdefault(key(d["VendorShippingID"]), []).append(d)
    out = []
    for r in rows:
        c = _container(r, sups)
        if status and c["status"] != status.strip().lower():
            continue
        ls = by_c.get(key(c["vendorShippingId"]), [])
        c["poIds"] = sorted({s(l.get("PurchaseOrderID")) for l in ls if l.get("PurchaseOrderID") is not None})
        c["lineCount"] = len(ls)
        c["qty"] = sum(num(l.get("InvShippedQty")) for l in ls)
        if with_lines:
            c["lines"] = [{"poId": s(l.get("PurchaseOrderID")) or None, "itemNo": _item(l.get("ItemNo"), prods),
                           "qty": num(l.get("InvShippedQty")), "unitPrice": num(l.get("InvUnitPrice")),
                           "warehouseId": s(l.get("WarehouseID")) or None} for l in ls]
        out.append(c)
    out.sort(key=lambda c: (c["shippedDate"] or "", c["vendorShippingId"]), reverse=True)
    counts: Dict[str, int] = {}
    for c in out:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    return {**window_out(d0, d1), "count": len(out), "byStatus": counts, "containers": out}


@router.get("/containers/{vendor_shipping_id}", dependencies=AUTH)
def container_detail(vendor_shipping_id: str):
    """One container: header, invoiced lines, what was received into
    Inventory against it (VendorInvNo) per item, and serial numbers recorded.
    Example: GET /api/purchasing/containers/JLLM26260B"""
    vid = vendor_shipping_id.strip()
    with db() as cur:
        rows = fetch(cur, f"SELECT {_CONTAINER_COLS} FROM [Vendor Shipment] WHERE VendorShippingID = ?", (vid,))
        if not rows:
            raise HTTPException(status_code=404, detail=f"Container {vid} not found")
        sups = suppliers(cur)
        det = fetch(cur, "SELECT VendorShippingID, WarehouseID, PurchaseOrderID, ItemNo, InvShippedQty, InvUnitPrice "
                         "FROM [Vendor Shipment Details] WHERE VendorShippingID = ?", (vid,))
        inv = fetch(cur, "SELECT ProductID, Location, MPackNo, UnitNo, WarehouseReceiptDate, WarehouseReceiptQty, "
                         "OrderShippedQty, WarehouseID FROM Inventory WHERE VendorInvNo = ?", (vid,))
        try:
            serials = fetch(cur, "SELECT ProdSerialNoID, ItemNo FROM [Product Serial Numbers] WHERE SupplierInvoiceNo = ?",
                            (vid,))
        except Exception:  # noqa: BLE001 - optional table
            serials = []
        prods = products(cur)
    c = _container(rows[0], sups)
    invoiced: Dict[str, float] = {}
    for d in det:
        code = key(_item(d.get("ItemNo"), prods))
        invoiced[code] = invoiced.get(code, 0.0) + num(d.get("InvShippedQty"))
    received: Dict[str, float] = {}
    for r in inv:
        code = key(prods.get(key(r.get("ProductID")), {}).get("itemNo")) or f"#{s(r.get('ProductID'))}"
        received[code] = received.get(code, 0.0) + num(r.get("WarehouseReceiptQty"))
    ser: Dict[str, int] = {}
    for r in serials:
        ser[key(r.get("ItemNo"))] = ser.get(key(r.get("ItemNo")), 0) + 1
    check = []
    for code in sorted(set(invoiced) | set(received)):
        inv_q, rec_q = invoiced.get(code, 0.0), received.get(code, 0.0)
        check.append({"itemNo": code, "invoicedQty": inv_q, "receivedQty": rec_q, "difference": rec_q - inv_q,
                      "serials": ser.get(code, 0)})
    c["lines"] = [{"poId": s(d.get("PurchaseOrderID")) or None, "itemNo": _item(d.get("ItemNo"), prods),
                   "qty": num(d.get("InvShippedQty")), "unitPrice": num(d.get("InvUnitPrice")),
                   "value": money(num(d.get("InvShippedQty")) * num(d.get("InvUnitPrice"))),
                   "warehouseId": s(d.get("WarehouseID")) or None} for d in det]
    c["receivedLots"] = [{"itemNo": prods.get(key(r.get("ProductID")), {}).get("itemNo") or None,
                          "location": s(r.get("Location")) or None, "masterPackNo": s(r.get("MPackNo")) or None,
                          "unitNo": s(r.get("UnitNo")) or None, "receiptDate": day(r.get("WarehouseReceiptDate")),
                          "receiptQty": num(r.get("WarehouseReceiptQty")), "shippedQty": num(r.get("OrderShippedQty")),
                          "warehouseId": s(r.get("WarehouseID")) or None} for r in inv]
    c["invoicedVsReceived"] = check
    c["serialCount"] = len(serials)
    return c
