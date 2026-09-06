"""Weekly metrics report: engagement and motivation trends over the past 7 days,
compared against a 4-week trailing baseline. Pure computation -- bot.py owns
scheduling and posting the returned string to Discord.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

import storage

# Mirrors bot.py's TIMEZONE and EVENING_TIME. Kept independent (not imported from
# bot.py) to avoid a circular import -- bot.py imports this module to build the report.
TIMEZONE = ZoneInfo("America/Los_Angeles")
EVENING_REVEAL_HOUR = 18

RETURN_GAP_DAYS = 4
BASELINE_WEEKS = 4
MAX_LISTED_RETURNING_USERS = 10


def _parse_rows(rows):
    for row in rows:
        row["_dt"] = datetime.fromisoformat(row["timestamp"]).astimezone(TIMEZONE)
    return rows


def _in_window(rows, start, end):
    return [r for r in rows if start <= r["_dt"] < end]


def _week_windows_before(end, count):
    """`count` consecutive 7-day windows immediately before `end`, oldest first."""
    windows = []
    cursor = end
    for _ in range(count):
        start = cursor - timedelta(days=7)
        windows.append((start, cursor))
        cursor = start
    windows.reverse()
    return windows


def _active_days_by_user(rows):
    days = defaultdict(set)
    for r in rows:
        days[r["user_id"]].add(r["_dt"].date())
    return days


def _engagement_stats(rows):
    users = {r["user_id"] for r in rows}
    days_by_user = _active_days_by_user(rows)
    total_active_days = sum(len(d) for d in days_by_user.values())
    return {
        "active_users": len(users),
        "total_attempts": len(rows),
        "avg_active_days_per_user": round(total_active_days / len(users), 1) if users else 0.0,
        "attempts_per_active_day": round(len(rows) / total_active_days, 1) if total_active_days else 0.0,
    }


def _avg_engagement(rows, windows):
    per_window = [_engagement_stats(_in_window(rows, start, end)) for start, end in windows]
    if not per_window:
        return _engagement_stats([])
    return {key: round(mean(w[key] for w in per_window), 1) for key in per_window[0]}


def _level_breakdown(rows):
    """% of this window's distinct active users who had at least one attempt at each level."""
    total_users = {r["user_id"] for r in rows}
    if not total_users:
        return {}
    users_by_level = defaultdict(set)
    for r in rows:
        if r.get("level"):
            users_by_level[r["level"]].add(r["user_id"])
    return {
        level: round(len(users) / len(total_users) * 100, 0)
        for level, users in sorted(users_by_level.items(), key=lambda kv: -len(kv[1]))
    }


def _returning_users(all_rows, current_start, current_end):
    """Users active this week whose most recent prior active day (anywhere in
    history) was RETURN_GAP_DAYS+ days before their first active day this week."""
    days_by_user = _active_days_by_user(all_rows)
    returning = []
    for user_id, days in days_by_user.items():
        days_sorted = sorted(days)
        this_week = [d for d in days_sorted if current_start.date() <= d < current_end.date()]
        if not this_week:
            continue
        prior = [d for d in days_sorted if d < this_week[0]]
        if not prior:
            continue
        gap = (this_week[0] - prior[-1]).days
        if gap >= RETURN_GAP_DAYS:
            returning.append((user_id, gap))
    returning.sort(key=lambda item: -item[1])
    return returning


def _voice_pct(rows):
    if not rows:
        return None
    voice = sum(1 for r in rows if r["is_voice"])
    return round(voice / len(rows) * 100, 0)


def _exchange_distribution(rows):
    checkme_rows = [r for r in rows if r["type"] == "checkme" and r.get("exchange_n") is not None]
    return Counter(r["exchange_n"] for r in checkme_rows)


def _before_reveal_pct(rows):
    if not rows:
        return None
    before = sum(1 for r in rows if r["_dt"].hour < EVENING_REVEAL_HOUR)
    return round(before / len(rows) * 100, 0)


def _avg_pct(values):
    present = [v for v in values if v is not None]
    return round(mean(present), 0) if present else None


def _fmt(value, suffix=""):
    return f"{value:.0f}{suffix}" if value is not None else "n/a"


def build_report(level_labels=None):
    """level_labels: optional {internal level name: human-readable label} lookup
    (e.g. bot.py's LEVELS_BY_NAME labels) -- falls back to the raw level name."""
    level_labels = level_labels or {}
    metrics = _parse_rows(storage.read_all_metrics(include_test=False))

    now = datetime.now(TIMEZONE)
    current_start, current_end = now - timedelta(days=7), now
    baseline_windows = _week_windows_before(current_start, BASELINE_WEEKS)

    current = _in_window(metrics, current_start, current_end)

    engagement_now = _engagement_stats(current)
    engagement_baseline = _avg_engagement(metrics, baseline_windows)

    returning = _returning_users(metrics, current_start, current_end)
    level_now = _level_breakdown(current)

    voice_now = _voice_pct(current)
    voice_baseline = _avg_pct(_voice_pct(_in_window(metrics, s, e)) for s, e in baseline_windows)

    exchange_now = _exchange_distribution(current)

    before_reveal_now = _before_reveal_pct(current)
    before_reveal_baseline = _avg_pct(_before_reveal_pct(_in_window(metrics, s, e)) for s, e in baseline_windows)

    lines = [
        f"# 📊 Weekly Report — {current_start.date()} to {current_end.date()}",
        "",
        "**Engagement**",
        f"- Active users: {engagement_now['active_users']} (4wk avg: {engagement_baseline['active_users']})",
        f"- Total attempts: {engagement_now['total_attempts']} (4wk avg: {engagement_baseline['total_attempts']})",
        f"- Active days/user: {engagement_now['avg_active_days_per_user']} (4wk avg: {engagement_baseline['avg_active_days_per_user']})",
        f"- Attempts per active day: {engagement_now['attempts_per_active_day']} (4wk avg: {engagement_baseline['attempts_per_active_day']})",
    ]

    if returning:
        shown = returning[:MAX_LISTED_RETURNING_USERS]
        listed = ", ".join(f"{uid} ({gap}d)" for uid, gap in shown)
        extra = f" (+{len(returning) - len(shown)} more)" if len(returning) > len(shown) else ""
        lines.append(f"- Returning after {RETURN_GAP_DAYS}+ day gap: {listed}{extra}")
    else:
        lines.append(f"- Returning after {RETURN_GAP_DAYS}+ day gap: none")

    if level_now:
        breakdown = ", ".join(
            f"{level_labels.get(level, level)}: {pct:.0f}%" for level, pct in level_now.items()
        )
        lines.append(f"- Participation by level (% of this week's active users): {breakdown}")
    else:
        lines.append("- Participation by level: no activity this week")

    lines += [
        "",
        "**Motivation**",
        f"- Voice attempts: {_fmt(voice_now, '%')} (4wk avg: {_fmt(voice_baseline, '%')})",
        "- Checkme exchange distribution: "
        + (", ".join(f"{n}: {c}" for n, c in sorted(exchange_now.items())) if exchange_now else "no checkme activity"),
        f"- Attempts before evening reveal: {_fmt(before_reveal_now, '%')} (4wk avg: {_fmt(before_reveal_baseline, '%')})",
    ]

    return "\n".join(lines)
