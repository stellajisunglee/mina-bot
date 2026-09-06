"""One-off backfill: reconstruct submissions.jsonl/metrics.jsonl from
MINA_BOT_CHANNEL_ID's message history. Writes is_test=False rows only.
Not a bot -- connects, reads history, writes, disconnects, exits.

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
    TIMEZONE,
    JAPANESE_PATTERN,
    contains_japanese,
    is_voice_message,
    save_voice_attachment,
)


async def backfill(dry_run):
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    stats = {"scanned": 0, "matched": 0, "voice": 0, "earliest": None, "latest": None}

    @client.event
    async def on_ready():
        try:
            channel = client.get_channel(MINA_BOT_CHANNEL_ID) or await client.fetch_channel(MINA_BOT_CHANNEL_ID)

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

                if dry_run:
                    continue

                attachment = message.attachments[0] if voice and message.attachments else None
                content = attachment.url if attachment else message.content
                voice_path = await save_voice_attachment(attachment, message.author.id) if attachment else None
                timestamp = created.isoformat()

                storage.append_submission(
                    message.author.id, "public_attempt", False,
                    content=content, is_voice=voice, voice_path=voice_path, focus={},
                    timestamp=timestamp,
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
                    timestamp=timestamp,
                )
        finally:
            await client.close()

    await client.start(DISCORD_TOKEN)
    return stats


def main():
    parser = argparse.ArgumentParser(description="Backfill submissions.jsonl/metrics.jsonl from channel history.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                         help="Report what would be written without writing anything (default).")
    parser.add_argument("--execute", action="store_true",
                         help="Actually write rows and download voice attachments.")
    args = parser.parse_args()
    dry_run = not args.execute

    stats = asyncio.run(backfill(dry_run))

    print()
    print("=== Dry run: nothing written ===" if dry_run else "=== Backfill complete ===")
    print(f"Messages scanned: {stats['scanned']}")
    print(f"Matching messages (voice or Japanese): {stats['matched']} "
          f"({stats['voice']} voice, {stats['matched'] - stats['voice']} text)")
    if stats["earliest"] and stats["latest"]:
        print(f"Date range: {stats['earliest'].isoformat()} to {stats['latest'].isoformat()}")
    else:
        print("Date range: n/a (no matching messages found)")
    if dry_run:
        print("\nRe-run with --execute to write these rows and download voice attachments.")


if __name__ == "__main__":
    main()
