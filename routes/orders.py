from fastapi import APIRouter, Depends, HTTPException
from database.connection import get_db_connection
from typing import List, Dict, Any, Optional
from middleware.auth import verify_api_key
from pydantic import BaseModel
from datetime import datetime

# Pydantic models - Updated to match actual database columns
class OrderDetailItem(BaseModel):
    OrderID: Optional[int] = None
    ProductID: Optional[str] = None
    RequiredDate: Optional[str] = None
    OrderQty: Optional[float] = None
    OrderUnitPrice: Optional[float] = None
    ProdLotNoID: Optional[str] = None
    ShippingID: Optional[int] = None
    ShippingDate: Optional[str] = None
    OrderShippedQty: Optional[float] = None
    WarehouseID: Optional[int] = None

class OrderResponse(BaseModel):
    OrderID: int
    CustomerID: Optional[str] = None
    OrderDate: Optional[str] = None
    CustomerOrderNumber: Optional[str] = None
    BuyerName: Optional[str] = None
    order_details: List[OrderDetailItem] = []

router = APIRouter()

@router.get("/order/{order_id}", dependencies=[Depends(verify_api_key)])
def get_order(order_id: int):
    """
    Get complete order information including line items
    
    Example: GET /api/orders/order/1
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Get main order information
        cursor.execute("""
            SELECT *
            FROM Orders
            WHERE OrderID = ?
        """, (order_id,))
        
        order = cursor.fetchone()
        
        if not order:
            raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
        
        # Get column names
        columns = [col[0] for col in cursor.description]
        order_dict = dict(zip(columns, order))
        
        # Format dates
        for col in columns:
            if 'Date' in col and order_dict.get(col):
                if isinstance(order_dict[col], datetime):
                    order_dict[col] = order_dict[col].strftime('%m/%d/%Y')
        
        # Get order line items
        cursor.execute("""
            SELECT *
            FROM [Order Details]
            WHERE OrderID = ?
        """, (order_id,))
        
        details = cursor.fetchall()
        detail_columns = [col[0] for col in cursor.description]
        
        order_details = []
        for row in details:
            detail_dict = dict(zip(detail_columns, row))
            # Format dates in details
            for col in detail_columns:
                if 'Date' in col and detail_dict.get(col):
                    if isinstance(detail_dict[col], datetime):
                        detail_dict[col] = detail_dict[col].strftime('%m/%d/%Y')
            order_details.append(detail_dict)
        
        order_dict['order_details'] = order_details
        
        return order_dict
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()


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
        cursor.execute("SELECT COUNT(*) FROM Orders")
        total_count = cursor.fetchone()[0]
        
        # Get all orders sorted, then slice in Python
        # This is more efficient for small-medium datasets in Access
        if offset == 0:
            # No offset, just use TOP
            cursor.execute(f"""
                SELECT TOP {limit} *
                FROM Orders
                ORDER BY OrderID DESC
            """)
        else:
            # With offset, we need a different approach
            # Option 1: Fetch more and slice (simpler, works for moderate data)
            cursor.execute(f"""
                SELECT TOP {limit + offset} *
                FROM Orders
                ORDER BY OrderID DESC
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


@router.get("/customer/{customer_id}", dependencies=[Depends(verify_api_key)])
def get_orders_by_customer(customer_id: str):
    """
    Get all orders for a specific customer
    
    Example: GET /api/orders/customer/CUST001
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT *
            FROM Orders
            WHERE CustomerID = ?
            ORDER BY OrderDate DESC
        """, (customer_id,))
        
        orders = cursor.fetchall()
        
        if not orders:
            raise HTTPException(status_code=404, detail=f"No orders found for customer {customer_id}")
        
        columns = [col[0] for col in cursor.description]
        
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
            "customer_id": customer_id,
            "total_orders": len(orders_list),
            "orders": orders_list
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.get("/search", dependencies=[Depends(verify_api_key)])
def search_orders(
    customer_order_number: Optional[str] = None,
    order_date_from: Optional[str] = None,
    order_date_to: Optional[str] = None,
    customer_id: Optional[str] = None,
    buyer_name: Optional[str] = None
):
    """
    Search orders by various criteria
    
    Example: GET /api/orders/search?customer_order_number=12345&customer_id=CUST001
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        query = "SELECT * FROM Orders WHERE 1=1"
        params = []
        
        if customer_order_number:
            query += " AND CustomerOrderNumber LIKE ?"
            params.append(f"%{customer_order_number}%")
        
        if customer_id:
            query += " AND CustomerID LIKE ?"
            params.append(f"%{customer_id}%")
        
        if buyer_name:
            query += " AND BuyerName LIKE ?"
            params.append(f"%{buyer_name}%")
        
        if order_date_from:
            query += " AND OrderDate >= ?"
            params.append(order_date_from)
        
        if order_date_to:
            query += " AND OrderDate <= ?"
            params.append(order_date_to)
        
        query += " ORDER BY OrderDate DESC"
        
        cursor.execute(query, params)
        orders = cursor.fetchall()
        columns = [col[0] for col in cursor.description]
        
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
            "total": len(orders_list),
            "orders": orders_list
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.get("/stats", dependencies=[Depends(verify_api_key)])
def get_order_statistics():
    """
    Get order statistics
    
    Example: GET /api/orders/stats
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Total orders
        cursor.execute("SELECT COUNT(*) FROM Orders")
        total_orders = cursor.fetchone()[0]
        
        # Total order details
        cursor.execute("SELECT COUNT(*) FROM [Order Details]")
        total_line_items = cursor.fetchone()[0]
        
        # Recent orders (last 30 days) - Fixed column name
        cursor.execute("""
            SELECT COUNT(*) 
            FROM Orders 
            WHERE OrderDate >= DateAdd('d', -30, Date())
        """)
        recent_orders = cursor.fetchone()[0]
        
        return {
            "total_orders": total_orders,
            "total_line_items": total_line_items,
            "recent_orders_30_days": recent_orders,
            "database": "SRJDatabase.mdb"
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
        # Get Orders table columns
        cursor.execute("SELECT TOP 1 * FROM Orders")
        orders_columns = [col[0] for col in cursor.description]
        
        # Get Order Details table columns
        cursor.execute("SELECT TOP 1 * FROM [Order Details]")
        details_columns = [col[0] for col in cursor.description]
        
        return {
            "Orders_columns": orders_columns,
            "Order_Details_columns": details_columns
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()