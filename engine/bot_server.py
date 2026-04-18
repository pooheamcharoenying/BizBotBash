"""
Bot-mode simulation server — runs the sim day-by-day, controlled by an external bot.

Start:  python engine/bot_server.py [--port 5056] [--months 12] [--seed 42]

API endpoints:
  POST /start          → Start a new simulation, returns initial observable state
  POST /step           → Advance one day (accepts JSON array of commands)
  GET  /state          → Current observable state (stock, financials, pending POs)
  GET  /history/sales  → Full sales history
  GET  /history/pos    → Full PO history
  GET  /catalog        → Product catalog (public info only, no hidden vars)
  GET  /locations      → Location info (public info only, no grades/conversion)
  GET  /suppliers      → Supplier info

Hidden variables (product grades, base demand, buzz, loyalty, trend, conversion
rates, location grades) are NEVER exposed through this API.

Bot commands (JSON array in POST /step body):
  {"action": "issue_po",    "supplier_id": "SUP-XX",
       "items": [{"product_id": "PRD-01", "qty": 50}, {"product_id": "PRD-02", "qty": 30}]}
       → Grouped PO: one purchase order (one processing fee) with multiple products.
  {"action": "issue_po",    "supplier_id": "SUP-XX", "product_id": "PRD-XX", "qty": 100}
       → Single-product PO (legacy): each command = one PO = one processing fee.
  {"action": "transfer",    "from_loc": "WH-01", "to_loc": "LOC-XX",
       "items": [{"product_id": "PRD-01", "qty": 5}, {"product_id": "PRD-02", "qty": 3}]}
       → Grouped transfer: one truck (one base fee) carrying multiple products.
  {"action": "transfer",    "from_loc": "WH-01", "to_loc": "LOC-XX",
       "product_id": "PRD-XX", "qty": 50}
       → Single-product transfer (legacy): each gets its own truck & base fee.
  {"action": "set_discount", "product_id": "PRD-XX", "discount_pct": 0.1, "location_id": "LOC-XX"}
  {"action": "set_shelf",    "location_id": "LOC-XX", "product_id": "PRD-XX", "shelf_grade": "A"}
  (send [] or omit body to pass/do nothing)

Default port: 5056
"""
import json
import os
import sys
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import date as date_type
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, BASE_DIR)

from sim_engine import load_config, SimulationEngine, save_run, build_compact


class DateEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, date_type):
            return obj.isoformat()
        return super().default(obj)


# ═══════════════════════════════════════════════════════════
# Observable state filters — strip hidden variables
# ═══════════════════════════════════════════════════════════

def public_products(cfg):
    """Product catalog: only what a real company would know (name, price, cost, category)."""
    return [{"id": p["id"], "name": p["name"], "cat": p["cat"],
             "cost": p["cost"], "price": p["price"],
             "refill_num": p.get("refill_num", 5),
             "volume_cm3": p.get("volume_cm3", 5000)}
            for p in cfg["products"]]


def public_locations(cfg):
    """Locations: public info only — no grades, no conversion rates."""
    return [{"id": l["id"], "name": l["name"], "type": l["type"],
             "region": l["region"], "address": l.get("address", ""),
             "operating_hours": l.get("operating_hours", ""),
             "shelves": l.get("shelves", 0),
             "total_slots": l.get("total_slots", 0),
             "working_days": l.get("working_days", None)}
            for l in cfg["sales_locations"]]


def public_suppliers(cfg):
    """Suppliers: all info is public (bots need this to issue POs)."""
    return [{"id": s["id"], "name": s["name"],
             "categories": s.get("categories", []),
             "lead_days": s.get("lead_days", [5, 10]),
             "reliability": s.get("reliability", 0.9),
             "min_order_thb": s.get("min_order_thb", 20000),
             "payment_terms": s.get("payment_terms", 30)}
            for s in cfg["suppliers"]]


def observable_stock(engine):
    """Current stock levels — bots can see what's on shelves and in warehouse."""
    stock = {}
    for (loc, pid), qty in engine.stock.items():
        if loc not in stock:
            stock[loc] = {}
        stock[loc][pid] = qty
    return stock


def observable_pending_pos(engine):
    """POs that are in transit (bot can track what it ordered)."""
    return [{"po_id": po["po_id"],
             "supplier_id": po["supplier_id"],
             "product_id": po["product_id"],
             "qty_ordered": po["qty_ordered"],
             "order_date": po["date"],
             "expected_arrival": po["arrival_date"],
             "received": po["received"],
             "qty_received": po.get("qty_received", 0),
             "status": po.get("status", "pending")}
            for po in engine.pending_pos]


def observable_pending_transfers(engine):
    """Transfers that are in transit."""
    return [{"transfer_id": t["transfer_id"],
             "product_id": t["product_id"],
             "from_loc": t.get("from_loc", t.get("from_wh", "")),
             "to_loc": t.get("to_loc", t.get("to_wh", "")),
             "qty": t["qty"],
             "arrival_date": t["arrival_date"],
             "received": t["received"]}
            for t in engine.pending_transfers]


def observable_active_discounts(engine):
    """Current active discounts."""
    return [{"product_id": pid, "location_id": lid or "ALL", "discount_pct": d}
            for (pid, lid), d in engine.discounts.items() if d > 0]


def day_summary(engine, prev_order_count, prev_revenue, prev_cogs):
    """What happened today — sales, deliveries, events."""
    new_orders = engine.order_log[prev_order_count:]
    sales_by_product = defaultdict(lambda: {"qty": 0, "revenue": 0})
    sales_by_location = defaultdict(lambda: {"qty": 0, "revenue": 0})
    for o in new_orders:
        sales_by_product[o["product_id"]]["qty"] += o["qty_filled"]
        sales_by_product[o["product_id"]]["revenue"] += o["line_total"]
        sales_by_location[o["sales_location_id"]]["qty"] += o["qty_filled"]
        sales_by_location[o["sales_location_id"]]["revenue"] += o["line_total"]

    day_rev = engine.total_revenue - prev_revenue
    day_cogs = engine.total_cogs - prev_cogs

    return {
        "sales_by_product": dict(sales_by_product),
        "sales_by_location": dict(sales_by_location),
        "total_units_sold": sum(v["qty"] for v in sales_by_product.values()),
        "total_revenue": round(day_rev, 2),
        "total_cogs": round(day_cogs, 2),
        "gross_profit": round(day_rev - day_cogs, 2),
    }


def build_state(engine):
    """Full observable state snapshot."""
    return {
        "date": engine.current_date,
        "day_count": engine.day_count,
        "month_index": engine.month_index(),
        "is_working_day": engine.is_working_day(),
        "stock": observable_stock(engine),
        "pending_pos": observable_pending_pos(engine),
        "pending_transfers": observable_pending_transfers(engine),
        "active_discounts": observable_active_discounts(engine),
        "cumulative": {
            "total_revenue": round(engine.total_revenue, 2),
            "total_cogs": round(engine.total_cogs, 2),
            "gross_profit": round(engine.total_revenue - engine.total_cogs, 2),
            "total_orders": engine.order_counter,
            "total_pos": engine.po_counter,
            "total_transfers": engine.transfer_counter,
        }
    }


# ═══════════════════════════════════════════════════════════
# HTTP Handler
# ═══════════════════════════════════════════════════════════

class BotHandler(BaseHTTPRequestHandler):
    engine = None
    cfg = None
    sim_months = 12
    seed = None
    sim_active = False

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/state":
            if not self.sim_active:
                return self._json_error(400, "No active simulation. POST /start first.")
            return self._json_ok(build_state(self.engine))

        elif self.path == "/catalog":
            if not self.cfg:
                return self._json_error(400, "No simulation configured. POST /start first.")
            return self._json_ok({"products": public_products(self.cfg)})

        elif self.path == "/locations":
            if not self.cfg:
                return self._json_error(400, "No simulation configured. POST /start first.")
            return self._json_ok({"locations": public_locations(self.cfg)})

        elif self.path == "/suppliers":
            if not self.cfg:
                return self._json_error(400, "No simulation configured. POST /start first.")
            return self._json_ok({"suppliers": public_suppliers(self.cfg)})

        elif self.path == "/history/sales":
            if not self.sim_active:
                return self._json_error(400, "No active simulation.")
            # Return aggregated sales (not raw order log — too large)
            agg = defaultdict(lambda: {"qty": 0, "revenue": 0})
            for o in self.engine.order_log:
                key = (str(o["date"]), o["product_id"], o["sales_location_id"])
                agg[key]["qty"] += o["qty_filled"]
                agg[key]["revenue"] += o["line_total"]
            sales = [{"date": k[0], "product_id": k[1], "location_id": k[2],
                       "qty": v["qty"], "revenue": round(v["revenue"], 2)}
                      for k, v in agg.items()]
            return self._json_ok({"sales": sales})

        elif self.path == "/history/pos":
            if not self.sim_active:
                return self._json_error(400, "No active simulation.")
            pos = [{"po_id": po["po_id"], "date": po["date"],
                     "supplier_id": po["supplier_id"],
                     "product_id": po["product_id"],
                     "qty_ordered": po["qty_ordered"],
                     "qty_received": po.get("qty_received", 0),
                     "total_cost": round(po["total_cost"], 2),
                     "lead_days": po.get("lead_days", 0),
                     "status": po.get("status", "pending")}
                    for po in self.engine.po_log]
            return self._json_ok({"purchase_orders": pos})

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        body = self._read_body()

        if self.path == "/start":
            params = json.loads(body) if body else {}
            months = params.get("months", BotHandler.sim_months)
            seed = params.get("seed", BotHandler.seed)
            label = params.get("label", "bot_run")

            print(f"\n▶ Starting bot simulation: {months} months" +
                  (f", seed={seed}" if seed is not None else "") +
                  f", label={label}")

            cfg = load_config()
            cfg["company"]["sim_months"] = months
            if seed is not None:
                cfg["company"]["random_seed"] = seed

            BotHandler.cfg = cfg
            BotHandler.engine = SimulationEngine(cfg, mode="bot")
            BotHandler.sim_active = True
            BotHandler.run_label = label

            print(f"  Sim initialized: {BotHandler.engine.current_date}")

            return self._json_ok({
                "status": "started",
                "start_date": BotHandler.engine.current_date,
                # NOTE: sim_months intentionally NOT sent — bots must not know when sim ends
                "state": build_state(BotHandler.engine),
                "catalog": public_products(cfg),
                "locations": public_locations(cfg),
                "suppliers": public_suppliers(cfg),
            })

        elif self.path == "/step":
            if not self.sim_active:
                return self._json_error(400, "No active simulation. POST /start first.")

            commands = json.loads(body) if body else []

            # Snapshot before step
            prev_orders = len(self.engine.order_log)
            prev_rev = self.engine.total_revenue
            prev_cogs = self.engine.total_cogs

            # Advance one day
            cont = self.engine.step_day(commands=commands)

            # Build response
            summary = day_summary(self.engine, prev_orders, prev_rev, prev_cogs)
            state = build_state(self.engine)

            result = {
                "continue": cont,
                "date": self.engine.current_date - __import__('datetime').timedelta(days=1),
                "day_summary": summary,
                "state": state,
            }

            if not cont:
                BotHandler.sim_active = False
                result["status"] = "simulation_complete"
                result["final"] = {
                    "total_revenue": round(self.engine.total_revenue, 2),
                    "total_cogs": round(self.engine.total_cogs, 2),
                    "gross_profit": round(self.engine.total_revenue - self.engine.total_cogs, 2),
                    "total_days": self.engine.day_count,
                    "total_orders": self.engine.order_counter,
                    "total_pos": self.engine.po_counter,
                    "total_transfers": self.engine.transfer_counter,
                }
                print(f"\n✓ Simulation complete: {self.engine.day_count} days, "
                      f"revenue={self.engine.total_revenue:,.0f} THB")

                # Save run data to data/ folder
                compact = build_compact(self.engine)
                label = getattr(BotHandler, 'run_label', 'bot_run')
                run_dir = save_run(self.engine, label=label, compact_data=compact)
                result["run_folder"] = os.path.basename(run_dir)

            return self._json_ok(result)

        else:
            self.send_response(404)
            self.end_headers()

    # ── Helpers ──
    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _json_ok(self, data):
        body = json.dumps(data, separators=(',', ':'), cls=DateEncoder).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, code, msg):
        body = json.dumps({"error": msg}).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="ToyLand Bot Simulation Server")
    parser.add_argument("--port", type=int, default=5056)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    BotHandler.sim_months = args.months
    BotHandler.seed = args.seed

    server = HTTPServer(("127.0.0.1", args.port), BotHandler)
    print(f"ToyLand Bot Server running on http://127.0.0.1:{args.port}")
    print(f"Default sim: {args.months} months" +
          (f", seed={args.seed}" if args.seed else ", random seed"))
    print(f"\nEndpoints:")
    print(f"  POST /start          Start new simulation")
    print(f"  POST /step           Advance one day (send commands as JSON array)")
    print(f"  GET  /state          Current observable state")
    print(f"  GET  /catalog        Product catalog")
    print(f"  GET  /locations      Location info")
    print(f"  GET  /suppliers      Supplier info")
    print(f"  GET  /history/sales  Sales history")
    print(f"  GET  /history/pos    PO history")
    print(f"\nPress Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
