"""One-off backfill: reconstruct submissions.jsonl/metrics.jsonl from Discord
channel history. MINA_BOT_CHANNEL_ID gets full treatment (content, voice
download, submissions row). Every other channel the bot can access, except
BOT_DEV_CHANNEL_ID, gets voice-only presence logging to metrics.jsonl --
no content, no submissions row, no voice download. Writes is_test=False
rows only. Not a bot -- connects, reads history, writes, disconnects, exits.

Idempotent: every row written carries the source message_id, and re-running
skips any message_id already present in metrics.jsonl rather than
duplicating it.

Usage:
    venv/bin/python3 backfill.py            # dry run (default): report only
    venv/bin/python3 backfill.py --execute  # actually write + download voice
"""
import argparse
import asyncio

import discord

import storage
from bot import (
    DISCORD_TOKEN,
    MINA_BOT_CHANNEL_ID,
    BOT_DEV_CHANNEL_ID,
    TIMEZONE,
    JAPANESE_PATTERN,
    contains_japanese,
    is_voice_message,
    save_voice_attachment,
)


def _existing_identifiers():
    """Message ids for rows that have one; (user_id, timestamp, channel_id) composite
    keys as a fallback for rows written before message_id existed. Matching gets
    strictly by-id automatically as more rows accumulate a message_id going forward."""
    message_ids = set()
    composite_keys = set()
    for r in storage.read_all_metrics(include_test=True):
        if r.get("message_id") is not None:
            message_ids.add(r["message_id"])
        else:
            composite_keys.add((r.get("user_id"), r.get("timestamp"), r.get("channel_id")))
    return message_ids, composite_keys


def _already_backfilled(message_ids, composite_keys, message_id, user_id, timestamp, channel_id):
    if message_id in message_ids:
        return True
    return (str(user_id), timestamp, channel_id) in composite_keys


def _new_stats():
    return {"scanned": 0, "matched": 0, "voice": 0, "already_backfilled": 0, "earliest": None, "latest": None}


async def _scan_mina_bot_channel(channel, dry_run, stats, message_ids, composite_keys):
    async for message in channel.history(limit=None, oldest_first=True):
        stats["scanned"] += 1
        if message.author.bot:
            continue

        voice = is_voice_message(message)
        if not (voice or contains_japanese(message.content)):
            continue

        stats["matched"] += 1
        stats["voice"] += voice
        created = message.created_at.astimezone(TIMEZONE)
        stats["earliest"] = stats["earliest"] or created
        stats["latest"] = created
        timestamp = created.isoformat()

        if _already_backfilled(message_ids, composite_keys, message.id, message.author.id, timestamp, None):
            stats["already_backfilled"] += 1
            continue

        if dry_run:
            continue

        attachment = message.attachments[0] if voice and message.attachments else None
        content = attachment.url if attachment else message.content
        voice_path = await save_voice_attachment(attachment, message.author.id) if attachment else None

        storage.append_submission(
            message.author.id, "public_attempt", False,
            content=content, is_voice=voice, voice_path=voice_path, focus={},
            timestamp=timestamp, message_id=message.id,
        )
        storage.append_metrics(
            message.author.id, False,
            type="public_attempt",
            level=None, grammar=None, theme=None,
            char_count=None if voice else len(message.content),
            japanese_char_count=None if voice else len(JAPANESE_PATTERN.findall(message.content)),
            is_voice=voice,
            duration_secs=getattr(attachment, "duration_secs", None) if attachment else None,
            file_size_bytes=attachment.size if attachment else None,
            timestamp=timestamp, message_id=message.id,
        )


async def _scan_other_channel(channel, dry_run, stats, message_ids, composite_keys):
    """Voice-only, metrics-only, no content -- presence logging for channels
    people didn't join to have their messages measured in."""
    async for message in channel.history(limit=None, oldest_first=True):
        stats["scanned"] += 1
        if message.author.bot:
            continue
        if not is_voice_message(message):
            continue

        stats["matched"] += 1
        stats["voice"] += 1
        created = message.created_at.astimezone(TIMEZONE)
        stats["earliest"] = stats["earliest"] or created
        stats["latest"] = created
        timestamp = created.isoformat()

        if _already_backfilled(message_ids, composite_keys, message.id, message.author.id, timestamp, channel.id):
            stats["already_backfilled"] += 1
            continue

        if dry_run:
            continue

        storage.append_metrics(
            message.author.id, False,
            channel_id=channel.id,
            channel_name=channel.name,
            is_voice=True,
            timestamp=timestamp, message_id=message.id,
        )


async def backfill(dry_run):
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    per_channel = {}
    message_ids, composite_keys = _existing_identifiers()

    @client.event
    async def on_ready():
        try:
            anchor = client.get_channel(MINA_BOT_CHANNEL_ID) or await client.fetch_channel(MINA_BOT_CHANNEL_ID)
            guild = anchor.guild

            for channel in guild.text_channels:
                if channel.id == BOT_DEV_CHANNEL_ID:
                    per_channel[channel.name] = {"skipped": "BOT_DEV_CHANNEL_ID"}
                    continue

                stats = _new_stats()
                try:
                    if channel.id == MINA_BOT_CHANNEL_ID:
                        await _scan_mina_bot_channel(channel, dry_run, stats, message_ids, composite_keys)
                    else:
                        await _scan_other_channel(channel, dry_run, stats, message_ids, composite_keys)
                    stats["is_mina_bot"] = channel.id == MINA_BOT_CHANNEL_ID
                    per_channel[channel.name] = stats
                except discord.Forbidden:
                    per_channel[channel.name] = {"skipped": "no access"}
        finally:
            await client.close()

    await client.start(DISCORD_TOKEN)
    return per_channel


def main():
    parser = argparse.ArgumentParser(description="Backfill submissions.jsonl/metrics.jsonl from channel history.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                         help="Report what would be written without writing anything (default).")
    parser.add_argument("--execute", action="store_true",
                         help="Actually write rows and download voice attachments.")
    args = parser.parse_args()
    dry_run = not args.execute

    per_channel = asyncio.run(backfill(dry_run))

    print()
    print("=== Dry run: nothing written ===" if dry_run else "=== Backfill complete ===")

    mina_bot = next((s for s in per_channel.values() if s.get("is_mina_bot")), None)
    if mina_bot:
        print()
        print("MINA_BOT_CHANNEL_ID (full treatment):")
        print(f"  Messages scanned: {mina_bot['scanned']}")
        print(f"  Matching (voice or Japanese): {mina_bot['matched']} "
              f"({mina_bot['voice']} voice, {mina_bot['matched'] - mina_bot['voice']} text)")
        print(f"  Already backfilled (skipped): {mina_bot['already_backfilled']}")
        print(f"  New: {mina_bot['matched'] - mina_bot['already_backfilled']}")
        if mina_bot["earliest"]:
            print(f"  Date range: {mina_bot['earliest'].isoformat()} to {mina_bot['latest'].isoformat()}")

    print()
    print("Other channels (voice-only, metrics.jsonl only):")
    other_total = 0
    other_new = 0
    other_already = 0
    for name, stats in sorted(per_channel.items(), key=lambda kv: -(kv[1].get("voice") or 0)):
        if stats.get("is_mina_bot") or "skipped" in stats:
            continue
        if stats["voice"] == 0:
            continue
        other_total += stats["voice"]
        other_new += stats["voice"] - stats["already_backfilled"]
        other_already += stats["already_backfilled"]
        date_range = f"{stats['earliest'].date()} to {stats['latest'].date()}" if stats["earliest"] else "n/a"
        print(f"  {name}: {stats['voice']} voice, {stats['already_backfilled']} already backfilled ({date_range})")

    skipped_dev = [name for name, s in per_channel.items() if s.get("skipped") == "BOT_DEV_CHANNEL_ID"]
    skipped_forbidden = [name for name, s in per_channel.items() if s.get("skipped") == "no access"]
    print()
    print(f"Skipped (BOT_DEV_CHANNEL_ID): {', '.join(skipped_dev) or 'none'}")
    print(f"Skipped (no access): {len(skipped_forbidden)} channels")

    print()
    print(f"TOTAL other-channel voice rows: {other_total} ({other_new} new, {other_already} already backfilled)")
    if dry_run:
        print("\nRe-run with --execute to write these rows and download voice attachments.")


if __name__ == "__main__":
    main()
