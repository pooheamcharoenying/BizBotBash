"""Run storage helpers backed by MongoDB.

Replaces the old filesystem-based save_run/list_runs/get_run_detail
that wrote to data/<folder>/compact.json + run_meta.json + xlsx.

Three collections:
  runs        — summary + metadata, one doc per run (indexed for leaderboards)
  run_detail  — compact aggregates for the dashboard, one doc per run
  run_raw     — raw transaction logs for on-demand Excel regeneration
"""
from datetime import datetime, timezone
from bson import ObjectId

from db import get_db


CHALLENGE_SLUG = "toyland"


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _iso(obj):
    """Recursively convert datetime/date to ISO strings for BSON safety."""
    if isinstance(obj, dict):
        return {k: _iso(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_iso(v) for v in obj]
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj


def _get_challenge_id(db):
    doc = db.challenges.find_one({"slug": CHALLENGE_SLUG}, {"_id": 1})
    return doc["_id"] if doc else None


def _summary_from_engine(engine):
    units_sold = sum(o["qty_filled"] for o in engine.order_log)
    stockouts = sum(o.get("qty_backordered", 0) for o in engine.order_log)
    revenue = round(engine.total_revenue, 2)
    cogs = round(engine.total_cogs, 2)
    return {
        "total_revenue": revenue,
        "total_cogs": cogs,
        "total_profit": round(revenue - cogs, 2),
        "net_margin": round((revenue - cogs) / revenue, 4) if revenue > 0 else 0,
        "units_sold": units_sold,
        "stockout_rate": round(stockouts / max(units_sold + stockouts, 1), 4),
    }


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def save_run(engine, label, compact_data, bot_slug=None, user_id=None):
    """Persist a completed simulation run across the three collections.
    Returns the run_id (str) — used as the identifier the dashboard passes
    back to /runs/{id}."""
    db = get_db()
    if db is None:
        raise RuntimeError("MongoDB not configured (MONGODB_USER/PWD missing)")

    challenge_id = _get_challenge_id(db)
    bot_id = None
    if bot_slug and bot_slug != "auto":
        bot_doc = db.bots.find_one(
            {"challenge_id": challenge_id, "slug": bot_slug}, {"_id": 1}
        )
        if bot_doc:
            bot_id = bot_doc["_id"]

    now = _now()
    run_doc = {
        "challenge_id": challenge_id,
        "bot_id": bot_id,
        "bot_slug": bot_slug or "auto",
        "user_id": user_id,
        "label": label,
        "months": engine.cfg["company"].get("sim_months", 12),
        "seed": engine.cfg["company"].get("random_seed"),
        "status": "done",
        "started_at": now,
        "finished_at": now,
        "summary": _summary_from_engine(engine),
    }
    run_id = db.runs.insert_one(run_doc).inserted_id

    db.run_detail.insert_one({
        "run_id": run_id,
        "compact_data": _iso(compact_data),
        "created_at": now,
    })

    raw_doc = {
        "run_id": run_id,
        "order_log": _iso(engine.order_log),
        "po_log": _iso(engine.po_log),
        "transfer_log": _iso(engine.transfer_log),
        "daily_stock_log": _iso(engine.daily_stock_log),
        "financial_log": _iso(engine.financial_log),
        "action_log": _iso(getattr(engine, "action_log", [])),
        "created_at": now,
    }
    try:
        db.run_raw.insert_one(raw_doc)
    except Exception as e:
        # BSON 16MB cap — we skip raw if oversized. Excel download will fail
        # for this run but the dashboard still works from run_detail.
        print(f"WARN: run_raw insert failed (likely >16MB): {e}")

    return str(run_id)


def list_runs(user_id=None, limit=100):
    """Return a list of run summaries (most recent first) shaped for the
    existing /runs endpoint — same fields the dashboard was already
    consuming from list_runs() before: label, folder, timestamp, mode,
    months, total_revenue."""
    db = get_db()
    if db is None:
        return []

    query = {}
    # For anonymous demo: return all runs. Once auth ships, filter by user.
    if user_id is not None:
        query["user_id"] = user_id

    cursor = db.runs.find(query).sort("started_at", -1).limit(limit)
    out = []
    for d in cursor:
        ts = d.get("started_at")
        out.append({
            # "folder" is what the dashboard uses to key runs — keep the
            # field name for zero-change compatibility.
            "folder": str(d["_id"]),
            "label": d.get("label", "unknown"),
            "timestamp": ts.isoformat() if ts else "",
            "mode": "auto" if d.get("bot_slug") == "auto" else "bot",
            "bot_slug": d.get("bot_slug"),
            "months": d.get("months", 12),
            "total_revenue": d.get("summary", {}).get("total_revenue", 0),
        })
    return out


def get_run_detail(run_id):
    """Return compact_data for a specific run, or None."""
    db = get_db()
    if db is None:
        return None
    try:
        oid = ObjectId(run_id)
    except Exception:
        return None
    doc = db.run_detail.find_one({"run_id": oid})
    return doc["compact_data"] if doc else None


def get_run_summary(run_id):
    db = get_db()
    if db is None:
        return None
    try:
        oid = ObjectId(run_id)
    except Exception:
        return None
    return db.runs.find_one({"_id": oid})


def get_run_raw(run_id):
    """Return raw logs for a specific run, or None."""
    db = get_db()
    if db is None:
        return None
    try:
        oid = ObjectId(run_id)
    except Exception:
        return None
    return db.run_raw.find_one({"run_id": oid})


def delete_run(run_id):
    db = get_db()
    if db is None:
        return False
    try:
        oid = ObjectId(run_id)
    except Exception:
        return False
    db.runs.delete_one({"_id": oid})
    db.run_detail.delete_one({"run_id": oid})
    db.run_raw.delete_one({"run_id": oid})
    return True


def delete_all_runs():
    """Wipe all runs. Used by the /clear-all-runs admin endpoint."""
    db = get_db()
    if db is None:
        return 0
    n = db.runs.count_documents({})
    db.runs.delete_many({})
    db.run_detail.delete_many({})
    db.run_raw.delete_many({})
    return n


# ─────────────────────────────────────────────────────────────
# Bot helpers (read + upsert, keep cache of source on disk for imports)
# ─────────────────────────────────────────────────────────────

def list_bots():
    """Return active bots for the ToyLand challenge, shaped for /bots.
    Sorted by display_order first (default bots), then by creation time
    (user-submitted). 'id' is aliased to 'slug' for dashboard compat."""
    db = get_db()
    if db is None:
        return []
    challenge_id = _get_challenge_id(db)
    if not challenge_id:
        return []
    cursor = db.bots.find(
        {"challenge_id": challenge_id, "status": "active"},
        {"slug": 1, "name": 1, "type": 1, "description": 1,
         "author_user_id": 1, "display_order": 1, "created_at": 1},
    ).sort([("display_order", 1), ("created_at", 1)])
    out = []
    for d in cursor:
        out.append({
            # dashboard's modeSelector uses `id` as the option value
            "id": d["slug"],
            "slug": d["slug"],
            "name": d.get("name", d["slug"]),
            "type": d.get("type", "user"),
            "description": d.get("description", ""),
            "submitted_by": str(d["author_user_id"]) if d.get("author_user_id") else None,
        })
    return out


def get_bot_code(slug):
    """Return bot source code for a given slug, or None."""
    db = get_db()
    if db is None:
        return None
    challenge_id = _get_challenge_id(db)
    doc = db.bots.find_one(
        {"challenge_id": challenge_id, "slug": slug, "status": "active"},
        {"code": 1},
    )
    return doc["code"] if doc else None


def save_bot(slug, name, code, description="", bot_type="user", author_user_id=None):
    """Upsert a user-submitted bot."""
    db = get_db()
    if db is None:
        raise RuntimeError("MongoDB not configured")
    challenge_id = _get_challenge_id(db)
    now = _now()
    db.bots.update_one(
        {"challenge_id": challenge_id, "slug": slug},
        {
            "$set": {
                "challenge_id": challenge_id,
                "slug": slug,
                "name": name,
                "description": description,
                "type": bot_type,
                "code": code,
                "author_user_id": author_user_id,
                "status": "active",
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return slug
