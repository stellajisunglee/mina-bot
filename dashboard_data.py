"""Pure computation for the admin dashboard: buckets metrics.jsonl into
daily/weekly/monthly time series, plus the self-declared-level snapshot
breakdown. No FastAPI/Jinja dependency -- testable standalone, read-only.

Deliberately duplicates a few small primitives that also live in
metrics_report.py (TIMEZONE, _fraction-style helpers, mina-bot row scoping)
rather than importing from it or extracting a shared module.
metrics_report.py is deployed and working; this dashboard's shape hasn't
settled yet, so duplication is the safer default for now.

Does not import bot.py: that would construct live Anthropic/Discord client
objects for no reason. The handful of constants shared with bot.py are
duplicated below with a note to keep them in sync.
"""
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import storage

load_dotenv()

# Kept in sync with bot.py by hand -- see that file for the source of truth.
TIMEZONE = ZoneInfo("America/Los_Angeles")
EVENING_REVEAL_HOUR = 18
LEVEL_LABEL_MOVED_TO_EVENING = "2026-09-06"
MINA_BOT_CHANNEL_ID = int(os.getenv("MINA_BOT_CHANNEL_ID"))

SELF_DECLARED_LEVELS_FILE = "self_declared_levels.json"
SELF_DECLARED_LEVEL_ORDER = ["N5", "N4", "N3", "N2", "N1", "日本人", "unknown"]
SELF_LEVEL_MIN_USERS = 3

GRANULARITIES = ("daily", "weekly", "monthly")


def _parse_rows(rows):
    for row in rows:
        row["_dt"] = datetime.fromisoformat(row["timestamp"]).astimezone(TIMEZONE)
    return rows


def _bucket_key(dt, granularity):
    if granularity == "daily":
        return dt.date().isoformat()
    if granularity == "weekly":
        monday = dt.date() - timedelta(days=dt.date().weekday())
        return monday.isoformat()
    if granularity == "monthly":
        return dt.date().replace(day=1).isoformat()
    raise ValueError(f"unknown granularity: {granularity}")


def _bucket_range(start_date, end_date, granularity):
    """Every bucket key from start_date to end_date inclusive, in order --
    including empty ones, so a quiet stretch doesn't get skipped over and
    misread as adjacent to whatever comes next."""
    keys = []
    if granularity == "daily":
        cursor = start_date
        while cursor <= end_date:
            keys.append(cursor.isoformat())
            cursor += timedelta(days=1)
    elif granularity == "weekly":
        cursor = start_date - timedelta(days=start_date.weekday())
        end_monday = end_date - timedelta(days=end_date.weekday())
        while cursor <= end_monday:
            keys.append(cursor.isoformat())
            cursor += timedelta(days=7)
    elif granularity == "monthly":
        cursor = start_date.replace(day=1)
        while cursor <= end_date:
            keys.append(cursor.isoformat())
            cursor = (cursor.replace(year=cursor.year + 1, month=1) if cursor.month == 12
                      else cursor.replace(month=cursor.month + 1))
    else:
        raise ValueError(f"unknown granularity: {granularity}")
    return keys


def _group_by_bucket(rows, granularity):
    buckets = defaultdict(list)
    for r in rows:
        buckets[_bucket_key(r["_dt"], granularity)].append(r)
    return buckets


def _coverage(rows):
    if not rows:
        return {"earliest": None, "latest": None, "days": 0}
    earliest = min(r["_dt"] for r in rows).date()
    latest = max(r["_dt"] for r in rows).date()
    return {"earliest": earliest.isoformat(), "latest": latest.isoformat(), "days": (latest - earliest).days + 1}


def _no_level_coverage(rows):
    total = len(rows)
    no_level = sum(1 for r in rows if not r.get("level"))
    return {"no_level": no_level, "total": total}


# ---- per-chart series, each keyed to a shared bucket_keys list -----------

def _attempts_and_active_users(rows, granularity, bucket_keys):
    grouped = _group_by_bucket(rows, granularity)
    return {
        "labels": bucket_keys,
        "attempts": [len(grouped.get(k, [])) for k in bucket_keys],
        "active_users": [len({r["user_id"] for r in grouped.get(k, [])}) for k in bucket_keys],
    }


def _voice_ratio_series(rows, granularity, bucket_keys):
    grouped = _group_by_bucket(rows, granularity)
    pct, voice_counts, totals = [], [], []
    for k in bucket_keys:
        bucket_rows = grouped.get(k, [])
        voice = sum(1 for r in bucket_rows if r["is_voice"])
        total = len(bucket_rows)
        pct.append(round(voice / total * 100, 1) if total else None)
        voice_counts.append(voice)
        totals.append(total)
    return {"labels": bucket_keys, "pct": pct, "voice_count": voice_counts, "total_count": totals}


def _voice_per_channel_series(all_rows, granularity, bucket_keys):
    voice_rows = [r for r in all_rows if r["is_voice"]]
    channels = sorted({r.get("channel_name") or "mina-bot" for r in voice_rows})
    grouped = _group_by_bucket(voice_rows, granularity)
    series = {
        channel: [
            sum(1 for r in grouped.get(k, []) if (r.get("channel_name") or "mina-bot") == channel)
            for k in bucket_keys
        ]
        for channel in channels
    }
    return {"labels": bucket_keys, "channels": series}


def _participation_by_level_series(rows, granularity, bucket_keys):
    """Raw user counts per level per bucket, not percentages -- the active-user
    denominator shifts every bucket, which would make bucket-to-bucket
    percentage comparisons misleading."""
    grouped = _group_by_bucket(rows, granularity)
    levels = sorted({r["level"] for r in rows if r.get("level")})
    series = {
        level: [
            len({r["user_id"] for r in grouped.get(k, []) if r.get("level") == level})
            for k in bucket_keys
        ]
        for level in levels
    }
    return {"labels": bucket_keys, "levels": series}


def _exchange_depth_series(rows, granularity, bucket_keys):
    """Checkme sessions (max exchange_n per user-day, same definition used in
    metrics_report.py) bucketed by the session's own date."""
    sessions = {}
    for r in rows:
        if r.get("type") == "checkme" and r.get("exchange_n") is not None:
            key = (r["user_id"], r["_dt"].date())
            sessions[key] = max(sessions.get(key, 0), r["exchange_n"])

    def session_bucket(d):
        dt = datetime.combine(d, datetime.min.time(), tzinfo=TIMEZONE)
        return _bucket_key(dt, granularity)

    buckets = defaultdict(Counter)
    for (uid, d), max_n in sessions.items():
        depth = "3+" if max_n >= 3 else str(max_n)
        buckets[session_bucket(d)][depth] += 1

    depths = ["1", "2", "3+"]
    series = {depth: [buckets.get(k, Counter()).get(depth, 0) for k in bucket_keys] for depth in depths}
    return {"labels": bucket_keys, "depths": series}


def _before_reveal_series(rows, granularity, bucket_keys):
    grouped = _group_by_bucket(rows, granularity)
    pct, before_counts, totals = [], [], []
    for k in bucket_keys:
        bucket_rows = grouped.get(k, [])
        before = sum(1 for r in bucket_rows if r["_dt"].hour < EVENING_REVEAL_HOUR)
        total = len(bucket_rows)
        pct.append(round(before / total * 100, 1) if total else None)
        before_counts.append(before)
        totals.append(total)
    return {"labels": bucket_keys, "pct": pct, "before_count": before_counts, "total_count": totals}


# ---- self-declared level (snapshot, not time-bucketed) -------------------

def _load_self_declared_levels():
    if not os.path.exists(SELF_DECLARED_LEVELS_FILE):
        return None
    with open(SELF_DECLARED_LEVELS_FILE) as f:
        return json.load(f)


def _self_declared_level_breakdown(self_declared_by_user, mina_bot_rows, all_rows):
    """Denominator per level = members holding that role. Multi-role members
    count in each of their levels (and are called out separately). Members
    with none of the roles bucket as 'unknown'. Levels under
    SELF_LEVEL_MIN_USERS get their rate suppressed -- raw counts still show."""
    attempters = {r["user_id"] for r in mina_bot_rows}
    voice_senders = {r["user_id"] for r in all_rows if r["is_voice"]}

    members_by_level = defaultdict(set)
    multi_level_users = 0
    for uid, levels in self_declared_by_user.items():
        if not levels:
            members_by_level["unknown"].add(uid)
            continue
        if len(levels) > 1:
            multi_level_users += 1
        for lvl in levels:
            members_by_level[lvl].add(uid)

    breakdown = {}
    for level in SELF_DECLARED_LEVEL_ORDER:
        members = members_by_level.get(level, set())
        denom = len(members)
        attempted = len(members & attempters)
        voiced = len(members & voice_senders)
        suppressed = denom < SELF_LEVEL_MIN_USERS
        breakdown[level] = {
            "denominator": denom,
            "attempted_count": attempted,
            "voiced_count": voiced,
            "attempted_pct": None if suppressed or not denom else round(attempted / denom * 100, 1),
            "voiced_pct": None if suppressed or not denom else round(voiced / denom * 100, 1),
            "suppressed": suppressed,
        }
    return breakdown, multi_level_users


def build_dashboard_data():
    metrics = _parse_rows(storage.read_all_metrics(include_test=False))
    mina_bot_rows = [r for r in metrics if not r.get("channel_name")]

    now = datetime.now(TIMEZONE)
    today = now.date()
    # Two different date ranges: mina-bot-scoped charts start when #minabot's
    # own history starts, but voice_per_channel is server-wide and must not be
    # clipped to that -- some channels (e.g. daily-journal) go back much further.
    mina_bot_earliest = min((r["_dt"] for r in mina_bot_rows), default=now).date()
    server_wide_earliest = min((r["_dt"] for r in metrics), default=now).date()

    granularities = {}
    for granularity in GRANULARITIES:
        mb_bucket_keys = _bucket_range(mina_bot_earliest, today, granularity)
        sw_bucket_keys = _bucket_range(server_wide_earliest, today, granularity)
        granularities[granularity] = {
            "attempts_and_active_users": _attempts_and_active_users(mina_bot_rows, granularity, mb_bucket_keys),
            "voice_ratio": _voice_ratio_series(mina_bot_rows, granularity, mb_bucket_keys),
            "voice_per_channel": _voice_per_channel_series(metrics, granularity, sw_bucket_keys),
            "participation_by_level": _participation_by_level_series(mina_bot_rows, granularity, mb_bucket_keys),
            "exchange_depth": _exchange_depth_series(mina_bot_rows, granularity, mb_bucket_keys),
            "before_reveal": _before_reveal_series(mina_bot_rows, granularity, mb_bucket_keys),
        }

    self_declared_by_user = _load_self_declared_levels()
    self_declared_level = None
    if self_declared_by_user is not None:
        breakdown, multi_level_users = _self_declared_level_breakdown(self_declared_by_user, mina_bot_rows, metrics)
        self_declared_level = {
            "breakdown": breakdown,
            "multi_level_users": multi_level_users,
            "total_members_scanned": len(self_declared_by_user),
        }

    # Chart.js's annotation plugin needs an exact label match on a category
    # axis; the raw switchover date only ever lands exactly on a daily bucket,
    # so precompute which weekly/monthly bucket it falls into too.
    switchover_dt = datetime.fromisoformat(LEVEL_LABEL_MOVED_TO_EVENING).replace(tzinfo=TIMEZONE)
    switchover_buckets = {g: _bucket_key(switchover_dt, g) for g in GRANULARITIES}

    return {
        "generated_at": now.isoformat(),
        "switchover_date": LEVEL_LABEL_MOVED_TO_EVENING,
        "switchover_buckets": switchover_buckets,
        "granularities": granularities,
        "coverage": {
            "mina_bot": _coverage(mina_bot_rows),
            "server_wide": _coverage(metrics),
            "no_level": _no_level_coverage(mina_bot_rows),
        },
        "self_declared_level": self_declared_level,
    }
