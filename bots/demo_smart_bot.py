"""
Smart Bot — ToyLand Distribution Simulation
=============================================
A smarter-than-baseline bot that uses basic statistics and simple
rule-based systems to outperform the default bot.

See BOT_ACTIONS.md for what actions bots can perform.

Strategies:
  1. DEMAND-BASED REORDERING — Track sales velocity per product.
     Order more of fast sellers, less of slow movers. Adaptive
     reorder points based on observed demand + supplier lead times.

  2. PROACTIVE STORE REFILL — Transfer stock to stores BEFORE they
     hit 0. Trigger refill when stock falls below estimated 3-day
     demand, so shelves never go empty.

  3. SMART SHELF ALLOCATION — After collecting 14+ days of sales
     data, rank products by revenue per location and assign the
     top sellers to A-shelves (1.25x multiplier).

  4. STRATEGIC DISCOUNTING — Detect slow-moving products with high
     warehouse stock and apply small discounts (5-10%) to move them.
     Remove discounts once stock normalizes.

Usage:
  1. Start the bot server:   python engine/bot_server.py
  2. Run this bot:           python bots/demo_smart_bot.py
"""
import json
import urllib.request
import os
import math
from collections import defaultdict

SERVER = os.environ.get("BOT_SERVER", "http://127.0.0.1:5056")


# ── HTTP helpers ──

def api_get(path):
    req = urllib.request.Request(f"{SERVER}{path}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def api_post(path, data=None):
    body = json.dumps(data or {}).encode()
    req = urllib.request.Request(f"{SERVER}{path}", data=body,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


# ══════════════════════════════════════════════════════════════
# ANALYTICS — simple statistics from observable sales data
# ══════════════════════════════════════════════════════════════

class SalesTracker:
    """Tracks daily sales to compute moving averages and rankings."""

    def __init__(self):
        # product_id → list of daily qty sold (global)
        self.daily_product_sales = defaultdict(list)
        # (product_id, location_id) → list of daily qty sold
        self.daily_loc_sales = defaultdict(list)
        # (product_id, location_id) → list of daily revenue
        self.daily_loc_revenue = defaultdict(list)
        self.days_tracked = 0

    def record_day(self, day_summary):
        """Record one day's sales from the step response."""
        self.days_tracked += 1

        # Track per-product global sales
        product_totals = defaultdict(int)
        for pid, info in day_summary.get("sales_by_product", {}).items():
            product_totals[pid] = info.get("qty", 0)

        # Track per-product-location sales
        loc_totals = defaultdict(lambda: defaultdict(int))
        loc_revenue = defaultdict(lambda: defaultdict(float))
        for lid, info in day_summary.get("sales_by_location", {}).items():
            # day_summary doesn't break down by product per location,
            # so we'll use the aggregate per-product data for global stats
            pass

        # For global product velocity, record today's qty
        for pid, qty in product_totals.items():
            self.daily_product_sales[pid].append(qty)

    def record_sales_history(self, sales_history):
        """Process full sales history from /history/sales for per-location analysis."""
        # sales_history is a list of {date, product_id, location_id, qty, revenue}
        loc_product_qty = defaultdict(lambda: defaultdict(float))
        loc_product_rev = defaultdict(lambda: defaultdict(float))
        for sale in sales_history:
            key = (sale["product_id"], sale["location_id"])
            loc_product_qty[key][sale["date"]] += sale["qty"]
            loc_product_rev[key][sale["date"]] += sale["revenue"]

        # Store per-location daily series
        self.daily_loc_sales = {}
        self.daily_loc_revenue = {}
        for key, date_qty in loc_product_qty.items():
            self.daily_loc_sales[key] = list(date_qty.values())
        for key, date_rev in loc_product_rev.items():
            self.daily_loc_revenue[key] = list(date_rev.values())

    def product_velocity(self, pid, window=14):
        """Average daily units sold for a product (last N days)."""
        series = self.daily_product_sales.get(pid, [])
        if not series:
            return 0.0
        recent = series[-window:]
        return sum(recent) / len(recent)

    def product_velocity_all(self, window=14):
        """Dict of pid → avg daily sales for all tracked products."""
        return {pid: self.product_velocity(pid, window)
                for pid in self.daily_product_sales}

    def location_product_revenue(self, loc_id, window=30):
        """Dict of pid → total revenue at this location over window."""
        revenue = {}
        for (pid, lid), rev_series in self.daily_loc_revenue.items():
            if lid == loc_id:
                recent = rev_series[-window:]
                revenue[pid] = sum(recent)
        return revenue

    def slow_movers(self, catalog, wh_stock, threshold_days=60):
        """Products with enough WH stock for 60+ days at current velocity."""
        slow = []
        for pid in catalog:
            vel = self.product_velocity(pid, window=14)
            on_hand = wh_stock.get(pid, 0)
            if vel > 0:
                days_of_stock = on_hand / vel
                if days_of_stock > threshold_days:
                    slow.append((pid, vel, days_of_stock))
            elif on_hand > 50:
                # No sales at all but we have stock
                slow.append((pid, 0, 999))
        return slow


# ══════════════════════════════════════════════════════════════
# SMART BOT
# ══════════════════════════════════════════════════════════════

class SmartBot:
    def __init__(self):
        self.catalog = {}
        self.suppliers = []
        self.locations = []
        self.physical_locs = []
        self.online_locs = []
        self.product_supplier = {}
        self.supplier_map = {}
        self.tracker = SalesTracker()
        self.shelf_optimized = set()    # locations where we've set shelves
        self.active_discounts = {}      # pid → discount_pct
        self.day_count = 0

    def start(self, months=12, seed=2026, label="demo_smart"):
        """Initialize simulation and load catalog."""
        resp = api_post("/start", {"months": months, "seed": seed, "label": label})
        print(f"Simulation started: {resp['state']['date']}, {months} months")

        for p in resp["catalog"]:
            self.catalog[p["id"]] = p
        self.suppliers = resp["suppliers"]
        self.supplier_map = {s["id"]: s for s in self.suppliers}
        self.locations = resp["locations"]
        self.physical_locs = [l for l in self.locations if l["type"] != "Online"]
        self.online_locs = [l for l in self.locations if l["type"] == "Online"]

        # Map each product to supplier with shortest avg lead time
        sup_by_cat = defaultdict(list)
        for s in self.suppliers:
            for cat in s["categories"]:
                sup_by_cat[cat].append(s)

        for pid, p in self.catalog.items():
            candidates = sup_by_cat.get(p["cat"], [])
            if candidates:
                best = min(candidates, key=lambda s: sum(s["lead_days"]) / 2)
                self.product_supplier[pid] = best["id"]

        return resp["state"]

    # ──────────────────────────────────────────────────────────
    # STRATEGY 1: Demand-based warehouse reordering
    # ──────────────────────────────────────────────────────────

    def _reorder_commands(self, state):
        """Adaptive PO ordering based on observed sales velocity."""
        commands = []
        wh_stock = state.get("stock", {}).get("WH-01", {})
        num_stores = len(self.physical_locs)

        pending_products = set()
        for po in state.get("pending_pos", []):
            if not po["received"]:
                pending_products.add(po["product_id"])

        # Calculate pending PO quantities per product
        pending_qty = defaultdict(int)
        for po in state.get("pending_pos", []):
            if not po["received"]:
                pending_qty[po["product_id"]] += po["qty_ordered"]

        po_by_supplier = defaultdict(list)

        for pid, p in self.catalog.items():
            if pid in pending_products:
                continue

            # Estimate daily demand from sales data
            velocity = self.tracker.product_velocity(pid, window=14)
            refill = p.get("refill_num", 5)

            if self.day_count < 7:
                # Not enough data yet — use baseline formula
                reorder_qty = max(refill * num_stores * 2, 10)
                reorder_point = 20
            else:
                # Smart reorder: account for lead time demand
                sup_id = self.product_supplier.get(pid)
                sup = self.supplier_map.get(sup_id, {})
                avg_lead = sum(sup.get("lead_days", [14, 28])) / 2
                safety_stock = max(velocity * 7, 10)  # 7-day safety buffer

                # Reorder point = demand during lead time + safety stock
                reorder_point = max(velocity * avg_lead + safety_stock, 10)

                # Order qty = demand for lead time + 2 weeks buffer
                order_horizon = avg_lead + 14
                reorder_qty = max(int(velocity * order_horizon), refill * num_stores)
                reorder_qty = max(reorder_qty, 10)

            on_hand = wh_stock.get(pid, 0)
            effective_stock = on_hand + pending_qty.get(pid, 0)

            if effective_stock >= reorder_point:
                continue

            sup_id = self.product_supplier.get(pid)
            if not sup_id:
                continue
            cost = reorder_qty * p["cost"]
            po_by_supplier[sup_id].append((pid, reorder_qty, cost))

        # Group by supplier, respect ฿20,000 minimum
        for sup_id, items in po_by_supplier.items():
            total_cost = sum(cost for _, _, cost in items)
            if total_cost < 20000:
                # Try to meet minimum by scaling up quantities slightly
                if total_cost > 10000:
                    scale = 20000 / total_cost
                    items = [(pid, max(int(qty * scale), qty), qty * scale * self.catalog[pid]["cost"])
                             for pid, qty, cost in items]
                    total_cost = sum(c for _, _, c in items)
                if total_cost < 20000:
                    continue  # Still can't meet minimum

            po_items = [{"product_id": pid, "qty": qty} for pid, qty, _ in items]
            commands.append({
                "action": "issue_po",
                "supplier_id": sup_id,
                "items": po_items,
            })

        return commands

    # ──────────────────────────────────────────────────────────
    # STRATEGY 2: Proactive store refill
    # ──────────────────────────────────────────────────────────

    def _refill_commands(self, state):
        """Transfer to stores BEFORE they run out — trigger at 3-day demand."""
        commands = []
        stock = state.get("stock", {})
        wh_stock = stock.get("WH-01", {})

        pending_transfers = set()
        for tr in state.get("pending_transfers", []):
            if not tr["received"]:
                pending_transfers.add((tr["product_id"], tr["to_loc"]))

        shipment_items = {}  # loc_id → [{product_id, qty}, ...]
        for loc in self.physical_locs:
            loc_id = loc["id"]
            loc_stock = stock.get(loc_id, {})

            for pid, p in self.catalog.items():
                if (pid, loc_id) in pending_transfers:
                    continue

                store_qty = loc_stock.get(pid, 0)
                refill = p.get("refill_num", 5)

                if self.day_count < 7:
                    # Early days: same as baseline (refill when 0)
                    if store_qty > 0:
                        continue
                    transfer_qty = refill
                else:
                    # Smart: estimate per-location demand
                    velocity = self.tracker.product_velocity(pid, window=14)
                    # Rough per-store estimate (divide global by num stores + online)
                    # This is approximate — real demand varies by location
                    num_selling = len(self.physical_locs) + len(self.online_locs)
                    per_store_vel = velocity / max(num_selling, 1)

                    # Refill threshold: enough stock for ~3 days
                    threshold = max(int(per_store_vel * 3), 1)

                    if store_qty > threshold:
                        continue

                    # Transfer enough for ~7 days of demand, capped by refill_num
                    desired = max(int(per_store_vel * 7), refill)
                    transfer_qty = min(desired, refill * 2)  # Don't overfill

                wh_avail = wh_stock.get(pid, 0)
                actual = min(transfer_qty, wh_avail)
                if actual > 0:
                    if loc_id not in shipment_items:
                        shipment_items[loc_id] = []
                    shipment_items[loc_id].append({"product_id": pid, "qty": actual})
                    wh_stock[pid] = wh_avail - actual

        # Emit one grouped transfer per destination store
        for loc_id, items in shipment_items.items():
            commands.append({
                "action": "transfer",
                "from_loc": "WH-01",
                "to_loc": loc_id,
                "items": items,
            })

        return commands

    # ──────────────────────────────────────────────────────────
    # STRATEGY 3: Smart shelf allocation
    # ──────────────────────────────────────────────────────────

    def _shelf_commands(self, state):
        """Rank products by revenue at each location, assign top sellers to A-shelves."""
        commands = []

        # Only optimize shelves after we have enough sales data
        # Re-optimize every 30 days
        if self.day_count < 14:
            return commands
        if self.day_count % 30 != 0:
            return commands

        # Fetch full sales history for per-location analysis
        try:
            history = api_get("/history/sales")
            sales = history.get("sales", [])
            self.tracker.record_sales_history(sales)
        except Exception:
            return commands

        for loc in self.physical_locs:
            loc_id = loc["id"]
            shelves = loc.get("shelves", 4)
            slots_per_shelf = loc.get("total_slots", 80) // max(shelves, 1)

            # Estimate A/B/C shelf distribution
            # Typical pattern: ~25% A, ~35% B, ~40% C
            a_slots = max(int(shelves * 0.25) * slots_per_shelf, slots_per_shelf)
            b_slots = max(int(shelves * 0.35) * slots_per_shelf, slots_per_shelf)

            # Rank products by revenue at this location
            loc_revenue = self.tracker.location_product_revenue(loc_id, window=30)
            if not loc_revenue:
                continue

            # Sort by revenue descending
            ranked = sorted(loc_revenue.items(), key=lambda x: x[1], reverse=True)

            a_assigned = 0
            b_assigned = 0
            for pid, rev in ranked:
                if a_assigned < a_slots:
                    commands.append({
                        "action": "set_shelf",
                        "location_id": loc_id,
                        "product_id": pid,
                        "shelf_grade": "A"
                    })
                    a_assigned += 1
                elif b_assigned < b_slots:
                    commands.append({
                        "action": "set_shelf",
                        "location_id": loc_id,
                        "product_id": pid,
                        "shelf_grade": "B"
                    })
                    b_assigned += 1
                else:
                    commands.append({
                        "action": "set_shelf",
                        "location_id": loc_id,
                        "product_id": pid,
                        "shelf_grade": "C"
                    })

        return commands

    # ──────────────────────────────────────────────────────────
    # STRATEGY 4: Strategic discounting
    # ──────────────────────────────────────────────────────────

    def _discount_commands(self, state):
        """Apply small discounts to slow movers, remove discounts when cleared."""
        commands = []

        # Only start discounting after collecting enough data
        if self.day_count < 21:
            return commands

        # Only review discounts weekly
        if self.day_count % 7 != 0:
            return commands

        wh_stock = state.get("stock", {}).get("WH-01", {})

        # Find slow movers (60+ days of stock at current velocity)
        slow = self.tracker.slow_movers(self.catalog, wh_stock, threshold_days=60)

        new_discounts = {}
        for pid, vel, days_of_stock in slow:
            if days_of_stock > 120:
                # Very slow — 10% discount
                new_discounts[pid] = 0.10
            elif days_of_stock > 60:
                # Moderately slow — 5% discount
                new_discounts[pid] = 0.05

        # Apply new discounts
        for pid, disc in new_discounts.items():
            if self.active_discounts.get(pid, 0) != disc:
                commands.append({
                    "action": "set_discount",
                    "product_id": pid,
                    "discount_pct": disc
                })
                self.active_discounts[pid] = disc

        # Remove discounts for products that are no longer slow
        slow_pids = set(pid for pid, _, _ in slow)
        for pid in list(self.active_discounts.keys()):
            if pid not in slow_pids and self.active_discounts[pid] > 0:
                commands.append({
                    "action": "set_discount",
                    "product_id": pid,
                    "discount_pct": 0
                })
                self.active_discounts[pid] = 0

        return commands

    # ──────────────────────────────────────────────────────────
    # MAIN DECISION LOOP
    # ──────────────────────────────────────────────────────────

    def decide(self, state):
        """Combine all strategies into one set of commands for today."""
        commands = []

        # Strategy 1: Smart warehouse reordering (POs first, like baseline)
        commands.extend(self._reorder_commands(state))

        # Strategy 2: Proactive store refill
        commands.extend(self._refill_commands(state))

        # Strategy 3: Shelf optimization (periodic)
        commands.extend(self._shelf_commands(state))

        # Strategy 4: Discount management (periodic)
        commands.extend(self._discount_commands(state))

        return commands

    def run(self, months=12, seed=2026):
        """Run the full simulation loop."""
        state = self.start(months, seed)
        current_month = None
        month_revenue = 0
        month_units = 0
        last_total_pos = 0

        while True:
            self.day_count += 1
            commands = self.decide(state)
            resp = api_post("/step", commands)

            step_date = resp.get("date", "")
            step_month = str(step_date)[:7]
            ds = resp["day_summary"]

            # Feed daily sales data to tracker
            self.tracker.record_day(ds)

            # Accumulate monthly stats
            month_revenue += ds["total_revenue"]
            month_units += ds["total_units_sold"]

            if current_month and step_month != current_month:
                cur_pos = resp["state"]["cumulative"]["total_pos"]
                print(f"  {current_month} | Revenue: {month_revenue:>12,.0f} THB | "
                      f"Units: {month_units:>6,} | POs: {cur_pos - last_total_pos}")
                last_total_pos = cur_pos
                month_revenue = 0
                month_units = 0

            current_month = step_month
            state = resp["state"]

            if not resp.get("continue", True):
                break

        # Final results
        final = resp.get("final", {})
        print(f"\n{'='*60}")
        print(f"SMART BOT — SIMULATION COMPLETE")
        print(f"{'='*60}")
        print(f"  Days simulated:  {final.get('total_days', 0)}")
        print(f"  Total revenue:   {final.get('total_revenue', 0):>14,.0f} THB")
        print(f"  Total COGS:      {final.get('total_cogs', 0):>14,.0f} THB")
        print(f"  Gross profit:    {final.get('gross_profit', 0):>14,.0f} THB")
        print(f"  Total orders:    {final.get('total_orders', 0):>10,}")
        print(f"  Total POs:       {final.get('total_pos', 0):>10,}")
        print(f"  Total transfers: {final.get('total_transfers', 0):>10,}")
        print(f"{'='*60}")

        return final


if __name__ == "__main__":
    bot = SmartBot()
    bot.run(months=12, seed=2026)
