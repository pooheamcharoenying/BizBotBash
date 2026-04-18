"""
BizBot Bash Champion — ToyLand Distribution Simulation
=======================================================
An advanced bot that uses statistical learning and adaptive optimization
to maximize NET SCORE = Gross Profit - Ending Inventory Value.

PHILOSOPHY: This bot treats the simulation as a black box. It learns
demand patterns purely from observable sales data — no reverse engineering,
no hidden variable access. It wins through superior algorithms:

  1. EWMA DEMAND FORECASTING — Exponentially-weighted moving averages
     with variance tracking. Adapts to trends and detects demand surges
     via z-score analysis. Per-product AND per-location granularity.

  2. OPTIMAL (s, S) INVENTORY POLICY — Reorder point (s) and order-up-to
     level (S) computed from learned demand distribution + supplier lead
     time estimates + configurable service level. The math:
       s = μ_demand × L_max + z × σ_demand × √L_max
       S = μ_demand × (L_avg + R) + z × σ_demand × √L_max
     where L = lead time, R = review period, z = service level z-score.

  3. PER-LOCATION DEMAND LEARNING — Infers location-specific product
     demand from stock snapshot deltas (yesterday's stock - today's stock
     = units sold, when no transfer arrived). Uses this for targeted
     store replenishment instead of naive equal-split.

  4. SUPPLIER LEAD TIME LEARNING — Tracks actual PO→delivery times to
     replace listed ranges with observed distributions.

  5. REVENUE-MAXIMIZING SHELF ALLOCATION — Ranks products by observed
     revenue per location and assigns A-shelves to top performers.

  6. MARGIN-AWARE DISCOUNTING — Only discounts when expected profit
     increase from volume exceeds margin loss. Conservative approach.

Usage (in-process via job_runner):
  Registered as "bizbotbash_champion" in the dashboard.
"""
import math
from collections import defaultdict


# ═══════════════════════════════════════════════════════════
# STATISTICAL LEARNING ENGINE
# ═══════════════════════════════════════════════════════════

class DemandLearner:
    """
    Learns demand patterns from observable sales data using
    exponentially-weighted statistics and surge detection.
    """

    def __init__(self, ewma_alpha=0.15, surge_threshold=2.0):
        self.alpha = ewma_alpha
        self.surge_z = surge_threshold

        # Per-product global stats
        self.product_ewma = {}            # pid → EWMA of daily qty
        self.product_ewma_var = {}        # pid → EWMA of variance
        self.product_total_qty = defaultdict(int)
        self.product_total_rev = defaultdict(float)
        self.product_days_seen = defaultdict(int)

        # Per (product, location) stats — from stock delta inference
        self.loc_product_ewma = {}        # (pid, lid) → EWMA
        self.loc_product_total = defaultdict(int)

        # Surge tracking
        self.surging_products = set()
        self.days_tracked = 0

    def record_day(self, day_summary):
        """Update demand estimates from one day's sales observations."""
        self.days_tracked += 1

        product_qty = {}
        for pid, info in day_summary.get("sales_by_product", {}).items():
            qty = info.get("qty", 0)
            rev = info.get("revenue", 0)
            product_qty[pid] = qty
            self.product_total_qty[pid] += qty
            self.product_total_rev[pid] += rev
            self.product_days_seen[pid] += 1

            # EWMA update
            if pid not in self.product_ewma:
                self.product_ewma[pid] = float(qty)
                self.product_ewma_var[pid] = 0.0
            else:
                prev = self.product_ewma[pid]
                self.product_ewma[pid] = self.alpha * qty + (1 - self.alpha) * prev
                diff_sq = (qty - prev) ** 2
                self.product_ewma_var[pid] = (
                    self.alpha * diff_sq + (1 - self.alpha) * self.product_ewma_var[pid]
                )

        # Surge detection: product >2σ above EWMA
        self.surging_products.clear()
        for pid, qty in product_qty.items():
            ewma = self.product_ewma.get(pid, 0)
            var = self.product_ewma_var.get(pid, 0)
            if var > 0 and self.days_tracked > 7:
                sigma = math.sqrt(var)
                z = (qty - ewma) / sigma if sigma > 0.1 else 0
                if z > self.surge_z:
                    self.surging_products.add(pid)

    def record_location_sale(self, pid, lid, qty):
        """Record inferred per-location sale from stock delta."""
        key = (pid, lid)
        self.loc_product_total[key] += qty
        if key not in self.loc_product_ewma:
            self.loc_product_ewma[key] = float(qty)
        else:
            prev = self.loc_product_ewma[key]
            self.loc_product_ewma[key] = self.alpha * qty + (1 - self.alpha) * prev

    def product_demand(self, pid):
        """Best estimate of daily demand for a product (units/day)."""
        if pid in self.product_ewma:
            return max(self.product_ewma[pid], 0.1)
        days = self.product_days_seen.get(pid, 0)
        if days > 0:
            return max(self.product_total_qty[pid] / days, 0.1)
        return 1.0  # Unknown product — assume 1/day

    def product_demand_std(self, pid):
        """Standard deviation estimate for demand."""
        var = self.product_ewma_var.get(pid, 0)
        return math.sqrt(var) if var > 0 else self.product_demand(pid) * 0.3

    def location_demand(self, pid, lid):
        """Estimated daily demand at a specific location."""
        key = (pid, lid)
        if key in self.loc_product_ewma:
            return max(self.loc_product_ewma[key], 0.01)
        # Fallback: global demand ÷ number of locations
        return max(self.product_demand(pid) * 0.08, 0.01)

    def is_surging(self, pid):
        return pid in self.surging_products

    def product_revenue_rate(self, pid):
        """Estimated daily revenue for a product."""
        days = max(self.days_tracked, 1)
        return self.product_total_rev.get(pid, 0) / days


class SupplierLearner:
    """Learns actual lead times from PO delivery observations."""

    def __init__(self):
        self.observed_lead_times = defaultdict(list)
        self.po_submit_dates = {}

    def record_po_submitted(self, po_id, supplier_id, date_str):
        self.po_submit_dates[po_id] = (supplier_id, date_str)

    def record_po_received(self, po_id, received_date_str):
        if po_id in self.po_submit_dates:
            sup_id, submit_date = self.po_submit_dates[po_id]
            try:
                from datetime import date
                sd = date.fromisoformat(submit_date)
                rd = date.fromisoformat(received_date_str)
                lead = (rd - sd).days
                if lead > 0:
                    self.observed_lead_times[sup_id].append(lead)
            except Exception:
                pass

    def avg_lead_time(self, supplier_id, fallback_days=(21, 35)):
        observed = self.observed_lead_times.get(supplier_id, [])
        if len(observed) >= 3:
            return sum(observed) / len(observed)
        return sum(fallback_days) / 2

    def max_lead_time(self, supplier_id, fallback_days=(21, 35)):
        observed = self.observed_lead_times.get(supplier_id, [])
        if len(observed) >= 3:
            return max(observed) * 1.1  # 10% buffer
        return fallback_days[1]


# ═══════════════════════════════════════════════════════════
# THE CHAMPION BOT
# ═══════════════════════════════════════════════════════════

class ChampionBot:
    """
    BizBot Bash Champion — uses statistical learning to optimize
    every aspect of ToyLand distribution management.

    Scoring: NET SCORE = Gross Profit - Ending Inventory Value
    Strategy: maximize revenue through superior availability and demand
    learning. Treats simulation duration as unknown — no end-game logic.
    """

    def __init__(self):
        # Required attributes (initialized by job_runner)
        self.catalog = {}
        self.suppliers = []
        self.locations = []
        self.physical_locs = []
        self.product_supplier = {}

        # Learning engines
        self.demand = DemandLearner(ewma_alpha=0.12, surge_threshold=1.8)
        self.supplier_learner = SupplierLearner()
        self.tracker = self.demand  # alias for job_runner compatibility

        # Strategy state
        self.day_count = 0
        self.supplier_map = {}
        self.online_locs = []
        self.reorder_qty = {}
        self.active_discounts = {}
        self.last_shelf_optimize = -999
        self.last_discount_review = -999
        self.margin_by_product = {}

        # Per-location demand inference
        self.previous_stock = {}

        # ── Tunable parameters ──
        self.SERVICE_LEVEL_Z = 2.05      # z-score for ~98% service level
        self.LEARNING_PERIOD = 5         # days before switching to learned demand
        self.REVIEW_PERIOD = 14          # days between potential reorders
        self.SURGE_MULTIPLIER = 1.8      # extra stock during surges
        self.SHELF_REOPTIMIZE_DAYS = 7
        self.DISCOUNT_REVIEW_DAYS = 7
        self.MAX_DISCOUNT = 0.12

    def decide(self, state):
        """Main decision function called each day."""
        commands = []

        stock = state.get("stock", {})
        wh_stock = stock.get("WH-01", {})

        # ── Infer per-location demand from stock snapshots ──
        self._infer_location_demand(state, stock)

        # ── Track pending POs and transfers ──
        pending_po_qty = defaultdict(int)
        pending_po_products = set()
        for po in state.get("pending_pos", []):
            if not po["received"]:
                pending_po_qty[po["product_id"]] += po["qty_ordered"]
                pending_po_products.add(po["product_id"])

        pending_transfers = set()
        pending_transfer_qty = defaultdict(lambda: defaultdict(int))
        for tr in state.get("pending_transfers", []):
            if not tr["received"]:
                pending_transfers.add((tr["product_id"], tr["to_loc"]))
                pending_transfer_qty[tr["to_loc"]][tr["product_id"]] += tr["qty"]

        # Compute margins once
        if not self.margin_by_product:
            for pid, p in self.catalog.items():
                cost = p.get("cost", 1)
                price = p.get("price", 1)
                self.margin_by_product[pid] = (price - cost) / price if price > 0 else 0

        # ── STRATEGY 1: Optimal warehouse reordering ──
        commands.extend(self._optimal_reorder(
            state, wh_stock, pending_po_qty, pending_po_products))

        # ── STRATEGY 2: Intelligent store replenishment ──
        commands.extend(self._smart_replenish(
            state, stock, wh_stock, pending_transfers, pending_transfer_qty))

        # ── STRATEGY 3: Revenue-maximizing shelf allocation ──
        commands.extend(self._optimize_shelves(state))

        # ── STRATEGY 4: Margin-aware discounting ──
        commands.extend(self._smart_discounts(state, wh_stock))

        return commands

    def _infer_location_demand(self, state, stock):
        """Infer per-location-product demand from stock snapshot changes."""
        if self.previous_stock and self.day_count > 1:
            arrived_transfers = set()
            for tr in state.get("pending_transfers", []):
                if tr.get("received"):
                    arrived_transfers.add((tr["product_id"], tr["to_loc"]))

            for loc in self.physical_locs:
                lid = loc["id"]
                prev_loc = self.previous_stock.get(lid, {})
                curr_loc = stock.get(lid, {})
                for pid in self.catalog:
                    prev_qty = prev_loc.get(pid, 0)
                    curr_qty = curr_loc.get(pid, 0)
                    if (pid, lid) not in arrived_transfers:
                        sold = max(prev_qty - curr_qty, 0)
                    else:
                        sold = 0
                    self.demand.record_location_sale(pid, lid, sold)

        self.previous_stock = {
            loc_id: dict(loc_stock) for loc_id, loc_stock in stock.items()
        }

    # ──────────────────────────────────────────────────────────
    # STRATEGY 1: Optimal (s, S) Inventory Policy
    # ──────────────────────────────────────────────────────────

    def _optimal_reorder(self, state, wh_stock, pending_po_qty, pending_po_products):
        """
        (s, S) inventory policy with proper statistical foundations:
          s = reorder_point = demand_during_lead_time + safety_stock
          S = order_up_to = demand_during_(lead_time + review_period) + safety_stock

        No end-game logic — treats simulation duration as unknown.
        """
        commands = []
        num_stores = max(len(self.physical_locs), 1)

        po_by_supplier = defaultdict(list)

        for pid, p in self.catalog.items():
            sup_id = self.product_supplier.get(pid)
            if not sup_id:
                continue

            sup = self.supplier_map.get(sup_id, {})
            refill = p.get("refill_num", 5)
            lead_days_range = sup.get("lead_days", [21, 35])
            avg_lead = self.supplier_learner.avg_lead_time(sup_id, lead_days_range)
            max_lead = self.supplier_learner.max_lead_time(sup_id, lead_days_range)

            if self.day_count <= self.LEARNING_PERIOD:
                # ── Early game: baseline ordering with slight boost ──
                on_hand = wh_stock.get(pid, 0)
                in_transit = pending_po_qty.get(pid, 0)
                effective = on_hand + in_transit

                reorder_point = 15
                reorder_qty = max(refill * num_stores * 3, 15)

                if effective < reorder_point and pid not in pending_po_products:
                    cost = reorder_qty * p["cost"]
                    po_by_supplier[sup_id].append((pid, reorder_qty, cost))
            else:
                # ── Learned demand-based ordering ──
                demand_rate = self.demand.product_demand(pid)
                demand_std = self.demand.product_demand_std(pid)

                # Safety stock: z × σ_demand × √(lead_time)
                safety_stock = self.SERVICE_LEVEL_Z * demand_std * math.sqrt(max_lead)

                # Reorder point
                reorder_point = demand_rate * avg_lead + safety_stock

                # Surge adjustment
                surge_mult = self.SURGE_MULTIPLIER if self.demand.is_surging(pid) else 1.0

                # Order-up-to level
                order_up_to = (demand_rate * (avg_lead + self.REVIEW_PERIOD) * surge_mult
                               + safety_stock)

                # Inventory position (warehouse + in-transit)
                on_hand = wh_stock.get(pid, 0)
                in_transit = pending_po_qty.get(pid, 0)
                effective_position = on_hand + in_transit

                # Reorder check
                if effective_position < reorder_point:
                    order_qty = max(int(order_up_to - effective_position), 1)
                    # Ensure minimum viable order
                    min_order = max(refill * num_stores, 5)
                    order_qty = max(order_qty, min_order)

                    cost = order_qty * p["cost"]
                    po_by_supplier[sup_id].append((pid, order_qty, cost))

        # ── Group by supplier, ensure ฿20,000 minimum ──
        for sup_id, items in po_by_supplier.items():
            total_cost = sum(cost for _, _, cost in items)

            if total_cost < 20000:
                if total_cost > 8000:
                    # Scale up to meet minimum
                    scale = 20000 / total_cost * 1.05
                    items = [
                        (pid, max(int(qty * scale), qty + 1),
                         max(int(qty * scale), qty + 1) * self.catalog[pid]["cost"])
                        for pid, qty, cost in items
                    ]
                    total_cost = sum(c for _, _, c in items)

                if total_cost < 20000:
                    items = self._fill_po_gap(sup_id, items, wh_stock, pending_po_products)
                    total_cost = sum(c for _, _, c in items)

                if total_cost < 20000:
                    continue

            # Group all products into one PO command per supplier
            po_items = [{"product_id": pid, "qty": qty}
                        for pid, qty, _ in items if qty > 0]
            if po_items:
                commands.append({
                    "action": "issue_po",
                    "supplier_id": sup_id,
                    "items": po_items,
                })

        return commands

    def _fill_po_gap(self, sup_id, existing_items, wh_stock, pending_po_products):
        """Add high-velocity products to reach ฿20,000 PO minimum."""
        current_cost = sum(c for _, _, c in existing_items)
        gap = 20000 - current_cost
        if gap <= 0:
            return existing_items

        existing_pids = set(pid for pid, _, _ in existing_items)
        sup = self.supplier_map.get(sup_id, {})
        sup_cats = set(sup.get("categories", []))

        candidates = []
        for pid, p in self.catalog.items():
            if pid in existing_pids or pid in pending_po_products:
                continue
            if p["cat"] not in sup_cats:
                continue
            demand = self.demand.product_demand(pid)
            margin = self.margin_by_product.get(pid, 0.3)
            wh_qty = wh_stock.get(pid, 0)
            # Score: prefer items with low stock and high demand × margin
            need = max(demand * 14 - wh_qty, 0)
            score = need * margin * p.get("price", 100)
            candidates.append((pid, score, p["cost"]))

        candidates.sort(key=lambda x: x[1], reverse=True)

        result = list(existing_items)
        remaining_gap = gap

        for pid, _, unit_cost in candidates:
            if remaining_gap <= 0:
                break
            qty = max(int(remaining_gap / max(unit_cost, 1)), 3)
            qty = min(qty, 20)
            cost = qty * unit_cost
            result.append((pid, qty, cost))
            remaining_gap -= cost

        return result

    # ──────────────────────────────────────────────────────────
    # STRATEGY 2: Intelligent Store Replenishment
    # ──────────────────────────────────────────────────────────

    def _smart_replenish(self, state, stock, wh_stock, pending_transfers, pending_transfer_qty):
        """
        Transfer stock to stores based on learned per-location demand.

        Key advantages over baseline/smart:
        - Per-location-product demand estimation (not naive equal-split)
        - Trigger at 4-day supply (vs 0 for baseline, 3-day for smart)
        - Target 10-day supply (vs refill_num for baseline)
        - Priority-based allocation when warehouse stock is limited
        """
        commands = []
        transfer_requests = []

        for loc in self.physical_locs:
            loc_id = loc["id"]
            loc_stock = stock.get(loc_id, {})

            for pid, p in self.catalog.items():
                if (pid, loc_id) in pending_transfers:
                    continue

                store_qty = loc_stock.get(pid, 0)
                incoming = pending_transfer_qty.get(loc_id, {}).get(pid, 0)
                effective_store = store_qty + incoming
                refill = p.get("refill_num", 5)

                if self.day_count <= self.LEARNING_PERIOD:
                    # Early game: refill when below refill_num (not just 0!)
                    if store_qty > max(refill // 2, 1):
                        continue
                    transfer_qty = refill * 2
                    priority = p.get("price", 100)
                else:
                    # ── Per-location demand-driven replenishment ──
                    loc_demand = self.demand.location_demand(pid, loc_id)

                    # Threshold: 4 days of local demand
                    threshold = max(int(loc_demand * 4), 2)

                    if effective_store > threshold:
                        continue

                    # Target: 10 days of local demand
                    target = max(int(loc_demand * 10), refill)
                    transfer_qty = max(target - effective_store, 1)

                    # Surge: increase transfer during surges
                    if self.demand.is_surging(pid):
                        transfer_qty = int(transfer_qty * 1.5)

                    # Priority: revenue potential × urgency
                    price = p.get("price", 100)
                    margin = self.margin_by_product.get(pid, 0.3)
                    urgency = max(1.0, threshold / max(effective_store, 0.1))
                    priority = price * margin * loc_demand * urgency

                transfer_requests.append((priority, loc_id, pid, transfer_qty))

        # Sort by priority (highest revenue impact first)
        transfer_requests.sort(key=lambda x: x[0], reverse=True)

        # Execute, tracking WH deductions — group by destination for shipment pooling
        local_wh = dict(wh_stock)
        shipment_items = defaultdict(list)  # loc_id → [{product_id, qty}, ...]
        for priority, loc_id, pid, qty in transfer_requests:
            wh_avail = local_wh.get(pid, 0)
            actual = min(qty, wh_avail)
            if actual >= 1:
                shipment_items[loc_id].append({"product_id": pid, "qty": actual})
                local_wh[pid] = wh_avail - actual

        # Emit one grouped transfer command per destination store
        for loc_id, items in shipment_items.items():
            commands.append({
                "action": "transfer",
                "from_loc": "WH-01",
                "to_loc": loc_id,
                "items": items,
            })

        return commands

    # ──────────────────────────────────────────────────────────
    # STRATEGY 3: Revenue-Maximizing Shelf Allocation
    # ──────────────────────────────────────────────────────────

    def _optimize_shelves(self, state):
        """
        Rank products by observed revenue at each location, assign top
        sellers to A-shelves. Re-optimizes weekly after learning period.
        """
        commands = []

        if self.day_count < self.LEARNING_PERIOD + 2:
            return commands

        if self.day_count - self.last_shelf_optimize < self.SHELF_REOPTIMIZE_DAYS:
            return commands

        self.last_shelf_optimize = self.day_count

        for loc in self.physical_locs:
            loc_id = loc["id"]
            shelves = loc.get("shelves", 4)
            total_slots = loc.get("total_slots", 80)

            # Score each product by revenue potential at this location
            scored = {}
            for pid, p in self.catalog.items():
                loc_demand = self.demand.location_demand(pid, loc_id)
                price = p.get("price", 100)
                margin = self.margin_by_product.get(pid, 0.3)
                # Revenue potential = demand × price, weighted by margin
                scored[pid] = loc_demand * price * (0.5 + margin)

            ranked = sorted(scored.items(), key=lambda x: x[1], reverse=True)

            # Distribute across ABC shelf grades: ~25% A, ~35% B, ~40% C
            shelf_grades = loc.get("shelf_grades", [])
            if shelf_grades:
                a_count = shelf_grades.count("A")
                b_count = shelf_grades.count("B")
                sps = total_slots // max(shelves, 1)
                a_slots = a_count * sps
                b_slots = b_count * sps
            else:
                a_slots = max(int(total_slots * 0.25), 1)
                b_slots = max(int(total_slots * 0.35), 1)

            a_used = 0
            b_used = 0
            for pid, score in ranked:
                if a_used < a_slots:
                    grade = "A"
                    a_used += 1
                elif b_used < b_slots:
                    grade = "B"
                    b_used += 1
                else:
                    grade = "C"

                commands.append({
                    "action": "set_shelf",
                    "location_id": loc_id,
                    "product_id": pid,
                    "shelf_grade": grade
                })

        return commands

    # ──────────────────────────────────────────────────────────
    # STRATEGY 4: Margin-Aware Discounting
    # ──────────────────────────────────────────────────────────

    def _smart_discounts(self, state, wh_stock):
        """
        Conservative discounting strategy:
        - Only discount truly overstocked items (>90 days of supply)
        - Never discount below minimum margin threshold
        - No end-game clearance — treats simulation duration as unknown
        """
        commands = []

        if self.day_count < 14:
            return commands

        if self.day_count - self.last_discount_review < self.DISCOUNT_REVIEW_DAYS:
            return commands

        self.last_discount_review = self.day_count

        new_discounts = {}

        for pid, p in self.catalog.items():
            demand = self.demand.product_demand(pid)
            cost = p.get("cost", 1)
            price = p.get("price", 1)
            margin_ratio = (price - cost) / price if price > 0 else 0

            # Total system stock
            on_hand = wh_stock.get(pid, 0)
            total_store = sum(
                state.get("stock", {}).get(loc["id"], {}).get(pid, 0)
                for loc in self.physical_locs
            )
            total_stock = on_hand + total_store

            if demand < 0.1:
                continue

            days_of_stock = total_stock / demand

            # Only discount severe overstock
            if days_of_stock > 120:
                disc = min(0.08, margin_ratio * 0.2)
                new_discounts[pid] = round(max(disc, 0.03), 2)
            elif days_of_stock > 90:
                disc = min(0.05, margin_ratio * 0.12)
                new_discounts[pid] = round(max(disc, 0.02), 2)
            elif days_of_stock < 30 and self.active_discounts.get(pid, 0) > 0:
                new_discounts[pid] = 0  # Remove discount when stock normalizes

        # Apply changes
        for pid, disc in new_discounts.items():
            current = self.active_discounts.get(pid, 0)
            if abs(current - disc) > 0.005:
                commands.append({
                    "action": "set_discount",
                    "product_id": pid,
                    "discount_pct": disc
                })
                self.active_discounts[pid] = disc

        # Cleanup: remove discounts for products not in new_discounts that normalized
        for pid in list(self.active_discounts.keys()):
            if pid not in new_discounts and self.active_discounts[pid] > 0:
                demand = self.demand.product_demand(pid)
                on_hand = wh_stock.get(pid, 0)
                if demand > 0 and on_hand / demand < 40:
                    commands.append({
                        "action": "set_discount",
                        "product_id": pid,
                        "discount_pct": 0
                    })
                    self.active_discounts[pid] = 0

        return commands
