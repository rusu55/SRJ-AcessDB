from fastapi import APIRouter, Depends, HTTPException
from database.connection import get_db_connection
from typing import Optional
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


def build_inventory_status(cursor, item_no: Optional[str] = None):
    item_filter = "WHERE p.ItemNo = ?" if item_no else ""
    params = [item_no] if item_no else []

    # -- 1. Inventory lots --
    cursor.execute(f"""
        SELECT i.*, p.ItemNo
        FROM Inventory i
        INNER JOIN Products p ON i.ProductID = p.ProductID
        {item_filter}
        ORDER BY p.ItemNo, i.WarehouseReceiptDate
    """, params)
    inv_rows = rows_to_dicts(cursor, cursor.fetchall())

    products = {}
    for row in inv_rows:
        key = row.get("ItemNo") or str(row["ProductID"])
        if key not in products:
            products[key] = {
                "item_no":    key,
                "product_id": row["ProductID"],
                "_inv_lots":  [],
                "_po_rows":   [],
                "_so_rows":   [],
            }
        products[key]["_inv_lots"].append(row)

    # -- 2. Open Purchase Orders --
    cursor.execute(f"""
        SELECT pod.*, po.SupplierID, po.PODate, p.ItemNo
        FROM ([Purchase Order Details] pod
        INNER JOIN [Purchase Orders] po ON pod.PurchaseOrderID = po.PurchaseOrderID)
        INNER JOIN Products p ON pod.ProductID = p.ProductID
        {item_filter}
        ORDER BY p.ItemNo, pod.PurDueShip
    """, params)
    po_rows = rows_to_dicts(cursor, cursor.fetchall())

    cursor.execute("SELECT SupplierID, SupplierName FROM Suppliers")
    supplier_map = {r[0]: r[1] for r in cursor.fetchall()}

    for row in po_rows:
        key = row.get("ItemNo") or str(row.get("ProductID", ""))
        if not key:
            continue
        open_qty = (row.get("PurQty") or 0) - (row.get("PurShippedQty") or 0)
        if open_qty <= 0:
            continue
        if key not in products:
            products[key] = {
                "item_no":    key,
                "product_id": row.get("ProductID"),
                "_inv_lots":  [],
                "_po_rows":   [],
                "_so_rows":   [],
            }
        products[key]["_po_rows"].append(row)

    # -- 3. Open Sales Orders --
    cursor.execute(f"""
        SELECT od.*, o.CustomerID, o.OrderDate, p.ItemNo
        FROM ([Order Details] od
        INNER JOIN Orders o ON od.OrderID = o.OrderID)
        INNER JOIN Products p ON od.ProductID = p.ProductID
        {item_filter}
        ORDER BY p.ItemNo, od.RequiredDate
    """, params)
    so_rows = rows_to_dicts(cursor, cursor.fetchall())

    cursor.execute("SELECT CustomerID, CompanyName FROM Customers")
    customer_map = {r[0]: r[1] for r in cursor.fetchall()}

    for row in so_rows:
        key = row.get("ItemNo") or str(row.get("ProductID", ""))
        if not key:
            continue
        if key not in products:
            products[key] = {
                "item_no":    key,
                "product_id": row.get("ProductID"),
                "_inv_lots":  [],
                "_po_rows":   [],
                "_so_rows":   [],
            }
        products[key]["_so_rows"].append(row)

    # -- Shape final response --
    result = []
    for key, p in products.items():
        lots = [shape_lot(r) for r in p["_inv_lots"] if (r.get("WarehouseReceiptQty") or 0) != (r.get("OrderShippedQty") or 0)]
        pos  = [shape_po(r, supplier_map) for r in p["_po_rows"]]
        sos  = [shape_so(r, customer_map) for r in p["_so_rows"]]

        result.append({
            "product": {
                "item_no":    p["item_no"],
                "product_id": p["product_id"],
            },
            "inventory": {
                "total_qty": sum(l["qty_on_hand"] for l in lots if l["qty_on_hand"] > 0),
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
        return result[0] if result else {}
    except HTTPException:
        raise
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
