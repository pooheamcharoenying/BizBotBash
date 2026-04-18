# ToyLand Bot API Reference

## Overview

You are managing **ToyLand Distribution Co., Ltd.**, a Thai-Japanese toy distributor. The simulation runs day-by-day. Each day your bot can observe the current state (stock levels, sales, pending orders) and issue commands (purchase orders, transfers, discounts, shelf allocation). The goal is to maximize gross profit over the simulation period.

**Server:** `http://127.0.0.1:5056` (default)

## Quick Start

```bash
# Terminal 1: start the server
python engine/bot_server.py --seed 42

# Terminal 2: run your bot
python bots/demo_baseline_bot.py
```

## Endpoints

### POST /start

Start a new simulation. Call this once before stepping.

**Request body (optional):**
```json
{"months": 12, "seed": 42}
```

**Response:** Initial state + full catalog, locations, suppliers.

### POST /step

Advance simulation by one day. Send your commands as a JSON array.

**Request body:**
```json
[
  {"action": "issue_po", "supplier_id": "SUP-01", "product_id": "PRD-001", "qty": 200},
  {"action": "transfer", "from_loc": "WH-01", "to_loc": "LOC-001", "product_id": "PRD-005", "qty": 10},
  {"action": "set_discount", "product_id": "PRD-010", "discount_pct": 0.15, "location_id": "LOC-002"},
  {"action": "set_shelf", "location_id": "LOC-001", "product_id": "PRD-003", "shelf_grade": "A"}
]
```

Send `[]` or empty body to pass (do nothing).

**Response:**
```json
{
  "continue": true,
  "date": "2025-04-01",
  "day_summary": {
    "sales_by_product": {"PRD-001": {"qty": 5, "revenue": 2495.0}},
    "sales_by_location": {"LOC-001": {"qty": 12, "revenue": 8540.0}},
    "total_units_sold": 45,
    "total_revenue": 32150.0,
    "total_cogs": 18200.0,
    "gross_profit": 13950.0
  },
  "state": { ... }
}
```

When `"continue": false`, the simulation is over and `"final"` contains totals.

### GET /state

Current observable state at any time.

```json
{
  "date": "2025-04-15",
  "day_count": 11,
  "month_index": 0,
  "is_working_day": true,
  "stock": {
    "WH-01": {"PRD-001": 150, "PRD-002": 80},
    "LOC-001": {"PRD-001": 4, "PRD-002": 1}
  },
  "pending_pos": [...],
  "pending_transfers": [...],
  "active_discounts": [...],
  "cumulative": {
    "total_revenue": 125000.0,
    "total_cogs": 72000.0,
    "gross_profit": 53000.0,
    "total_orders": 340,
    "total_pos": 12,
    "total_transfers": 85
  }
}
```

### GET /catalog

Product catalog (public info only).

### GET /locations

Location info (public info only — no grades or conversion rates).

### GET /suppliers

Supplier info (lead times, reliability, minimum orders).

### GET /history/sales

Aggregated sales history (date × product × location).

### GET /history/pos

Full purchase order history.

## Available Commands

| Action | Required Fields | Optional | Description |
|--------|----------------|----------|-------------|
| `issue_po` | `supplier_id`, `product_id`, `qty` | — | Order stock from a supplier. Arrives after lead time. |
| `transfer` | `from_loc`, `to_loc`, `product_id`, `qty` | — | Move stock between warehouse and stores. |
| `set_discount` | `product_id`, `discount_pct` | `location_id` | Set price discount (0.0–1.0). Omit location for global. |
| `set_shelf` | `location_id`, `product_id`, `shelf_grade` | — | Change shelf placement (A/B/C). |

## What You Can See vs. What's Hidden

**Observable (via API):**
- Product names, prices, costs, categories
- Location names, types, regions, shelf counts
- Supplier names, lead times, reliability, min orders
- Current stock levels (warehouse + all stores)
- Sales as they happen (units, revenue per product per location)
- PO status and delivery tracking
- Active discounts

**Hidden (not accessible):**
- Product grades and composite scores
- Base daily demand per product
- Social media buzz, brand loyalty, trend rates
- Location grades and conversion rates
- Shelf grade assignments (you can set them, but don't know the current mapping)
- Seasonality curves

Your bot must infer demand patterns, product popularity, and location performance from observed sales data.

## Tips for Building Your Bot

1. **Track sales velocity** — monitor daily sales per product per location to estimate demand
2. **Lead times matter** — suppliers take 2-8 weeks to deliver; order early
3. **Shelf grade A sells more** — put your best products on Grade A shelves
4. **Discounts boost volume** — but cut margin; use strategically on slow movers
5. **Don't over-order** — excess stock ties up capital with no benefit
6. **Weekends and holidays** — some locations only operate certain days
7. **Online stores** — no shelf/volume constraints but generally lower conversion

## Project Structure

```
bots/
├── demo_baseline_bot.py ← Example bot (start here)
├── your_bot.py         ← Your bot goes here
├── API.md              ← This file
└── public/             ← Public data files (reference only)
    ├── products.json
    ├── locations.json
    ├── suppliers.json
    ├── categories.json
    └── company.json
```

The `public/` folder contains static snapshots of the catalog data for offline reference. During simulation, always use the API endpoints for live data.
