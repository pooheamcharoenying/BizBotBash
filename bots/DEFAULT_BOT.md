# Default Bot Configuration

The default bot (auto mode) represents a **baseline operator** running ToyLand Distribution
with simple, publicly available information only. It does not use any hidden variables
(demand forecasts, product grades, social media buzz, brand loyalty, seasonality, etc.).

Competitors should aim to **beat the default bot** by building smarter strategies
using the observable data available through the API.

---

## Warehouse Reordering (PO from Suppliers)

- **Target level** (order-up-to): `refill_num × num_physical_stores`
  — one full refill round across every store, no safety cushion
- **Trigger:** WH-01 stock drops below `max(refill_num, target / 3)`
  — scales per product; small-refill products reorder at lower thresholds
- **Skip if:** A pending (unreceived) PO already exists for that product
- **Order quantity:** `target − current_on_hand` — brings WH back up to target
  (adaptive, not a fixed batch size)
- **Supplier selection:** Each product is mapped to the supplier that covers its category
  with the shortest average lead time
- **Minimum order value:** POs are grouped by supplier; if the total cost for a supplier's
  batch is below **฿20,000 THB**, the entire batch is skipped that day
- **Lead time:** Random between the supplier's `lead_days[min]` and `lead_days[max]`
- **Supplier reliability:** On delivery, there is a chance of partial fulfillment based on
  the supplier's `reliability` rating (e.g., 0.92 = 8% chance of receiving only 60-90% of order)

## Store Refill (Transfers from Warehouse)

- **Trigger:** A product's shelf stock drops to **≤ half of its shelf capacity** at a store
- **Skip if:** A pending (unreceived) transfer already exists for that product → that store
- **Quantity:** `shelf_capacity − current_stock` — fills the shelf all the way up (capped by WH availability)
- **Shelf capacity** is derived from the product's `base_area_cm2` and the store's
  per-grade shelf area: `units_per_shelf = floor(shelf_area / base_area)`,
  `cap = units_per_shelf × shelves_at_that_grade`
- **Source:** WH-01 only (no Store→Store transfers in default mode)
- **Transit time:**
  - Bangkok stores: **1 day**
  - Upcountry stores (Chiang Mai, Phuket): **2-3 days** (random)
- **Online stores** do not receive transfers — they fulfill directly from WH-01

## Initial Stock Allocation

- **Warehouse (WH-01):** `refill_num × num_physical_stores × 2` per product
  - After physical stores draw their initial stock, the warehouse retains roughly a 1:1 ratio
- **Physical stores:** Each store gets `refill_num` units of each product, allocated by
  shelf category priority:
  - **A-shelves** → Capsule Toys (CAT-03), Plush & Stuffed Toys (CAT-01)
  - **B-shelves** → Action Figures (CAT-02), Stationery (CAT-04), TCG (CAT-05)
  - **C-shelves** → Model Kits (CAT-06), Baby & Preschool (CAT-07)
  - Products are placed first-come-first-served within their category tier,
    constrained by the store's volume capacity

## Discounts

- **None.** The default bot does not apply any discounts.
- Competitors can use the `set_discount` action to boost demand for slow movers.

## What the Default Bot Does NOT Do

- Does not analyze sales history to adjust ordering
- Does not forecast demand or seasonality
- Does not apply discounts to slow-moving products
- Does not transfer stock between stores (Store→Store)
- Does not adjust shelf allocation based on product performance
- Does not vary reorder quantities by product popularity
- Does not use any hidden variables (demand, buzz, loyalty, grades, etc.)

---

## Summary Table

| Parameter                | Value                                  |
|--------------------------|----------------------------------------|
| WH order-up-to target    | `refill_num × num_stores`              |
| WH reorder trigger       | `max(refill_num, target / 3)`          |
| WH reorder quantity      | `target − on_hand` (adaptive)          |
| Min PO value per supplier| ฿20,000 THB                            |
| Store refill trigger     | Stock = 0                              |
| Store refill quantity    | `refill_num` (capped by shelf volume)  |
| Store refill source      | WH-01 only                             |
| Transit time (Bangkok)   | 1 day                                  |
| Transit time (Upcountry) | 2-3 days                               |
| Discounts                | None                                   |
| Store→Store transfers    | None                                   |
| Shelf allocation         | Category priority (A/B/C shelves)      |
