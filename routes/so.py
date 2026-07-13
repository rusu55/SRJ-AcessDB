from fastapi import APIRouter, Depends, HTTPException
from database.connection import get_db_connection
from typing import List, Dict, Any, Optional
from middleware.auth import verify_api_key
from pydantic import BaseModel
from datetime import datetime



router = APIRouter()



@router.get("/all", dependencies=[Depends(verify_api_key)])
def get_all_purchase_orders(limit: int = 3000, last_po_id: Optional[int] = None):
    """
    Get list of all purchase orders (cursor-based pagination)
    
    Example: GET /api/purchase-orders/all?limit=50
    Example: GET /api/purchase-orders/all?limit=50&last_po_id=123
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Get total count
        cursor.execute("SELECT COUNT(*) FROM [Orders]")
        total_count = cursor.fetchone()[0]
        
        # Cursor-based pagination
        if last_po_id is None:
            # First page
            cursor.execute(f"""
                SELECT TOP {limit}
                so.*,
                IIF(
                    (SELECT SUM(sod.[OrderQty] * sod.[OrderUnitPrice])
                    FROM [Order Details] sod
                    WHERE sod.[OrderID] = so.[OrderID]) IS NULL,
                    0,
                    (SELECT SUM(sod.[OrderQty] * sod.[OrderUnitPrice])
                    FROM [Order Details] sod
                    WHERE sod.[OrderID] = so.[OrderID])
                ) AS orderTotal
                FROM [Orders] so
                ORDER BY so.[OrderID] DESC
            """)
        else:
            # Subsequent pages
            cursor.execute(f"""
                SELECT TOP {limit} *
                FROM [Orders]
                WHERE OrderID < ?
                ORDER BY OrderID DESC
            """, (last_po_id,))
        
        sales_orders = cursor.fetchall()
        columns = [col[0] for col in cursor.description]
        
        po_list = []
        for row in sales_orders:
            po_dict = dict(zip(columns, row))
            
            # Format dates
            for col in columns:
                if 'Date' in col and po_dict.get(col):
                    if isinstance(po_dict[col], datetime):
                        po_dict[col] = po_dict[col].strftime('%m/%d/%Y')
            
            po_list.append(po_dict)
        
        # Get the last PurchaseOrderID for next page cursor
        next_cursor = po_list[-1]['OrderID'] if po_list else None
        
        return {
            "total": total_count,
            "limit": limit,
            "count": len(po_list),
            "next_cursor": next_cursor,
            "sales_orders": po_list
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()

@router.get("/details/{sales_order_no}", dependencies=[Depends(verify_api_key)])
def get_po_details(sales_order_no: str):
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # --- Order header (carries Freight, SalesTax, HandlingCharge, etc.) ---
        cursor.execute("SELECT * FROM [Orders] WHERE OrderID = ?", (sales_order_no,))
        header_row = cursor.fetchone()

        if not header_row:
            raise HTTPException(status_code=404, detail=f"No sales order found for {sales_order_no}")

        header_columns = [column[0] for column in cursor.description]
        order = dict(zip(header_columns, header_row))

        # Format header dates
        for col in header_columns:
            if 'Date' in col and isinstance(order.get(col), datetime):
                order[col] = order[col].strftime('%m/%d/%Y')

        # --- Line items ---
        cursor.execute("""
            SELECT
                sod.*,
                p.ProductID AS Product_ProductID,
                p.ProductName,
                p.ItemNo
            FROM [Order Details] sod
            LEFT JOIN [Products] p ON sod.ProductID = p.ProductID
            WHERE sod.OrderID = ?
        """, (sales_order_no,))

        columns = [column[0] for column in cursor.description]
        line_items = [dict(zip(columns, row)) for row in cursor.fetchall()]

        # Format line-item dates
        for item in line_items:
            for col in columns:
                if 'Date' in col and isinstance(item.get(col), datetime):
                    item[col] = item[col].strftime('%m/%d/%Y')

        # --- Totals ---
        # Subtotal is computed from the line items. Freight and HandlingCharge on
        # the Orders header are dollar AMOUNTS, but **SalesTax is a RATE, not an
        # amount** (e.g. 0.1 = 10%, and it varies per order / state). The Access
        # "Orders New" form computes the displayed tax the same way:
        #     Sales Tax = [SalesTax] * [Sub-Total]
        # So the tax AMOUNT = subtotal * SalesTax. The old code returned the raw
        # rate as the tax (e.g. 0.1 instead of 286.39 for order 43953).
        subtotal = sum(
            (item.get('OrderQty') or 0) * (item.get('OrderUnitPrice') or 0)
            for item in line_items
        )
        freight = order.get('Freight') or 0
        tax_rate = order.get('SalesTax') or 0
        tax = subtotal * tax_rate
        handling = order.get('HandlingCharge') or 0
        total = subtotal + freight + tax + handling

        return {
            "order": order,
            "line_items": line_items,
            "totals": {
                "subtotal": round(subtotal, 2),
                "freight": round(freight, 2),
                "tax_rate": tax_rate,
                "tax": round(tax, 2),
                "handling_charge": round(handling, 2),
                "total": round(total, 2),
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()
    
    