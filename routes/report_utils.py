"""
Shared helpers for the read-only REPORT routers (customers, sales,
open_orders, purchasing, stock, quality, data_health).

Rules every report router follows:
  * plain SELECTs only - nothing here writes, locks or alters the .mdb;
  * no Access-only SQL (no TOP / IIF / Nz / Year()): filter in SQL, then
    group and total in Python. Slower by a hair, but every query is easy to
    read and behaves the same in every Access driver;
  * lookups (item codes, customer names, ...) are read once per request and
    joined in Python - Access needs nested brackets for multi-table JOINs and
    one bad bracket fails the whole call;
  * a lookup that fails returns an empty map, so a missing name never breaks
    a report.
"""

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from fastapi import HTTPException

from database.connection import get_db_connection


# ---------------------------------------------------------------- values ---

def s(v: Any) -> str:
    """Text, trimmed; None -> ''."""
    return "" if v is None else str(v).strip()


def key(v: Any) -> str:
    """Matching key for codes. Access compares text case-insensitively and
    ignores trailing spaces, so 'jl-1 ' and 'JL-1' are the same code."""
    return s(v).upper()


def num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def dt(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    t = s(v)
    if not t:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(t[:19] if "-" in t[:5] else t[:10], fmt)
        except ValueError:
            continue
    return None


def day(v: Any) -> Optional[str]:
    """YYYY-MM-DD or None."""
    d = dt(v)
    return d.strftime("%Y-%m-%d") if d else None


def money(v: float) -> float:
    return round(v + 0.0, 2)


def today() -> datetime:
    n = datetime.now()
    return datetime(n.year, n.month, n.day)


# --------------------------------------------------------------- params ---

def parse_day(d: str, name: str) -> datetime:
    try:
        return datetime.strptime(d.strip()[:10], "%Y-%m-%d")
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM-DD, got {d!r}")


def window(since: Optional[str], until: Optional[str], default_days: int = 90,
           max_days: int = 800) -> Tuple[datetime, datetime]:
    """[since, until) as datetimes. until defaults to tomorrow (so today is in),
    since to `default_days` before until."""
    d1 = parse_day(until, "until") if until else today() + timedelta(days=1)
    d0 = parse_day(since, "since") if since else d1 - timedelta(days=default_days)
    if d1 <= d0:
        raise HTTPException(status_code=400, detail="until must be after since")
    if (d1 - d0).days > max_days:
        raise HTTPException(status_code=400, detail=f"window is limited to {max_days} days")
    return d0, d1


def window_out(d0: datetime, d1: datetime) -> Dict[str, str]:
    return {"since": d0.strftime("%Y-%m-%d"), "until": d1.strftime("%Y-%m-%d")}


# ------------------------------------------------------------------- db ---

@contextmanager
def db() -> Iterator[Any]:
    """Cursor on a fresh connection, always closed. Database errors become a
    500 that says which report failed instead of a bare stack trace."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        yield cur
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - surface the driver message
        raise HTTPException(status_code=500, detail=f"Access read failed: {e}")
    finally:
        try:
            cur.close()
        finally:
            conn.close()


def fetch(cur: Any, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    cur.execute(sql, tuple(params))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def fetch_in(cur: Any, sql_before_in: str, values: Iterable[Any], sql_after: str = "",
             params_before: Sequence[Any] = (), size: int = 200) -> List[Dict[str, Any]]:
    """Run `sql_before_in (?,?,...) sql_after` in chunks - the Access driver
    chokes on very long IN lists. `sql_before_in` must end with 'IN'."""
    vals = list(dict.fromkeys(v for v in values if v is not None and s(v) != ""))
    out: List[Dict[str, Any]] = []
    for i in range(0, len(vals), size):
        chunk = vals[i:i + size]
        out.extend(fetch(cur, f"{sql_before_in} ({','.join('?' * len(chunk))}) {sql_after}",
                         list(params_before) + chunk))
    return out


def fetch_by(cur: Any, select_from: str, col: str, values: Iterable[Any], many: int = 2000) -> List[Dict[str, Any]]:
    """Rows of `select_from` (a SELECT ... FROM ... with NO where clause)
    whose `col` is in `values`. A few hundred values -> chunked IN queries;
    thousands -> one full read filtered here, which is far quicker than
    dozens of round trips to the .mdb."""
    wanted = {key(v): v for v in values if s(v)}
    if not wanted:
        return []
    if len(wanted) > many:
        return [r for r in fetch(cur, select_from) if key(r.get(col)) in wanted]
    return fetch_in(cur, f"{select_from} WHERE {col} IN", list(wanted.values()))


def safe_map(cur: Any, sql: str, k: str, build) -> Dict[str, Any]:
    """{key(row[k]): build(row)} - empty on any failure (context only)."""
    try:
        return {key(r.get(k)): build(r) for r in fetch(cur, sql)}
    except Exception:  # noqa: BLE001
        return {}


# -------------------------------------------------------------- lookups ---

def products(cur: Any) -> Dict[str, Dict[str, Any]]:
    """ProductID -> item code, name, product line, pack."""
    return safe_map(cur, "SELECT ProductID, ItemNo, ProductName, ProductLine, UnitPackQty, SellingPrice, "
                         "ReOrderLevel FROM Products", "ProductID",
                    lambda r: {"itemNo": s(r.get("ItemNo")), "name": s(r.get("ProductName")) or None,
                               "productLine": s(r.get("ProductLine")) or None,
                               "unitPackQty": r.get("UnitPackQty"), "sellingPrice": r.get("SellingPrice"),
                               "reorderLevel": r.get("ReOrderLevel")})


def product_ids_for_item(cur: Any, item_no: str) -> List[str]:
    """Every ProductID whose ItemNo matches (case/space-insensitive). The
    same code can sit on more than one Products row."""
    target = key(item_no)
    return [s(r["ProductID"]) for r in fetch(cur, "SELECT ProductID, ItemNo FROM Products")
            if key(r.get("ItemNo")) == target]


def customers(cur: Any) -> Dict[str, str]:
    return safe_map(cur, "SELECT CustomerID, CompanyName FROM Customers", "CustomerID",
                    lambda r: s(r.get("CompanyName")) or None)


def suppliers(cur: Any) -> Dict[str, str]:
    return safe_map(cur, "SELECT SupplierID, SupplierName FROM Suppliers", "SupplierID",
                    lambda r: s(r.get("SupplierName")) or None)


def salesmen(cur: Any) -> Dict[str, str]:
    return safe_map(cur, "SELECT SalesmanID, FirstName, LastName FROM Salesmen", "SalesmanID",
                    lambda r: (" ".join(x for x in (s(r.get("FirstName")), s(r.get("LastName"))) if x)) or None)


def order_sources(cur: Any) -> Dict[str, str]:
    return safe_map(cur, "SELECT OrderSourceID, OrderSource FROM [Order Sources]", "OrderSourceID",
                    lambda r: s(r.get("OrderSource")) or None)


def warehouses(cur: Any) -> Dict[str, str]:
    return safe_map(cur, "SELECT WarehouseID, WarehouseName FROM [Warehouse Code]", "WarehouseID",
                    lambda r: s(r.get("WarehouseName")) or None)


def shippers(cur: Any) -> Dict[str, str]:
    return safe_map(cur, "SELECT ShipperID, ShipperName FROM Shippers", "ShipperID",
                    lambda r: s(r.get("ShipperName")) or None)


def on_hand_by_product(cur: Any) -> Dict[str, float]:
    """ProductID -> qty on hand (received - shipped, positive lots only) -
    the same rule as /api/inventory/all."""
    out: Dict[str, float] = {}
    for r in fetch(cur, "SELECT ProductID, WarehouseReceiptQty, OrderShippedQty FROM Inventory"):
        q = num(r.get("WarehouseReceiptQty")) - num(r.get("OrderShippedQty"))
        if q > 0:
            k = key(r.get("ProductID"))
            out[k] = out.get(k, 0.0) + q
    return out


# ----------------------------------------------------------- order rules ---

def line_shipped(r: Dict[str, Any]) -> bool:
    """An order line is shipped (history) once it has a ship date, a
    ShippingID or a shipped qty - the same rule as orders_write._line_locked."""
    return bool(dt(r.get("ShippingDate")) or s(r.get("ShippingID")) or num(r.get("OrderShippedQty")) > 0)


def line_amount(r: Dict[str, Any], basis: str = "ordered") -> float:
    qty = num(r.get("OrderShippedQty")) if basis == "shipped" else num(r.get("OrderQty"))
    return qty * num(r.get("OrderUnitPrice"))
