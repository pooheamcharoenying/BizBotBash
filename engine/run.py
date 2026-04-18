#!/usr/bin/env python3
"""
ToyLand Distribution — One-Command Simulation Runner
=====================================================

Usage (run from the engine/ folder):
    python run.py              # Run simulation + export Excel files
    python run.py --sim-only   # Run simulation only (save JSON)
    python run.py --export-only # Export Excel from existing sim_output.json

To customize the simulation, edit the JSON files in ../config/:
    config/company.json         — Company profile, sim duration, random seed
    config/products.json        — Product catalog (add/remove/edit SKUs)
    config/hidden_variables.json — Demand drivers (the secret sauce)
    config/suppliers.json       — Supplier list, lead times, reliability
    config/customers.json       — Customer list, order frequency, tiers
    config/warehouses.json      — Warehouse setup
    config/costs.json           — Fixed costs and discount tiers

Quick experiments:
    - Change "random_seed" in company.json → different random outcomes
    - Change "sim_months" → simulate longer/shorter
    - Increase a product's "base_daily_demand" → watch revenue shift
    - Add a hype_event → create a demand spike in a specific month
    - Lower supplier "reliability" → more partial deliveries
"""
import sys
import os
import json
from datetime import date as date_type

# Ensure imports work from engine/ folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sim_engine import load_config, SimulationEngine
from export_excel import export_all

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(ENGINE_DIR)

class DateEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, date_type):
            return obj.isoformat()
        return super().default(obj)

def run_simulation():
    print("=" * 60)
    print("  ToyLand Distribution — Simulation Runner")
    print("=" * 60)
    print()

    print("[1/3] Loading configuration from config/ ...")
    cfg = load_config()
    print(f"      Company: {cfg['company']['name']}")
    print(f"      Products: {len(cfg['products'])} SKUs")
    print(f"      Customers: {len(cfg['customers'])}")
    print(f"      Suppliers: {len(cfg['suppliers'])}")
    print(f"      Duration: {cfg['company']['sim_months']} months from {cfg['company']['sim_start']}")
    print(f"      Random seed: {cfg['company'].get('random_seed', 'none')}")
    print()

    print("[2/3] Running simulation ...")
    engine = SimulationEngine(cfg)
    engine.run()
    output = engine.get_output()

    json_path = os.path.join(PROJECT_DIR, "sim_output.json")
    with open(json_path, "w") as f:
        json.dump(output, f, cls=DateEncoder, default=str)
    print(f"      Raw data saved to sim_output.json")
    print()

    return output

def run_export(data_dict=None):
    print("[3/3] Exporting Excel workbooks ...")
    if data_dict:
        export_all(data_dict=data_dict)
    else:
        export_all()
    print()
    print("=" * 60)
    print("  Done! Check output/data/ for Excel files")
    print("  Hidden variables in output/hidden_variables/")
    print("=" * 60)

if __name__ == "__main__":
    args = sys.argv[1:]

    if "--export-only" in args:
        run_export()
    elif "--sim-only" in args:
        run_simulation()
    else:
        output = run_simulation()
        run_export(data_dict=output)
