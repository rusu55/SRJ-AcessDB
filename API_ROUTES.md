# SRJ AccessDB API - all routes

Base URL: `http://192.168.0.101:8000` · every call needs the header `X-API-Key: <API_KEY from .env>` (only `/api/po/all` skips the check today). 71 routes, 22 of them new.

Postman: import `SRJ-AccessDB.postman_collection.json`, then set the collection variables `baseUrl` and `apiKey`. Path parts written `:name` are Postman path variables (filled with an example in the collection). Dates are `YYYY-MM-DD`; `until` is always exclusive. Query params shown in *italics* are optional.

**(new)** = read-only report routes added for CRMReports (no writes, no locks). **(careful)** = can write to the .mdb; bodies default to `dry_run: true`.

## Health

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/health/` | - | API is up. |

## Customers (new)

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/customers/` | `q` (text in id, name, contact, city, state, email), `with_stats` (adds orders, first/last order, sales (slower)), `limit` | Search / list customers. With with_stats=true each row carries orders, firstOrder, lastOrder, sales, salesLast12Months. |
| GET | `/api/customers/inactive` | `months` (no order for this many months), `min_orders`, `min_sales`, `limit` | Win-back list: bought before, nothing lately. Biggest lifetime sales first. |
| GET | `/api/customers/new` | `since`, *`until`* | Customers whose FIRST order is in the window. |
| GET | `/api/customers/:customerId` | `recent` (how many recent orders) | One customer: contact, totals (orders, sales, last 12 months, open lines), recent orders with status, equipment list. |
| GET | `/api/customers/:customerId/items` | *`since`* (default all history) | What the customer buys: qty, sales, orders, last price paid and date per item. |

Example calls:

```
GET {{baseUrl}}/api/customers/?q=united&with_stats=true&limit=50
GET {{baseUrl}}/api/customers/inactive?months=12&min_orders=2&min_sales=0&limit=500
GET {{baseUrl}}/api/customers/new?since=2026-01-01
GET {{baseUrl}}/api/customers/EASHAR?recent=20
GET {{baseUrl}}/api/customers/EASHAR/items
```

## Sales (new)

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/sales/lines` | `since`, `until`, `basis` (ordered / shipped), *`customer_id`*, *`item_no`*, *`salesman_id`*, *`limit`* | Order lines in a window with customer, item, rep, source, carrier, amount. basis=ordered uses order date and OrderQty; basis=shipped uses ShippingDate and OrderShippedQty. Max 800 days. |
| GET | `/api/sales/summary` | `group_by` (month / week / customer / item / product_line / salesman / source / state / warehouse / ship_via), `since`, *`until`*, `basis`, *`customer_id`*, *`item_no`*, *`salesman_id`*, *`limit`* | Totals per group: orders, customers, lines, qty, amount, sharePct. Date groups sorted by date, others biggest first. |
| GET | `/api/sales/compare` | `year`, *`basis`* | 12 months of this year vs last year, plus YTD to today's date for both. |

Example calls:

```
GET {{baseUrl}}/api/sales/lines?since=2026-09-01&until=2026-10-01&basis=ordered
GET {{baseUrl}}/api/sales/summary?group_by=month&since=2026-01-01&basis=ordered
GET {{baseUrl}}/api/sales/compare?year=2026
```

## Open orders (new)

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/open-orders/so-lines` | *`customer_id`*, *`item_no`*, `late_only`, `include_stale`, *`stale_days`* | Unshipped SO lines: required date, days late, status, open value, onHand and stockCover (full / partial / none, allocated by required date). Lines on orders older than stale_days are left out but counted. |
| GET | `/api/open-orders/po-lines` | *`supplier_id`*, *`po_id`*, *`item_no`*, `overdue_only`, `include_stale`, *`stale_days`* | Every open PO line in ONE call (replaces /api/po/all + /api/po/details per PO): supplier, item, open qty/value, due ship date, days overdue. |
| GET | `/api/open-orders/summary` | - | Dashboard tile: open SO / PO lines and values split into late, due this week, due this month, later, no date. |

Example calls:

```
GET {{baseUrl}}/api/open-orders/so-lines?late_only=false&include_stale=false
GET {{baseUrl}}/api/open-orders/po-lines?overdue_only=false&include_stale=false
GET {{baseUrl}}/api/open-orders/summary
```

## Purchasing (new)

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/purchasing/suppliers` | *`q`* | Suppliers with PO count, last PO date, PO value, open lines/value, containers, last container shipped. |
| GET | `/api/purchasing/po-lines` | `since`, *`until`*, *`supplier_id`* | Every line (open and closed) of POs dated in the window, header fields included. |
| GET | `/api/purchasing/containers` | *`since`* (vendor ship date, default 180 days ago), *`until`* (default 120 days ahead), *`status`* (booked / on the water / at port / inland / received), *`supplier_id`*, `with_lines` | Containers (Vendor Shipment): status, container/BL/vessel, ship / ETA / receipt dates, transit days, invoice & freight, paperwork checklist, PO list. |
| GET | `/api/purchasing/containers/:vendorShippingId` | - | One container: invoiced lines, lots received against it, invoiced vs received per item, serial count. |

Example calls:

```
GET {{baseUrl}}/api/purchasing/suppliers
GET {{baseUrl}}/api/purchasing/po-lines?since=2026-01-01
GET {{baseUrl}}/api/purchasing/containers?with_lines=false
GET {{baseUrl}}/api/purchasing/containers/JLLM26260C
```

## Stock (new)

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/stock/by-location` | *`q`*, *`warehouse_id`* | On-hand qty, lots and items per location. |
| GET | `/api/stock/lots` | *`item_no`*, *`location`*, `pallet` (MPackNo or UnitNo), *`vendor_inv`* (container / VendorInvNo), *`include_empty`* | Inventory lots by item, location, pallet or container (at least one filter). |
| GET | `/api/stock/received` | `since`, *`until`* | Receiving log: lots put away in the window plus totals per container. |
| GET | `/api/stock/reorder` | `all_items` | Items where stock + open PO - open SO is at or below Products.ReOrderLevel, with a suggested qty. |

Example calls:

```
GET {{baseUrl}}/api/stock/by-location
GET {{baseUrl}}/api/stock/lots?pallet=JL260930-46
GET {{baseUrl}}/api/stock/received?since=2026-09-01
GET {{baseUrl}}/api/stock/reorder?all_items=false
```

## Quality / ISO (new)

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/quality/on-time` | `since`, *`until`*, `group_by` (month / customer / item / warehouse), `late_lines` | Customer on-time shipping: ShippingDate vs RequiredDate per line and per shipment. |
| GET | `/api/quality/serial/:serial` | - | Traceability: vendor + container for a serial / lot number, the inventory lot, and the order(s) it shipped on. |

Example calls:

```
GET {{baseUrl}}/api/quality/on-time?since=2026-01-01&group_by=month&late_lines=false
GET {{baseUrl}}/api/quality/serial/2609180CN
```

## Data health (new)

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/data-health/` | `samples` | 13 checks (duplicate lines, empty orders, odd dates, unknown products/customers, over-shipments, half-recorded shipments, stale open lines, duplicate customers / item codes, negative lots, PO over-shipments) with counts and examples. Reads whole tables - run it nightly, not on page load. |

Example calls:

```
GET {{baseUrl}}/api/data-health/?samples=20
```

## Orders

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/orders/order/:orderId` | - | Order header + lines (SELECT *). |
| GET | `/api/orders/all` | `limit`, `offset` | Orders, newest first. |
| GET | `/api/orders/customer/:customerId` | - | All orders of one customer. |
| GET | `/api/orders/search` | *`customer_order_number`*, *`order_date_from`*, *`order_date_to`*, *`customer_id`*, *`buyer_name`* | Search orders. |
| GET | `/api/orders/ur-stores/locations` | - | States / cities that UR orders shipped to. |
| GET | `/api/orders/ur-stores/ids` | - | Every UR branch id that ordered. |
| GET | `/api/orders/ur-stores/search` | *`q`*, *`state`*, *`city`*, *`limit`* | UR orders by store id / state / city. |
| GET | `/api/orders/stats` | - | Order counts. |
| GET | `/api/orders/debug/columns` | - | Columns of Orders / Order Details. |

## Orders write (careful)

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/orders/next-id` | - | Next OrderID. |
| GET | `/api/orders/lookups` | - | Distinct header values. |
| GET | `/api/orders/customers-list` | *`q`*, *`limit`* | Customers for the picker. |
| GET | `/api/orders/last-price` | `product_id`, *`customer_id`* | Last price for a product. |
| GET | `/api/orders/salesmen` | - | Salesmen. |
| GET | `/api/orders/categories` | - | Categories. |
| GET | `/api/orders/schema` | *`table`*, *`sample`* | Table schema. |
| GET | `/api/orders/pickers` | - | Salesmen, categories, shippers, sources, warehouses. |
| GET | `/api/orders/tax-samples` | - | Recent SalesTax values. |
| GET | `/api/orders/machines` | *`product_id`* | Machine make / model list. |
| GET | `/api/orders/trace/:orderId` | *`empty`* | Every field of an order. |
| GET | `/api/orders/common-values` | *`limit`* | Most used header values. |
| GET | `/api/orders/edit-load/:orderId` | - | Order for the edit form, lines marked locked. |
| GET | `/api/orders/write-status` | - | Are order writes enabled. |
| POST | `/api/orders/create` | - | Create an order. dry_run=true only validates. Needs ACCESS_WRITES_ENABLED=1 for a live run. |
| PUT | `/api/orders/update/:orderId` | - | Edit an order. Shipped lines are locked. dry_run=true only previews. |
| DELETE | `/api/orders/delete/:orderId` | `dry_run` | Delete an unshipped order. dry_run=true only checks. |

Body for `POST /api/orders/create`:

```json
{
  "customerId": "EASHAR",
  "orderDate": "2026-10-12",
  "customerOrderNumber": "PO-1234",
  "buyerName": "Charles",
  "shipVia": "CENTRA",
  "freight": 0,
  "handlingCharge": 0,
  "salesTax": 0,
  "shipName": "Eastman Hardscapes",
  "shipAddress": "7824 Lebanon Rd",
  "shipCity": "Mt Juliet",
  "shipRegion": "TN",
  "shipPostalCode": "37122",
  "nameOrderEntry": "API",
  "lines": [
    {
      "productId": "5802",
      "orderQty": 2,
      "orderUnitPrice": 305.0,
      "requiredDate": "2026-10-20",
      "warehouseId": "SRJ"
    }
  ],
  "dry_run": true
}
```

Body for `PUT /api/orders/update/:orderId`:

```json
{
  "header": {
    "customerOrderNumber": "PO-1234-B",
    "freight": 45.0
  },
  "lines": [
    {
      "keyIndex": 0,
      "productId": "5802",
      "orderQty": 3,
      "orderUnitPrice": 305.0
    }
  ],
  "deleteMissingLines": false,
  "dry_run": true
}
```

## SO

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/so/all` | `limit`, *`last_po_id`* (cursor: OrderID of the last row) | Sales orders with orderTotal. |
| GET | `/api/so/details/:salesOrderNo` | - | Order + lines + totals (tax = subtotal x SalesTax rate). |

## PO

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/po/all` | `limit`, *`last_po_id`* | Purchase orders with orderTotal (no API key check on this one). |
| GET | `/api/po/details/:purchaseOrderNo` | - | PO lines with item and supplier. |

## Products

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/products/all` | `limit`, `offset` | Products. |
| GET | `/api/products/debug/columns` | - | Products columns. |

## Inventory

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/inventory/all` | - | Stock, open POs and open SOs for every item. |
| GET | `/api/inventory/item/:itemNo` | - | The same for one item. |
| GET | `/api/inventory/debug/trace/:itemNo` | - | Debug one item. |
| GET | `/api/inventory/debug/duplicate-codes` | - | Item codes on several products. |
| GET | `/api/inventory/debug/products` | - | Products in Inventory. |
| GET | `/api/inventory/debug/columns` | - | Inventory columns. |

## Inventory rows (careful)

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/inventory-rows/on-hand` | - | Every on-hand row with fingerprint + dup_count. |
| POST | `/api/inventory-rows/backup` | - | Copy the .mdb to _backups (needs ACCESS_INVENTORY_WRITES_ENABLED=1). |
| POST | `/api/inventory-rows/update` | - | Change ONE inventory row (Location, WarehouseID, MPackNo, UnitNo, qty). dry_run=true only previews. |

Body for `POST /api/inventory-rows/update`:

```json
{
  "fingerprint": "<fingerprint from /on-hand>",
  "product_id": "6166",
  "set": {
    "Location": "(IL) I2-FL4-R"
  },
  "expect": {
    "Location": "(IL) RCV"
  },
  "dry_run": true
}
```

## Tracking

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/tracking/` | *`order_no`*, *`tracking_no`*, *`year`*, *`min_order_no`*, *`limit`* | Shipping / tracking rows. |
| GET | `/api/tracking/enriched` | *`order_no`*, *`tracking_no`*, *`year`*, *`min_order_no`*, *`limit`* | Tracking rows with order, customer, carrier. |
| GET | `/api/tracking/stats/by-year` | *`year`*, *`from_year`*, *`to_year`* | Shipments per year / carrier. |
| GET | `/api/tracking/stats/late` | *`year`*, *`from_year`*, *`to_year`*, *`limit`* | Late deliveries. |
| GET | `/api/tracking/debug/tables` | - | Tables in the .mdb. |
| GET | `/api/tracking/debug/columns` | `table` | Columns of a table. |

## Shipments

| Method | URL | Query params | What it returns |
|---|---|---|---|
| GET | `/api/shipments/recent` | `since`, `until`, *`full_orders`* | Order lines shipped in a window. |

