from fastapi import APIRouter, Depends, HTTPException
from database.connection import get_db_connection
from typing import Optional, List
from middleware.auth import verify_api_key
from datetime import datetime

router = APIRouter()


def fmt_date(val):
    if isinstance(val, datetime):
        return val.strftime('%m/%d/%Y')
    return val


def rows_to_dicts(cursor, rows):
    columns = [col[0] for col in cursor.description]
    result = []
    for row in rows:
        d = dict(zip(columns, row))
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.strftime('%m/%d/%Y')
        result.append(d)
    return result


def norm_code(val) -> str:
    """Canonical form of an item code.

    Access compares text case-insensitively and ignores TRAILING spaces, so
    'JL-1', 'jl-1' and 'JL-1  ' are one item as far as a WHERE clause is
    concerned. Python dict keys are not so forgiving - grouping on the raw
    value silently splits one item into several products, and the /item
    endpoint then returns only the first slice. Everything that groups or
    matches an item code goes through here.
    """
    return ("" if val is None else str(val)).strip().upper()


def shape_lot(row: dict) -> dict:
    """Reshape a raw Inventory row into a clean lot object."""
    lot_numbers = [
        row[f"ProdLotNo{i}"]
        for i in range(1, 41)
        if row.get(f"ProdLotNo{i}")
    ]
    return {
        "master_pack_no":   row.get("MPackNo"),
        "vendor_invoice_no": row.get("VendorInvNo"),
        "unit_no":          row.get("UnitNo"),
        "location":         row.get("Location"),
        "warehouse_id":     row.get("WarehouseID"),
        "receipt_date":     row.get("WarehouseReceiptDate"),
        "receipt_qty":      row.get("WarehouseReceiptQty") or 0,
        "shipped_qty":      row.get("OrderShippedQty") or 0,
        "qty_on_hand":      (row.get("WarehouseReceiptQty") or 0) - (row.get("OrderShippedQty") or 0),
        "lot_numbers":      lot_numbers,
    }


def shape_po(row: dict, supplier_map: dict) -> dict:
    """Reshape a raw Purchase Order Details row into a clean PO object."""
    pur_qty = row.get("PurQty") or 0
    shipped_qty = row.get("PurShippedQty") or 0
    return {
        "purchase_order_id": row.get("PurchaseOrderID"),
        "po_date":           row.get("PODate"),
        "due_ship_date":     row.get("PurDueShip"),
        "supplier":          supplier_map.get(row.get("SupplierID"), ""),
        "ordered_qty":       pur_qty,
        "shipped_qty":       shipped_qty,
        "open_qty":          pur_qty - shipped_qty,
        "unit_price":        row.get("PurUnitPrice"),
    }


def shape_so(row: dict, customer_map: dict) -> dict:
    """Reshape a raw Order Details row into a clean SO object."""
    return {
        "order_id":       row.get("OrderID"),
        "order_date":     row.get("OrderDate"),
        "required_date":  row.get("RequiredDate"),
        "customer":       customer_map.get(row.get("CustomerID"), ""),
        "order_qty":      row.get("OrderQty") or 0,
        "unit_price":     row.get("OrderUnitPrice"),
        "shipped_qty":    row.get("OrderShippedQty") or 0,
        "shipping_date":  row.get("ShippingDate"),
        "warehouse_id":   row.get("WarehouseID"),
    }


def resolve_product_ids(cursor, item_no: str) -> List[dict]:
    """Every Products row whose ItemNo canonicalises to item_no.

    Resolving IDs first, then filtering on ProductID, means a code stored with
    a leading space or in a different case is still found - `WHERE p.ItemNo = ?`
    would miss the leading-space variant entirely, and split the others into
    separate groups downstream.
    """
    target = norm_code(item_no)
    cursor.execute("SELECT ProductID, ItemNo FROM Products")
    return [
        {"product_id": r[0], "item_no": r[1]}
        for r in cursor.fetchall()
        if norm_code(r[1]) == target
    ]


def build_inventory_status(cursor, item_no: Optional[str] = None):
    where_inv = where_po = where_so = ""
    params: list = []

    if item_no:
        matches = resolve_product_ids(cursor, item_no)
        if not matches:
            raise HTTPException(
                status_code=404, detail=f"No data found for product '{item_no}'")
        ids = [m["product_id"] for m in matches]
        marks = ",".join("?" for _ in ids)
        where_inv = f"WHERE i.ProductID IN ({marks})"
        where_po = f"WHERE pod.ProductID IN ({marks})"
        where_so = f"WHERE od.ProductID IN ({marks})"
        params = list(ids)

    # -- 1. Inventory lots --
    cursor.execute(f"""
        SELECT i.*, p.ItemNo
        FROM Inventory i
        INNER JOIN Products p ON i.ProductID = p.ProductID
        {where_inv}
        ORDER BY p.ItemNo, i.WarehouseReceiptDate
    """, params)
    inv_rows = rows_to_dicts(cursor, cursor.fetchall())

    products = {}

    def bucket(row, product_id):
        """Group on the CANONICAL code, keep the first raw spelling for display."""
        raw = ("" if row.get("ItemNo") is None else str(row.get("ItemNo"))).strip()
        key = norm_code(raw) or f"#PRODUCTID:{product_id}"
        if key not in products:
            products[key] = {
                "item_no":     raw or str(product_id),
                "product_id":  product_id,
                "product_ids": [],
                "_inv_lots":   [],
                "_po_rows":    [],
                "_so_rows":    [],
            }
        p = products[key]
        if product_id is not None and product_id not in p["product_ids"]:
            p["product_ids"].append(product_id)
        return p

    for row in inv_rows:
        bucket(row, row.get("ProductID"))["_inv_lots"].append(row)

    # -- 2. Open Purchase Orders --
    cursor.execute(f"""
        SELECT pod.*, po.SupplierID, po.PODate, p.ItemNo
        FROM ([Purchase Order Details] pod
        INNER JOIN [Purchase Orders] po ON pod.PurchaseOrderID = po.PurchaseOrderID)
        INNER JOIN Products p ON pod.ProductID = p.ProductID
        {where_po}
        ORDER BY p.ItemNo, pod.PurDueShip
    """, params)
    po_rows = rows_to_dicts(cursor, cursor.fetchall())

    cursor.execute("SELECT SupplierID, SupplierName FROM Suppliers")
    supplier_map = {r[0]: r[1] for r in cursor.fetchall()}

    for row in po_rows:
        open_qty = (row.get("PurQty") or 0) - (row.get("PurShippedQty") or 0)
        if open_qty <= 0:
            continue
        bucket(row, row.get("ProductID"))["_po_rows"].append(row)

    # -- 3. Open Sales Orders --
    cursor.execute(f"""
        SELECT od.*, o.CustomerID, o.OrderDate, p.ItemNo
        FROM ([Order Details] od
        INNER JOIN Orders o ON od.OrderID = o.OrderID)
        INNER JOIN Products p ON od.ProductID = p.ProductID
        {where_so}
        ORDER BY p.ItemNo, od.RequiredDate
    """, params)
    so_rows = rows_to_dicts(cursor, cursor.fetchall())

    cursor.execute("SELECT CustomerID, CompanyName FROM Customers")
    customer_map = {r[0]: r[1] for r in cursor.fetchall()}

    for row in so_rows:
        bucket(row, row.get("ProductID"))["_so_rows"].append(row)

    # -- Shape final response --
    result = []
    for key, p in products.items():
        lots = [shape_lot(r) for r in p["_inv_lots"]
                if (r.get("WarehouseReceiptQty") or 0) != (r.get("OrderShippedQty") or 0)]
        pos = [shape_po(r, supplier_map) for r in p["_po_rows"]]
        sos = [shape_so(r, customer_map) for r in p["_so_rows"]]

        negative = [l for l in lots if l["qty_on_hand"] < 0]

        result.append({
            "product": {
                "item_no":     p["item_no"],
                "product_id":  p["product_id"],
                "product_ids": p["product_ids"],
            },
            "inventory": {
                "total_qty": sum(l["qty_on_hand"] for l in lots if l["qty_on_hand"] > 0),
                "lot_count": len(lots),
                # shipped > received on these rows; excluded from total_qty, but
                # a silent exclusion is how a total drifts unnoticed.
                "negative_lot_count": len(negative),
                "negative_qty": sum(l["qty_on_hand"] for l in negative),
                "lots":      lots,
            },
            "open_purchase_orders": {
                "total_qty": sum(po["open_qty"] for po in pos),
                "orders":    pos,
            },
            "open_sales_orders": {
                "total_qty": sum(so["order_qty"] for so in sos),
                "orders":    sos,
            },
        })

    if not result and item_no:
        raise HTTPException(status_code=404, detail=f"No data found for product '{item_no}'")
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/all", dependencies=[Depends(verify_api_key)])
def get_all_inventory_status():
    """
    Get inventory status for ALL products.
    Example: GET /api/inventory/all
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        result = build_inventory_status(cursor)
        return {"count": len(result), "inventory": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.get("/item/{item_no}", dependencies=[Depends(verify_api_key)])
def get_inventory_status_by_item(item_no: str):
    """
    Get inventory status for a specific product by its item code.
    Example: GET /api/inventory/item/150x67x48SBA
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        result = build_inventory_status(cursor, item_no=item_no)
        if not result:
            return {}
        if len(result) == 1:
            return result[0]
        # Should not happen now that grouping is canonicalised, but returning
        # result[0] and dropping the rest is exactly the bug this replaces.
        merged = result[0]
        for extra in result[1:]:
            merged["product"]["product_ids"] += extra["product"]["product_ids"]
            for section in ("inventory", "open_purchase_orders", "open_sales_orders"):
                merged[section]["total_qty"] += extra[section]["total_qty"]
            merged["inventory"]["lots"] += extra["inventory"]["lots"]
            merged["inventory"]["lot_count"] += extra["inventory"]["lot_count"]
            merged["open_purchase_orders"]["orders"] += extra["open_purchase_orders"]["orders"]
            merged["open_sales_orders"]["orders"] += extra["open_sales_orders"]["orders"]
        merged["warning"] = f"{len(result)} product groups merged for '{item_no}'"
        return merged
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.get("/debug/trace/{item_no}", dependencies=[Depends(verify_api_key)])
def debug_trace_item(item_no: str):
    """Why does this item total what it totals?

    Lists every Inventory row for the item with no filtering at all, and
    replays the OLD grouping algorithm alongside the new one, so the exact
    rows that used to be dropped are named rather than inferred.

    Example: GET /api/inventory/debug/trace/230x44x72SWW
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        target = norm_code(item_no)
        matches = resolve_product_ids(cursor, item_no)
        if not matches:
            raise HTTPException(status_code=404, detail=f"No Products row matches '{item_no}'")

        ids = [m["product_id"] for m in matches]
        marks = ",".join("?" for _ in ids)
        cursor.execute(f"""
            SELECT i.*, p.ItemNo
            FROM Inventory i
            INNER JOIN Products p ON i.ProductID = p.ProductID
            WHERE i.ProductID IN ({marks})
            ORDER BY p.ItemNo, i.WarehouseReceiptDate
        """, ids)
        cols = [c[0] for c in cursor.description]
        dupe_cols = sorted({c for c in cols if cols.count(c) > 1})
        rows = rows_to_dicts(cursor, cursor.fetchall())

        # --- replay the OLD algorithm ---------------------------------------
        # `WHERE p.ItemNo = ?` under the Access engine: case-insensitive and
        # trailing spaces ignored, but a LEADING space is a real difference.
        def access_eq(a, b):
            return ("" if a is None else str(a)).rstrip().upper() == \
                   ("" if b is None else str(b)).rstrip().upper()

        old_groups: dict = {}
        for r in rows:
            if not access_eq(r.get("ItemNo"), item_no):
                continue                      # the old WHERE never saw this row
            k = r.get("ItemNo") or str(r["ProductID"])
            old_groups.setdefault(k, []).append(r)
        old_first_key = next(iter(old_groups), None)
        old_rows = old_groups.get(old_first_key, [])

        def net(r):
            return (r.get("WarehouseReceiptQty") or 0) - (r.get("OrderShippedQty") or 0)

        def total(rs):
            return sum(net(r) for r in rs if net(r) > 0)

        old_keys = {id(r) for r in old_rows}

        lots = []
        for r in rows:
            lots.append({
                "master_pack_no":  r.get("MPackNo"),
                "product_id":      r.get("ProductID"),
                "item_no_raw":     r.get("ItemNo"),
                "item_no_repr":    repr(r.get("ItemNo")),
                "receipt_date":    r.get("WarehouseReceiptDate"),
                "location":        r.get("Location"),
                "warehouse_id":    r.get("WarehouseID"),
                "receipt_qty":     r.get("WarehouseReceiptQty") or 0,
                "shipped_qty":     r.get("OrderShippedQty") or 0,
                "qty_on_hand":     net(r),
                "counted_by_old_code": id(r) in old_keys and net(r) > 0,
                "counted_now":     net(r) > 0,
            })

        dropped = [l for l in lots if l["counted_now"] and not l["counted_by_old_code"]]

        return {
            "requested":  item_no,
            "normalized": target,
            "products_matched": [
                {"product_id": m["product_id"],
                 "item_no": m["item_no"],
                 "item_no_repr": repr(m["item_no"]),
                 "matches_old_exact_where": m["item_no"] == item_no}
                for m in matches
            ],
            "product_count":        len(matches),
            "duplicate_columns":    dupe_cols,
            "inventory_row_count":  len(rows),
            "old_group_keys":       [repr(k) for k in old_groups.keys()],
            "totals": {
                "old_endpoint_returned": total(old_rows),
                "new_endpoint_returns":  total(rows),
                "all_rows_net":          sum(net(r) for r in rows),
                "negative_rows":         len([r for r in rows if net(r) < 0]),
                "zero_rows":             len([r for r in rows if net(r) == 0]),
            },
            "dropped_by_old_code": {
                "count": len(dropped),
                "qty":   sum(l["qty_on_hand"] for l in dropped),
                "master_packs": [l["master_pack_no"] for l in dropped],
            },
            "lots": lots,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.get("/debug/duplicate-codes", dependencies=[Depends(verify_api_key)])
def debug_duplicate_codes():
    """Every item code held under more than one Products row.

    These are the codes where the old grouping split an item in two and the
    /item endpoint returned only half. Empty list = the problem is elsewhere.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT ProductID, ItemNo FROM Products")
        byc: dict = {}
        for pid, code in cursor.fetchall():
            byc.setdefault(norm_code(code), []).append({"product_id": pid, "item_no": code})
        dupes = [
            {"normalized": k,
             "product_count": len(v),
             "spellings": sorted({repr(x["item_no"]) for x in v}),
             "products": v}
            for k, v in byc.items() if k and len(v) > 1
        ]
        dupes.sort(key=lambda d: (-d["product_count"], d["normalized"]))
        return {"count": len(dupes), "duplicates": dupes}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.get("/debug/products", dependencies=[Depends(verify_api_key)])
def debug_inventory_products():
    """Debug: list distinct ItemNo values that have inventory records."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT DISTINCT p.ItemNo, i.ProductID
            FROM Inventory i
            INNER JOIN Products p ON i.ProductID = p.ProductID
            ORDER BY p.ItemNo
        """)
        rows = cursor.fetchall()
        return {"count": len(rows), "products": [{"item_no": r[0], "product_id": r[1]} for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.get("/debug/columns", dependencies=[Depends(verify_api_key)])
def get_inventory_columns():
    """Debug: returns column names for all inventory-related tables."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        result = {}
        for table in ["Inventory", "Products", "Purchase Order Details", "Purchase Orders", "Order Details", "Orders", "Customers", "Suppliers"]:
            cursor.execute(f"SELECT TOP 1 * FROM [{table}]")
            result[table] = [col[0] for col in cursor.description]
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()
