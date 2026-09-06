import json
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("America/Los_Angeles")

SUBMISSIONS_FILE = "submissions.jsonl"
METRICS_FILE = "metrics.jsonl"
SUBMISSIONS_PER_USER_LIMIT = 7


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_jsonl(path, entries):
    """Write via a temp file + os.replace so a crash mid-write can't truncate the file."""
    directory = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        os.replace(temp_path, path)
    except Exception:
        os.remove(temp_path)
        raise


def _append_jsonl(path, entry):
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def append_submission(user_id, type, is_test, **fields):
    """Append one submission, then trim this user's is_test bucket down to their most
    recent SUBMISSIONS_PER_USER_LIMIT entries. Real and test rows are trimmed separately,
    so dev testing can't evict real submissions; other users are untouched."""
    user_id = str(user_id)
    entry = {
        "timestamp": datetime.now(TIMEZONE).isoformat(),
        "user_id": user_id,
        "type": type,
        "is_test": is_test,
        **fields,
    }

    entries = _read_jsonl(SUBMISSIONS_FILE)
    entries.append(entry)

    kept_for_bucket = 0
    trimmed = []
    for existing in reversed(entries):
        if existing.get("user_id") == user_id and existing.get("is_test") == is_test:
            kept_for_bucket += 1
            if kept_for_bucket > SUBMISSIONS_PER_USER_LIMIT:
                continue
        trimmed.append(existing)
    trimmed.reverse()

    _write_jsonl(SUBMISSIONS_FILE, trimmed)


def append_metrics(user_id, is_test, **fields):
    """Append one metrics line. Never trimmed — caller must only pass
    deterministic fields, no message text."""
    entry = {
        "timestamp": datetime.now(TIMEZONE).isoformat(),
        "user_id": str(user_id),
        "is_test": is_test,
        **fields,
    }
    _append_jsonl(METRICS_FILE, entry)


def read_submissions(user_id, include_test=False):
    user_id = str(user_id)
    return [
        e for e in _read_jsonl(SUBMISSIONS_FILE)
        if e.get("user_id") == user_id and (include_test or not e.get("is_test"))
    ]


def read_metrics(user_id, include_test=False):
    user_id = str(user_id)
    return [
        e for e in _read_jsonl(METRICS_FILE)
        if e.get("user_id") == user_id and (include_test or not e.get("is_test"))
    ]
