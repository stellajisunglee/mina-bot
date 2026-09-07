"""Weekly metrics report, in three sections:

MINABOT     -- everything scoped to MINA_BOT_CHANNEL_ID only (rows with no
               channel_name -- the only kind of row that predates
               multi-channel logging). Past 7 days vs. a 4-week trailing
               baseline where that comparison makes sense.
SERVER-WIDE -- voice activity across every channel the bot logs, mina-bot
               included as one bucket among others. All-time, not
               window-limited.
FUNNEL      -- kaiwa crew's size and behavior relative to the broader
               onboarded population.

Pure computation, no Discord dependency -- bot.py fetches Discord-side data
(level labels, member counts/ids, MINA_BOT_CHANNEL_ID) and passes it in as
parameters. Channel creation dates are decoded from Discord snowflake IDs
directly (pure math on the numeric ID, no API call), so that doesn't need
to be passed in separately.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from zoneinfo import ZoneInfo

import storage

TIMEZONE = ZoneInfo("America/Los_Angeles")
EVENING_REVEAL_HOUR = 18
DISCORD_EPOCH_MS = 1420070400000  # Discord snowflake epoch: 2015-01-01T00:00:00Z

RETURN_GAP_DAYS = 4
BASELINE_WEEKS = 4
VOICE_RATE_MIN_MEMOS = 5
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


def _fraction(count, total):
    """Bundles a count/total/pct together so raw numbers can travel alongside
    every percentage instead of being lost to rounding."""
    if not total:
        return None
    return {"count": count, "total": total, "pct": round(count / total * 100, 0)}


def _fmt_fraction(frac):
    if frac is None:
        return "n/a"
    return f"{frac['pct']:.0f}% ({frac['count']} of {frac['total']})"


def _snowflake_created_at(snowflake_id):
    ms = (int(snowflake_id) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TIMEZONE)


def _avg_pct(fractions):
    values = [f["pct"] for f in fractions if f is not None]
    return round(mean(values), 0) if values else None


def _fmt(value, suffix=""):
    return f"{value:.0f}{suffix}" if value is not None else "n/a"


# ---------------------------------------------------------------- MINABOT --

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


def _participation_around_switchover(rows, switchover_date, now):
    """One-time before/after split around a fixed date -- not a rolling weekly metric,
    so no 4-week comparison here. Needs >=SWITCHOVER_MIN_DAYS of calendar span on both
    sides or the comparison is misleading (e.g. right after the switchover happened)."""
    if not switchover_date or not rows:
        return None
    cutoff = date.fromisoformat(switchover_date)
    earliest = min(r["_dt"].date() for r in rows)
    if (cutoff - earliest).days < SWITCHOVER_MIN_DAYS or (now.date() - cutoff).days < SWITCHOVER_MIN_DAYS:
        return {"insufficient": True}

    def _rate(subset):
        if not subset:
            return {"active_users": 0, "attempts_per_day": 0.0}
        days = {r["_dt"].date() for r in subset}
        users = {r["user_id"] for r in subset}
        return {"active_users": len(users), "attempts_per_day": round(len(subset) / len(days), 1)}

    before = [r for r in rows if r["_dt"].date() < cutoff]
    after = [r for r in rows if r["_dt"].date() >= cutoff]
    return {"insufficient": False, "before": _rate(before), "after": _rate(after)}


def _level_breakdown(rows):
    """% of this window's distinct active users who had at least one attempt at
    each level, plus how many rows in the window carried no level data at all
    (backfilled history has none) -- so an empty/thin breakdown can be told
    apart from a genuine quiet week."""
    total_users = {r["user_id"] for r in rows}
    no_level_rows = sum(1 for r in rows if not r.get("level"))
    breakdown = {}
    if total_users:
        users_by_level = defaultdict(set)
        for r in rows:
            if r.get("level"):
                users_by_level[r["level"]].add(r["user_id"])
        breakdown = {
            level: _fraction(len(users), len(total_users))
            for level, users in sorted(users_by_level.items(), key=lambda kv: -len(kv[1]))
        }
    return breakdown, no_level_rows, len(rows)


def _voice_pct(rows):
    voice = sum(1 for r in rows if r["is_voice"])
    return _fraction(voice, len(rows))


def _voice_ratio_distribution(rows):
    """Per-user lifetime (within these rows) voice ratio, bucketed."""
    by_user = defaultdict(list)
    for r in rows:
        by_user[r["user_id"]].append(r["is_voice"])
    buckets = {"0% (text only)": 0, "1-49%": 0, "50-99%": 0, "100% (voice only)": 0}
    for uid, flags in by_user.items():
        ratio = sum(flags) / len(flags)
        if ratio == 0:
            buckets["0% (text only)"] += 1
        elif ratio < 0.5:
            buckets["1-49%"] += 1
        elif ratio < 1:
            buckets["50-99%"] += 1
        else:
            buckets["100% (voice only)"] += 1
    return buckets


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
    stopped_at_one = sum(1 for v in max_exchange.values() if v == 1)
    return _fraction(stopped_at_one, len(max_exchange))


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
    return _fraction(revised, sessions_with_attempt)


def _before_reveal_pct(rows):
    before = sum(1 for r in rows if r["_dt"].hour < EVENING_REVEAL_HOUR)
    return _fraction(before, len(rows))


# ------------------------------------------------------------ SERVER-WIDE --

def _voice_by_channel(all_rows, now, mina_bot_channel_id=None):
    """All-time per-channel voice breakdown, mina-bot included as one channel
    among others. The per-month rate is suppressed under VOICE_RATE_MIN_MEMOS
    -- with only a couple of memos, the rate is mostly noise -- and measured
    from the channel's creation date (decoded from its snowflake ID) rather
    than first-to-last message, since a channel that's been quiet for most
    of its life shouldn't look as active as its messages' own span would
    suggest."""
    voice_rows = [r for r in all_rows if r["is_voice"]]
    by_channel = defaultdict(list)
    for r in voice_rows:
        by_channel[r.get("channel_name") or "mina-bot"].append(r)

    channels = {}
    for channel, rows in by_channel.items():
        senders = Counter(r["user_id"] for r in rows)
        top_user, top_count = senders.most_common(1)[0]
        total = len(rows)

        channel_id = next((r.get("channel_id") for r in rows if r.get("channel_id")), None)
        if channel_id is None and channel == "mina-bot":
            channel_id = mina_bot_channel_id

        per_month = None
        if total >= VOICE_RATE_MIN_MEMOS and channel_id:
            created = _snowflake_created_at(channel_id)
            span_days = max((now - created).days, 1)
            per_month = round(total / (span_days / 30.44), 1)

        channels[channel] = {
            "total": total,
            "per_month": per_month,
            "distinct_senders": len(senders),
            "top_share": _fraction(top_count, total),
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


# ----------------------------------------------------------------- FUNNEL --

def _funnel(all_rows, mina_bot_rows, unlocked_member_count, kaiwa_crew_member_ids):
    """kaiwa crew's size and behavior relative to the broader onboarded
    population. 'Ever-attempted' is scoped to #minabot specifically (the
    feature this funnel is about); 'zero-voice' uses voice anywhere in the
    server, as the broadest read on whether someone has tried voice at all."""
    if kaiwa_crew_member_ids is None:
        return None

    kaiwa_crew_count = len(kaiwa_crew_member_ids)
    crew_share = _fraction(kaiwa_crew_count, unlocked_member_count) if unlocked_member_count else None

    attempters = {r["user_id"] for r in mina_bot_rows}
    ever_attempted = _fraction(len(kaiwa_crew_member_ids & attempters), kaiwa_crew_count)

    voice_users = {r["user_id"] for r in all_rows if r["is_voice"]}
    zero_voice = _fraction(len(kaiwa_crew_member_ids - voice_users), kaiwa_crew_count)

    return {
        "crew_share_of_any_role": crew_share,
        "ever_attempted_of_crew": ever_attempted,
        "zero_voice_of_crew": zero_voice,
    }


def build_report(level_labels=None, switchover_date=None, unlocked_member_count=None,
                  mina_bot_channel_id=None, kaiwa_crew_member_ids=None):
    level_labels = level_labels or {}
    metrics = _parse_rows(storage.read_all_metrics(include_test=False))
    mina_bot_rows = [r for r in metrics if not r.get("channel_name")]

    now = datetime.now(TIMEZONE)
    current_start, current_end = now - timedelta(days=7), now
    baseline_windows = _week_windows_before(current_start, BASELINE_WEEKS)

    mb_current = _in_window(mina_bot_rows, current_start, current_end)

    engagement_now = _engagement_stats(mb_current)
    engagement_baseline = _avg_engagement(mina_bot_rows, baseline_windows)

    returning = _returning_users(mina_bot_rows, current_start, current_end)

    first_seen = _first_seen_by_user(mina_bot_rows)
    first_timers_now = _count_first_timers(first_seen, current_start, current_end)
    first_timers_baseline = round(
        mean(_count_first_timers(first_seen, s, e) for s, e in baseline_windows), 1
    ) if baseline_windows else 0.0

    level_now, no_level_now, level_rows_now = _level_breakdown(mb_current)
    attempts_per_drop_now = _attempts_per_drop_by_level(mb_current)
    switchover = _participation_around_switchover(mina_bot_rows, switchover_date, now)

    voice_now = _voice_pct(mb_current)
    voice_baseline = _avg_pct(_voice_pct(_in_window(mina_bot_rows, s, e)) for s, e in baseline_windows)
    voice_buckets = _voice_ratio_distribution(mina_bot_rows)

    exchange_now = _exchange_distribution(mb_current)
    exchange_one_stop_now = _exchange_one_stop_rate(mb_current)
    exchange_one_stop_baseline = _avg_pct(
        _exchange_one_stop_rate(_in_window(mina_bot_rows, s, e)) for s, e in baseline_windows
    )

    revise_now = _attempt_then_revise_rate(mb_current)
    revise_baseline = _avg_pct(
        _attempt_then_revise_rate(_in_window(mina_bot_rows, s, e)) for s, e in baseline_windows
    )

    before_reveal_now = _before_reveal_pct(mb_current)
    before_reveal_baseline = _avg_pct(
        _before_reveal_pct(_in_window(mina_bot_rows, s, e)) for s, e in baseline_windows
    )

    voice_by_channel = _voice_by_channel(metrics, now, mina_bot_channel_id=mina_bot_channel_id)
    single_channel_senders, multi_channel_senders = _sender_channel_spread(voice_by_channel)

    funnel = _funnel(metrics, mina_bot_rows, unlocked_member_count, kaiwa_crew_member_ids)

    lines = [
        f"# 📊 Weekly Report — {current_start.date()} to {current_end.date()}",
        "",
        "## MINABOT (scoped to #minabot)",
        f"- Active users (of this week's #minabot rows): {engagement_now['active_users']} (4wk avg: {engagement_baseline['active_users']})",
        f"- Total attempts: {engagement_now['total_attempts']} (4wk avg: {engagement_baseline['total_attempts']})",
        f"- Active days/user: {engagement_now['avg_active_days_per_user']} (4wk avg: {engagement_baseline['avg_active_days_per_user']})",
        f"- Attempts per active day: {engagement_now['attempts_per_active_day']} (4wk avg: {engagement_baseline['attempts_per_active_day']})",
        f"- Returning after {RETURN_GAP_DAYS}+ day gap (of this week's active users): {len(returning)}",
        f"- First-time attempters (of this week's active users): {first_timers_now} (4wk avg: {first_timers_baseline})",
    ]

    if attempts_per_drop_now:
        per_level = ", ".join(
            f"{level_labels.get(level, level)}: {val} (4wk avg: {_fmt(_avg_attempts_per_drop_for_level(mina_bot_rows, baseline_windows, level))})"
            for level, val in attempts_per_drop_now.items()
        )
        lines.append(f"- Attempts per drop by level: {per_level}")

    if level_now:
        breakdown = ", ".join(
            f"{level_labels.get(level, level)}: {_fmt_fraction(frac)}" for level, frac in level_now.items()
        )
        lines.append(f"- Participation by level (of this week's active users): {breakdown}")
    else:
        lines.append("- Participation by level: no activity this week")
    lines.append(f"- Rows with no level data (of this week's #minabot rows): {no_level_now} of {level_rows_now}")

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
        f"- Voice ratio, this week (of this week's #minabot attempts): {_fmt_fraction(voice_now)} (4wk avg: {_fmt(voice_baseline, '%')})",
        "- Voice ratio distribution, all-time (per #minabot attempter): "
        + ", ".join(f"{label}: {count}" for label, count in voice_buckets.items()),
        "- Checkme exchange distribution: "
        + (", ".join(f"{n}: {c}" for n, c in sorted(exchange_now.items())) if exchange_now else "no checkme activity"),
        f"- Exchange-1 stop rate (of this week's checkme sessions): {_fmt_fraction(exchange_one_stop_now)} (4wk avg: {_fmt(exchange_one_stop_baseline, '%')})",
        f"- Attempt-then-revise rate (of this week's sessions with an attempt): {_fmt_fraction(revise_now)} (4wk avg: {_fmt(revise_baseline, '%')})",
        f"- Attempts before evening reveal (of this week's #minabot attempts): {_fmt_fraction(before_reveal_now)} (4wk avg: {_fmt(before_reveal_baseline, '%')})",
    ]

    lines += ["", "## SERVER-WIDE (voice, all channels, all-time)"]
    if voice_by_channel:
        for channel, data in sorted(voice_by_channel.items(), key=lambda kv: -kv[1]["total"]):
            rate = f"{data['per_month']}/mo" if data["per_month"] is not None else f"rate n/a (<{VOICE_RATE_MIN_MEMOS} memos)"
            lines.append(
                f"- {channel}: {data['total']} voice memos total, {rate}, "
                f"{data['distinct_senders']} distinct senders, "
                f"top sender {_fmt_fraction(data['top_share'])} of {channel}'s voice total"
            )
        lines.append(
            f"- Voice senders active in one channel only (of all server-wide voice senders): {single_channel_senders}; "
            f"in multiple channels: {multi_channel_senders}"
        )
    else:
        lines.append("- No voice activity recorded in any channel")

    lines += ["", "## FUNNEL (kaiwa crew vs. broader membership)"]
    if funnel:
        lines.append(f"- Kaiwa crew (of members with any role): {_fmt_fraction(funnel['crew_share_of_any_role'])}")
        lines.append(f"- Ever attempted in #minabot (of kaiwa crew): {_fmt_fraction(funnel['ever_attempted_of_crew'])}")
        lines.append(f"- Zero voice anywhere in the server (of kaiwa crew): {_fmt_fraction(funnel['zero_voice_of_crew'])}")
    else:
        lines.append("- n/a (kaiwa crew membership not provided)")

    return "\n".join(lines)
