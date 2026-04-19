"""
Demo Bot — ToyLand Distribution Simulation
============================================
This bot mirrors the DEFAULT BOT (auto mode) behavior exactly,
implemented via the bot_server API. It uses only public information.

See DEFAULT_BOT.md for the full specification.
See BOT_ACTIONS.md for what actions bots can perform.

Strategy (deliberately simple — competitors should beat this):
  1. Reorder from supplier when WH stock < 20 units
  2. Order qty = refill_num × num_physical_stores × 2
  3. Group POs by supplier, skip if batch < ฿20,000 THB
  4. Refill store shelves from WH when store stock hits 0
  5. No discounts, no Store→Store transfers, no shelf changes

Usage:
  1. Start the bot server:   python engine/bot_server.py
  2. Run this bot:           python bots/demo_baseline_bot.py

The bot will run the full simulation day-by-day, printing a summary
at the end of each month and final results when done.
"""
import json
import urllib.request
import os
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


# ── Bot logic ──

class DemoBot:
    def __init__(self):
        self.catalog = {}               # product_id → product info
        self.suppliers = []
        self.locations = []
        self.physical_locs = []
        self.product_supplier = {}      # product_id → best supplier_id
        self.reorder_qty = {}           # product_id → qty to order

    def start(self, months=12, seed=2026, label="demo_baseline"):
        """Initialize simulation and load catalog."""
        resp = api_post("/start", {"months": months, "seed": seed, "label": label})
        print(f"Simulation started: {resp['state']['date']}, {months} months")

        # Build catalog lookup
        for p in resp["catalog"]:
            self.catalog[p["id"]] = p
        self.suppliers = resp["suppliers"]
        self.locations = resp["locations"]
        self.physical_locs = [l for l in self.locations if l["type"] != "Online"]
        num_stores = len(self.physical_locs)

        # Map each product to the supplier with shortest avg lead time
        sup_by_cat = defaultdict(list)
        for s in self.suppliers:
            for cat in s["categories"]:
                sup_by_cat[cat].append(s)

        for pid, p in self.catalog.items():
            candidates = sup_by_cat.get(p["cat"], [])
            if candidates:
                best = min(candidates, key=lambda s: sum(s["lead_days"]) / 2)
                self.product_supplier[pid] = best["id"]

        # Order-up-to target: 1.75× one refill round per product.
        # Trigger at 1/2 of target — covers lead-time demand + the ~2%
        # yield loss on partial supplier deliveries without accumulating
        # the way the old fixed 2× rule did under the shelf mechanic.
        self.target_wh = {}
        self.trigger_wh = {}
        for pid, p in self.catalog.items():
            refill = p.get("refill_num", 5)
            target = max(int(refill * num_stores * 1.75), 10)
            self.target_wh[pid] = target
            self.trigger_wh[pid] = max(refill, target // 2)

        return resp["state"]

    def decide(self, state):
        """Look at current state, return list of commands for today."""
        commands = []
        stock = state.get("stock", {})
        wh_stock = stock.get("WH-01", {})

        # ── 1. Warehouse reorder: order-up-to target when below trigger ──
        # Trigger is 1/3 of target (per product). Qty brings WH back to
        # target. Group by supplier, skip batch if total < ฿20,000 THB.
        pending_products = set()
        for po in state.get("pending_pos", []):
            if not po["received"]:
                pending_products.add(po["product_id"])

        po_by_supplier = defaultdict(list)  # supplier_id → [(pid, qty, cost)]
        for pid in self.catalog:
            if pid in pending_products:
                continue
            target = self.target_wh[pid]
            trigger = self.trigger_wh[pid]
            on_hand = wh_stock.get(pid, 0)
            if on_hand >= trigger:
                continue
            sup_id = self.product_supplier.get(pid)
            if not sup_id:
                continue
            qty = max(1, target - on_hand)
            cost = qty * self.catalog[pid]["cost"]
            po_by_supplier[sup_id].append((pid, qty, cost))

        for sup_id, items in po_by_supplier.items():
            total_cost = sum(cost for _, _, cost in items)
            if total_cost < 20000:
                continue  # Below minimum — skip this supplier's batch
            po_items = [{"product_id": pid, "qty": qty} for pid, qty, _ in items]
            commands.append({
                "action": "issue_po",
                "supplier_id": sup_id,
                "items": po_items,
            })

        # ── 2. Store refill: transfer from WH when store stock = 0 ──
        # Group products by destination store for shipment pooling
        pending_transfers = set()
        for tr in state.get("pending_transfers", []):
            if not tr["received"]:
                pending_transfers.add((tr["product_id"], tr["to_loc"]))

        shipment_items = {}  # loc_id → [{product_id, qty}, ...]
        for loc in self.physical_locs:
            loc_id = loc["id"]
            loc_stock = stock.get(loc_id, {})
            for pid in self.catalog:
                store_qty = loc_stock.get(pid, 0)
                if store_qty > 0:
                    continue  # Still has stock
                if (pid, loc_id) in pending_transfers:
                    continue  # Transfer already in progress

                refill = self.catalog[pid].get("refill_num", 5)
                wh_avail = wh_stock.get(pid, 0)
                transfer_qty = min(refill, wh_avail)

                if transfer_qty > 0:
                    if loc_id not in shipment_items:
                        shipment_items[loc_id] = []
                    shipment_items[loc_id].append({"product_id": pid, "qty": transfer_qty})
                    # Track locally so we don't double-transfer from WH
                    wh_stock[pid] = wh_avail - transfer_qty

        # Emit one grouped transfer per destination store
        for loc_id, items in shipment_items.items():
            commands.append({
                "action": "transfer",
                "from_loc": "WH-01",
                "to_loc": loc_id,
                "items": items,
            })

        # ── 3. No discounts, no shelf changes (default bot) ──

        return commands

    def run(self, months=12, seed=2026):
        """Run the full simulation loop."""
        state = self.start(months, seed)
        current_month = None
        month_revenue = 0
        month_units = 0
        last_total_pos = 0

        while True:
            commands = self.decide(state)
            resp = api_post("/step", commands)

            step_date = resp.get("date", "")
            step_month = str(step_date)[:7]
            ds = resp["day_summary"]

            # Accumulate daily stats
            month_revenue += ds["total_revenue"]
            month_units += ds["total_units_sold"]

            if current_month and step_month != current_month:
                # Month boundary — print per-month summary
                cur_pos = resp["state"]["cumulative"]["total_pos"]
                print(f"  {current_month} | Revenue: {month_revenue:>12,.0f} THB | "
                      f"Units: {month_units:>6,} | POs issued: {cur_pos - last_total_pos}")
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
        print(f"SIMULATION COMPLETE")
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
    bot = DemoBot()
    bot.run(months=12, seed=2026)
