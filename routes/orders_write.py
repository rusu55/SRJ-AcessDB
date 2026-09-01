"""
Sales-order WRITE path for the Access database - built for the CRMReports
"New SO" form.

THE NUMBER IS BORN HERE: SRJ's process takes the next available OrderID from
Access and uses that same number for the Sage 100 sales order (Sage stores it
zero-padded to 7: 43087 -> "0043087"). So /create inserts the Orders header +
Order Details rows first (claiming the number), Sage is created with it
second, and /delete/{id} is the compensation when Sage refuses.

SAFETY - TWO GATES, BOTH REQUIRED FOR A REAL WRITE:
  1. dry_run defaults TRUE and returns the exact SQL + parameters it WOULD
     run, plus every validation, writing nothing.
  2. Even with dry_run=false, live writes are refused unless the environment
     variable ACCESS_WRITES_ENABLED=1 is set on the API process. The
     production instance simply never sets it; only a test instance pointed
     at a COPY of the .mdb does. Danger requires two deliberate steps.
"""
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from database.connection import get_db_connection
from middleware.auth import verify_api_key

router = APIRouter()


def _writes_enabled() -> bool:
    return os.getenv("ACCESS_WRITES_ENABLED", "") == "1"


def _rows(cursor) -> List[Dict[str, Any]]:
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Read helpers the form needs
# ---------------------------------------------------------------------------
@router.get("/next-id", dependencies=[Depends(verify_api_key)])
def next_order_id():
    """Next available OrderID = numeric MAX + 1. VAL() tolerates the text
    column; a read here does NOT reserve the number - only /create does,
    by inserting."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(VAL(OrderID)) FROM Orders")
        row = cursor.fetchone()
        current = int(row[0] or 0)
        return {"currentMax": current, "nextOrderId": str(current + 1)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"next-id failed: {e}")
    finally:
        conn.close()


@router.get("/lookups", dependencies=[Depends(verify_api_key)])
def order_lookups():
    """Dropdown values, the way the Access combos effectively behave:
    DISTINCT of what is already in Orders (free-typing stays allowed)."""
    fields = ["PaymentTerm", "ShipVia", "PrePaidOrCollect", "OrderSize",
              "SalesmanID", "OrderSourceID", "BillFreightTo", "PriceTerm"]
    out: Dict[str, List[str]] = {}
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        for f in fields:
            try:
                cursor.execute(
                    f"SELECT DISTINCT [{f}] FROM Orders WHERE [{f}] IS NOT NULL")
                vals = sorted({str(r[0]).strip() for r in cursor.fetchall()
                               if str(r[0] or "").strip()})
                out[f] = vals[:200]
            except Exception:
                out[f] = []
        return out
    finally:
        conn.close()


@router.get("/customers-list", dependencies=[Depends(verify_api_key)])
def customers_list(q: Optional[str] = None, limit: int = 3000):
    """Customers for the picker. CustomerID here IS the Sage customer code
    (proven: 'INTERA'), so one pick serves both systems. All columns - the
    CRM maps names with fallbacks rather than this API guessing."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if q:
            cursor.execute(
                f"SELECT TOP {int(limit)} * FROM Customers "
                f"WHERE CustomerID LIKE ? OR CompanyName LIKE ?",
                (f"%{q}%", f"%{q}%"))
        else:
            cursor.execute(f"SELECT TOP {int(limit)} * FROM Customers")
        return {"data": _rows(cursor)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"customers failed: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
class OrderLineIn(BaseModel):
    productId: str
    orderQty: float
    orderUnitPrice: float
    requiredDate: Optional[str] = None   # YYYY-MM-DD
    warehouseId: str = "SRJ"
    mcMake: Optional[str] = None         # [Order Details].MCMake
    mcModel: Optional[str] = None        # [Order Details].MCModel
    # Shipping columns on [Order Details] - normally filled after the order
    # ships, but the form allows entering them. Access only; never sent to Sage.
    shippingId: Optional[str] = None     # ShippingID       VARCHAR(7)
    shippingDate: Optional[str] = None   # ShippingDate     DATETIME (YYYY-MM-DD)
    orderShippedQty: Optional[int] = None  # OrderShippedQty SMALLINT
    prodLotNoId: Optional[str] = None    # ProdLotNoID      VARCHAR(255)
    skidOrPalletNo: Optional[int] = None # SkidOrPalletNo   INTEGER
    unitNo: Optional[str] = None         # UnitNo           VARCHAR(20)
    noPack: Optional[int] = None         # NoPack           INTEGER


class OrderCreateIn(BaseModel):
    customerId: str
    orderDate: Optional[str] = None      # YYYY-MM-DD, default today
    customerOrderNumber: Optional[str] = None
    buyerName: Optional[str] = None
    buyerTelNo: Optional[str] = None
    buyerEmail: Optional[str] = None
    paymentTerm: Optional[str] = None
    orderSize: Optional[str] = None
    codAmount: Optional[float] = None
    priceTerm: Optional[str] = None
    shipVia: Optional[str] = None
    priceTermPlace: Optional[str] = None
    handlingCharge: float = 0
    freight: float = 0
    salesTax: float = 0   # FRACTION: 0.07 = 7% (matches Orders.SalesTax)
    salesmanId: Optional[int] = None   # Orders.SalesmanID is INTEGER
    orderSourceId: Optional[str] = None
    shipName: Optional[str] = None
    attentionOrDepartment: Optional[str] = None
    shipAddress: Optional[str] = None
    shipCity: Optional[str] = None
    shipRegion: Optional[str] = None
    shipPostalCode: Optional[str] = None
    shipCountry: Optional[str] = None
    shipPhoneNumber: Optional[str] = None
    shipContact: Optional[str] = None
    shipContactEmail: Optional[str] = None
    urStoreId: Optional[str] = None
    rentalStoreId: Optional[str] = None
    billFreightTo: Optional[str] = None
    prePaidOrCollect: Optional[str] = None
    specialInstructionsFreight: Optional[str] = None
    nameOrderEntry: Optional[str] = None
    lines: List[OrderLineIn] = Field(min_length=1)
    dry_run: bool = True


def _mdy(iso: Optional[str]) -> Optional[str]:
    """Access date literal from YYYY-MM-DD; None passes through as NULL."""
    if not iso:
        return None
    d = datetime.strptime(iso.strip()[:10], "%Y-%m-%d")
    return d.strftime("%m/%d/%Y")


# Orders column widths straight from the Access schema - a value longer than
# this makes the INSERT fail, so the dry run reports it as a problem first.
_ORDERS_WIDTHS = {
    "OrderID": 5, "CustomerID": 6, "OrderSourceID": 50,
    "CustomerOrderNumber": 30, "BuyerName": 30, "BuyerTelNo": 30,
    "BuyerEmail": 30, "PaymentTerm": 50, "OrderSize": 50, "PriceTerm": 20,
    "ShipVia": 7, "PriceTermPlace": 20, "ShipName": 80,
    "AttentionOrDepartment": 50, "ShipAddress": 60, "ShipCity": 20,
    "ShipRegion": 15, "ShipPostalCode": 10, "ShipCountry": 10,
    "ShipPhoneNumber": 50, "ShipContact": 30, "URStoreID": 10,
    "RentalStoreID": 20, "ShipContactEmail": 30, "BillFreightTo": 255,
    "PrePaidOrCollect": 50, "SpecialInstructionsFreight": 255,
    "NameOrderEntry": 20,
}


_LINE_WIDTHS = {"OrderID": 5, "ProductID": 4, "ShippingID": 7,
                "ProdLotNoID": 255, "UnitNo": 20, "WarehouseID": 6,
                "MCMake": 25, "MCModel": 25, "SurveyComment": 255}


# ---------------------------------------------------------------------------
# LINE IDENTITY.
#
# [Order Details] has NO primary key and no autonumber, so a line can only be
# addressed by its VALUES. Keying on OrderID + ProductID + OrderQty +
# OrderUnitPrice alone is not enough: an order may legitimately carry the same
# product twice at the same quantity and price, and then one UPDATE rewrites
# both rows and one DELETE removes both. This is the widest reliable signature
# - every scalar column that distinguishes one line from another.
#
# Even this is not guaranteed unique (two lines can be identical in every
# column), so callers must ALSO check how many rows share a signature and fall
# back to rewriting the whole group. See _row_predicate and the group rewrite
# in update_order.
_SIG_COLS = ["ProductID", "RequiredDate", "OrderQty", "OrderUnitPrice",
             "WarehouseID", "MCMake", "MCModel", "ShippingID", "ShippingDate",
             "OrderShippedQty", "ProdLotNoID", "SkidOrPalletNo", "UnitNo",
             "NoPack", "RTMCHour"]


def _row_predicate(order_id: str, row: Dict[str, Any],
                   sig_cols: List[str]) -> tuple:
    """WHERE fragment + parameters addressing rows that look like `row`.

    NULL needs `IS NULL`, not `= ?` - in SQL nothing equals NULL, so a column
    left empty would silently make the predicate match no rows at all.
    """
    where = ["OrderID = ?"]
    vals: List[Any] = [order_id]
    for c in sig_cols:
        v = row.get(c)
        if v is None:
            where.append(f"[{c}] IS NULL")
        else:
            where.append(f"[{c}] = ?")
            vals.append(v)
    return " AND ".join(where), vals


def _too_long(cols: List[str], vals: List[Any]) -> List[str]:
    out = []
    for c, v in zip(cols, vals):
        cap = _ORDERS_WIDTHS.get(c) or _LINE_WIDTHS.get(c)
        if cap and isinstance(v, str) and len(v) > cap:
            out.append(f"{c} is {len(v)} chars, Access allows {cap}: {v[:40]!r}…")
    return out


@router.post("/create", dependencies=[Depends(verify_api_key)])
def create_order(body: OrderCreateIn):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        problems: List[str] = []

        # Customer must exist (its ID doubles as the Sage customer code).
        cursor.execute("SELECT CustomerID, CompanyName FROM Customers WHERE CustomerID = ?",
                       (body.customerId,))
        cust = cursor.fetchone()
        if not cust:
            problems.append(f"Customer {body.customerId!r} not in Customers")

        # Every product must exist; carry its ItemNo back for the Sage side.
        line_info = []
        for i, ln in enumerate(body.lines, 1):
            cursor.execute(
                "SELECT ProductID, ItemNo, ProductName FROM Products WHERE ProductID = ?",
                (ln.productId,))
            prod = cursor.fetchone()
            if not prod:
                problems.append(f"Line {i}: ProductID {ln.productId!r} not in Products")
                line_info.append({"line": i, "productId": ln.productId,
                                  "itemNo": None, "productName": None})
            else:
                line_info.append({"line": i, "productId": str(prod[0]),
                                  "itemNo": str(prod[1] or "").strip() or None,
                                  "productName": str(prod[2] or "").strip() or None})
            if ln.orderQty <= 0:
                problems.append(f"Line {i}: quantity must be positive")

        # The number is claimed by THIS insert.
        cursor.execute("SELECT MAX(VAL(OrderID)) FROM Orders")
        next_id = str(int(cursor.fetchone()[0] or 0) + 1)

        # Orders.SalesTax is a fraction (0.0825 = 8.25%). If a caller sends a
        # whole percent by mistake, fold it down rather than writing 825%.
        sales_tax = float(body.salesTax or 0)
        tax_note = None
        if sales_tax > 1:
            tax_note = (f"salesTax {sales_tax} looked like a whole percent - "
                        f"stored as {sales_tax / 100} (Access keeps a fraction)")
            sales_tax = sales_tax / 100

        order_date = _mdy(body.orderDate) or datetime.now().strftime("%m/%d/%Y")
        entry_date = datetime.now().strftime("%m/%d/%Y")

        header_cols = ["OrderID", "CustomerID", "OrderSourceID", "OrderDate",
                       "CustomerOrderNumber", "BuyerName", "BuyerTelNo",
                       "BuyerEmail", "PaymentTerm", "OrderSize", "CODAmount",
                       "PriceTerm", "ShipVia", "PriceTermPlace",
                       "HandlingCharge", "Freight", "SalesTax", "SalesmanID",
                       "ShipName", "AttentionOrDepartment", "ShipAddress",
                       "ShipCity", "ShipRegion", "ShipPostalCode",
                       "ShipCountry", "ShipPhoneNumber", "ShipContact",
                       "URStoreID", "RentalStoreID", "ShipContactEmail",
                       "BillFreightTo", "PrePaidOrCollect",
                       "SpecialInstructionsFreight", "NameOrderEntry",
                       "OrderEntryDate"]
        header_vals = [next_id, body.customerId, body.orderSourceId, order_date,
                       body.customerOrderNumber, body.buyerName, body.buyerTelNo,
                       body.buyerEmail, body.paymentTerm, body.orderSize,
                       body.codAmount, body.priceTerm, body.shipVia,
                       body.priceTermPlace, body.handlingCharge, body.freight,
                       sales_tax, body.salesmanId, body.shipName,
                       body.attentionOrDepartment, body.shipAddress,
                       body.shipCity, body.shipRegion, body.shipPostalCode,
                       body.shipCountry, body.shipPhoneNumber, body.shipContact,
                       body.urStoreId, body.rentalStoreId, body.shipContactEmail,
                       body.billFreightTo, body.prePaidOrCollect,
                       body.specialInstructionsFreight, body.nameOrderEntry,
                       entry_date]
        problems.extend(_too_long(header_cols, header_vals))

        header_sql = (f"INSERT INTO Orders ([{'], ['.join(header_cols)}]) "
                      f"VALUES ({', '.join('?' * len(header_cols))})")

        # Order entry writes only the columns it actually has values for.
        # A traced unshipped order (44295) leaves WarehouseID / MCMake /
        # MCModel NULL, so blanks are omitted rather than written as "" -
        # that keeps CRM orders indistinguishable from hand-entered ones.
        line_inserts = []
        for ln in body.lines:
            cols = ["OrderID", "ProductID", "RequiredDate", "OrderQty",
                    "OrderUnitPrice"]
            vals: List[Any] = [next_id, ln.productId, _mdy(ln.requiredDate),
                               ln.orderQty, ln.orderUnitPrice]
            # text columns - added only when they carry something
            for col, val in (("WarehouseID", ln.warehouseId),
                             ("MCMake", ln.mcMake),
                             ("MCModel", ln.mcModel),
                             ("ShippingID", ln.shippingId),
                             ("ProdLotNoID", ln.prodLotNoId),
                             ("UnitNo", ln.unitNo)):
                if val is not None and str(val).strip() != "":
                    cols.append(col)
                    vals.append(str(val).strip())
            # a real date, not a string
            if ln.shippingDate and str(ln.shippingDate).strip():
                cols.append("ShippingDate")
                vals.append(_mdy(ln.shippingDate))
            # numerics - 0 is a legitimate value, so only None is skipped
            for col, num in (("OrderShippedQty", ln.orderShippedQty),
                             ("SkidOrPalletNo", ln.skidOrPalletNo),
                             ("NoPack", ln.noPack)):
                if num is not None:
                    cols.append(col)
                    vals.append(num)
            sql = (f"INSERT INTO [Order Details] ([{'], ['.join(cols)}]) "
                   f"VALUES ({', '.join('?' * len(cols))})")
            problems.extend(_too_long(cols, vals))
            line_inserts.append({"sql": sql, "values": vals})

        plan = {
            "nextOrderId": next_id,
            "sageSalesOrderNo": next_id.zfill(7),
            "customer": {"customerId": body.customerId,
                         "companyName": str(cust[1]) if cust else None},
            "lines": line_info,
            "headerInsert": {"sql": header_sql,
                             "values": [str(v) if v is not None else None
                                        for v in header_vals]},
            "lineInserts": [{"sql": li["sql"],
                             "values": [str(v) if v is not None else None
                                        for v in li["values"]]}
                            for li in line_inserts],
            "problems": problems,
            "salesTaxStored": sales_tax,
            "notes": [n for n in [tax_note] if n],
        }

        if body.dry_run:
            return {"dry_run": True, "wouldWrite": not problems, **plan}

        if problems:
            raise HTTPException(status_code=400,
                                detail="; ".join(problems))
        if not _writes_enabled():
            raise HTTPException(
                status_code=403,
                detail="Live Access writes are DISABLED on this instance "
                       "(set ACCESS_WRITES_ENABLED=1 on a test instance "
                       "pointed at a COPY of the database).")

        cursor.execute(header_sql, header_vals)
        for li in line_inserts:
            cursor.execute(li["sql"], li["values"])
        conn.commit()
        return {"dry_run": False, "written": True, **plan}
    except HTTPException:
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"create failed: {e}")
    finally:
        conn.close()


@router.delete("/delete/{order_id}", dependencies=[Depends(verify_api_key)])
def delete_order(order_id: str, dry_run: bool = True):
    """Compensation ONLY: remove an order this API just created, when the
    Sage side refused. Same two gates as /create."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM [Order Details] WHERE OrderID = ?",
                       (order_id,))
        n_lines = int(cursor.fetchone()[0] or 0)
        cursor.execute("SELECT COUNT(*) FROM Orders WHERE OrderID = ?", (order_id,))
        n_hdr = int(cursor.fetchone()[0] or 0)
        # A shipped/served order must never be deletable through this path.
        cursor.execute(
            "SELECT COUNT(*) FROM [Order Details] "
            "WHERE OrderID = ? AND (OrderShippedQty > 0 OR ShippingID IS NOT NULL)",
            (order_id,))
        n_shipped = int(cursor.fetchone()[0] or 0)
        if n_shipped > 0:
            raise HTTPException(status_code=409,
                                detail=f"Order {order_id} has shipped lines - refusing.")
        if dry_run:
            return {"dry_run": True, "orderId": order_id,
                    "wouldDelete": {"headerRows": n_hdr, "lineRows": n_lines}}
        if not _writes_enabled():
            raise HTTPException(status_code=403,
                                detail="Live Access writes are DISABLED on this instance.")
        cursor.execute("DELETE FROM [Order Details] WHERE OrderID = ?", (order_id,))
        cursor.execute("DELETE FROM Orders WHERE OrderID = ?", (order_id,))
        conn.commit()
        return {"dry_run": False, "orderId": order_id,
                "deleted": {"headerRows": n_hdr, "lineRows": n_lines}}
    except HTTPException:
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"delete failed: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Price assist: what did we last sell this product for?
# ---------------------------------------------------------------------------
@router.get("/last-price", dependencies=[Depends(verify_api_key)])
def last_price(product_id: str, customer_id: Optional[str] = None):
    """Read-only price helper for the New-SO form.

    Most recent non-zero OrderUnitPrice for the product: for THIS customer if
    we can get it, otherwise the last sale to anyone. OrderID is text in
    Access, so VAL() is what sorts it newest-first.

    Never raises - the form must stay usable even if pricing history is
    unreadable; failures come back as {suggestedPrice: null, error: ...}.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        def row_out(r):
            if not r:
                return None
            return {
                "price": float(r[0]) if r[0] is not None else None,
                "orderId": str(r[1]).strip() if r[1] is not None else None,
                "orderDate": str(r[2]).strip() if len(r) > 2 and r[2] is not None else None,
                "customerId": str(r[3]).strip() if len(r) > 3 and r[3] is not None else None,
            }

        # Last sale to anyone - no join, so no text/number mismatch risk.
        any_customer = None
        try:
            cursor.execute(
                "SELECT TOP 1 od.OrderUnitPrice, od.OrderID, NULL, NULL "
                "FROM [Order Details] od "
                "WHERE od.ProductID = ? AND od.OrderUnitPrice > 0 "
                "ORDER BY VAL(od.OrderID) DESC",
                [product_id],
            )
            any_customer = row_out(cursor.fetchone())
        except Exception:
            any_customer = None

        # Same product, same customer. Needs a join, which Access will refuse
        # if the two OrderID columns differ in type - hence the try.
        for_customer = None
        if customer_id:
            try:
                cursor.execute(
                    "SELECT TOP 1 od.OrderUnitPrice, od.OrderID, o.OrderDate, o.CustomerID "
                    "FROM [Order Details] od INNER JOIN Orders o "
                    "ON od.OrderID = o.OrderID "
                    "WHERE od.ProductID = ? AND o.CustomerID = ? "
                    "AND od.OrderUnitPrice > 0 "
                    "ORDER BY VAL(od.OrderID) DESC",
                    [product_id, customer_id],
                )
                for_customer = row_out(cursor.fetchone())
            except Exception:
                for_customer = None

        suggested = for_customer or any_customer
        return {
            "productId": product_id,
            "customerId": customer_id,
            "suggestedPrice": suggested["price"] if suggested else None,
            "source": "customer" if for_customer else ("last-sale" if any_customer else None),
            "forCustomer": for_customer,
            "lastSale": any_customer,
        }
    except Exception as e:
        return {
            "productId": product_id,
            "customerId": customer_id,
            "suggestedPrice": None,
            "source": None,
            "error": f"last-price failed: {e}",
        }
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Salesmen: the form should show people, not codes
# ---------------------------------------------------------------------------
_SALESMAN_TABLES = ["Salesman", "Salesmen", "SalesMan", "SalesPerson",
                    "Salespersons", "SalesPeople", "Employees", "Employee",
                    "Staff", "Users"]
_ID_COLS = ["SalesmanID", "SalesManID", "SalesID", "SalespersonID",
            "EmployeeID", "UserID", "ID", "Initials", "Code"]
_NAME_COLS = ["SalesmanName", "SalespersonName", "FullName", "Name",
              "EmployeeName", "DisplayName", "Description"]
_FIRST_COLS = ["FirstName", "First", "GivenName"]
_LAST_COLS = ["LastName", "Last", "Surname", "FamilyName"]


def _find(cols: List[str], wanted: List[str]) -> Optional[str]:
    low = {c.lower(): c for c in cols}
    for w in wanted:
        if w.lower() in low:
            return low[w.lower()]
    return None


@router.get("/salesmen", dependencies=[Depends(verify_api_key)])
def salesmen():
    """[{id, name}] for the SalesmanID picker.

    Access keeps the person's name in a lookup table whose name varies between
    installs, so probe the usual candidates and report which one answered.
    Falls back to the DISTINCT SalesmanID values already used on orders, so the
    picker is never empty.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        for tbl in _SALESMAN_TABLES:
            try:
                cursor.execute(f"SELECT TOP 1 * FROM [{tbl}]")
            except Exception:
                continue
            cols = [c[0] for c in cursor.description]
            cursor.fetchall()

            id_col = _find(cols, _ID_COLS)
            if not id_col:
                continue
            name_col = _find(cols, _NAME_COLS)
            first_col = _find(cols, _FIRST_COLS)
            last_col = _find(cols, _LAST_COLS)
            if not (name_col or first_col or last_col):
                continue

            select = [f"[{id_col}]"]
            for c in (name_col, first_col, last_col):
                if c:
                    select.append(f"[{c}]")
            try:
                cursor.execute(f"SELECT {', '.join(select)} FROM [{tbl}]")
                rows = cursor.fetchall()
            except Exception:
                continue

            out = []
            for r in rows:
                sid = str(r[0] or "").strip()
                if not sid:
                    continue
                parts = [str(v or "").strip() for v in r[1:]]
                if name_col:
                    nm = parts[0]
                    rest = [p for p in parts[1:] if p]
                    if not nm and rest:
                        nm = " ".join(rest)
                else:
                    nm = " ".join(p for p in parts if p)
                out.append({"id": sid, "name": nm or sid})

            if out:
                seen, uniq = set(), []
                for o in sorted(out, key=lambda x: x["name"].lower()):
                    if o["id"] in seen:
                        continue
                    seen.add(o["id"])
                    uniq.append(o)
                return {"source": tbl, "idColumn": id_col, "salesmen": uniq}

        # Nothing matched - fall back to the codes seen on existing orders.
        cursor.execute(
            "SELECT DISTINCT [SalesmanID] FROM Orders WHERE [SalesmanID] IS NOT NULL")
        codes = sorted({str(r[0]).strip() for r in cursor.fetchall()
                        if str(r[0] or "").strip()})
        return {
            "source": None,
            "salesmen": [{"id": c, "name": c} for c in codes],
            "note": "no salesman table found - showing codes from Orders",
        }
    except Exception as e:
        return {"source": None, "salesmen": [], "error": f"salesmen failed: {e}"}
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Customer categories: Customers.CategoryID -> readable name
# ---------------------------------------------------------------------------
_CATEGORY_TABLES = ["Categories", "Category", "CustomerCategory",
                    "CustomerCategories", "CustomerType", "CustomerTypes"]
_CAT_ID_COLS = ["CategoryID", "CustomerCategoryID", "CustomerTypeID", "ID", "Code"]
_CAT_NAME_COLS = ["CategoryName", "Category", "Name", "Description",
                  "CustomerType", "TypeName", "Title"]


@router.get("/categories", dependencies=[Depends(verify_api_key)])
def categories():
    """[{id, name}] so the form can show 'Contractors' instead of a CategoryID."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        for tbl in _CATEGORY_TABLES:
            try:
                cursor.execute(f"SELECT TOP 1 * FROM [{tbl}]")
            except Exception:
                continue
            cols = [c[0] for c in cursor.description]
            cursor.fetchall()
            id_col = _find(cols, _CAT_ID_COLS)
            name_col = _find(cols, [c for c in _CAT_NAME_COLS if c != id_col])
            if not id_col or not name_col:
                continue
            try:
                cursor.execute(f"SELECT [{id_col}], [{name_col}] FROM [{tbl}]")
                rows = cursor.fetchall()
            except Exception:
                continue
            out = []
            for r in rows:
                cid = str(r[0] or "").strip()
                if not cid:
                    continue
                out.append({"id": cid, "name": str(r[1] or "").strip() or cid})
            if out:
                return {"source": tbl, "categories": out}
        return {"source": None, "categories": [],
                "note": "no category table found - CategoryID shown as-is"}
    except Exception as e:
        return {"source": None, "categories": [], "error": f"categories failed: {e}"}
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Schema dump - read-only, so the field mapping can be built from facts
# ---------------------------------------------------------------------------
@router.get("/schema", dependencies=[Depends(verify_api_key)])
def schema(table: Optional[str] = None, sample: bool = False):
    """Tables and columns in the Access file.

    /schema                      -> every user table with its columns + types
    /schema?table=Orders         -> just that table
    /schema?table=Orders&sample=true -> plus one recent row, to see formats
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        names: List[str] = []
        for t in cursor.tables(tableType="TABLE"):
            n = t.table_name
            if n and not n.startswith("MSys"):
                names.append(n)
        names.sort(key=str.lower)
        if table:
            wanted = table.strip().lower()
            names = [n for n in names if n.lower() == wanted]
            if not names:
                return {"error": f"table {table!r} not found",
                        "availableTables": [t.table_name for t in
                                            cursor.tables(tableType="TABLE")
                                            if not t.table_name.startswith("MSys")]}

        out: Dict[str, Any] = {"database": None, "tables": {}}
        try:
            from config.settings import settings as _s
            out["database"] = _s.ACCESS_DB_PATH
        except Exception:
            pass

        for n in names:
            cols = []
            try:
                for c in cursor.columns(table=n):
                    cols.append({
                        "name": c.column_name,
                        "type": c.type_name,
                        "size": c.column_size,
                        "nullable": bool(getattr(c, "nullable", 1)),
                    })
            except Exception as e:
                cols = [{"error": str(e)}]
            entry: Dict[str, Any] = {"columns": cols}

            if sample and table:
                try:
                    cursor.execute(f"SELECT TOP 1 * FROM [{n}]")
                    r = cursor.fetchone()
                    if r:
                        entry["sampleRow"] = {
                            d[0]: (str(v) if v is not None else None)
                            for d, v in zip(cursor.description, r)
                        }
                except Exception as e:
                    entry["sampleError"] = str(e)

            out["tables"][n] = entry
        out["tableCount"] = len(out["tables"])
        return out
    except Exception as e:
        return {"error": f"schema failed: {e}"}
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Real lookup tables (from the Access schema) - id + label for every picker
# ---------------------------------------------------------------------------
@router.get("/pickers", dependencies=[Depends(verify_api_key)])
def pickers():
    """Bound dropdowns, exactly like the Access combos:

      Salesman      Salesmen.SalesmanID (int)  -> First + Last
      Category      Categories.CategoryID      -> CategoryName
      Ship Via      Shippers.ShipperID         -> ShipperName
      Order Source  [Order Sources].OrderSourceID -> OrderSource
      Warehouse     [Warehouse Code].WarehouseID  -> WarehouseName

    Read-only; a table that fails just comes back empty.
    """
    conn = None
    out: Dict[str, Any] = {"salesmen": [], "categories": [], "shippers": [],
                           "orderSources": [], "warehouses": [], "errors": {}}
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        def grab(key: str, sql: str, label_join: bool = False):
            try:
                cursor.execute(sql)
                rows = []
                for r in cursor.fetchall():
                    rid = str(r[0]).strip() if r[0] is not None else ""
                    if not rid:
                        continue
                    if label_join:
                        parts = [str(v).strip() for v in r[1:] if v is not None
                                 and str(v).strip()]
                        label = " ".join(parts)
                    else:
                        label = str(r[1]).strip() if r[1] is not None else ""
                    rows.append({"id": rid, "name": label or rid})
                seen, uniq = set(), []
                for row in sorted(rows, key=lambda x: x["name"].lower()):
                    if row["id"] in seen:
                        continue
                    seen.add(row["id"])
                    uniq.append(row)
                out[key] = uniq
            except Exception as e:
                out["errors"][key] = str(e)

        grab("salesmen",
             "SELECT [SalesmanID], [FirstName], [LastName] FROM [Salesmen]",
             label_join=True)
        grab("categories",
             "SELECT [CategoryID], [CategoryName] FROM [Categories]")
        grab("shippers",
             "SELECT [ShipperID], [ShipperName] FROM [Shippers]")
        grab("orderSources",
             "SELECT [OrderSourceID], [OrderSource] FROM [Order Sources]")
        grab("warehouses",
             "SELECT [WarehouseID], [WarehouseName] FROM [Warehouse Code]")
        return out
    except Exception as e:
        out["errors"]["connection"] = str(e)
        return out
    finally:
        if conn is not None:
            conn.close()


@router.get("/tax-samples", dependencies=[Depends(verify_api_key)])
def tax_samples():
    """What do non-zero SalesTax values actually look like in Orders?

    Access shows the column as a percent, which displays the stored number
    x100 - so 7.00% is stored either as 0.07 or as 7. This settles it from
    real data instead of guessing.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT TOP 25 [OrderID], [SalesTax] FROM Orders "
            "WHERE [SalesTax] IS NOT NULL AND [SalesTax] <> 0 "
            "ORDER BY VAL([OrderID]) DESC")
        rows = [{"orderId": str(r[0]).strip(), "salesTax": str(r[1])}
                for r in cursor.fetchall()]
        vals = [float(r["salesTax"]) for r in rows] or [0]
        biggest = max(vals)
        verdict = ("stored as a whole percent (7 = 7%)" if biggest > 1
                   else "stored as a fraction (0.07 = 7%)" if rows
                   else "no non-zero SalesTax on file")
        return {"rows": rows, "max": biggest, "verdict": verdict}
    except Exception as e:
        return {"error": f"tax-samples failed: {e}"}
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Machine make / model - for the MCMake + MCModel combos on the line grid
# ---------------------------------------------------------------------------
_MACHINE_SOURCES = [
    # (table, make col, model col, product col or None)
    ("Machine Make Model", "MCMake", "MCModel", "ProductID"),
    ("Products Details - Rubber Tracks", "MCMake", "MCModel", "ProductID"),
    ("Products Details", "MCMake", "MCModel", None),
]


@router.get("/machines", dependencies=[Depends(verify_api_key)])
def machines(product_id: Optional[str] = None):
    """Makes and models pulled from the machine tables.

    makes      - every distinct MCMake, sorted
    pairs      - [{make, model}] so the model list can follow the chosen make
    forProduct - the makes/models actually linked to this product, when a
                 product_id is given (that is the short list a picker wants)
    """
    conn = None
    makes: set = set()
    pairs: set = set()
    for_product: set = set()
    used: List[str] = []
    errors: Dict[str, str] = {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        for tbl, mk, md, pc in _MACHINE_SOURCES:
            try:
                cols = f"[{mk}], [{md}]" + (f", [{pc}]" if pc else "")
                cursor.execute(f"SELECT {cols} FROM [{tbl}]")
                rows = cursor.fetchall()
            except Exception as e:
                errors[tbl] = str(e)
                continue
            used.append(tbl)
            for r in rows:
                make = str(r[0] or "").strip()
                model = str(r[1] or "").strip()
                if make:
                    makes.add(make)
                if make and model:
                    pairs.add((make, model))
                    if pc and product_id:
                        pid = str(r[2] or "").strip()
                        if pid and pid.upper() == product_id.strip().upper():
                            for_product.add((make, model))

        return {
            "sources": used,
            "makes": sorted(makes, key=str.lower),
            "pairs": [{"make": a, "model": b}
                      for a, b in sorted(pairs, key=lambda x: (x[0].lower(),
                                                               x[1].lower()))],
            "forProduct": [{"make": a, "model": b}
                           for a, b in sorted(for_product,
                                              key=lambda x: (x[0].lower(),
                                                             x[1].lower()))],
            "errors": errors,
        }
    except Exception as e:
        return {"makes": [], "pairs": [], "forProduct": [],
                "errors": {"connection": str(e)}}
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Trace one order across every table that has an OrderID - mapping check
# ---------------------------------------------------------------------------
@router.get("/trace/{order_id}", dependencies=[Depends(verify_api_key)])
def trace_order(order_id: str, empty: bool = False):
    """Everything stored for one order, table by table.

    Finds every table with an OrderID column, pulls the matching rows, and
    splits each row into the fields that actually carry data and the ones
    left NULL/blank - so the CRM form's mapping can be checked against what
    order entry really writes.

    /trace/44292             -> populated fields only
    /trace/44292?empty=true  -> also list the empty columns
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        tables = [t.table_name for t in cursor.tables(tableType="TABLE")
                  if t.table_name and not t.table_name.startswith("MSys")]

        hits: Dict[str, Any] = {}
        scanned: List[str] = []
        for tbl in tables:
            # cursor.columns() is unreliable on some Access tables, so read
            # the column names off a real (empty) result set instead.
            try:
                cursor.execute(f"SELECT TOP 1 * FROM [{tbl}]")
                cols = [d[0] for d in cursor.description]
                cursor.fetchall()
            except Exception:
                continue
            key = next((c for c in cols if c.lower() == "orderid"), None)
            if not key:
                continue
            scanned.append(tbl)
            try:
                cursor.execute(f"SELECT * FROM [{tbl}] WHERE [{key}] = ?",
                               (order_id,))
                rows = cursor.fetchall()
                names = [d[0] for d in cursor.description]
            except Exception as e:
                hits[tbl] = {"error": str(e)}
                continue
            if not rows:
                continue

            out_rows = []
            for r in rows:
                filled, blank = {}, []
                for n, v in zip(names, r):
                    if v is None or str(v).strip() == "":
                        blank.append(n)
                    else:
                        filled[n] = str(v)
                row: Dict[str, Any] = {"filled": filled,
                                       "emptyCount": len(blank)}
                if empty:
                    row["empty"] = blank
                out_rows.append(row)
            hits[tbl] = {"rowCount": len(out_rows), "rows": out_rows}

        return {
            "orderId": order_id,
            "tablesWithOrderID": scanned,
            "tablesWithData": [t for t in hits if "error" not in hits[t]],
            "data": hits,
        }
    except Exception as e:
        return {"error": f"trace failed: {e}"}
    finally:
        if conn is not None:
            conn.close()


@router.get("/common-values", dependencies=[Depends(verify_api_key)])
def common_values(limit: int = 15):
    """Most-used values for the free-text header fields, newest orders first.

    Order entry retypes the same freight boilerplate and FOB place every
    time; this turns them into pick-lists ranked by how often they are used.
    """
    fields = ["SpecialInstructionsFreight", "PriceTermPlace", "BillFreightTo",
              "PaymentTerm", "OrderSize", "PrePaidOrCollect", "PriceTerm"]
    conn = None
    out: Dict[str, List[str]] = {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        for f in fields:
            try:
                cursor.execute(
                    f"SELECT [{f}], COUNT(*) AS n FROM Orders "
                    f"WHERE [{f}] IS NOT NULL AND [{f}] <> '' "
                    f"GROUP BY [{f}] ORDER BY COUNT(*) DESC")
                vals = []
                for r in cursor.fetchall():
                    v = str(r[0] or "").strip()
                    if v:
                        vals.append(v)
                    if len(vals) >= limit:
                        break
                out[f] = vals
            except Exception:
                out[f] = []
        return out
    except Exception as e:
        return {"error": f"common-values failed: {e}"}
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# EDIT an existing order
#
# Load, then update. The rule that governs everything here: a line that has
# SHIPPED is history. [Order Details] carries ShippingID / ShippingDate /
# OrderShippedQty; once any of those is set, that line may not be deleted and
# its quantity may not drop below what shipped. Those are reported as locked,
# never silently changed.
# ---------------------------------------------------------------------------
_LINE_EDIT_COLS = ["ProductID", "RequiredDate", "OrderQty", "OrderUnitPrice",
                   "WarehouseID", "MCMake", "MCModel", "ShippingID",
                   "ShippingDate", "OrderShippedQty", "ProdLotNoID",
                   "SkidOrPalletNo", "UnitNo", "NoPack"]


def _line_locked(row: Dict[str, Any]) -> bool:
    """Shipped in any sense => the line is history."""
    shipped_qty = row.get("OrderShippedQty")
    return bool(
        (shipped_qty is not None and float(shipped_qty or 0) > 0)
        or str(row.get("ShippingID") or "").strip()
        or str(row.get("ShippingDate") or "").strip()
    )


# NOTE: path is /edit-load, not /order/{id} — routes/orders.py already owns
# GET /order/{order_id} on this same prefix and is registered first, so a
# duplicate here would simply never be reached.
@router.get("/edit-load/{order_id}", dependencies=[Depends(verify_api_key)])
def load_order(order_id: str):
    """Header + lines for the edit form, with each line marked locked or not."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM Orders WHERE OrderID = ?", (order_id,))
        hdr_row = cursor.fetchone()
        if not hdr_row:
            raise HTTPException(status_code=404,
                                detail=f"Order {order_id} not found")
        header = {d[0]: v for d, v in zip(cursor.description, hdr_row)}

        cursor.execute("SELECT * FROM [Order Details] WHERE OrderID = ?",
                       (order_id,))
        cols = [d[0] for d in cursor.description]
        lines = []
        for i, r in enumerate(cursor.fetchall()):
            row = dict(zip(cols, r))
            row["_index"] = i          # position, the only stable handle we have
            row["_locked"] = _line_locked(row)
            lines.append(row)

        return {
            "orderId": order_id,
            "header": {k: (str(v) if v is not None else None)
                       for k, v in header.items()},
            # Values are stringified for the form, EXCEPT the two synthetic
            # keys: _index must stay a number (it is the line handle the update
            # payload sends back) and _locked must stay a boolean.
            "lines": [
                {
                    k: (
                        v
                        if k in ("_index", "_locked") or isinstance(v, bool) or v is None
                        else str(v)
                    )
                    for k, v in ln.items()
                }
                for ln in lines
            ],
            "lockedLines": sum(1 for l in lines if l["_locked"]),
            "editableLines": sum(1 for l in lines if not l["_locked"]),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"load failed: {e}")
    finally:
        if conn is not None:
            conn.close()


class OrderUpdateLine(BaseModel):
    """A desired line. `keyIndex` points at an existing row's position (from
    /order/{id}); a line without one is new."""
    keyIndex: Optional[int] = None
    productId: str
    orderQty: float
    orderUnitPrice: float
    requiredDate: Optional[str] = None
    warehouseId: Optional[str] = None
    mcMake: Optional[str] = None
    mcModel: Optional[str] = None
    shippingId: Optional[str] = None
    shippingDate: Optional[str] = None
    orderShippedQty: Optional[int] = None
    prodLotNoId: Optional[str] = None
    skidOrPalletNo: Optional[int] = None
    unitNo: Optional[str] = None
    noPack: Optional[int] = None
    # Post-delivery columns — recorded well after the order is entered.
    regCardReceived: Optional[bool] = None      # RTRegCardReceived      BIT
    regCardReceivedDate: Optional[str] = None   # RTRegCardReceivedDate  DATETIME
    machineHour: Optional[int] = None           # RTMCHour               INTEGER
    surveySent: Optional[str] = None            # SurveySent             DATETIME
    surveyReturned: Optional[str] = None        # SurveyReturned         DATETIME
    surveyComment: Optional[str] = None         # SurveyComment          VARCHAR(255)
    surveyDiscount: Optional[bool] = None       # SurveyDisountApplied   BIT


class OrderUpdateIn(BaseModel):
    header: Dict[str, Any] = Field(default_factory=dict)
    lines: List[OrderUpdateLine] = Field(default_factory=list)
    # Remove existing lines whose position is absent from `lines`. Off by
    # default so a partial payload can never wipe an order.
    deleteMissingLines: bool = False
    dry_run: bool = True


# Header fields the edit form may change, mapped to their Access columns.
_HEADER_EDITABLE = {
    "customerId": "CustomerID",
    "orderSourceId": "OrderSourceID",
    "orderDate": "OrderDate",
    "customerOrderNumber": "CustomerOrderNumber",
    "buyerName": "BuyerName",
    "buyerTelNo": "BuyerTelNo",
    "buyerEmail": "BuyerEmail",
    "paymentTerm": "PaymentTerm",
    "orderSize": "OrderSize",
    "codAmount": "CODAmount",
    "priceTerm": "PriceTerm",
    "shipVia": "ShipVia",
    "priceTermPlace": "PriceTermPlace",
    "handlingCharge": "HandlingCharge",
    "freight": "Freight",
    "salesTax": "SalesTax",
    "salesmanId": "SalesmanID",
    "shipName": "ShipName",
    "attentionOrDepartment": "AttentionOrDepartment",
    "shipAddress": "ShipAddress",
    "shipCity": "ShipCity",
    "shipRegion": "ShipRegion",
    "shipPostalCode": "ShipPostalCode",
    "shipCountry": "ShipCountry",
    "shipPhoneNumber": "ShipPhoneNumber",
    "shipContact": "ShipContact",
    "urStoreId": "URStoreID",
    "rentalStoreId": "RentalStoreID",
    "shipContactEmail": "ShipContactEmail",
    "billFreightTo": "BillFreightTo",
    "prePaidOrCollect": "PrePaidOrCollect",
    "specialInstructionsFreight": "SpecialInstructionsFreight",
    "nameOrderEntry": "NameOrderEntry",
}
_DATE_HEADER_COLS = {"OrderDate"}
_NUM_HEADER_COLS = {"CODAmount", "HandlingCharge", "Freight", "SalesTax",
                    "SalesmanID"}


@router.put("/update/{order_id}", dependencies=[Depends(verify_api_key)])
def update_order(order_id: str, body: OrderUpdateIn):
    """Edit an existing order's header and its UNSHIPPED lines.

    Produces the same shape as /create: a full plan of the exact SQL it would
    run, refusing on any problem, and writing only when dry_run is false AND
    the instance has writes enabled.

    Shipped lines are never deleted and never reduced below what shipped —
    those attempts come back as problems.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        problems: List[str] = []
        notes: List[str] = []

        # ---- the order must exist ------------------------------------------
        cursor.execute("SELECT COUNT(*) FROM Orders WHERE OrderID = ?", (order_id,))
        if int(cursor.fetchone()[0] or 0) == 0:
            raise HTTPException(status_code=404,
                                detail=f"Order {order_id} not found")

        # ---- current lines, in position order ------------------------------
        cursor.execute("SELECT * FROM [Order Details] WHERE OrderID = ?",
                       (order_id,))
        cols = [d[0] for d in cursor.description]
        current = [dict(zip(cols, r)) for r in cursor.fetchall()]

        # ---- header --------------------------------------------------------
        header_sets: List[str] = []
        header_vals: List[Any] = []
        header_cols_used: List[str] = []
        for key, col in _HEADER_EDITABLE.items():
            if key not in body.header:
                continue
            val = body.header[key]
            if col in _DATE_HEADER_COLS:
                val = _mdy(val) if val else None
            elif col in _NUM_HEADER_COLS:
                if val in ("", None):
                    val = None
                else:
                    val = float(val)
                    # SalesTax is a fraction in Access (0.07 = 7%).
                    if col == "SalesTax" and val > 1:
                        notes.append(
                            f"salesTax {val} looked like a whole percent - "
                            f"storing {val / 100}")
                        val = val / 100
                    if col == "SalesmanID":
                        val = int(val)
            elif val is not None:
                val = str(val)
            header_sets.append(f"[{col}] = ?")
            header_vals.append(val)
            header_cols_used.append(col)

        problems.extend(_too_long(header_cols_used, header_vals))

        header_sql = None
        if header_sets:
            header_sql = (f"UPDATE Orders SET {', '.join(header_sets)} "
                          f"WHERE OrderID = ?")

        # ---- lines ----------------------------------------------------------
        steps: List[Dict[str, Any]] = []
        seen_indexes: set = set()
        # Line changes are collected here first and turned into SQL at the end,
        # once it is known which lines have an identical twin on the order.
        pending_updates: Dict[int, Dict[str, Any]] = {}
        pending_deletes: set = set()

        for ln in body.lines:
            if ln.keyIndex is None:
                # ---- NEW line ----
                cols_i = ["OrderID", "ProductID", "RequiredDate", "OrderQty",
                          "OrderUnitPrice"]
                vals_i: List[Any] = [order_id, ln.productId,
                                     _mdy(ln.requiredDate), ln.orderQty,
                                     ln.orderUnitPrice]
                for col, val in (("WarehouseID", ln.warehouseId),
                                 ("MCMake", ln.mcMake),
                                 ("MCModel", ln.mcModel),
                                 ("ShippingID", ln.shippingId),
                                 ("ProdLotNoID", ln.prodLotNoId),
                                 ("UnitNo", ln.unitNo)):
                    if val is not None and str(val).strip() != "":
                        cols_i.append(col)
                        vals_i.append(str(val).strip())
                if ln.shippingDate and str(ln.shippingDate).strip():
                    cols_i.append("ShippingDate")
                    vals_i.append(_mdy(ln.shippingDate))
                for col, num in (("OrderShippedQty", ln.orderShippedQty),
                                 ("SkidOrPalletNo", ln.skidOrPalletNo),
                                 ("NoPack", ln.noPack),
                                 ("RTMCHour", ln.machineHour)):
                    if num is not None:
                        cols_i.append(col)
                        vals_i.append(num)
                if ln.surveyComment:
                    cols_i.append("SurveyComment")
                    vals_i.append(ln.surveyComment)
                for col, dt in (("RTRegCardReceivedDate", ln.regCardReceivedDate),
                                ("SurveySent", ln.surveySent),
                                ("SurveyReturned", ln.surveyReturned)):
                    if dt and str(dt).strip():
                        cols_i.append(col)
                        vals_i.append(_mdy(dt))
                for col, flag in (("RTRegCardReceived", ln.regCardReceived),
                                  ("SurveyDisountApplied", ln.surveyDiscount)):
                    if flag is not None:
                        cols_i.append(col)
                        vals_i.append(bool(flag))
                problems.extend(_too_long(cols_i, vals_i))
                steps.append({
                    "op": "insert",
                    "productId": ln.productId,
                    "sql": (f"INSERT INTO [Order Details] "
                            f"([{'], ['.join(cols_i)}]) "
                            f"VALUES ({', '.join('?' * len(cols_i))})"),
                    "values": vals_i,
                })
                continue

            # ---- EXISTING line ----
            if ln.keyIndex < 0 or ln.keyIndex >= len(current):
                problems.append(
                    f"line {ln.keyIndex}: no such line on order {order_id}")
                continue
            seen_indexes.add(ln.keyIndex)
            row = current[ln.keyIndex]
            locked = _line_locked(row)
            shipped = float(row.get("OrderShippedQty") or 0)

            if locked and float(ln.orderQty) < shipped:
                problems.append(
                    f"line {ln.keyIndex} ({row.get('ProductID')}): quantity "
                    f"{ln.orderQty} is below the {shipped:g} already shipped")
                continue

            desired = {
                "ProductID": ln.productId,
                "RequiredDate": _mdy(ln.requiredDate),
                "OrderQty": ln.orderQty,
                "OrderUnitPrice": ln.orderUnitPrice,
                "WarehouseID": (ln.warehouseId or None),
                "MCMake": (ln.mcMake or None),
                "MCModel": (ln.mcModel or None),
                "ShippingID": (ln.shippingId or None),
                "ShippingDate": _mdy(ln.shippingDate) if ln.shippingDate else None,
                "OrderShippedQty": ln.orderShippedQty,
                "ProdLotNoID": (ln.prodLotNoId or None),
                "SkidOrPalletNo": ln.skidOrPalletNo,
                "UnitNo": (ln.unitNo or None),
                "NoPack": ln.noPack,
                "RTRegCardReceived": ln.regCardReceived,
                "RTRegCardReceivedDate": (_mdy(ln.regCardReceivedDate)
                                          if ln.regCardReceivedDate else None),
                "RTMCHour": ln.machineHour,
                "SurveySent": _mdy(ln.surveySent) if ln.surveySent else None,
                "SurveyReturned": (_mdy(ln.surveyReturned)
                                   if ln.surveyReturned else None),
                "SurveyComment": (ln.surveyComment or None),
                "SurveyDisountApplied": ln.surveyDiscount,
            }

            # A locked line keeps its shipping columns and its product.
            if locked:
                for protected in ("ProductID", "ShippingID", "ShippingDate",
                                  "OrderShippedQty"):
                    cur_v = row.get(protected)
                    if desired[protected] is not None and \
                            str(desired[protected]).strip() != str(cur_v or "").strip():
                        notes.append(
                            f"line {ln.keyIndex}: {protected} left as "
                            f"{cur_v!r} — the line has shipped")
                    desired.pop(protected, None)

            sets: List[str] = []
            vals_u: List[Any] = []
            changed_cols: List[str] = []
            for col, want in desired.items():
                cur = row.get(col)
                cur_cmp = "" if cur is None else str(cur).strip()
                want_cmp = "" if want is None else str(want).strip()
                # Dates come back as datetimes; compare on the date part only.
                if col in ("RequiredDate", "ShippingDate") and cur_cmp:
                    cur_cmp = cur_cmp.split(" ")[0]
                    try:
                        cur_cmp = datetime.strptime(cur_cmp, "%Y-%m-%d").strftime("%m/%d/%Y")
                    except Exception:
                        pass
                if col in ("RTRegCardReceived", "SurveyDisountApplied"):
                    # Non-nullable BIT: None means "leave it alone".
                    if want is None or bool(cur) == bool(want):
                        continue
                elif col in ("OrderQty", "OrderUnitPrice", "OrderShippedQty",
                             "SkidOrPalletNo", "NoPack", "RTMCHour"):
                    try:
                        if float(cur or 0) == float(want or 0):
                            continue
                    except Exception:
                        pass
                elif cur_cmp == want_cmp:
                    continue
                sets.append(f"[{col}] = ?")
                vals_u.append(want)
                changed_cols.append(col)

            if not sets:
                continue
            problems.extend(_too_long(changed_cols, vals_u))
            # Recorded, not emitted: the SQL that can address this line safely
            # depends on whether any OTHER line looks identical to it, which is
            # only known once every requested change has been collected.
            pending_updates[ln.keyIndex] = {
                "locked": locked,
                "changed": changed_cols,
                "values": dict(zip(changed_cols, vals_u)),
            }

        # ---- deletions -------------------------------------------------------
        if body.deleteMissingLines:
            for idx, row in enumerate(current):
                if idx in seen_indexes:
                    continue
                if _line_locked(row):
                    problems.append(
                        f"line {idx} ({row.get('ProductID')}) has shipped — "
                        f"refusing to delete it")
                    continue
                pending_deletes.add(idx)

        # ---- turn the collected changes into SQL ----------------------------
        #
        # A line with no twin can be addressed directly by its values. A line
        # that has one CANNOT: `UPDATE ... WHERE <values>` rewrites every twin
        # and `DELETE ... WHERE <values>` removes every twin. For those, the
        # whole look-alike GROUP is deleted and re-inserted from the rows we
        # already read, with the intended change applied to just one of them —
        # every other column is carried across verbatim, so nothing is lost.
        sig_cols = [c for c in _SIG_COLS if c in cols]

        def _sig(r: Dict[str, Any]) -> tuple:
            return tuple(r.get(c) for c in sig_cols)

        sig_counts: Dict[tuple, int] = {}
        for r in current:
            sig_counts[_sig(r)] = sig_counts.get(_sig(r), 0) + 1

        touched = set(pending_updates) | pending_deletes
        # Groups that need the delete-and-reinsert treatment.
        rewrite_sigs = {
            _sig(current[i]) for i in touched if sig_counts[_sig(current[i])] > 1
        }

        for idx in sorted(touched):
            row = current[idx]
            if _sig(row) in rewrite_sigs:
                continue  # handled as a group below
            where, key_vals = _row_predicate(order_id, row, sig_cols)
            if idx in pending_deletes:
                steps.append({
                    "op": "delete",
                    "keyIndex": idx,
                    "productId": row.get("ProductID"),
                    "sql": f"DELETE FROM [Order Details] WHERE {where}",
                    "values": key_vals,
                })
            else:
                up = pending_updates[idx]
                sets = [f"[{c}] = ?" for c in up["changed"]]
                steps.append({
                    "op": "update",
                    "keyIndex": idx,
                    "productId": row.get("ProductID"),
                    "locked": up["locked"],
                    "changed": up["changed"],
                    "sql": (f"UPDATE [Order Details] SET {', '.join(sets)} "
                            f"WHERE {where}"),
                    "values": [up["values"][c] for c in up["changed"]] + key_vals,
                })

        for sig in sorted(rewrite_sigs, key=lambda s: [str(x) for x in s]):
            members = [i for i, r in enumerate(current) if _sig(r) == sig]
            sample = current[members[0]]
            # A shipped line must never be removed, not even for a moment and
            # not even to be written straight back: it carries shipping history
            # this endpoint has no business recreating. Refuse instead.
            locked_members = [i for i in members if _line_locked(current[i])]
            if locked_members:
                problems.append(
                    f"lines {members} ({sample.get('ProductID')}) are identical "
                    f"and line(s) {locked_members} have shipped — this edit "
                    f"cannot be applied to one of them without rewriting a "
                    f"shipped line. Change it in Access directly."
                )
                continue
            where, key_vals = _row_predicate(order_id, sample, sig_cols)
            survivors = []
            for i in members:
                if i in pending_deletes:
                    continue
                r = dict(current[i])
                if i in pending_updates:
                    r.update(pending_updates[i]["values"])
                survivors.append((i, r))
            notes.append(
                f"lines {members} are identical on this order — no key tells "
                f"them apart, so all {len(members)} are removed and "
                f"{len(survivors)} written back with the change applied to the "
                f"one line you edited"
            )
            steps.append({
                "op": "delete-group",
                "keyIndexes": members,
                "productId": sample.get("ProductID"),
                "sql": f"DELETE FROM [Order Details] WHERE {where}",
                "values": key_vals,
            })
            for i, r in survivors:
                ins_cols = [c for c in cols if r.get(c) is not None]
                steps.append({
                    "op": "reinsert",
                    "keyIndex": i,
                    "productId": r.get("ProductID"),
                    "changed": pending_updates.get(i, {}).get("changed", []),
                    "sql": (f"INSERT INTO [Order Details] "
                            f"([{'], ['.join(ins_cols)}]) "
                            f"VALUES ({', '.join('?' * len(ins_cols))})"),
                    "values": [r[c] for c in ins_cols],
                })

        plan = {
            "orderId": order_id,
            "sageSalesOrderNo": order_id.zfill(7),
            "headerUpdate": (
                {"sql": header_sql,
                 "values": [str(v) if v is not None else None
                            for v in header_vals] + [order_id]}
                if header_sql else None
            ),
            "lineSteps": [
                {**st, "values": [str(v) if v is not None else None
                                  for v in st["values"]]}
                for st in steps
            ],
            "currentLineCount": len(current),
            "lockedLineCount": sum(1 for r in current if _line_locked(r)),
            "problems": problems,
            "notes": notes,
        }

        if body.dry_run:
            return {"dry_run": True, "written": False, **plan}

        if problems:
            raise HTTPException(status_code=400, detail="; ".join(problems))
        if not _writes_enabled():
            raise HTTPException(
                status_code=403,
                detail="Live Access writes are DISABLED on this instance.")

        if header_sql:
            cursor.execute(header_sql, header_vals + [order_id])
        for st in steps:
            cursor.execute(st["sql"], st["values"])
        conn.commit()
        return {"dry_run": False, "written": True, **plan}

    except HTTPException:
        raise
    except Exception as e:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"update failed: {e}")
    finally:
        if conn is not None:
            conn.close()


@router.get("/write-status", dependencies=[Depends(verify_api_key)])
def write_status():
    """Can this instance write? Callers that update a SECOND system first (the
    CRM writes Sage before Access on edits) must be able to ask before they
    touch anything — otherwise a refusal here leaves the two out of step."""
    conn = None
    db_ok, db_error = False, None
    try:
        conn = get_db_connection()
        conn.cursor().execute("SELECT TOP 1 OrderID FROM Orders")
        db_ok = True
    except Exception as e:
        db_error = str(e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    try:
        from config.settings import settings as _s
        db_path = _s.ACCESS_DB_PATH
    except Exception:
        db_path = None
    return {
        "writesEnabled": _writes_enabled(),
        "databaseReachable": db_ok,
        "database": db_path,
        "error": db_error,
    }
