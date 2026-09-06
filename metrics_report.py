"""Weekly metrics report: engagement and motivation trends over the past 7 days,
compared against a 4-week trailing baseline where that comparison makes sense.
Pure computation, no Discord dependency -- bot.py fetches Discord-side data
(level labels, unlocked-member count) and passes it in as parameters.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

import storage

TIMEZONE = ZoneInfo("America/Los_Angeles")
EVENING_REVEAL_HOUR = 18

RETURN_GAP_DAYS = 4
BASELINE_WEEKS = 4
SWITCHOVER_MIN_DAYS = 7


def _parse_rows(rows):
    for row in rows:
        row["_dt"] = datetime.fromisoformat(row["timestamp"]).astimezone(TIMEZONE)
    return rows


def _in_window(rows, start, end):
    return [r for r in rows if start <= r["_dt"] < end]


def _week_windows_before(end, count):
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


def _attempts_per_drop_by_level(rows):
    """Average attempts per day, split by that day's level. A 'drop' = one day's sentence."""
    days_by_level = defaultdict(set)
    attempts_by_level = defaultdict(int)
    for r in rows:
        level = r.get("level")
        if not level:
            continue
        days_by_level[level].add(r["_dt"].date())
        attempts_by_level[level] += 1
    return {
        level: round(attempts_by_level[level] / len(days), 1)
        for level, days in sorted(days_by_level.items(), key=lambda kv: -attempts_by_level[kv[0]])
    }


def _avg_attempts_per_drop_for_level(rows, windows, level):
    values = []
    for start, end in windows:
        window_rows = [r for r in _in_window(rows, start, end) if r.get("level") == level]
        if not window_rows:
            continue
        days = {r["_dt"].date() for r in window_rows}
        values.append(len(window_rows) / len(days))
    return round(mean(values), 1) if values else None


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
    return returning


def _first_seen_by_user(all_rows):
    first_seen = {}
    for r in all_rows:
        uid = r["user_id"]
        if uid not in first_seen or r["_dt"] < first_seen[uid]:
            first_seen[uid] = r["_dt"]
    return first_seen


def _count_first_timers(first_seen, start, end):
    return sum(1 for dt in first_seen.values() if start <= dt < end)


def _participation_around_switchover(all_rows, switchover_date, now):
    """One-time before/after split around a fixed date -- not a rolling weekly metric,
    so no 4-week comparison here. Needs >=SWITCHOVER_MIN_DAYS of calendar span on both
    sides or the comparison is misleading (e.g. right after the switchover happened)."""
    if not switchover_date or not all_rows:
        return None
    cutoff = date.fromisoformat(switchover_date)
    earliest = min(r["_dt"].date() for r in all_rows)
    if (cutoff - earliest).days < SWITCHOVER_MIN_DAYS or (now.date() - cutoff).days < SWITCHOVER_MIN_DAYS:
        return {"insufficient": True}

    def _rate(subset):
        if not subset:
            return {"active_users": 0, "attempts_per_day": 0.0}
        days = {r["_dt"].date() for r in subset}
        users = {r["user_id"] for r in subset}
        return {"active_users": len(users), "attempts_per_day": round(len(subset) / len(days), 1)}

    before = [r for r in all_rows if r["_dt"].date() < cutoff]
    after = [r for r in all_rows if r["_dt"].date() >= cutoff]
    return {"insufficient": False, "before": _rate(before), "after": _rate(after)}


def _voice_pct(rows):
    if not rows:
        return None
    voice = sum(1 for r in rows if r["is_voice"])
    return round(voice / len(rows) * 100, 0)


def _voice_ratio_distribution(all_rows):
    """Per-user lifetime voice ratio, bucketed. All-time, not window-limited."""
    by_user = defaultdict(list)
    for r in all_rows:
        by_user[r["user_id"]].append(r["is_voice"])
    buckets = {"0% (text only)": 0, "1-49%": 0, "50-99%": 0, "100% (voice only)": 0}
    voice_user_ids = set()
    for uid, flags in by_user.items():
        ratio = sum(flags) / len(flags)
        if any(flags):
            voice_user_ids.add(uid)
        if ratio == 0:
            buckets["0% (text only)"] += 1
        elif ratio < 0.5:
            buckets["1-49%"] += 1
        elif ratio < 1:
            buckets["50-99%"] += 1
        else:
            buckets["100% (voice only)"] += 1
    return buckets, voice_user_ids


def _voice_by_channel(all_rows):
    """All-time per-channel voice breakdown. Rows with no channel_name are the
    original MINA_BOT_CHANNEL_ID feature channel (the only kind of row that
    predates multi-channel logging and survives the include_test=False filter
    used to build this report)."""
    voice_rows = [r for r in all_rows if r["is_voice"]]
    by_channel = defaultdict(list)
    for r in voice_rows:
        by_channel[r.get("channel_name") or "mina-bot"].append(r)

    channels = {}
    for channel, rows in by_channel.items():
        senders = Counter(r["user_id"] for r in rows)
        timestamps = [r["_dt"] for r in rows]
        span_days = max((max(timestamps) - min(timestamps)).days, 1)
        top_user, top_count = senders.most_common(1)[0]
        channels[channel] = {
            "total": len(rows),
            "per_month": round(len(rows) / (span_days / 30.44), 1),
            "distinct_senders": len(senders),
            "top_share": round(top_count / len(rows) * 100, 0),
            "sender_ids": set(senders.keys()),
        }
    return channels


def _sender_channel_spread(channels):
    """How many voice senders stick to one channel vs. show up in several."""
    channels_by_user = defaultdict(set)
    for channel, data in channels.items():
        for uid in data["sender_ids"]:
            channels_by_user[uid].add(channel)
    single = sum(1 for chans in channels_by_user.values() if len(chans) == 1)
    multiple = sum(1 for chans in channels_by_user.values() if len(chans) > 1)
    return single, multiple


def _exchange_distribution(rows):
    checkme_rows = [r for r in rows if r.get("type") == "checkme" and r.get("exchange_n") is not None]
    return Counter(r["exchange_n"] for r in checkme_rows)


def _exchange_one_stop_rate(rows):
    """% of (user, day) checkme sessions whose highest exchange_n that day was 1."""
    max_exchange = defaultdict(int)
    for r in rows:
        if r.get("type") == "checkme" and r.get("exchange_n") is not None:
            key = (r["user_id"], r["_dt"].date())
            max_exchange[key] = max(max_exchange[key], r["exchange_n"])
    if not max_exchange:
        return None
    stopped_at_one = sum(1 for v in max_exchange.values() if v == 1)
    return round(stopped_at_one / len(max_exchange) * 100, 0)


def _attempt_then_revise_rate(rows):
    """% of (user, day) sessions with a public_attempt, then a checkme, then another
    public_attempt -- approximating 'one active window' as one calendar day."""
    by_user_day = defaultdict(list)
    for r in rows:
        if r.get("type") in ("public_attempt", "checkme"):
            by_user_day[(r["user_id"], r["_dt"].date())].append(r)

    sessions_with_attempt = 0
    revised = 0
    for entries in by_user_day.values():
        entries.sort(key=lambda r: r["_dt"])
        if not any(r.get("type") == "public_attempt" for r in entries):
            continue
        sessions_with_attempt += 1
        seen_attempt = False
        seen_checkme_after = False
        for r in entries:
            if r.get("type") == "public_attempt":
                if seen_checkme_after:
                    revised += 1
                    break
                seen_attempt = True
            elif r.get("type") == "checkme" and seen_attempt:
                seen_checkme_after = True
    return round(revised / sessions_with_attempt * 100, 0) if sessions_with_attempt else None


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


def build_report(level_labels=None, switchover_date=None, unlocked_member_count=None):
    level_labels = level_labels or {}
    metrics = _parse_rows(storage.read_all_metrics(include_test=False))

    now = datetime.now(TIMEZONE)
    current_start, current_end = now - timedelta(days=7), now
    baseline_windows = _week_windows_before(current_start, BASELINE_WEEKS)

    current = _in_window(metrics, current_start, current_end)

    engagement_now = _engagement_stats(current)
    engagement_baseline = _avg_engagement(metrics, baseline_windows)

    attempts_per_drop_now = _attempts_per_drop_by_level(current)

    returning = _returning_users(metrics, current_start, current_end)

    first_seen = _first_seen_by_user(metrics)
    first_timers_now = _count_first_timers(first_seen, current_start, current_end)
    first_timers_baseline = round(
        mean(_count_first_timers(first_seen, s, e) for s, e in baseline_windows), 1
    ) if baseline_windows else 0.0

    level_now = _level_breakdown(current)
    switchover = _participation_around_switchover(metrics, switchover_date, now)

    voice_now = _voice_pct(current)
    voice_baseline = _avg_pct(_voice_pct(_in_window(metrics, s, e)) for s, e in baseline_windows)
    voice_buckets, voice_user_ids = _voice_ratio_distribution(metrics)
    voice_by_channel = _voice_by_channel(metrics)
    single_channel_senders, multi_channel_senders = _sender_channel_spread(voice_by_channel)

    exchange_now = _exchange_distribution(current)
    exchange_one_stop_now = _exchange_one_stop_rate(current)
    exchange_one_stop_baseline = _avg_pct(
        _exchange_one_stop_rate(_in_window(metrics, s, e)) for s, e in baseline_windows
    )

    revise_now = _attempt_then_revise_rate(current)
    revise_baseline = _avg_pct(
        _attempt_then_revise_rate(_in_window(metrics, s, e)) for s, e in baseline_windows
    )

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

    if attempts_per_drop_now:
        per_level = ", ".join(
            f"{level_labels.get(level, level)}: {val} (4wk avg: {_fmt(_avg_attempts_per_drop_for_level(metrics, baseline_windows, level))})"
            for level, val in attempts_per_drop_now.items()
        )
        lines.append(f"- Attempts per drop by level: {per_level}")

    lines.append(f"- Returning after {RETURN_GAP_DAYS}+ day gap: {len(returning)}")
    lines.append(f"- First-time attempters: {first_timers_now} (4wk avg: {first_timers_baseline})")

    if level_now:
        breakdown = ", ".join(
            f"{level_labels.get(level, level)}: {pct:.0f}%" for level, pct in level_now.items()
        )
        lines.append(f"- Participation by level (% of this week's active users): {breakdown}")
    else:
        lines.append("- Participation by level: no activity this week")

    if switchover and switchover["insufficient"]:
        lines.append(f"- Participation around level-switchover ({switchover_date}): insufficient data")
    elif switchover:
        b, a = switchover["before"], switchover["after"]
        lines.append(
            f"- Participation around level-switchover ({switchover_date}): "
            f"before — {b['active_users']} users, {b['attempts_per_day']}/day; "
            f"after — {a['active_users']} users, {a['attempts_per_day']}/day"
        )

    lines += [
        "",
        "**Motivation**",
        f"- Voice attempts: {_fmt(voice_now, '%')} (4wk avg: {_fmt(voice_baseline, '%')})",
        "- Voice ratio distribution (all-time, per user): "
        + ", ".join(f"{label}: {count}" for label, count in voice_buckets.items()),
    ]

    if unlocked_member_count is not None:
        zero_voice = max(unlocked_member_count - len(voice_user_ids), 0)
        pct = round(zero_voice / unlocked_member_count * 100, 0) if unlocked_member_count else None
        lines.append(
            f"- Never gone voice: {zero_voice} / {unlocked_member_count} unlocked members ({_fmt(pct, '%')})"
        )

    if voice_by_channel:
        lines.append("- Voice by channel (all-time):")
        for channel, data in sorted(voice_by_channel.items(), key=lambda kv: -kv[1]["total"]):
            lines.append(
                f"  - {channel}: {data['total']} total, {data['per_month']}/mo, "
                f"{data['distinct_senders']} senders, top sender {data['top_share']:.0f}% of channel total"
            )
        lines.append(
            f"- Voice senders in one channel only: {single_channel_senders}; "
            f"in multiple channels: {multi_channel_senders}"
        )

    lines += [
        "- Checkme exchange distribution: "
        + (", ".join(f"{n}: {c}" for n, c in sorted(exchange_now.items())) if exchange_now else "no checkme activity"),
        f"- Exchange-1 stop rate: {_fmt(exchange_one_stop_now, '%')} (4wk avg: {_fmt(exchange_one_stop_baseline, '%')})",
        f"- Attempt-then-revise rate: {_fmt(revise_now, '%')} (4wk avg: {_fmt(revise_baseline, '%')})",
        f"- Attempts before evening reveal: {_fmt(before_reveal_now, '%')} (4wk avg: {_fmt(before_reveal_baseline, '%')})",
    ]

    return "\n".join(lines)
