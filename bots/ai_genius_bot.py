"""
BizBot Bash AI Genius — ToyLand Distribution Simulation
========================================================
Extends the Champion bot with SUPPLY CHAIN COST OPTIMIZATION.

KEY INSIGHT: Both Champion and AI Genius achieve similar revenue
(~51M) because both converge to the same shelf placements. The BBS
differentiator is OPERATING COST EFFICIENCY.

The biggest controllable cost: PO processing fees (฿8,000 per PO).
Champion issues ~500 POs = ฿4M+ in processing fees alone.

AI GENIUS STRATEGY: Fewer POs through smarter batching.

  1. BATCH PO CONSOLIDATION — Instead of ordering immediately when
     any product hits reorder point, accumulates needs over a 3-day
     window to batch more products into fewer POs. Each avoided PO
     saves ฿8,000 in processing fees.

  2. LONGER REVIEW PERIOD (21 days vs 14) — Larger order quantities
     per PO, reducing reorder frequency. Combined with batching,
     this can cut PO count by 20-30%.

  3. MORE FREQUENT SHELF OPTIMIZATION (every 3 days vs 7) — Faster
     adaptation to shifting demand puts top sellers on A-shelves
     sooner for the 1.80× visibility boost.

  4. EARLIER LEARNING (4 days vs 5) — Starts shelf optimization
     one day sooner.

NOTE: No end-game logic — the bot does NOT know simulation duration.
Product grades (ABCDE) are HIDDEN.
"""
import math
from collections import defaultdict

from bizbotbash_champion import ChampionBot, DemandLearner, SupplierLearner


class AIGeniusBot(ChampionBot):
    """
    Champion core + batch PO optimization + faster shelf reopt.
    Uses the SAME EWMA (α=0.12) as Champion for stable demand estimation.
    """

    def __init__(self):
        super().__init__()

        # ── PO cost optimization ──
        self.REVIEW_PERIOD = 21           # vs Champion's 14 → larger, fewer POs

        # ── Faster shelf optimization ──
        self.SHELF_REOPTIMIZE_DAYS = 3    # vs Champion's 7
        self.LEARNING_PERIOD = 4          # vs Champion's 5

        # ── Batch ordering state ──
        self.pending_needs = defaultdict(dict)  # sup_id → {pid: (qty, cost)}
        self.last_order_day = defaultdict(int)   # sup_id → last day ordered
        self.ORDER_BATCH_WINDOW = 14            # days to accumulate before ordering

    # ──────────────────────────────────────────────────────────
    # Override: More Frequent Shelf Optimization
    # ──────────────────────────────────────────────────────────

    def _optimize_shelves(self, state):
        """Same formula as Champion, just every 3 days instead of 7."""
        commands = []

        if self.day_count < self.LEARNING_PERIOD + 1:
            return commands

        if self.day_count - self.last_shelf_optimize < self.SHELF_REOPTIMIZE_DAYS:
            return commands

        self.last_shelf_optimize = self.day_count

        for loc in self.physical_locs:
            loc_id = loc["id"]
            shelves = loc.get("shelves", 4)
            total_slots = loc.get("total_slots", 80)

            scored = {}
            for pid, p in self.catalog.items():
                loc_demand = self.demand.location_demand(pid, loc_id)
                price = p.get("price", 100)
                margin = self.margin_by_product.get(pid, 0.3)
                scored[pid] = loc_demand * price * (0.5 + margin)

            ranked = sorted(scored.items(), key=lambda x: x[1], reverse=True)

            shelf_grades = loc.get("shelf_grades", [])
            if shelf_grades:
                sps = total_slots // max(shelves, 1)
                a_slots = shelf_grades.count("A") * sps
                b_slots = shelf_grades.count("B") * sps
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
    # Override: Batch-Optimized Warehouse Reordering
    # ──────────────────────────────────────────────────────────

    def _optimal_reorder(self, state, wh_stock, pending_po_qty, pending_po_products):
        """
        Same (s,S) policy as Champion but with batch consolidation:
        - Accumulates ordering needs over 3-day windows
        - Issues one larger PO instead of multiple small ones
        - REVIEW_PERIOD 21 (vs 14) further increases order sizes

        Each avoided PO saves ฿8,000 in processing fees.
        """
        commands = []
        num_stores = max(len(self.physical_locs), 1)

        # Phase 1: Compute today's ordering needs
        today_needs = defaultdict(list)

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
                on_hand = wh_stock.get(pid, 0)
                in_transit = pending_po_qty.get(pid, 0)
                effective = on_hand + in_transit
                reorder_point = 15
                reorder_qty = max(refill * num_stores * 3, 15)
                if effective < reorder_point and pid not in pending_po_products:
                    cost = reorder_qty * p["cost"]
                    today_needs[sup_id].append((pid, reorder_qty, cost))
            else:
                demand_rate = self.demand.product_demand(pid)
                demand_std = self.demand.product_demand_std(pid)

                safety_stock = self.SERVICE_LEVEL_Z * demand_std * math.sqrt(max_lead)
                reorder_point = demand_rate * avg_lead + safety_stock

                on_hand = wh_stock.get(pid, 0)
                in_transit = pending_po_qty.get(pid, 0)
                effective_position = on_hand + in_transit

                if effective_position < reorder_point:
                    surge_mult = self.SURGE_MULTIPLIER if self.demand.is_surging(pid) else 1.0
                    order_up_to = (demand_rate * (avg_lead + self.REVIEW_PERIOD) *
                                   surge_mult + safety_stock)
                    order_qty = max(int(order_up_to - effective_position), 1)
                    min_order = max(refill * num_stores, 5)
                    order_qty = max(order_qty, min_order)
                    cost = order_qty * p["cost"]
                    today_needs[sup_id].append((pid, order_qty, cost))

        # Phase 2: Accumulate into batch buffer
        for sup_id, items in today_needs.items():
            for pid, qty, cost in items:
                # Update or add to pending needs
                self.pending_needs[sup_id][pid] = (qty, cost)

        # Phase 3: Decide which supplier batches to release
        suppliers_to_order = set()
        for sup_id, needs in self.pending_needs.items():
            if not needs:
                continue

            total_cost = sum(cost for _, cost in needs.values())
            days_waiting = self.day_count - self.last_order_day.get(sup_id, 0)

            # Release batch if:
            # 1. Enough value AND items to justify a PO
            # 2. OR batch window expired (don't delay too long)
            # 3. OR learning period (stock up quickly)
            if total_cost >= 20000 and len(needs) >= 3:
                suppliers_to_order.add(sup_id)
            elif days_waiting >= self.ORDER_BATCH_WINDOW and total_cost > 0:
                suppliers_to_order.add(sup_id)
            elif self.day_count <= self.LEARNING_PERIOD and total_cost > 0:
                suppliers_to_order.add(sup_id)

        # Phase 4: Issue POs for released batches
        for sup_id in suppliers_to_order:
            items = list(self.pending_needs[sup_id].items())
            if not items:
                continue

            order_items = [(pid, qty, cost) for pid, (qty, cost) in items]
            total_cost = sum(cost for _, _, cost in order_items)

            # Meet ฿20,000 minimum
            if total_cost < 20000:
                if total_cost > 8000:
                    scale = 20000 / total_cost * 1.05
                    order_items = [
                        (pid, max(int(qty * scale), qty + 1),
                         max(int(qty * scale), qty + 1) * self.catalog[pid]["cost"])
                        for pid, qty, cost in order_items
                    ]
                    total_cost = sum(c for _, _, c in order_items)

                if total_cost < 20000:
                    order_items = self._fill_po_gap(
                        sup_id, order_items, wh_stock, pending_po_products)
                    total_cost = sum(c for _, _, c in order_items)

                if total_cost < 20000:
                    continue  # Still can't meet minimum, keep accumulating

            po_items = [{"product_id": pid, "qty": qty}
                        for pid, qty, _ in order_items if qty > 0]
            if po_items:
                commands.append({
                    "action": "issue_po",
                    "supplier_id": sup_id,
                    "items": po_items,
                })
                self.last_order_day[sup_id] = self.day_count
                self.pending_needs[sup_id].clear()

        return commands
