"""
Async Job Runner — runs bot simulations in background threads with safety limits.

Features:
  - Background execution: bot runs don't block the HTTP server
  - Per-step timeout: kills bots that take too long on a single decide() call
  - Total run timeout: caps total wall-clock time for a simulation
  - Step count cap: prevents infinite loops (max steps = sim_months × 35)
  - Progress tracking: jobs report current day / total days
  - Job history: completed jobs store results for dashboard polling

Usage from server.py:
    from job_runner import JobRunner
    runner = JobRunner()
    job_id = runner.submit(bot_name="demo_smart", months=12, seed=2026, label="my_run")
    status = runner.get_job(job_id)  # poll for progress
"""
import os
import sys
import time
import uuid
import signal
import threading
import traceback
import importlib
from datetime import datetime
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
BOTS_DIR = os.path.join(PROJECT_DIR, "bots")

sys.path.insert(0, BASE_DIR)
if BOTS_DIR not in sys.path:
    sys.path.insert(0, BOTS_DIR)

from sim_engine import load_config, SimulationEngine, build_compact
import mongo_runs
from bot_server import (public_products, public_locations, public_suppliers,
                        observable_stock, observable_pending_pos,
                        observable_pending_transfers, observable_active_discounts,
                        day_summary as bot_day_summary, build_state as bot_build_state)


# ═══════════════════════════════════════════════════════════
# Safety limits
# ═══════════════════════════════════════════════════════════

DEFAULT_STEP_TIMEOUT = 5        # seconds per bot.decide() call
DEFAULT_TOTAL_TIMEOUT = 300     # seconds total (5 minutes)
MAX_STEPS_PER_MONTH = 35        # max days per month (safety cap)
MAX_COMMANDS_PER_STEP = 500     # max commands a bot can issue per day


# ═══════════════════════════════════════════════════════════
# Job states
# ═══════════════════════════════════════════════════════════

class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


# ═══════════════════════════════════════════════════════════
# Step timeout via threading
# ═══════════════════════════════════════════════════════════

class StepTimeoutError(Exception):
    pass


def run_with_timeout(func, args=(), kwargs=None, timeout=5):
    """Run func(*args) in a thread with a timeout. Returns result or raises."""
    kwargs = kwargs or {}
    result = [None]
    error = [None]

    def target():
        try:
            result[0] = func(*args, **kwargs)
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)

    if t.is_alive():
        # Thread is still running — bot.decide() is stuck
        raise StepTimeoutError(
            f"Bot decide() exceeded {timeout}s timeout. "
            f"Possible infinite loop in bot logic."
        )

    if error[0]:
        raise error[0]

    return result[0]


# ═══════════════════════════════════════════════════════════
# Bot initialization (shared with server.py)
# ═══════════════════════════════════════════════════════════

def init_bot(bot, start_resp):
    """Initialize a bot instance from the /start response data."""
    resp = start_resp
    for p in resp["catalog"]:
        bot.catalog[p["id"]] = p
    bot.suppliers = resp["suppliers"]
    bot.locations = resp["locations"]
    bot.physical_locs = [l for l in bot.locations if l["type"] != "Online"]

    # Supplier mapping (shortest avg lead time)
    sup_by_cat = defaultdict(list)
    for s in bot.suppliers:
        for cat in s["categories"]:
            sup_by_cat[cat].append(s)

    for pid, p in bot.catalog.items():
        candidates = sup_by_cat.get(p["cat"], [])
        if candidates:
            best = min(candidates, key=lambda s: sum(s["lead_days"]) / 2)
            bot.product_supplier[pid] = best["id"]

    # NOTE: sim_months / estimated_total_days are intentionally NOT passed
    # to bots. Bots should not know when the simulation ends — this prevents
    # gaming the ending inventory score.

    # Bot-specific init
    if hasattr(bot, 'supplier_map'):
        bot.supplier_map = {s["id"]: s for s in bot.suppliers}
    if hasattr(bot, 'online_locs'):
        bot.online_locs = [l for l in bot.locations if l["type"] == "Online"]
    # Legacy fallback: bots that still use the flat reorder_qty field.
    # New rule: one full refill round, no 2× safety cushion.
    if hasattr(bot, 'reorder_qty'):
        num_stores = len(bot.physical_locs)
        for pid, p in bot.catalog.items():
            refill = p.get("refill_num", 5)
            bot.reorder_qty[pid] = max(refill * num_stores, 10)


# ═══════════════════════════════════════════════════════════
# Core bot execution with safety
# ═══════════════════════════════════════════════════════════

def execute_bot_run(bot_name, module_name, class_name, months, seed, label,
                    job, step_timeout=DEFAULT_STEP_TIMEOUT,
                    total_timeout=DEFAULT_TOTAL_TIMEOUT):
    """Run a bot simulation with full safety limits. Updates job dict in place."""

    start_time = time.time()
    max_steps = months * MAX_STEPS_PER_MONTH

    # Load config and create engine
    cfg = load_config()
    cfg["company"]["sim_months"] = months
    # Bot runs always start 2026-01-01, picking up after the welcome
    # baseline which covers all of 2025.
    cfg["company"]["sim_start"] = mongo_runs.USER_SIM_START
    if seed is not None:
        cfg["company"]["random_seed"] = seed

    engine = SimulationEngine(cfg, mode="bot")

    # Build initial state
    initial_state = bot_build_state(engine)
    start_resp = {
        "status": "started",
        "start_date": engine.current_date,
        # NOTE: sim_months intentionally NOT passed to bots.
        # Bots should not know when the simulation ends.
        "state": initial_state,
        "catalog": public_products(cfg),
        "locations": public_locations(cfg),
        "suppliers": public_suppliers(cfg),
    }

    # Import and instantiate bot
    mod = importlib.import_module(module_name)
    # Reload to pick up changes if the file was updated
    importlib.reload(mod)
    bot_class = getattr(mod, class_name)
    bot = bot_class()
    init_bot(bot, start_resp)

    # Update job progress
    job["status"] = JobStatus.RUNNING
    job["total_steps"] = max_steps  # Estimate; actual may be less
    job["current_step"] = 0
    job["started_at"] = datetime.now().isoformat()

    state = initial_state
    step_count = 0

    while True:
        step_count += 1
        job["current_step"] = step_count

        # ── Safety: step count cap ──
        if step_count > max_steps:
            raise RuntimeError(
                f"Exceeded maximum step count ({max_steps}). "
                f"Simulation should have ended by now — possible engine bug."
            )

        # ── Safety: total timeout ──
        elapsed = time.time() - start_time
        if elapsed > total_timeout:
            raise StepTimeoutError(
                f"Total run time exceeded {total_timeout}s limit "
                f"after {step_count} steps."
            )

        # ── Run bot.decide() with per-step timeout ──
        if hasattr(bot, 'day_count'):
            bot.day_count += 1

        commands = run_with_timeout(bot.decide, args=(state,), timeout=step_timeout)

        # ── Safety: cap commands per step ──
        if isinstance(commands, list) and len(commands) > MAX_COMMANDS_PER_STEP:
            commands = commands[:MAX_COMMANDS_PER_STEP]
            job.setdefault("warnings", []).append(
                f"Day {step_count}: commands capped at {MAX_COMMANDS_PER_STEP}"
            )

        # ── Advance simulation ──
        prev_orders = len(engine.order_log)
        prev_rev = engine.total_revenue
        prev_cogs = engine.total_cogs

        cont = engine.step_day(commands=commands)

        summary = bot_day_summary(engine, prev_orders, prev_rev, prev_cogs)
        state = bot_build_state(engine)

        # Feed tracker if bot has one
        if hasattr(bot, 'tracker') and hasattr(bot.tracker, 'record_day'):
            bot.tracker.record_day(summary)

        # Update progress
        job["current_day"] = str(engine.current_date)
        job["revenue"] = round(engine.total_revenue, 2)

        if not cont:
            break

    # ── Build results ──
    compact = build_compact(engine)
    try:
        run_id = mongo_runs.save_run(
            engine, label=label, compact_data=compact, bot_slug=bot_name
        )
        compact["run_folder"] = run_id
    except Exception as e:
        print(f"WARN: failed to save bot run to Mongo: {e}")
        run_id = None
        compact["run_folder"] = None

    elapsed = round(time.time() - start_time, 1)

    summary = compact.get("summary", {})
    return {
        "compact": compact,
        "run_folder": run_id,
        "total_days": engine.day_count,
        "total_revenue": round(engine.total_revenue, 2),
        "total_cogs": round(engine.total_cogs, 2),
        "gross_profit": round(engine.total_revenue - engine.total_cogs, 2),
        "ending_inventory_value": summary.get("ending_inventory_value", 0),
        "bizbotbash_score": summary.get("bizbotbash_score", 0),
        "elapsed_seconds": elapsed,
    }


# ═══════════════════════════════════════════════════════════
# Job Runner — manages background execution
# ═══════════════════════════════════════════════════════════

class JobRunner:
    """Manages async bot simulation jobs."""

    def __init__(self):
        self.jobs = {}       # job_id → job dict
        self.lock = threading.Lock()
        # Default bot registry
        self.bot_registry = {
            "demo_baseline": ("demo_baseline_bot", "DemoBot"),
            "demo_smart": ("demo_smart_bot", "SmartBot"),
            "bizbotbash_champion": ("bizbotbash_champion", "ChampionBot"),
            "ai_genius": ("ai_genius_bot", "AIGeniusBot"),
        }

    def register_bot(self, bot_id, module_name, class_name):
        """Register a new bot (e.g., from file upload)."""
        with self.lock:
            self.bot_registry[bot_id] = (module_name, class_name)

    def get_available_bots(self):
        """Return list of registered bots."""
        with self.lock:
            return [{"id": k, "name": k.replace("_", " ").title()}
                    for k in self.bot_registry.keys()]

    def submit(self, bot_name, months=12, seed=None, label=None,
               submitted_by=None, step_timeout=DEFAULT_STEP_TIMEOUT,
               total_timeout=DEFAULT_TOTAL_TIMEOUT):
        """Submit a bot run job. Returns job_id immediately."""

        with self.lock:
            if bot_name not in self.bot_registry:
                raise ValueError(
                    f"Unknown bot: {bot_name}. "
                    f"Available: {list(self.bot_registry.keys())}"
                )
            module_name, class_name = self.bot_registry[bot_name]

        job_id = str(uuid.uuid4())[:8]
        label = label or bot_name

        job = {
            "id": job_id,
            "bot": bot_name,
            "label": label,
            "months": months,
            "seed": seed,
            "submitted_by": submitted_by,
            "status": JobStatus.QUEUED,
            "submitted_at": datetime.now().isoformat(),
            "started_at": None,
            "completed_at": None,
            "current_step": 0,
            "total_steps": months * MAX_STEPS_PER_MONTH,
            "current_day": None,
            "revenue": 0,
            "result": None,
            "error": None,
            "warnings": [],
        }

        with self.lock:
            self.jobs[job_id] = job

        # Launch in background thread
        thread = threading.Thread(
            target=self._run_job,
            args=(job_id, bot_name, module_name, class_name,
                  months, seed, label, step_timeout, total_timeout),
            daemon=True
        )
        thread.start()

        return job_id

    def _run_job(self, job_id, bot_name, module_name, class_name,
                 months, seed, label, step_timeout, total_timeout):
        """Background thread: run the bot and update job status."""
        job = self.jobs[job_id]

        try:
            print(f"\n▶ Job {job_id}: Running {bot_name} bot, "
                  f"{months} months, label={label}")

            result = execute_bot_run(
                bot_name=bot_name,
                module_name=module_name,
                class_name=class_name,
                months=months,
                seed=seed,
                label=label,
                job=job,
                step_timeout=step_timeout,
                total_timeout=total_timeout,
            )

            with self.lock:
                job["status"] = JobStatus.COMPLETED
                job["completed_at"] = datetime.now().isoformat()
                job["result"] = result

            print(f"  ✓ Job {job_id}: Complete — "
                  f"revenue={result['total_revenue']:,.0f} THB, "
                  f"{result['elapsed_seconds']}s")

        except StepTimeoutError as e:
            with self.lock:
                job["status"] = JobStatus.TIMEOUT
                job["completed_at"] = datetime.now().isoformat()
                job["error"] = str(e)
            print(f"  ⏱ Job {job_id}: Timeout — {e}")

        except Exception as e:
            with self.lock:
                job["status"] = JobStatus.FAILED
                job["completed_at"] = datetime.now().isoformat()
                job["error"] = str(e)
            print(f"  ✗ Job {job_id}: Failed — {e}")
            traceback.print_exc()

    def get_job(self, job_id):
        """Get job status (safe copy without large compact data)."""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return None

            # Return a summary (don't send full compact data in status)
            summary = {k: v for k, v in job.items() if k != "result"}
            if job["result"]:
                summary["run_folder"] = job["result"].get("run_folder")
                summary["total_revenue"] = job["result"].get("total_revenue")
                summary["total_cogs"] = job["result"].get("total_cogs")
                summary["gross_profit"] = job["result"].get("gross_profit")
                summary["ending_inventory_value"] = job["result"].get("ending_inventory_value", 0)
                summary["bizbotbash_score"] = job["result"].get("bizbotbash_score", 0)
                summary["elapsed_seconds"] = job["result"].get("elapsed_seconds")
                summary["total_days"] = job["result"].get("total_days")
            return summary

    def get_job_result(self, job_id):
        """Get full job result including compact data (for loading into dashboard)."""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or not job.get("result"):
                return None
            return job["result"].get("compact")

    def list_jobs(self, limit=50):
        """List recent jobs (newest first)."""
        with self.lock:
            jobs = sorted(self.jobs.values(),
                          key=lambda j: j["submitted_at"], reverse=True)
            return [self.get_job(j["id"]) for j in jobs[:limit]]

    def cleanup_old_jobs(self, max_age_hours=24):
        """Remove completed/failed jobs older than max_age_hours."""
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        with self.lock:
            to_remove = []
            for jid, job in self.jobs.items():
                if job["status"] in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.TIMEOUT):
                    completed = job.get("completed_at", "")
                    if completed and datetime.fromisoformat(completed) < cutoff:
                        to_remove.append(jid)
            for jid in to_remove:
                del self.jobs[jid]
            if to_remove:
                print(f"  Cleaned up {len(to_remove)} old jobs")
