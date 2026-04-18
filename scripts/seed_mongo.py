"""Seed the BizBotBash MongoDB with the ToyLand challenge, its config,
and the four built-in bots.

Run:
    # Locally (with MONGODB_USER / MONGODB_PWD in your shell or .env)
    python scripts/seed_mongo.py

    # Against Railway-hosted cluster
    MONGODB_USER=... MONGODB_PWD=... python scripts/seed_mongo.py

Idempotent: uses upserts keyed on {challenge.slug} and {bots.slug},
so running multiple times won't duplicate data. Safe to re-run after
config or bot source changes.
"""
import os
import sys
import json
from datetime import datetime, timezone

# Make engine/ importable for the db module
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJECT_DIR, "engine"))

from db import get_db  # noqa: E402


CONFIG_DIR = os.path.join(PROJECT_DIR, "config")
BOTS_DIR = os.path.join(PROJECT_DIR, "bots")

CHALLENGE_SLUG = "toyland"
CHALLENGE_DOC = {
    "slug": CHALLENGE_SLUG,
    "name": "ToyLand Distribution",
    "description": (
        "Run a Bangkok toy distributor. 78 SKUs, 4 physical stores, "
        "1 warehouse, 2 online channels. Decide POs, transfers, shelf "
        "layouts. Beat the baseline bot over 12–60 months."
    ),
    "difficulty": 3,
    "status": "live",
    "simulator_module": "sim_engine",
    "duration_range": {"min": 1, "max": 60},
    "sku_count": 78,
    "location_count": 6,
}

# Maps bot source file → (slug, display name, type, description)
BOT_REGISTRY = [
    (
        "demo_baseline_bot.py",
        "demo_baseline",
        "Demo Baseline",
        "default",
        "Mirrors the built-in auto operator. Fixed reorder threshold, "
        "no discounts, no learning. Deliberately simple — competitors "
        "should beat this.",
    ),
    (
        "demo_smart_bot.py",
        "demo_smart",
        "Demo Smart",
        "demo",
        "Ranks products by revenue per location and assigns top sellers "
        "to A-shelves. Beats baseline via shelf optimization.",
    ),
    (
        "bizbotbash_champion.py",
        "bizbotbash_champion",
        "BizBotBash Champion",
        "demo",
        "EWMA demand learning + supplier reliability tracking + revenue-"
        "maximizing shelf allocation. The reference competitive bot.",
    ),
    (
        "ai_genius_bot.py",
        "ai_genius",
        "AI Genius",
        "demo",
        "Champion core + batch PO consolidation + faster shelf re-"
        "optimization. Tuned for operating-cost efficiency.",
    ),
]

# Which JSON files land in which nested key of challenge_configs
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


def load_json(filename):
    with open(os.path.join(CONFIG_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


def load_bot_source(filename):
    with open(os.path.join(BOTS_DIR, filename), encoding="utf-8") as f:
        return f.read()


def now_utc():
    return datetime.now(timezone.utc)


def seed_challenge(db):
    res = db.challenges.update_one(
        {"slug": CHALLENGE_SLUG},
        {
            "$set": {**CHALLENGE_DOC, "updated_at": now_utc()},
            "$setOnInsert": {"created_at": now_utc()},
        },
        upsert=True,
    )
    doc = db.challenges.find_one({"slug": CHALLENGE_SLUG})
    print(f"  challenges: {'inserted' if res.upserted_id else 'updated'}  _id={doc['_id']}")
    return doc["_id"]


def seed_challenge_config(db, challenge_id):
    config_doc = {
        "challenge_id": challenge_id,
        "version": 1,
    }
    for filename, key in CONFIG_FILES:
        config_doc[key] = load_json(filename)

    res = db.challenge_configs.update_one(
        {"challenge_id": challenge_id, "version": 1},
        {
            "$set": {**config_doc, "updated_at": now_utc()},
            "$setOnInsert": {"created_at": now_utc()},
        },
        upsert=True,
    )
    print(f"  challenge_configs: {'inserted' if res.upserted_id else 'updated'}  "
          f"version=1, {len(CONFIG_FILES)} config files merged")


def seed_bots(db, challenge_id):
    inserted = updated = 0
    for filename, slug, name, bot_type, description in BOT_REGISTRY:
        source = load_bot_source(filename)
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
                    "author_user_id": None,
                    "status": "active",
                    "source_filename": filename,
                    "updated_at": now_utc(),
                },
                "$setOnInsert": {"created_at": now_utc()},
            },
            upsert=True,
        )
        if res.upserted_id:
            inserted += 1
            print(f"    + {slug:28s} ({bot_type}, {len(source)} bytes)")
        else:
            updated += 1
            print(f"    ~ {slug:28s} ({bot_type}, {len(source)} bytes)")
    print(f"  bots: {inserted} inserted, {updated} updated")


def ensure_indexes(db):
    print("\nEnsuring indexes...")
    db.challenges.create_index("slug", unique=True)
    db.challenge_configs.create_index(
        [("challenge_id", 1), ("version", -1)]
    )
    db.bots.create_index([("challenge_id", 1), ("status", 1)])
    db.bots.create_index([("challenge_id", 1), ("slug", 1)], unique=True)
    db.bots.create_index("author_user_id")
    db.runs.create_index(
        [("challenge_id", 1), ("status", 1), ("summary.total_profit", -1)]
    )
    db.runs.create_index([("user_id", 1), ("started_at", -1)])
    db.run_detail.create_index("run_id", unique=True)
    db.run_raw.create_index("run_id", unique=True)
    db.users.create_index(
        [("provider", 1), ("provider_id", 1)], unique=True
    )
    db.users.create_index("email", unique=True, sparse=True)
    print("  done.")


def main():
    db = get_db()
    if db is None:
        print("ERROR: MONGODB_USER / MONGODB_PWD not set. "
              "Export them or add to a .env file and retry.",
              file=sys.stderr)
        sys.exit(1)

    print(f"Seeding {db.name} ...\n")
    print("challenges + challenge_configs")
    challenge_id = seed_challenge(db)
    seed_challenge_config(db, challenge_id)

    print("\nbots")
    seed_bots(db, challenge_id)

    ensure_indexes(db)

    print("\nDone. Collections now hold:")
    for cn in sorted(db.list_collection_names()):
        print(f"  {cn:22s}  {db[cn].count_documents({})} docs")


if __name__ == "__main__":
    main()
