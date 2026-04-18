# Bot Actions Reference

This document describes everything a bot can **do** and **see** in the ToyLand simulation.
The simulation runs day-by-day. Each day, the bot receives the current state, decides on
actions, and submits them. The simulation then processes sales, deliveries, and transfers.

---

## How It Works

1. **Start** the simulation → `POST /start`
2. Each day: **read state** → **decide actions** → **submit step** → `POST /step`
3. The simulation processes the day (sales happen, POs arrive, transfers complete)
4. Repeat until the simulation ends (`continue: false` in response)

---

## Actions a Bot Can Perform

### 1. Issue Purchase Order (`issue_po`)

Order products from a supplier to replenish the warehouse.

```json
{
  "action": "issue_po",
  "supplier_id": "SUP-001",
  "product_id": "PRD-006",
  "qty": 200
}
```

**Rules:**
- Products arrive at **WH-01** after the supplier's lead time (random within their `lead_days` range)
- Supplier may partially fulfill based on their `reliability` rating
- There is no enforced minimum order — but the real-world minimum is ฿20,000 THB per supplier.
  The simulation will process smaller orders, but the default bot groups by supplier and skips
  batches under this threshold
- You can issue multiple POs in a single step
- Cost is deducted based on `qty × product.cost`

### 2. Transfer Stock (`transfer`)

Move products between locations (warehouse → store, store → store, store → warehouse).

```json
{
  "action": "transfer",
  "from_loc": "WH-01",
  "to_loc": "LOC-001",
  "product_id": "PRD-004",
  "qty": 10
}
```

**Rules:**
- Stock is immediately deducted from `from_loc`
- Arrival time depends on route:
  - WH-01 → Bangkok store: **1 day**
  - WH-01 → Upcountry store: **2-3 days**
  - Store → Store: **1 day** (same region) or **2-3 days** (cross-region)
- You cannot transfer more than available stock at `from_loc`
- Online stores do not hold inventory — they fulfill from WH-01 directly

### 3. Set Discount (`set_discount`)

Apply a discount to a product, optionally at a specific location.

```json
{
  "action": "set_discount",
  "product_id": "PRD-005",
  "discount_pct": 0.10,
  "location_id": "LOC-001"
}
```

**Rules:**
- `discount_pct` is a decimal (0.10 = 10% off)
- If `location_id` is omitted, the discount applies to **all locations**
- Discounted products become more attractive to customers (weighted by price sensitivity)
- Setting `discount_pct: 0` removes the discount
- Revenue is reduced by the discount amount (sell price = `price × (1 - discount)`)

### 4. Set Shelf Allocation (`set_shelf`)

Change which shelf grade a product is assigned to at a specific store.

```json
{
  "action": "set_shelf",
  "location_id": "LOC-001",
  "product_id": "PRD-014",
  "shelf_grade": "A"
}
```

**Rules:**
- Shelf grades affect sales multipliers: **A = 1.25×**, **B = 1.0×**, **C = 0.8×**
- Each store has a fixed number of A, B, and C shelves
- Moving a product to a better shelf increases its sales at that location
- This is a strategic decision — putting the right products on A-shelves matters

---

## What a Bot Can See (Observable State)

Available via `GET /state` or in the `state` field of each `/step` response:

| Data                  | Description                                               |
|-----------------------|-----------------------------------------------------------|
| `date`                | Current simulation date                                   |
| `day_count`           | Number of working days elapsed                            |
| `month_index`         | Months since simulation start (0-based)                   |
| `is_working_day`      | Whether today is a working day                            |
| `stock`               | Current inventory at every location (WH + all stores)     |
| `pending_pos`         | Unreceived purchase orders (supplier, product, qty, ETA)  |
| `pending_transfers`   | In-transit transfers (from, to, product, qty, ETA)        |
| `active_discounts`    | Currently active discounts (product, location, pct)       |
| `cumulative`          | Running totals (revenue, COGS, gross profit, orders, POs) |

### Historical Data

| Endpoint              | Description                                               |
|-----------------------|-----------------------------------------------------------|
| `GET /history/sales`  | Every sale: date, product, location, qty, revenue         |
| `GET /history/pos`    | Every PO: date, supplier, product, qty, cost, lead, status|

### Reference Data (Static)

| Endpoint              | Description                                               |
|-----------------------|-----------------------------------------------------------|
| `GET /catalog`        | Products: id, name, category, cost, price, refill_num     |
| `GET /locations`      | Locations: id, name, type, region (no grades/conversion)  |
| `GET /suppliers`      | Suppliers: id, name, categories, lead_days, reliability   |

---

## What a Bot CANNOT See (Hidden Variables)

These drive the simulation but are **not exposed** to bots:

- `base_daily_demand` — how many customers want each product per day
- `product_grade` (A/B/C) — composite quality score
- `social_media_buzz` — viral factor affecting demand
- `brand_loyalty` — repeat purchase tendency
- `trend_monthly_pct` — whether demand is growing or shrinking
- `seasonality_12m` — monthly demand multipliers (Christmas peaks, school season, etc.)
- `hype_events` — temporary demand spikes in specific months
- `price_sensitivity` — how much discounts affect demand
- `competitor_pressure` — external market pressure
- `location_grade` (A/B/C) — location quality multiplier on conversion
- `conversion_rate` — what % of foot traffic becomes buyers
- `daily_foot_traffic` — raw visitor count per location

**Bots must infer these from sales data.** A bot that analyzes its sales history
to detect seasonality, identify top sellers, and adjust stocking accordingly
will outperform the default bot.

---

## Strategy Tips

1. **Analyze your sales history** — `/history/sales` tells you what's selling where.
   Use this to estimate demand per product per location.
2. **Stock more of what sells** — The default bot orders the same amount for every product.
   A smart bot orders more of high-demand products.
3. **Use discounts strategically** — Slow-moving products tie up warehouse space.
   A small discount can clear them out while the demand boost from price sensitivity
   may increase overall revenue.
4. **Optimize shelf placement** — Products on A-shelves sell 25% more. Put your top sellers
   on the best shelves.
5. **Watch for seasonality** — Some products spike in December (Christmas), others during
   school season (May). Adjust your PO timing 3-6 weeks ahead of expected peaks
   (accounting for supplier lead times).
6. **Don't let stores run empty** — Every day a shelf is empty is lost revenue.
   Consider transferring before stock hits 0, not after.
7. **Group POs efficiently** — The ฿20,000 minimum per supplier means it's sometimes
   worth ordering a bit extra to meet the threshold rather than delaying.
