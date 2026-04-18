"""MongoDB connection helpers.

Reads MONGODB_USER and MONGODB_PWD from the environment (set in
Railway Variables for prod, or a local .env file for dev). The
cluster host, database name, and app name are constants below —
update here if you move to a different cluster.

All callers should use get_db() to get the active Database instance.
Returns None if the env vars aren't set, so routes can degrade
gracefully instead of 500'ing the whole server.
"""
import os
import urllib.parse
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ConfigurationError

MONGO_HOST = "studysabaiapp.fiqyj.mongodb.net"
MONGO_DB_NAME = "BizBotBash"
MONGO_APP_NAME = "studysabaiapp"

_client = None  # module-level singleton


def _build_uri():
    user = os.environ.get("MONGODB_USER")
    pwd = os.environ.get("MONGODB_PWD")
    if not user or not pwd:
        return None
    return (
        f"mongodb+srv://{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(pwd)}"
        f"@{MONGO_HOST}/{MONGO_DB_NAME}"
        f"?retryWrites=true&w=majority&appName={MONGO_APP_NAME}"
    )


def get_client():
    """Return a cached MongoClient. None if env vars aren't set."""
    global _client
    if _client is not None:
        return _client
    uri = _build_uri()
    if not uri:
        return None
    _client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    return _client


def get_db():
    """Return the bizbotbash Database, or None if env vars missing."""
    client = get_client()
    if client is None:
        return None
    return client[MONGO_DB_NAME]


def ping():
    """Check the connection. Returns (ok: bool, message: str)."""
    user = os.environ.get("MONGODB_USER")
    pwd = os.environ.get("MONGODB_PWD")
    if not user or not pwd:
        return False, "MONGODB_USER / MONGODB_PWD not set"
    try:
        client = get_client()
        client.admin.command("ping")
        db = client[MONGO_DB_NAME]
        collections = db.list_collection_names()
        return True, {
            "host": MONGO_HOST,
            "database": MONGO_DB_NAME,
            "user": user,
            "collections": sorted(collections),
        }
    except ConnectionFailure as e:
        return False, f"connection failed: {e}"
    except ConfigurationError as e:
        return False, f"configuration error: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
