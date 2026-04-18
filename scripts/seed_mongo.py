"""CLI wrapper for engine.seed.seed_all.

Run:
    # Locally (with MONGODB_USER / MONGODB_PWD in your shell or .env)
    python scripts/seed_mongo.py

    # Against Railway-hosted cluster
    MONGODB_USER=... MONGODB_PWD=... python scripts/seed_mongo.py

Also available as POST /admin/seed from the running server.
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJECT_DIR, "engine"))

from db import get_db       # noqa: E402
from seed import seed_all   # noqa: E402


def main():
    db = get_db()
    if db is None:
        print("ERROR: MONGODB_USER / MONGODB_PWD not set.",
              file=sys.stderr)
        sys.exit(1)

    print(f"Seeding {db.name} ...\n")
    result = seed_all(db)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
