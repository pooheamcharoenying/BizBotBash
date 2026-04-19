# ToyLand Data Analysis — Claude Cowork Prompt

Hi Claude. You've been pointed at a folder of excel files from the
**BizBotBash ToyLand Distribution** simulation. Your job is to turn
them into a clean data pipeline + analysis toolkit so the human
running you can actually see what's going on in the business.

Read this whole file first, then execute in order. Ask before
installing any packages beyond pandas, openpyxl, matplotlib.

---

## What's in this folder

You should see:

- **`initial_state.xlsx`** — produced once at simulation start
  - `Products` — SKU catalog (id, name, category, cost, price, refill_num)
  - `Locations` — 4 physical stores + 2 online channels (traffic, shelves, capacity)
  - `Suppliers` — supplier list with lead times, reliability, min order value
  - `WH_Stock` — starting warehouse inventory per product
  - `Store_Stock` — starting store inventory per product per location
  - `Discounts` — any initial discount configuration

- **`month_YYYY-MM.xlsx`** — one file per calendar month the sim ran
  - `Sales` — every customer order line (date, order_id, product, location, qty_ordered, qty_filled, unit_price, line_total)
  - `Stock_Snapshot` — daily stock levels at WH and each store, per product
  - `Purchase_Orders` — POs issued to suppliers (date, product, qty, cost, lead, received flag)
  - `Transfers` — stock movements (date, from_loc, to_loc, product, qty, arrival)
  - `Financials` — daily revenue, COGS, fixed costs, variable costs
  - `Active_Discounts` — which discounts were in effect
  - `Action_Log` — every bot command executed that month (includes `set_shelf` events)
  - `Shelf_Layout` — per physical store: number of shelves per grade (A/B/C), slots, total storage capacity
  - `Shelf_Map` — per (store × grade): every product assigned to that grade tier at that store. **Best source for a shelf-plan dashboard.**
  - `Shelf_Assignments_Initial` / `Shelf_Assignments_Final` — per (location, product): grade, current shelf_qty, backroom_qty, product name, category name, unit price. Join to `Shelf_Layout` for per-grade total capacity.

**Shelf visualisation note**: the simulation assigns products to a *grade tier* (A/B/C), not to a specific physical shelf. A store with 2 A-shelves doesn't distinguish between Shelf#1 and Shelf#2 — both hold any product tagged grade A. If you want a physical shelf-by-shelf layout, group products by grade first, then optionally sub-partition your visualisation by shelf number within a grade.

---

## Task 1 — Build the aggregator

Create `aggregate.py` that:

1. Reads **all** `month_*.xlsx` files in the folder, sorted by date
2. Reads `initial_state.xlsx` to get the product/location/supplier lookups
3. Produces **one clean dataframe per concept**:
   - `sales_df` — one row per order line, with product name + category + location name joined in
   - `po_df` — all POs with supplier name joined
   - `transfers_df` — all transfers with from/to location names
   - `financials_df` — daily financials, one row per day
   - `stock_df` — daily stock snapshot, one row per (date, location, product)
4. Provides a `load_all()` function that returns a dict of these dataframes
5. Saves them as parquet files in `./cache/` so reruns are fast (check timestamps, skip rebuild if cache is newer than source files)

Put proper docstrings on every function. Use pandas. Keep it under 250 lines.

---

## Task 2 — Build the analysis module

Create `analysis.py` that imports from `aggregate.py` and provides:

1. **`overview()`** — prints total revenue, COGS, gross margin, net profit, total units sold, stockout rate %, unique customers
2. **`monthly_trend()`** — returns a dataframe of revenue / units / stockouts per month
3. **`top_products(n=10)`** — top N products by revenue, with % of total
4. **`top_locations()`** — revenue + units per location, sorted descending
5. **`worst_stockouts(n=20)`** — the (product, location, month) combos with the highest unfilled demand
6. **`supplier_performance()`** — per-supplier: # POs, avg lead time, % reliability (fully delivered / total)
7. **`category_mix()`** — revenue share by category over time (quarterly)
8. **`seasonality(product_id)`** — monthly seasonality index for a given product

Keep each function short and returning a clean dataframe or dict. The human will call these interactively. **Do not print** in these functions — return data.

---

## Task 3 — Build the charts module

Create `charts.py` that uses matplotlib to produce:

1. **`chart_revenue_trend()`** — monthly revenue line chart
2. **`chart_category_heatmap()`** — heatmap: category × month, cell = revenue share
3. **`chart_stockout_rate()`** — stockout rate % over time
4. **`chart_location_comparison()`** — bar chart of revenue by location
5. **`chart_top_products_cumulative()`** — pareto: top products cumulative % of revenue

Style: **black background, white axes, yellow accent** (`#facc15`) — match the BizBotBash aesthetic. Save each as a PNG in `./charts/`.

---

## Task 4 — The insights document

Run everything and write `insights.md` answering these questions:

1. What's the business's total revenue, profit, and net margin?
2. Which 5 products drive most of the revenue? What's their combined share?
3. Which location is the single most profitable? Which is the worst?
4. How often do customers hit a stockout, and is that rate trending up or down over the 5 years?
5. Which category has the strongest seasonality, and what's the peak month?
6. Which supplier is the weakest link (slowest, least reliable)?
7. Are there products with consistently high unfilled demand that we should reorder more aggressively?
8. Any interesting anomaly you spotted that a human should look at?

Keep each answer to 2-4 sentences. Use specific numbers. Be confident; if the data says something, say it.

---

## Ground rules

- **No network calls.** Everything is local.
- **No hardcoded file names.** Glob `month_*.xlsx` in the current folder.
- **Handle missing sheets gracefully** (not every month may have Active_Discounts).
- **Currency is Thai Baht (฿).** Use `฿{value:,.0f}` formatting.
- **Dates are ISO format** (`YYYY-MM-DD`). Parse them as pd.Timestamp.
- When you're done with each task, tell the human what you built and what command to run next.

---

## After this file

Once these three modules + `insights.md` exist, the human's next move is Stage 2: extending this into a read/write ERP. They may ask you to:

- Add write functions to `aggregate.py` (issue a PO, record a transfer, update shelves)
- Build a small Flask app around it
- Wire it up to call the BizBotBash backend at `/run` or `/run-bot`

But for now, focus on analysis. Ship it.

— The BizBotBash team
