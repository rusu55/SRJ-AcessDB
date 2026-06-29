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
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        
        if not results:
            raise HTTPException(status_code=404, detail=f"No details found for purchase order {purchase_order_no}")
            
        return results
        
    finally:
        conn.close()
    
    