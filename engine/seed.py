"""Seed the MongoDB with the ToyLand challenge, its config, and the
four built-in bots.

Exposed as seed_all(db) so it can be called from:
  - the CLI wrapper at scripts/seed_mongo.py
  - the /admin/seed server endpoint (frontend button)

Idempotent. Uses upserts keyed on slug/version so re-running refreshes
bot source code and config without creating duplicates.
"""
import os
import json
from datetime import datetime, timezone


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
CONFIG_DIR = os.path.join(PROJECT_DIR, "config")
BOTS_DIR = os.path.join(PROJECT_DIR, "bots")

CHALLENGE_SLUG = "toyland"


def _build_challenge_doc():
    """Compute challenge metadata from the actual config files so the
    numbers (SKU count, location count) stay in sync with the data."""
    products = _load_json("products.json")
    locations = _load_json("sales_locations.json")
    warehouses = _load_json("warehouses.json")

    physical = sum(1 for l in locations if l.get("type", "").lower() == "physical")
    online = sum(1 for l in locations if l.get("type", "").lower() == "online")
    total_locations = len(locations) + len(warehouses)

    description = (
        f"Run a Bangkok toy distributor. {len(products)} SKUs, "
        f"{physical} physical stores, {len(warehouses)} warehouse(s), "
        f"{online} online channels. Decide POs, transfers, shelf "
        f"layouts. Beat the baseline bot over 12-60 months."
    )

    return {
        "slug": CHALLENGE_SLUG,
        "name": "ToyLand Distribution",
        "description": description,
        "difficulty": 3,
        "status": "live",
        "simulator_module": "sim_engine",
        "duration_range": {"min": 1, "max": 60},
        "sku_count": len(products),
        "physical_store_count": physical,
        "online_channel_count": online,
        "warehouse_count": len(warehouses),
        "location_count": total_locations,
    }

# (filename, slug, display_name, type, description, display_order)
BOT_REGISTRY = [
    (
        "demo_baseline_bot.py",
        "demo_baseline",
        "Demo Baseline Bot",
        "default",
        "Mirrors the built-in auto operator. Fixed reorder threshold, "
        "no discounts, no learning. Deliberately simple.",
        1,
    ),
    (
        "demo_smart_bot.py",
        "demo_smart",
        "Demo Smart Bot",
        "demo",
        "Ranks products by revenue per location and assigns top sellers "
        "to A-shelves.",
        2,
    ),
    (
        "bizbotbash_champion.py",
        "bizbotbash_champion",
        "Demo Champion Bot",
        "demo",
        "EWMA demand learning + supplier reliability tracking + revenue-"
        "maximizing shelf allocation.",
        3,
    ),
    (
        "ai_genius_bot.py",
        "ai_genius",
        "Demo AI Genius Bot",
        "demo",
        "Champion core + batch PO consolidation + faster shelf re-"
        "optimization.",
        4,
    ),
]

CONFIG_FILES = [
    ("company.json",          "company"),
    ("categories.json",       "categories"),
    ("products.json",         "products"),
    ("suppliers.json",        "suppliers"),
    ("warehouses.json",       "warehouses"),
    ("sales_locations.json",  "sales_locations"),
    ("costs.json",            "costs"),
    ("shelf_config.json",     "shelf_config"),
    ("hidden_variables.json", "hidden_variables"),
]


def _load_json(filename):
    with open(os.path.join(CONFIG_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


def _load_bot_source(filename):
    with open(os.path.join(BOTS_DIR, filename), encoding="utf-8") as f:
        return f.read()


def _now():
    return datetime.now(timezone.utc)


def seed_challenge(db):
    challenge_doc = _build_challenge_doc()
    res = db.challenges.update_one(
        {"slug": CHALLENGE_SLUG},
        {
            "$set": {**challenge_doc, "updated_at": _now()},
            "$setOnInsert": {"created_at": _now()},
        },
        upsert=True,
    )
    doc = db.challenges.find_one({"slug": CHALLENGE_SLUG})
    return {
        "status": "inserted" if res.upserted_id else "updated",
        "challenge_id": str(doc["_id"]),
        "slug": CHALLENGE_SLUG,
        "sku_count": challenge_doc["sku_count"],
        "location_count": challenge_doc["location_count"],
    }


def seed_challenge_config(db, challenge_id):
    config_doc = {"challenge_id": challenge_id, "version": 1}
    for filename, key in CONFIG_FILES:
        config_doc[key] = _load_json(filename)

    res = db.challenge_configs.update_one(
        {"challenge_id": challenge_id, "version": 1},
        {
            "$set": {**config_doc, "updated_at": _now()},
            "$setOnInsert": {"created_at": _now()},
        },
        upsert=True,
    )
    return {
        "status": "inserted" if res.upserted_id else "updated",
        "version": 1,
        "files_merged": len(CONFIG_FILES),
    }


def seed_bots(db, challenge_id):
    items = []
    inserted = updated = 0
    for filename, slug, name, bot_type, description, order in BOT_REGISTRY:
        source = _load_bot_source(filename)
        res = db.bots.update_one(
            {"challenge_id": challenge_id, "slug": slug},
            {
                "$set": {
                    "challenge_id": challenge_id,
                    "slug": slug,
                    "name": name,
                    "description": description,
                    "type": bot_type,
                    "code": source,
                    "display_order": order,
                    "author_user_id": None,
                    "status": "active",
                    "source_filename": filename,
                    "updated_at": _now(),
                },
                "$setOnInsert": {"created_at": _now()},
            },
            upsert=True,
        )
        is_new = res.upserted_id is not None
        inserted += int(is_new)
        updated += int(not is_new)
        items.append({
            "slug": slug,
            "type": bot_type,
            "status": "inserted" if is_new else "updated",
            "bytes": len(source),
        })
    return {"inserted": inserted, "updated": updated, "items": items}


def ensure_indexes(db):
    created = []

    def idx(coll, keys, **kwargs):
        created.append(db[coll].create_index(keys, **kwargs))

    idx("challenges", "slug", unique=True)
    idx("challenge_configs", [("challenge_id", 1), ("version", -1)])
    idx("bots", [("challenge_id", 1), ("status", 1)])
    idx("bots", [("challenge_id", 1), ("slug", 1)], unique=True)
    idx("bots", "author_user_id")
    idx("runs",
        [("challenge_id", 1), ("status", 1), ("summary.total_profit", -1)])
    idx("runs", [("user_id", 1), ("started_at", -1)])
    idx("run_detail", "run_id", unique=True)
    idx("run_raw", "run_id", unique=True)
    idx("users", [("provider", 1), ("provider_id", 1)], unique=True)
    idx("users", "email", unique=True, sparse=True)

    return {"count": len(created), "names": created}


def seed_welcome_run(db, challenge_id):
    """Run a fresh 12-month auto-mode sim and persist it to runs +
    run_detail + run_raw, so the dashboard has something to show on
    first load. Skipped if a run with label 'welcome_baseline' already
    exists."""
    if db.runs.find_one({"challenge_id": challenge_id, "label": "welcome_baseline"}):
        return {"status": "skipped", "reason": "already exists"}

    # Lazy imports: these pull heavy deps, skip unless we actually need them
    from sim_engine import load_config, SimulationEngine, build_compact
    from mongo_runs import save_run

    cfg = load_config()
    cfg["company"]["sim_months"] = 12
    engine = SimulationEngine(cfg, mode="auto")
    engine.run()
    compact = build_compact(engine)
    run_id = save_run(
        engine,
        label="welcome_baseline",
        compact_data=compact,
        bot_slug="auto",
    )
    return {
        "status": "inserted",
        "run_id": run_id,
        "months": 12,
        "total_revenue": round(engine.total_revenue, 2),
    }


def seed_all(db):
    """Run all seed steps. Returns a structured result dict."""
    result = {}
    result["challenge"] = seed_challenge(db)
    challenge_id = db.challenges.find_one({"slug": CHALLENGE_SLUG})["_id"]
    result["challenge_config"] = seed_challenge_config(db, challenge_id)
    result["bots"] = seed_bots(db, challenge_id)
    result["indexes"] = ensure_indexes(db)
    result["welcome_run"] = seed_welcome_run(db, challenge_id)
    result["collections"] = {
        name: db[name].count_documents({})
        for name in sorted(db.list_collection_names())
    }
    return result
