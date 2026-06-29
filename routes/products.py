from fastapi import APIRouter, Depends, HTTPException
from database.connection import get_db_connection
from typing import List, Dict, Any, Optional
from middleware.auth import verify_api_key
from pydantic import BaseModel
from datetime import datetime

router = APIRouter()

@router.get("/all", dependencies=[Depends(verify_api_key)])
def get_all_orders(limit: int = 100, offset: int = 0):
    """
    Get list of all orders (paginated)
    
    Example: GET /api/orders/all?limit=50&offset=0
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Get total count
        cursor.execute("SELECT COUNT(*) FROM Products")
        total_count = cursor.fetchone()[0]
        
        # Get all orders sorted, then slice in Python
        # This is more efficient for small-medium datasets in Access
        if offset == 0:
            # No offset, just use TOP
            cursor.execute(f"""
                SELECT TOP {limit} *
                FROM Products
                ORDER BY ItemNo DESC
            """)
        else:
            # With offset, we need a different approach
            # Option 1: Fetch more and slice (simpler, works for moderate data)
            cursor.execute(f"""
                SELECT TOP {limit + offset} *
                FROM Products
                ORDER BY ItemNo DESC
            """)
            # We'll slice the results in Python below
        
        orders = cursor.fetchall()
        columns = [col[0] for col in cursor.description]
        
        # If we have offset, skip the first 'offset' rows
        if offset > 0 and len(orders) > offset:
            orders = orders[offset:]
        
        orders_list = []
        for row in orders:
            order_dict = dict(zip(columns, row))
            
            # Format dates
            for col in columns:
                if 'Date' in col and order_dict.get(col):
                    if isinstance(order_dict[col], datetime):
                        order_dict[col] = order_dict[col].strftime('%m/%d/%Y')
            
            orders_list.append(order_dict)
        
        return {
            "total": total_count,
            "limit": limit,
            "offset": offset,
            "count": len(orders_list),
            "orders": orders_list
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()

@router.get("/debug/columns", dependencies=[Depends(verify_api_key)])
def get_table_columns():
    """Debug endpoint to see actual column names"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Get Products table columns
        cursor.execute("SELECT TOP 1 * FROM Products")
        products_columns = [col[0] for col in cursor.description]
        
        return {
            "Products_columns": products_columns
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()