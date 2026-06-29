from fastapi import APIRouter, Depends, HTTPException
from database.connection import get_db_connection
from typing import List, Dict, Any, Optional
from middleware.auth import verify_api_key
from pydantic import BaseModel
from datetime import datetime


class PurchaseOrderResponse(BaseModel):
    PurchaseOrderID: Optional[int] = None
    SupplierID: Optional[int] = None
    PODate: Optional[str] = None
    WarehouseID: Optional[str] = None
    AgentID: Optional[int] = None
    PriceTerm: Optional[str] = None
    Place: Optional[str] = None
    PaymentTerm: Optional[str] = None
    ShipVia: Optional[str] = None
    NotifyName: Optional[str] = None
    NotifyAddress: Optional[str] = None
    NotifyCity: Optional[str] = None
    NotifyRegion: Optional[str] = None
    NotifyPostalCode: Optional[str] = None
    NotifyCountry: Optional[str] = None
    SpecialRequest: Optional[str] = None
    Notes: Optional[str] = None

router = APIRouter()



@router.get("/all")
def get_all_purchase_orders(limit: int = 5000, last_po_id: Optional[int] = None):
    """
    Get list of all purchase orders (cursor-based pagination)
    
    Example: GET /api/purchase-orders/all?limit=50
    Example: GET /api/purchase-orders/all?limit=50&last_po_id=123
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Get total count
        cursor.execute("SELECT COUNT(*) FROM [Purchase Orders]")
        total_count = cursor.fetchone()[0]
        
        # Cursor-based pagination
        if last_po_id is None:
            # First page
            cursor.execute(f"""
                SELECT TOP {limit} 
                    po.*,
                    IIF(
                        (SELECT SUM(pod.[PurQty] * pod.[PurUnitPrice])
                         FROM [Purchase Order Details] pod
                         WHERE pod.[PurchaseOrderID] = po.[PurchaseOrderID]) IS NULL,
                        0,
                        (SELECT SUM(pod.[PurQty] * pod.[PurUnitPrice])
                         FROM [Purchase Order Details] pod
                         WHERE pod.[PurchaseOrderID] = po.[PurchaseOrderID])
                    ) AS orderTotal
                FROM [Purchase Orders] po
                ORDER BY po.[PurchaseOrderID] DESC
            """)
        else:
            # Subsequent pages
            cursor.execute(f"""
                SELECT TOP {limit} 
                    po.*,
                    IIF(
                        (SELECT SUM(pod.[PurQty] * pod.[PurUnitPrice])
                         FROM [Purchase Order Details] pod
                         WHERE pod.[PurchaseOrderID] = po.[PurchaseOrderID]) IS NULL,
                        0,
                        (SELECT SUM(pod.[PurQty] * pod.[PurUnitPrice])
                         FROM [Purchase Order Details] pod
                         WHERE pod.[PurchaseOrderID] = po.[PurchaseOrderID])
                    ) AS orderTotal
                FROM [Purchase Orders] po
                WHERE po.[PurchaseOrderID] < ?
                ORDER BY po.[PurchaseOrderID] DESC
            """, (last_po_id,))
        
        purchase_orders = cursor.fetchall()
        columns = [col[0] for col in cursor.description]
        
        po_list = []
        for row in purchase_orders:
            po_dict = dict(zip(columns, row))
            
            # Format dates
            for col in columns:
                if 'Date' in col and po_dict.get(col):
                    if isinstance(po_dict[col], datetime):
                        po_dict[col] = po_dict[col].strftime('%m/%d/%Y')
            
            po_list.append(po_dict)
        
        # Get the last PurchaseOrderID for next page cursor
        next_cursor = po_list[-1]['PurchaseOrderID'] if po_list else None
        
        return {
            "total": total_count,
            "limit": limit,
            "count": len(po_list),
            "next_cursor": next_cursor,
            "purchase_orders": po_list
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.get("/details/{purchase_order_no}", dependencies=[Depends(verify_api_key)])
def get_po_details(purchase_order_no: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Join the PO header so each line carries the SupplierID (vendor).
        # Access SQL requires the two joins to be parenthesized.
        cursor.execute("""SELECT
                       pod.PurchaseOrderID,
                       pod.ProductID,
                       pod.PurDueShip,
                       pod.PurQty,
                       pod.PurUnitPrice,
                       pod.PackID,
                       pod.PurShippedDate,
                       pod.PurShippedQty,
                       pod.VendorShippingID,
                       pod.AgentCommPaid,
                       p.ProductID AS Product_ProductID,
                       p.ProductName,
                       p.ItemNo,
                       po.SupplierID
                     FROM ([Purchase Order Details] pod
                     LEFT JOIN Products p ON pod.ProductID = p.ProductID)
                     LEFT JOIN [Purchase Orders] po ON pod.PurchaseOrderID = po.PurchaseOrderID
                     WHERE pod.PurchaseOrderID = ?
                        """, (purchase_order_no,))

        columns = [column[0] for column in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]

        if not results:
            raise HTTPException(status_code=404, detail=f"No details found for purchase order {purchase_order_no}")

        # Attach the supplier name from the Suppliers lookup (mirrors inventory.py).
        cursor.execute("SELECT SupplierID, SupplierName FROM Suppliers")
        supplier_map = {r[0]: r[1] for r in cursor.fetchall()}
        for row in results:
            row["SupplierName"] = supplier_map.get(row.get("SupplierID"), "")

        return results
        
    finally:
        conn.close()
    