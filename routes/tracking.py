from fastapi import APIRouter, Depends, HTTPException, Query
from database.connection import get_db_connection
from typing import Optional
from middleware.auth import verify_api_key
from datetime import datetime
import re

# ---------------------------------------------------------------------------
# CONFIG — adjust to match the real Access schema.
#
# The tracking fields (ShippingID, OrderID, PRO/Tracking No, carrier dates,
# shipping advice, ...) live in whatever table the Access form
# "Carrier Transit Time + Shipping Advice" is bound to (its Record Source).
#
# If you're not sure of the exact names, hit these endpoints once the API is
# running and update the three constants below:
#     GET /api/tracking/debug/tables            -> find the tracking table
#     GET /api/tracking/debug/columns?table=X   -> confirm column spellings
#
# NOTE: Access object names with spaces or symbols MUST stay bracketed in SQL
# (e.g. [Carrier Transit Time+Shipping Advice], [PROTrack No]). The helpers
# below bracket automatically, so just put the plain name in the constants.
# ---------------------------------------------------------------------------
TRACKING_TABLE = "Shipping"        # base table behind the form (best guess; verify via /debug/tables)
ORDER_COLUMN = "OrderID"           # numeric internal order id on the tracking table
TRACKING_COLUMN = "PROTrack No"    # PRO / tracking number column

# Used by /enriched to attach order / customer / carrier / warehouse context.
# Each lookup degrades gracefully: if a table or column doesn't exist, that
# field comes back null instead of failing the whole request. Confirm names
# with /debug/columns?table=... and tweak the *_TABLE / *_KEY constants below.
ORDERS_TABLE = "Orders"
CUSTOMERS_TABLE = "Customers"
SHIPPERS_TABLE = "Shippers"          # carrier list (e.g. ESTES Express Lines)
WAREHOUSE_TABLE = "Warehouses"       # warehouse list (e.g. SRJ)
ORDER_DETAILS_TABLE = "Order Details"

# Join keys (FKs). Adjust if your schema differs.
ORDERS_KEY = "OrderID"               # Orders PK / tracking-table FK
CUSTOMERS_KEY = "CustomerID"         # Customers PK / Orders FK
SHIPPER_FK_ON_ORDER = "ShipVia"      # Orders -> Shippers (Northwind-style)
SHIPPERS_KEY = "ShipperID"           # Shippers PK
WAREHOUSE_KEY = "WarehouseID"        # Warehouses PK / Order Details FK

# Per-shipment date used to bucket shipments by year (on the tracking table).
SHIPMENT_DATE_COLUMN = "ShippingAdviceDate"
CARRIER_UNKNOWN_LABEL = "Unknown"

# Delivery dates used to flag a shipment "late" = delivered after the carrier's
# estimate (Actual Delivery Date > Carrier Est Delivery Time). Note the DB's
# original spelling of the estimate column ("Deliery").
EST_DELIVERY_COLUMN = "Carrier Est Deliery Time"
ACTUAL_DELIVERY_COLUMN = "Actual Delivery Date"

# Logical field -> candidate column names (first match wins; substring fallback).
FIELD_CANDIDATES = {
    "customer": ["CompanyName", "CustomerName"],
    "contact": ["ContactName", "Contact"],
    "phone": ["Phone", "PhoneNumber", "Telephone"],
    "email": ["Email", "EmailAddress", "E-mail"],
    "shipper_name": ["CompanyName", "ShipperName", "CarrierName"],
    "warehouse": ["WarehouseCode", "WarehouseName", "Warehouse", "Code", "Name"],
    "shipping_date": ["ShippingDate", "ShipDate"],
}

router = APIRouter()


def _bracket(name: str) -> str:
    """Wrap an Access identifier in [] unless already bracketed."""
    name = name.strip()
    return name if name.startswith("[") and name.endswith("]") else f"[{name}]"


def _pick(row: dict, candidates) -> Optional[object]:
    """Resolve a value from a row dict by candidate column names.

    Tries exact (case-insensitive) matches first, then substring matches, so it
    tolerates schema spelling differences (Email vs EmailAddress, etc.).
    """
    if not row:
        return None
    lower = {k.lower(): k for k in row}
    for cand in candidates:
        if cand.lower() in lower:
            return row[lower[cand.lower()]]
    for cand in candidates:
        for key in row:
            if cand.lower() in key.lower():
                return row[key]
    return None


def _lookup_one(cursor, table: str, key_col: str, value) -> dict:
    """SELECT TOP 1 * from a related table by key. Returns {} on any failure
    (missing table/column/value) so enrichment never breaks the main response."""
    if value is None:
        return {}
    try:
        cursor.execute(
            f"SELECT TOP 1 * FROM {_bracket(table)} WHERE {_bracket(key_col)} = ?",
            (value,),
        )
        found = cursor.fetchone()
        if not found:
            return {}
        cols = [c[0] for c in cursor.description]
        return _format_row(found, cols)
    except Exception:
        return {}


def _format_row(row, columns) -> dict:
    """Zip a pyodbc row into a dict, stringifying dates like the other routes."""
    d = dict(zip(columns, row))
    for key, value in d.items():
        if isinstance(value, datetime):
            d[key] = value.strftime("%m/%d/%Y")
    return d


def _enrich_rows(cursor, rows: list) -> list:
    """Attach customer / contact / phone / email / shipper_name / warehouse /
    shipping_date / customer_order_number / buyer_name to each tracking row via
    related-table lookups. Best-effort: a missing table/column leaves it null."""
    for row in rows:
        order_id = row.get(ORDER_COLUMN)
        shipping_id = row.get("ShippingID")

        order = _lookup_one(cursor, ORDERS_TABLE, ORDERS_KEY, order_id)
        customer = _lookup_one(
            cursor, CUSTOMERS_TABLE, CUSTOMERS_KEY, _pick(order, [CUSTOMERS_KEY])
        )
        shipper = _lookup_one(
            cursor,
            SHIPPERS_TABLE,
            SHIPPERS_KEY,
            _pick(order, [SHIPPER_FK_ON_ORDER, SHIPPERS_KEY]),
        )
        # warehouse id can sit on the tracking row or on the order line.
        order_line = _lookup_one(
            cursor, ORDER_DETAILS_TABLE, "ShippingID", shipping_id
        )
        warehouse_id = _pick(row, [WAREHOUSE_KEY]) or _pick(
            order_line, [WAREHOUSE_KEY]
        )
        warehouse = _lookup_one(cursor, WAREHOUSE_TABLE, WAREHOUSE_KEY, warehouse_id)

        row["customer"] = _pick(customer, FIELD_CANDIDATES["customer"])
        row["contact"] = _pick(customer, FIELD_CANDIDATES["contact"])
        row["phone"] = _pick(customer, FIELD_CANDIDATES["phone"])
        row["email"] = _pick(customer, FIELD_CANDIDATES["email"])
        row["shipper_name"] = _pick(shipper, FIELD_CANDIDATES["shipper_name"])
        row["warehouse"] = (
            _pick(warehouse, FIELD_CANDIDATES["warehouse"]) or warehouse_id
        )
        row["shipping_date"] = _pick(
            order_line, FIELD_CANDIDATES["shipping_date"]
        ) or row.get("ShippingAdviceDate")
        # Useful order reference fields, flattened (no full-row dump / no dup of
        # OrderID, which is already on the tracking row).
        row["customer_order_number"] = _pick(order, ["CustomerOrderNumber"])
        row["buyer_name"] = _pick(order, ["BuyerName"])
    return rows


def _year_of(value) -> Optional[int]:
    """Extract a 4-digit year from a date value (datetime or string)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.year
    match = re.search(r"\d{4}", str(value))
    return int(match.group(0)) if match else None


def _as_date(value) -> Optional[datetime]:
    """Coerce a pyodbc date value (datetime or string) to a datetime for
    comparison. Returns None when empty/unparseable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _order_carrier_map(cursor) -> dict:
    """Build {OrderID(str): carrier_name} by reading Shippers + Orders once.
    Fully defensive — any failure yields an empty/partial map, and the caller
    falls back to the 'Unknown' carrier label."""
    shipper_names: dict = {}
    try:
        cursor.execute(f"SELECT * FROM {_bracket(SHIPPERS_TABLE)}")
        cols = [c[0] for c in cursor.description]
        for r in cursor.fetchall():
            d = dict(zip(cols, r))
            sid = _pick(d, [SHIPPERS_KEY])
            if sid is not None:
                shipper_names[str(sid)] = _pick(
                    d, FIELD_CANDIDATES["shipper_name"]
                ) or str(sid)
    except Exception:
        shipper_names = {}

    order_carrier: dict = {}
    try:
        cursor.execute(f"SELECT * FROM {_bracket(ORDERS_TABLE)}")
        cols = [c[0] for c in cursor.description]
        for r in cursor.fetchall():
            d = dict(zip(cols, r))
            oid = _pick(d, [ORDERS_KEY])
            if oid is None:
                continue
            ship_via = _pick(d, [SHIPPER_FK_ON_ORDER, SHIPPERS_KEY])
            carrier = (
                shipper_names.get(str(ship_via))
                if ship_via is not None
                else None
            )
            order_carrier[str(oid)] = carrier or (
                str(ship_via) if ship_via is not None else None
            )
    except Exception:
        order_carrier = {}

    return order_carrier


@router.get("/", dependencies=[Depends(verify_api_key)])
def get_tracking(
    order_no: Optional[int] = Query(
        None, description="Filter by Orders.OrderID (internal numeric order id)"
    ),
    tracking_no: Optional[str] = Query(
        None, description="Filter by PRO / Tracking No (partial match)"
    ),
    year: Optional[int] = Query(
        None, description="Filter by shipment year (Year of ShippingAdviceDate). "
        "NOTE: a shipment with no shipping advice has no ShippingAdviceDate and "
        "is excluded by this filter — use min_order_no to page by order instead."
    ),
    min_order_no: Optional[int] = Query(
        None, description="Only rows with OrderID >= this (newest-first paging without a date)"
    ),
    limit: int = Query(500, ge=1, le=5000, description="Max rows to return (newest OrderID first)"),
):
    """
    Extract tracking info for shipments. Returns every column of the tracking
    table, plus flattened enrichment fields (customer, contact, phone, email,
    shipper_name, warehouse, shipping_date, customer_order_number, buyer_name).
    Both filters are optional and combine (AND):

      GET /api/tracking/                          -> all tracking rows (capped by limit)
      GET /api/tracking/?order_no=1042            -> rows for OrderID 1042
      GET /api/tracking/?tracking_no=1Z999        -> rows whose PRO No contains "1Z999"
      GET /api/tracking/?year=2026                -> all shipments in 2026
      GET /api/tracking/?order_no=1042&tracking_no=1Z999
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        table = _bracket(TRACKING_TABLE)
        query = f"SELECT TOP {int(limit)} * FROM {table} WHERE 1=1"
        params = []

        if order_no is not None:
            query += f" AND {_bracket(ORDER_COLUMN)} = ?"
            params.append(order_no)

        if tracking_no:
            query += f" AND {_bracket(TRACKING_COLUMN)} LIKE ?"
            params.append(f"%{tracking_no}%")

        if year is not None:
            query += f" AND Year({_bracket(SHIPMENT_DATE_COLUMN)}) = ?"
            params.append(year)

        if min_order_no is not None:
            query += f" AND {_bracket(ORDER_COLUMN)} >= ?"
            params.append(min_order_no)

        # Newest first, so TOP <limit> is the most recent shipments, not the oldest.
        query += f" ORDER BY {_bracket(ORDER_COLUMN)} DESC"

        cursor.execute(query, params)
        columns = [col[0] for col in cursor.description]
        rows = [_format_row(row, columns) for row in cursor.fetchall()]
        _enrich_rows(cursor, rows)

        return {
            "table": TRACKING_TABLE,
            "filters": {"order_no": order_no, "tracking_no": tracking_no, "year": year, "min_order_no": min_order_no},
            "count": len(rows),
            "tracking": rows,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.get("/enriched", dependencies=[Depends(verify_api_key)])
def get_tracking_enriched(
    order_no: Optional[int] = Query(
        None, description="Filter by Orders.OrderID (internal numeric order id)"
    ),
    tracking_no: Optional[str] = Query(
        None, description="Filter by PRO / Tracking No (partial match)"
    ),
    year: Optional[int] = Query(
        None, description="Filter by shipment year (Year of ShippingAdviceDate). "
        "NOTE: a shipment with no shipping advice has no ShippingAdviceDate and "
        "is excluded by this filter — use min_order_no to page by order instead."
    ),
    min_order_no: Optional[int] = Query(
        None, description="Only rows with OrderID >= this (newest-first paging without a date)"
    ),
    limit: int = Query(500, ge=1, le=5000, description="Max rows to return (newest OrderID first)"),
):
    """
    Same as GET /api/tracking/ but each tracking row is enriched, mirroring the
    form's subform, with:

        customer        Customers.CompanyName
        contact         Customers.ContactName
        phone           Customers.Phone
        email           Customers.Email
        shipper_name    the order's carrier (Shippers.CompanyName)
        warehouse       the order's warehouse code/name
        shipping_date   the order line's ship date
        customer_order_number / buyer_name   from the matching Orders row

    Enrichment is best-effort per related table: if a table/column isn't found,
    that field is null rather than failing the request.

      GET /api/tracking/enriched
      GET /api/tracking/enriched?order_no=43260
      GET /api/tracking/enriched?tracking_no=128-3052234
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 1) Base tracking rows (same query as GET /).
        table = _bracket(TRACKING_TABLE)
        query = f"SELECT TOP {int(limit)} * FROM {table} WHERE 1=1"
        params = []

        if order_no is not None:
            query += f" AND {_bracket(ORDER_COLUMN)} = ?"
            params.append(order_no)

        if tracking_no:
            query += f" AND {_bracket(TRACKING_COLUMN)} LIKE ?"
            params.append(f"%{tracking_no}%")

        if year is not None:
            query += f" AND Year({_bracket(SHIPMENT_DATE_COLUMN)}) = ?"
            params.append(year)

        if min_order_no is not None:
            query += f" AND {_bracket(ORDER_COLUMN)} >= ?"
            params.append(min_order_no)

        # Newest first, so TOP <limit> is the most recent shipments, not the oldest.
        query += f" ORDER BY {_bracket(ORDER_COLUMN)} DESC"

        cursor.execute(query, params)
        columns = [col[0] for col in cursor.description]
        rows = [_format_row(row, columns) for row in cursor.fetchall()]

        # 2) Enrich each row via related-table lookups (graceful fallback).
        _enrich_rows(cursor, rows)

        return {
            "table": TRACKING_TABLE,
            "enriched_from": [
                ORDERS_TABLE,
                CUSTOMERS_TABLE,
                SHIPPERS_TABLE,
                WAREHOUSE_TABLE,
            ],
            "filters": {"order_no": order_no, "tracking_no": tracking_no, "year": year, "min_order_no": min_order_no},
            "count": len(rows),
            "tracking": rows,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.get("/stats/by-year", dependencies=[Depends(verify_api_key)])
def shipments_by_year(
    year: Optional[int] = Query(
        None, description="Single year (overrides from_year/to_year)"
    ),
    from_year: Optional[int] = Query(None, description="Earliest year (inclusive)"),
    to_year: Optional[int] = Query(None, description="Latest year (inclusive)"),
):
    """
    Shipment counts grouped by year and carrier, for the stats dashboard.

    Returns:
      {
        "years":   [2024, 2025, 2026],
        "carriers": ["ESTES Express Lines", ...],
        "rows":    [{ "year": 2026, "carrier": "ESTES...", "shipments": 12,
                      "total_gross_weight": 14736.0 }, ...],
        "totals_by_year": [{ "year": 2026, "shipments": 40 }, ...]
      }

    Aggregation is done in Python over the Shipping table joined (in memory) to
    the order's carrier, so it tolerates schema spelling differences. Carrier
    falls back to "Unknown" when it can't be resolved.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        if year is not None:
            from_year = to_year = year

        order_carrier = _order_carrier_map(cursor)

        # Pull date + weight + delivery dates + order id for every shipment.
        cursor.execute(
            f"SELECT {_bracket(ORDER_COLUMN)}, {_bracket(SHIPMENT_DATE_COLUMN)}, "
            f"{_bracket('TotalGrossWt')}, {_bracket(EST_DELIVERY_COLUMN)}, "
            f"{_bracket(ACTUAL_DELIVERY_COLUMN)} FROM {_bracket(TRACKING_TABLE)}"
        )
        shipments = cursor.fetchall()

        # (year, carrier) -> [count, weight, late]
        agg: dict = {}
        for order_id, ship_date, gross_wt, est_dt, actual_dt in shipments:
            year = _year_of(ship_date)
            if year is None:
                continue
            if from_year is not None and year < from_year:
                continue
            if to_year is not None and year > to_year:
                continue
            carrier = order_carrier.get(str(order_id)) or CARRIER_UNKNOWN_LABEL
            key = (year, carrier)
            # [count, weight, late, delivered, late_days_sum]
            bucket = agg.setdefault(key, [0, 0.0, 0, 0, 0])
            bucket[0] += 1
            try:
                bucket[1] += float(gross_wt) if gross_wt is not None else 0.0
            except (TypeError, ValueError):
                pass
            # Only shipments with BOTH an estimate and an actual delivery are
            # "delivered" (measurable). Among those, late = delivered after the
            # estimate. Compared by calendar date so a same-day delivery with a
            # time component isn't flagged late.
            est = _as_date(est_dt)
            actual = _as_date(actual_dt)
            if est is not None and actual is not None:
                bucket[3] += 1
                days = (actual.date() - est.date()).days
                if days > 0:
                    bucket[2] += 1
                    bucket[4] += days

        rows = [
            {
                "year": year,
                "carrier": carrier,
                "shipments": cnt,
                "total_gross_weight": round(wt, 2),
                "late": late,
                "delivered": delivered,  # have both est + actual (late denominator)
                "late_days": late_days,  # total days late across late shipments
                # mean days late over the late shipments only (0 when none late)
                "avg_days_late": round(late_days / late, 1) if late else 0,
            }
            for (year, carrier), (cnt, wt, late, delivered, late_days) in agg.items()
        ]
        rows.sort(key=lambda r: (r["year"], -r["shipments"]))

        years = sorted({r["year"] for r in rows})
        carriers = sorted({r["carrier"] for r in rows})

        def _year_avg(y: int) -> float:
            late_sum = sum(r["late"] for r in rows if r["year"] == y)
            days_sum = sum(r["late_days"] for r in rows if r["year"] == y)
            return round(days_sum / late_sum, 1) if late_sum else 0

        totals_by_year = [
            {
                "year": y,
                "shipments": sum(r["shipments"] for r in rows if r["year"] == y),
                "late": sum(r["late"] for r in rows if r["year"] == y),
                "delivered": sum(r["delivered"] for r in rows if r["year"] == y),
                "avg_days_late": _year_avg(y),
            }
            for y in years
        ]

        return {
            "years": years,
            "carriers": carriers,
            "rows": rows,
            "totals_by_year": totals_by_year,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.get("/stats/late", dependencies=[Depends(verify_api_key)])
def late_shipments(
    year: Optional[int] = Query(
        None, description="Single year (overrides from_year/to_year)"
    ),
    from_year: Optional[int] = Query(None, description="Earliest year (inclusive)"),
    to_year: Optional[int] = Query(None, description="Latest year (inclusive)"),
    limit: int = Query(1000, ge=1, le=10000),
):
    """
    Verification list: every shipment counted as LATE, with the dates behind the
    flag so the numbers can be cross-checked against the Access form.

    Each row: order_no, pro_no, carrier, ship_date, est_delivery, actual_delivery,
    days_late. `summary` echoes the counts (late / delivered / total) so they can
    be reconciled with /stats/by-year.

      GET /api/tracking/stats/late?year=2026
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        if year is not None:
            from_year = to_year = year

        order_carrier = _order_carrier_map(cursor)

        cursor.execute(
            f"SELECT {_bracket(ORDER_COLUMN)}, {_bracket('ShippingID')}, "
            f"{_bracket(TRACKING_COLUMN)}, {_bracket(SHIPMENT_DATE_COLUMN)}, "
            f"{_bracket(EST_DELIVERY_COLUMN)}, {_bracket(ACTUAL_DELIVERY_COLUMN)} "
            f"FROM {_bracket(TRACKING_TABLE)}"
        )

        total = 0
        delivered = 0
        late_rows = []
        for order_id, shipping_id, pro_no, ship_date, est_dt, actual_dt in cursor.fetchall():
            yr = _year_of(ship_date)
            if yr is None:
                continue
            if from_year is not None and yr < from_year:
                continue
            if to_year is not None and yr > to_year:
                continue

            total += 1
            est = _as_date(est_dt)
            actual = _as_date(actual_dt)
            if est is None or actual is None:
                continue
            delivered += 1
            if actual.date() > est.date():
                late_rows.append(
                    {
                        "year": yr,
                        "order_no": order_id,
                        "shipping_id": shipping_id,
                        "pro_no": pro_no,
                        "carrier": order_carrier.get(str(order_id))
                        or CARRIER_UNKNOWN_LABEL,
                        "ship_date": ship_date.strftime("%m/%d/%Y")
                        if isinstance(ship_date, datetime)
                        else ship_date,
                        "est_delivery": est.strftime("%m/%d/%Y"),
                        "actual_delivery": actual.strftime("%m/%d/%Y"),
                        "days_late": (actual.date() - est.date()).days,
                    }
                )

        late_rows.sort(key=lambda r: r["days_late"], reverse=True)

        return {
            "summary": {
                "total_shipments": total,
                "delivered": delivered,
                "late": len(late_rows),
                "late_pct_of_delivered": round(
                    100 * len(late_rows) / delivered, 1
                )
                if delivered
                else 0,
            },
            "count": len(late_rows[:limit]),
            "late_shipments": late_rows[:limit],
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.get("/debug/tables", dependencies=[Depends(verify_api_key)])
def list_tables():
    """
    List all user tables in the Access DB so you can locate the tracking table,
    then set TRACKING_TABLE at the top of this file.

    Example: GET /api/tracking/debug/tables
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        tables = [
            row.table_name
            for row in cursor.tables(tableType="TABLE")
            if not str(row.table_name).startswith("MSys")
        ]
        return {"count": len(tables), "tables": sorted(tables)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.get("/debug/columns", dependencies=[Depends(verify_api_key)])
def list_columns(
    table: str = Query(..., description="Table or query name, e.g. Shipping")
):
    """
    Show the column names for a given table/query so you can confirm the exact
    spelling of ORDER_COLUMN and TRACKING_COLUMN.

    Example: GET /api/tracking/debug/columns?table=Shipping
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(f"SELECT TOP 1 * FROM {_bracket(table)}")
        columns = [col[0] for col in cursor.description]
        return {"table": table, "columns": columns}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()
