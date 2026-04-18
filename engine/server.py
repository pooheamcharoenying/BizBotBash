"""
Local simulation server — runs Python sim_engine when the dashboard requests it.

Start:  python engine/server.py
Then open the dashboard HTML in your browser.

Endpoints:
  POST /run              Run auto-mode simulation (synchronous)
  POST /run-bot          Submit a bot simulation job (async, returns job_id)
  GET  /bots             List available bots
  POST /submit-bot       Upload a custom bot Python file
  GET  /jobs             List all jobs
  GET  /jobs/{id}        Get job status / progress
  GET  /jobs/{id}/result Get full compact data for a completed job
  GET  /runs             List all saved data runs
  GET  /runs/{name}      Get compact data for a specific run
  DELETE /runs/{name}    Delete a saved run
  DELETE /clear-all-runs Delete ALL saved runs + sim_excel files

Default port: 5055  (change with --port)
"""
import json
import os
import sys
import re
import io
import zipfile
import shutil
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import date as date_type

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, BASE_DIR)

BOTS_DIR = os.path.join(PROJECT_DIR, "bots")
if BOTS_DIR not in sys.path:
    sys.path.insert(0, BOTS_DIR)

DASHBOARD_DIR = os.path.join(PROJECT_DIR, "dashboard")
LANDING_PATH = os.path.join(DASHBOARD_DIR, "index.html")
DASHBOARD_PATH = os.path.join(DASHBOARD_DIR, "ToyLand_Dashboard.html")
TUTORIAL_TOYLAND_PATH = os.path.join(DASHBOARD_DIR, "tutorial_toyland.html")
COWORK_PROMPT_PATH = os.path.join(DASHBOARD_DIR, "cowork-prompt.md")
ADMIN_PATH = os.path.join(DASHBOARD_DIR, "admin.html")
SIM_EXCEL_DIR = os.path.join(PROJECT_DIR, "sim_excel")

from sim_engine import load_config, SimulationEngine, build_compact, DATA_DIR
from job_runner import JobRunner
from db import ping as db_ping, get_db, MONGO_DB_NAME
from seed import seed_all
import mongo_runs


# ── Global job runner instance ──
runner = JobRunner()


class DateEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, date_type):
            return obj.isoformat()
        return super().default(obj)


class SimHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    # ── Helpers ──

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _json_ok(self, data):
        body = json.dumps(data, separators=(',', ':'), cls=DateEncoder).encode()
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _serve_html(self, path):
        if not os.path.isfile(path):
            return self._json_error(404, "Page not found")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_markdown_download(self, path, filename):
        if not os.path.isfile(path):
            return self._json_error(404, "File not found")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_excel_zip(self):
        """Serve the latest run's data as xlsx. Prefers Mongo — generates
        the workbook on the fly from run_raw logs. Falls back to the
        committed data/welcome_baseline_*/ folder (for legacy runs)."""
        # ── Try Mongo first ──
        try:
            runs = mongo_runs.list_runs(limit=1)
            if runs:
                from mongo_excel import run_to_xlsx_bytes
                body, filename = run_to_xlsx_bytes(runs[0]["folder"])
                if body:
                    self.send_response(200)
                    self._cors_headers()
                    self.send_header("Content-Type",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{filename}"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
        except Exception as e:
            print(f"WARN: Mongo xlsx generation failed, falling back: {e}")

        # ── Fallback: filesystem ──
        source_dir = None
        if os.path.isdir(DATA_DIR):
            candidates = [os.path.join(DATA_DIR, d) for d in os.listdir(DATA_DIR)]
            candidates = [d for d in candidates if os.path.isdir(d)]
            if candidates:
                candidates.sort(key=os.path.getmtime, reverse=True)
                source_dir = candidates[0]

        if source_dir is None and os.path.isdir(SIM_EXCEL_DIR):
            source_dir = SIM_EXCEL_DIR

        if source_dir is None:
            return self._json_error(
                404,
                "No excel files yet. Run a simulation first, then try again."
            )

        xlsx_files = [f for f in os.listdir(source_dir) if f.endswith(".xlsx")]
        if not xlsx_files:
            return self._json_error(404, "No excel files found.")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in sorted(xlsx_files):
                zf.write(os.path.join(source_dir, name), arcname=name)
        body = buf.getvalue()

        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         'attachment; filename="toyland-excel.zip"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # Suppress default logging

    def _json_error(self, code, msg):
        body = json.dumps({"error": msg}).encode()
        self.send_response(code)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    # ── GET endpoints ──

    def do_GET(self):
        # Strip trailing slash for comparison (but keep "/" itself)
        path = self.path
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]

        if path == "/" or path == "/index.html":
            self._serve_html(LANDING_PATH)

        elif path == "/toyland":
            self._serve_html(DASHBOARD_PATH)

        elif path == "/toyland/tutorial":
            self._serve_html(TUTORIAL_TOYLAND_PATH)

        elif path == "/toyland/cowork-prompt.md":
            self._serve_markdown_download(COWORK_PROMPT_PATH, "toyland-analysis.md")

        elif path == "/toyland/download-excel":
            self._serve_excel_zip()

        elif path == "/admin":
            self._serve_html(ADMIN_PATH)

        elif self.path == "/healthz":
            self._json_ok({"ok": True})

        elif self.path == "/healthz/db":
            ok, info = db_ping()
            payload = {"ok": ok}
            if ok:
                payload["mongo"] = info
            else:
                payload["error"] = info
            self._json_ok(payload)

        elif self.path == "/runs":
            self._json_ok(mongo_runs.list_runs())

        elif self.path == "/bots":
            self._json_ok(mongo_runs.list_bots())

        elif self.path == "/jobs":
            self._json_ok(runner.list_jobs())

        elif self.path.startswith("/jobs/") and "/result" in self.path:
            # GET /jobs/{id}/result — full compact data
            job_id = self.path.split("/")[2]
            result = runner.get_job_result(job_id)
            if result:
                self._json_ok(result)
            else:
                self._json_error(404, "Job not found or not complete")

        elif self.path.startswith("/jobs/"):
            # GET /jobs/{id} — job status
            job_id = self.path[len("/jobs/"):]
            job = runner.get_job(job_id)
            if job:
                self._json_ok(job)
            else:
                self._json_error(404, "Job not found")

        elif self.path.startswith("/runs/"):
            run_id = self.path[len("/runs/"):]
            compact = mongo_runs.get_run_detail(run_id)
            if compact is not None:
                self._json_ok(compact)
            else:
                self._json_error(404, "Run not found")

        else:
            self.send_response(404)
            self.end_headers()

    # ── DELETE endpoints ──

    def do_DELETE(self):
        if self.path == "/clear-all-runs":
            deleted = mongo_runs.delete_all_runs()
            with runner.lock:
                runner.jobs.clear()
            print(f"  Cleared all: {deleted} runs removed from Mongo")
            self._json_ok({
                "cleared": True,
                "deleted_runs": deleted,
            })

        elif self.path.startswith("/runs/"):
            run_id = self.path[len("/runs/"):]
            if mongo_runs.delete_run(run_id):
                print(f"  Deleted run: {run_id}")
                self._json_ok({"deleted": run_id})
            else:
                self._json_error(404, "Run not found")
        else:
            self.send_response(404)
            self.end_headers()

    # ── POST endpoints ──

    def do_POST(self):
        if self.path == "/run":
            # Synchronous auto-mode simulation
            params = json.loads(self._read_body() or b"{}")
            months = params.get("months", 12)
            seed = params.get("seed", None)
            label = params.get("label", "baseline")

            print(f"\n▶ Running auto simulation: {months} months" +
                  (f", seed={seed}" if seed else "") + f", label={label}")

            cfg = load_config()
            cfg["company"]["sim_months"] = months
            if seed is not None:
                cfg["company"]["random_seed"] = seed

            engine = SimulationEngine(cfg, mode="auto")
            engine.run()

            compact = build_compact(engine)
            try:
                run_id = mongo_runs.save_run(
                    engine, label=label, compact_data=compact, bot_slug="auto"
                )
                compact["run_folder"] = run_id
            except Exception as e:
                print(f"  WARN: failed to save run to Mongo: {e}")
                compact["run_folder"] = None

            self._json_ok(compact)
            print(f"  ✓ Auto simulation complete, sent to dashboard")

        elif self.path == "/run-bot":
            # Async bot simulation — returns job_id immediately
            params = json.loads(self._read_body() or b"{}")
            bot_name = params.get("bot", "demo_baseline")
            months = params.get("months", 12)
            seed = params.get("seed", None)
            label = params.get("label", bot_name)
            submitted_by = params.get("submitted_by", None)

            try:
                # Scale timeout for longer runs (base 300s for 12mo)
                total_timeout = max(300, int(months * 25))
                job_id = runner.submit(
                    bot_name=bot_name,
                    months=months,
                    seed=seed,
                    label=label,
                    submitted_by=submitted_by,
                    total_timeout=total_timeout,
                )
                self._json_ok({
                    "job_id": job_id,
                    "status": "queued",
                    "message": f"Bot '{bot_name}' simulation queued. "
                               f"Poll GET /jobs/{job_id} for progress."
                })
            except ValueError as e:
                self._json_error(400, str(e))
            except Exception as e:
                self._json_error(500, str(e))

        elif self.path == "/submit-bot":
            # Upload a custom bot Python file
            self._handle_bot_upload()

        elif self.path == "/admin/seed":
            # Idempotent: seed challenges, challenge_configs, bots from repo
            db = get_db()
            if db is None:
                return self._json_error(
                    503,
                    "Database not configured: MONGODB_USER / MONGODB_PWD missing."
                )
            try:
                results = seed_all(db)
                # Mongo returns ObjectId/datetime, which json can't encode
                def _coerce(obj):
                    if isinstance(obj, dict):
                        return {k: _coerce(v) for k, v in obj.items()}
                    if isinstance(obj, list):
                        return [_coerce(v) for v in obj]
                    if hasattr(obj, "isoformat"):  # datetime
                        return obj.isoformat()
                    if hasattr(obj, "binary"):  # ObjectId
                        return str(obj)
                    return obj
                self._json_ok({"ok": True, "results": _coerce(results)})
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                print(tb)
                self._json_error(500, f"{type(e).__name__}: {e}")

        else:
            self.send_response(404)
            self.end_headers()

    # ── Bot upload handler ──

    def _handle_bot_upload(self):
        """Accept a multipart or JSON bot file upload."""
        body = self._read_body()
        if not body:
            return self._json_error(400, "No file data")

        content_type = self.headers.get("Content-Type", "")

        # Support JSON upload: {"filename": "my_bot.py", "code": "..."}
        if "json" in content_type:
            try:
                data = json.loads(body)
                filename = data.get("filename", "")
                code = data.get("code", "")
                bot_name = data.get("bot_name", "")
                submitted_by = data.get("submitted_by", "anonymous")
            except json.JSONDecodeError:
                return self._json_error(400, "Invalid JSON")
        else:
            return self._json_error(400, "Send JSON with {filename, code, bot_name}")

        if not filename or not code:
            return self._json_error(400, "Missing filename or code")

        # ── Validate ──
        errors = validate_bot_code(code, filename)
        if errors:
            return self._json_error(400, "; ".join(errors))

        # ── Sanitize bot_name ──
        if not bot_name:
            bot_name = re.sub(r'[^a-zA-Z0-9_]', '_', filename.replace('.py', ''))
        bot_name = re.sub(r'[^a-zA-Z0-9_]', '_', bot_name).lower()

        # Ensure unique name (don't overwrite built-in bots)
        if bot_name in ("demo_baseline", "demo_smart"):
            bot_name = f"custom_{bot_name}"

        # ── Save file ──
        safe_filename = f"{bot_name}.py"
        filepath = os.path.join(BOTS_DIR, safe_filename)
        with open(filepath, 'w') as f:
            f.write(code)
        print(f"  Saved bot: {filepath}")

        # ── Detect bot class ──
        class_name = detect_bot_class(code)
        if not class_name:
            os.remove(filepath)
            return self._json_error(400,
                "No bot class found. Your file must define a class with "
                "catalog, suppliers, locations, physical_locs, product_supplier "
                "attributes and a decide(self, state) method.")

        # ── Register ──
        module_name = bot_name  # Python module name = filename without .py
        runner.register_bot(bot_name, module_name, class_name)

        # ── Persist to Mongo so the bot survives redeploys ──
        try:
            mongo_runs.save_bot(
                slug=bot_name,
                name=bot_name.replace("_", " ").title(),
                code=code,
                description=f"User-submitted bot ({class_name})",
                bot_type="user",
                author_user_id=None,  # wire to Clerk user id once auth ships
            )
        except Exception as e:
            print(f"  WARN: failed to save bot to Mongo: {e}")

        self._json_ok({
            "bot_name": bot_name,
            "class_name": class_name,
            "filename": safe_filename,
            "message": f"Bot '{bot_name}' registered. You can now run it."
        })


# ═══════════════════════════════════════════════════════════
# Bot code validation
# ═══════════════════════════════════════════════════════════

# Imports that are never acceptable in bot code
BANNED_IMPORTS = {
    "subprocess", "shutil", "ctypes", "socket", "http",
    "urllib", "requests", "flask", "django",
    "pickle", "shelve", "marshal",
    "signal", "multiprocessing",
    "importlib", "builtins", "__builtin__",
}

MAX_BOT_FILE_SIZE = 100_000  # 100 KB


def validate_bot_code(code, filename):
    """Check bot code for basic safety issues. Returns list of error strings."""
    errors = []

    if not filename.endswith('.py'):
        errors.append("File must be a .py Python file")

    if len(code) > MAX_BOT_FILE_SIZE:
        errors.append(f"File too large ({len(code)} bytes, max {MAX_BOT_FILE_SIZE})")

    # Check for banned imports
    for line in code.split('\n'):
        stripped = line.strip()
        if stripped.startswith('#'):
            continue
        for banned in BANNED_IMPORTS:
            if re.search(rf'\bimport\s+{banned}\b', stripped) or \
               re.search(rf'\bfrom\s+{banned}\b', stripped):
                errors.append(f"Forbidden import: '{banned}' is not allowed")

    # Check for dangerous builtins
    dangerous_calls = ['exec(', 'eval(', 'compile(', '__import__(', 'open(']
    for call in dangerous_calls:
        if call in code:
            errors.append(f"Forbidden call: '{call.rstrip('(')}' is not allowed")

    # Must have at least one class definition
    if not re.search(r'class\s+\w+', code):
        errors.append("No class definition found. Bot must define a class.")

    # Must have a decide method
    if not re.search(r'def\s+decide\s*\(', code):
        errors.append("No decide() method found. Bot must have decide(self, state).")

    return errors


def detect_bot_class(code):
    """Find the bot class name in the uploaded code."""
    # Look for a class that has a decide method
    classes = re.findall(r'class\s+(\w+)', code)
    for cls in classes:
        # Check if this class has a decide method (rough heuristic)
        # Look for 'def decide' after 'class ClassName'
        pattern = rf'class\s+{cls}.*?def\s+decide\s*\('
        if re.search(pattern, code, re.DOTALL):
            return cls
    return classes[0] if classes else None


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in a new thread so long-running bot sims don't block."""
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description="ToyLand Simulation Server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5055)))
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), SimHandler)
    print(f"ToyLand Simulation Server running on http://{args.host}:{args.port}")
    print(f"Dashboard will call POST /run or POST /run-bot to trigger simulations.")
    print(f"Available bots: {', '.join(runner.bot_registry.keys())}")
    print(f"Excel files → {os.path.join(PROJECT_DIR, 'sim_excel')}/")
    print(f"Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
