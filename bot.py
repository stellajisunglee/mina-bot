import discord
from discord import app_commands
from discord.ext import commands, tasks
from anthropic import Anthropic
from dotenv import load_dotenv
import os
import json
from datetime import time, datetime, date, timedelta
from zoneinfo import ZoneInfo

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
BETA_TESTERS_ROLE_ID = int(os.getenv("BETA_TESTERS_ROLE_ID"))
BOT_DEV_CHANNEL_ID = int(os.getenv("BOT_DEV_CHANNEL_ID"))

TIMEZONE = ZoneInfo("America/Los_Angeles")
MORNING_TIME = time(6, 0, tzinfo=TIMEZONE)
EVENING_TIME = time(18, 0, tzinfo=TIMEZONE)

STATE_FILE = "state.json"
TEST_STATE_FILE = "test_state.json"
ENGAGEMENT_FILE = "engagement.json"
CHECKME_DAILY_LIMIT = 5

client_ai = Anthropic(api_key=ANTHROPIC_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def load_state(state_file=STATE_FILE):
    if os.path.exists(state_file):
        with open(state_file, "r") as f:
            return json.load(f)
    return {}


def save_state(data, state_file=STATE_FILE):
    with open(state_file, "w") as f:
        json.dump(data, f)


def load_engagement():
    if os.path.exists(ENGAGEMENT_FILE):
        with open(ENGAGEMENT_FILE, "r") as f:
            return json.load(f)
    return {"users": {}, "checkme_daily": {}}


def save_engagement(data):
    with open(ENGAGEMENT_FILE, "w") as f:
        json.dump(data, f)


def today_str():
    return datetime.now(TIMEZONE).date().isoformat()


def default_user():
    return {
        "current_streak": 0,
        "longest_streak": 0,
        "last_participated": None,
        "total_days": 0,
        "checkme_uses": 0,
        "checkme_last_date": None,
        "checkme_count_today": 0,
    }


def record_participation(user_id):
    data = load_engagement()
    user = data["users"].setdefault(str(user_id), default_user())

    today = date.fromisoformat(today_str())
    last = date.fromisoformat(user["last_participated"]) if user["last_participated"] else None

    if last == today:
        return
    elif last == today - timedelta(days=1):
        user["current_streak"] += 1
    else:
        user["current_streak"] = 1

    user["longest_streak"] = max(user["longest_streak"], user["current_streak"])
    user["total_days"] += 1
    user["last_participated"] = today.isoformat()

    save_engagement(data)


def checkme_count_today(user_id):
    data = load_engagement()
    user = data["users"].get(str(user_id))
    if not user or user.get("checkme_last_date") != today_str():
        return 0
    return user.get("checkme_count_today", 0)


def record_checkme_usage(user_id):
    data = load_engagement()
    user = data["users"].setdefault(str(user_id), default_user())
    today = today_str()

    if user.get("checkme_last_date") != today:
        user["checkme_last_date"] = today
        user["checkme_count_today"] = 0

    user["checkme_count_today"] += 1
    user["checkme_uses"] += 1

    daily = data.setdefault("checkme_daily", {})
    daily[today] = daily.get(today, 0) + 1

    save_engagement(data)


def generate_english_sentence():
    response = client_ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": """Generate one natural, conversational English sentence for a Japanese learning community to translate.

Rules:
- It should feel like something a real person would say in daily life.
- Sentence theme should be either food, travel, fashion, hobbies, anime, movies, music, gaming, pets, sports, or books.
- Avoid overly simple sentences (not "I eat rice") and keep it accessible to beginner and intermediate learners.
- Do NOT include the Japanese translation
- Return ONLY the sentence, nothing else"""
        }]
    )
    return response.content[0].text.strip()


def generate_japanese_translation(sentence):
    response = client_ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"""You are a native Japanese speaker in your 20s living in Tokyo. 
            You made a new friend who came from America and is studying Japanese. They asked you how to say this English sentence in Japanese-
            how would you naturally say it in Japanese? It should be at the N5, N4, or N3 level.
            "{sentence}"

Rules:
- Write exactly how you would say it out loud to a new friend you just made, not how you would say it to a friend you have known for many years nor how a textbook would phrase it.
- Do NOT use contractions like んだ、ちゃう、てる, etc.
- Do NOT end sentences with よね、かな、じゃん、けど etc. unless it is the number one most prevalent way to say that sentence in Japan.
- If there are multiple natural ways to say it, pick the most conversational one.
- Avoid でございます、～いたします or any keigo unless the sentence specifically calls for it.
- Provide the Japanese in three forms: kanji/kana, hiragana reading, and romaji.
- Add a maximum two-sentence note explaining anything nuanced about the phrasing or any slang used.
- Format exactly like this:

**Japanese:**
[kanji/kana version]

**Reading:**
[hiragana]

**Romaji:**
[romaji]

**Note:**
[short cultural or nuance note]"""
        }]
    )
    return response.content[0].text.strip()


def generate_feedback(sentence, attempt):
    response = client_ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"""You are a native Japanese speaker in your 20s living in Tokyo, giving a friend who is learning Japanese quick feedback on their translation attempt.

The English sentence was: "{sentence}"
Their Japanese attempt: "{attempt}"

Rules:
- Write your feedback in English. You can quote specific Japanese words or phrases (in Japanese script) when pointing something out, but all explanations and commentary must be in English so an English-speaking learner can follow them.
- Be accurate. Do not soften or hide real grammar or word-choice errors, but frame everything as encouraging coaching, not grading.
- Start by naming what they got right, specifically (not just "good job").
- Then point out any real errors or unnatural phrasing, and briefly explain why.
- If there's a more natural way to say it, give that phrasing in Japanese with a quick English gloss.
- Keep the tone warm and casual, like an encouraging friend, not a teacher.
- Keep it short — a few sentences, not an essay.
- Do not use headers or markdown formatting like the translation reveal does — just write it as a short, natural message."""
        }]
    )
    return response.content[0].text.strip()


_commands_synced = False


@bot.event
async def on_ready():
    global _commands_synced
    print(f"Logged in as {bot.user}")
    if not _commands_synced:
        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            bot.tree.copy_global_to(guild=channel.guild)
            await bot.tree.sync(guild=channel.guild)
            _commands_synced = True
    daily_morning.start()
    daily_evening.start()


@bot.event
async def on_message(message):
    if not message.author.bot and message.channel.id == CHANNEL_ID:
        record_participation(message.author.id)
    await bot.process_commands(message)


async def run_morning(state_file=STATE_FILE, channel_id=CHANNEL_ID):
    channel = bot.get_channel(channel_id)
    if not channel:
        print("Channel not found")
        return

    sentence = generate_english_sentence()

    message = await channel.send(
        f"# 🌟 SAY IT IN JAPANESE 🌟\n\n"
        f"Hi <@&{BETA_TESTERS_ROLE_ID}> !!\n"
        f"**Sentence of the day:**\n> {sentence}\n\n"
        f"How would you say this sentence in Japanese? Send a quick voice memo or drop your translation below\n"
        f"Feel free to give each other feedback and come back in 12 hours for the reveal 😎\n\n"
        f"-# Want private feedback on your attempt? Right-click (or long-press) your message → Apps → Check my Japanese. Type `/streak` to see your streak."
    )

    save_state({
        "sentence": sentence,
        "message_id": message.id
    }, state_file)

    print(f"Morning drop sent: {sentence}")


async def run_evening(state_file=STATE_FILE, channel_id=CHANNEL_ID):
    state = load_state(state_file)
    if not state:
        print("No state found for evening reveal")
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    try:
        original_message = await channel.fetch_message(state["message_id"])
    except discord.NotFound:
        original_message = None

    japanese = generate_japanese_translation(state["sentence"])

    reveal_text = (
        f"# ✨ TRANSLATION REVEAL ✨\n\n"
        f"Hi <@&{BETA_TESTERS_ROLE_ID}> !!\n"
        f"How did you do??"
        f"**The sentence was:**\n"
        f"*{state['sentence']}*\n\n"
        f"{japanese}\n\n"
    )

    if original_message:
        await original_message.reply(reveal_text)
    else:
        await channel.send(reveal_text)

    print("Evening reveal sent")


@tasks.loop(time=MORNING_TIME)
async def daily_morning():
    await run_morning(STATE_FILE)


@tasks.loop(time=EVENING_TIME)
async def daily_evening():
    await run_evening(STATE_FILE)


@bot.command(name="testmorning")
@commands.check(lambda ctx: ctx.channel.id == BOT_DEV_CHANNEL_ID and (
    ctx.author.guild_permissions.administrator or
    any(role.name in ["moderator", "trial moderator"] for role in ctx.author.roles)))
async def test_morning(ctx):
    await run_morning(TEST_STATE_FILE, BOT_DEV_CHANNEL_ID)
    import asyncio
    await asyncio.sleep(1)
    await ctx.message.delete()

@bot.command(name="testevening")
@commands.check(lambda ctx: ctx.channel.id == BOT_DEV_CHANNEL_ID and (
    ctx.author.guild_permissions.administrator or
    any(role.name in ["moderator", "trial moderator"] for role in ctx.author.roles)))
async def test_evening(ctx):
    await run_evening(TEST_STATE_FILE, BOT_DEV_CHANNEL_ID)
    import asyncio
    await asyncio.sleep(1)
    await ctx.message.delete()


def get_current_sentence():
    candidates = [f for f in (STATE_FILE, TEST_STATE_FILE) if os.path.exists(f)]
    if not candidates:
        return None
    latest_file = max(candidates, key=os.path.getmtime)
    return load_state(latest_file).get("sentence")


async def send_checkme_feedback(interaction: discord.Interaction, attempt: str):
    sentence = get_current_sentence()
    if not sentence:
        await interaction.followup.send("No sentence live right now — check back after the morning drop! 🌅", ephemeral=True)
        return

    if checkme_count_today(interaction.user.id) >= CHECKME_DAILY_LIMIT:
        await interaction.followup.send(
            f"You've hit today's limit of {CHECKME_DAILY_LIMIT} checks — come back tomorrow, or keep practicing without me for now! 💪",
            ephemeral=True
        )
        return

    feedback = generate_feedback(sentence, attempt)
    await interaction.followup.send(feedback, ephemeral=True)

    record_participation(interaction.user.id)
    record_checkme_usage(interaction.user.id)


@bot.tree.context_menu(name="Check my Japanese")
async def checkme_context(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(ephemeral=True)

    if message.author.id != interaction.user.id:
        await interaction.followup.send("You can only check your own attempts this way!", ephemeral=True)
        return

    if not message.content.strip():
        await interaction.followup.send("That message doesn't have any text for me to check — voice memo feedback isn't supported yet, try posting a text attempt instead!", ephemeral=True)
        return

    await send_checkme_feedback(interaction, message.content)


@bot.tree.command(name="streak", description="See your participation streak")
async def streak(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    data = load_engagement()
    user = data["users"].get(str(interaction.user.id))
    if not user or user["total_days"] == 0:
        await interaction.followup.send("You haven't participated yet — jump into today's sentence to start your streak! 🔥", ephemeral=True)
        return

    await interaction.followup.send(
        f"**Your streak** 🔥\n"
        f"Current: {user['current_streak']} day{'s' if user['current_streak'] != 1 else ''}\n"
        f"Longest: {user['longest_streak']} day{'s' if user['longest_streak'] != 1 else ''}\n"
        f"Total days participated: {user['total_days']}",
        ephemeral=True
    )


@bot.tree.command(name="checkmestats", description="Admin-only: /checkme usage metrics")
@app_commands.default_permissions(administrator=True)
async def checkme_stats(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    data = load_engagement()
    users = data.get("users", {})
    daily = data.get("checkme_daily", {})

    total_uses = sum(u.get("checkme_uses", 0) for u in users.values())
    unique_users = sum(1 for u in users.values() if u.get("checkme_uses", 0) > 0)

    monthly = {}
    for day, count in daily.items():
        month = day[:7]
        monthly[month] = monthly.get(month, 0) + count

    trend = sorted(monthly.items())[-6:]
    trend_lines = "\n".join(f"  {month}: {count}" for month, count in trend) or "  (no data yet)"

    now = datetime.now(TIMEZONE)
    this_month = now.strftime("%Y-%m")
    last_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    this_count = monthly.get(this_month, 0)
    last_count = monthly.get(last_month, 0)
    change = f"{(this_count - last_count) / last_count * 100:+.0f}%" if last_count > 0 else "n/a"

    await interaction.followup.send(
        f"**/checkme usage** 📊\n\n"
        f"All-time: {total_uses} uses across {unique_users} unique users\n\n"
        f"This month ({this_month}): {this_count}\n"
        f"Last month ({last_month}): {last_count}\n"
        f"MoM change: {change}\n\n"
        f"Last 6 months:\n{trend_lines}",
        ephemeral=True
    )


bot.run(DISCORD_TOKEN)