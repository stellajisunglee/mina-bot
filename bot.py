import discord
from discord import app_commands
from discord.ext import commands, tasks
from anthropic import Anthropic
from dotenv import load_dotenv
import os
import json
import random
import re
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
EVENTS_FILE = "events.jsonl"
CHECKME_DAILY_LIMIT = 5

JAPANESE_PATTERN = re.compile(r'[぀-ヿ一-鿿]')

FLUENCY_LEVELS = [
    {
        "name": "elementary school",
        "label": "elementary school level, around JLPT N5",
        "english": "Short and concrete — one main idea, everyday words, the kind of thing a kid would say to a friend.",
        "japanese": "Keep the Japanese around JLPT N5: basic particles, simple verb forms, and common everyday vocabulary.",
        "grammar": [
            "て-form linking two actions",
            "〜たい (saying what you want to do)",
            "〜ませんか / 〜ましょう (inviting or suggesting)",
            "〜が好き / 〜が上手 (likes and skills marked with が)",
            "〜ている for something happening right now",
            "あります / います (saying that something exists)",
            "plain past tense (〜た)",
            "〜から (giving a reason)",
        ],
    },
    {
        "name": "middle school",
        "label": "middle school level, around JLPT N4",
        "english": "A full everyday sentence with a little detail — two connected ideas at most, still casual and common.",
        "japanese": "Keep the Japanese around JLPT N4: everyday compound sentences and common conversational patterns.",
        "grammar": [
            "〜たら conditional",
            "potential form (being able to do something)",
            "giving and receiving (あげる / くれる / もらう)",
            "〜てみる (trying something out)",
            "〜ておく (doing something ahead of time)",
            "〜なければいけない / 〜なくてもいい (obligation)",
            "〜と思う (giving your own opinion)",
            "noun-modifying clauses (relative clauses)",
            "〜すぎる (doing something too much)",
            "〜ながら (doing two things at once)",
        ],
    },
    {
        "name": "high school",
        "label": "high school level, around JLPT N3",
        "english": "A more textured sentence — an opinion, a comparison, or a bit of nuance, but still something said out loud in conversation.",
        "japanese": "Keep the Japanese around JLPT N3: more nuanced connectives and expressions, still fully conversational.",
        "grammar": [
            "〜ようになる (a change over time)",
            "〜ことにする / 〜ことになる (decisions and outcomes)",
            "〜らしい / 〜みたい (hearsay and resemblance)",
            "causative form (making or letting someone do something)",
            "passive form",
            "〜ば conditional",
            "〜てしまう (finishing something, or regretting it)",
            "〜たばかり (having just done something)",
            "〜そうです (it looks like / I heard that)",
            "〜のに (even though)",
        ],
    },
    {
        "name": "college",
        "label": "college level, around JLPT N2",
        "english": "An adult, layered sentence — a qualified opinion, a cause and effect, or a comparison with a caveat. Still natural speech, not writing.",
        "japanese": "Keep the Japanese around JLPT N2: richer connectives and set expressions, while staying conversational.",
        "grammar": [
            "〜おかげで / 〜せいで (good and bad causes)",
            "〜はずだ (what you would expect to be true)",
            "〜に違いない (being sure about something)",
            "〜として / 〜にとって (roles and perspectives)",
            "〜ば〜ほど (the more you do it, the more...)",
            "〜わけだ (so that explains it)",
            "causative-passive (being made to do something)",
            "〜さえ / 〜こそ (emphasis particles)",
            "〜つもりだった (what you had intended)",
            "〜ものだ (how things generally are)",
        ],
    },
]

LEVELS_BY_NAME = {level["name"]: level for level in FLUENCY_LEVELS}

VOCAB_THEMES = [
    "food and cooking", "travel", "fashion", "hobbies", "anime", "movies", "music",
    "gaming", "pets", "sports", "books", "cafés and coffee", "weather and seasons",
    "school and work life", "trains and getting around town", "shopping",
    "exercise and health", "holidays and festivals", "phones and technology",
    "making plans with friends",
]

# How many recent picks to avoid reusing, per category
FOCUS_HISTORY = {"recent_levels": 1, "recent_grammar": 6, "recent_themes": 5}

JAPANESE_SCHEMA = {
    "type": "object",
    "properties": {
        "japanese": {"type": "string", "description": "The translation in kanji/kana"},
        "reading": {"type": "string", "description": "The same sentence in hiragana"},
        "romaji": {"type": "string", "description": "The same sentence in romaji"},
        "note": {"type": "string", "description": "At most two sentences on anything nuanced about the phrasing or slang"},
        "grammar_focus": {"type": "string", "description": "At most two sentences explaining how the day's grammar structure works in this sentence"},
        "key_words": {
            "type": "array",
            "description": "The three words or phrases from the Japanese sentence a learner most needs in order to attempt it",
            "items": {
                "type": "object",
                "properties": {
                    "word": {"type": "string", "description": "The word in kanji/kana"},
                    "reading": {"type": "string", "description": "The word in hiragana"},
                    "meaning": {"type": "string", "description": "A short English gloss, a few words at most"},
                },
                "required": ["word", "reading", "meaning"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["japanese", "reading", "romaji", "note", "grammar_focus", "key_words"],
    "additionalProperties": False,
}

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


def log_event(event_type, user_id, **extra):
    event = {
        "timestamp": datetime.now(TIMEZONE).isoformat(),
        "event_type": event_type,
        "user_id": str(user_id) if user_id is not None else None,
    }
    event.update(extra)
    with open(EVENTS_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")


def contains_japanese(text):
    return bool(JAPANESE_PATTERN.search(text))


def is_voice_message(message):
    if message.flags.voice:
        return True
    return any((a.content_type or "").startswith("audio/") for a in message.attachments)


def is_within_active_window():
    state = load_state(STATE_FILE)
    posted_at = state.get("posted_at")
    if not posted_at:
        return False

    now = datetime.now(TIMEZONE)
    if now < datetime.fromisoformat(posted_at):
        return False

    revealed_at = state.get("revealed_at")
    if revealed_at and now >= datetime.fromisoformat(revealed_at):
        return False

    return True


def default_user():
    return {
        "current_streak": 0,
        "longest_streak": 0,
        "last_participated": None,
        "first_participated": None,
        "total_days": 0,
        "checkme_uses": 0,
        "checkme_last_date": None,
        "checkme_count_today": 0,
    }


def get_user(data, user_id):
    """Fetch a user record, backfilling any fields added since it was created."""
    user = {**default_user(), **data["users"].get(str(user_id), {})}
    data["users"][str(user_id)] = user
    return user


def record_participation(user_id):
    data = load_engagement()
    user = get_user(data, user_id)

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
    if not user["first_participated"]:
        user["first_participated"] = today.isoformat()

    save_engagement(data)


def checkme_count_today(user_id):
    data = load_engagement()
    user = get_user(data, user_id)
    if user["checkme_last_date"] != today_str():
        return 0
    return user["checkme_count_today"]


def record_checkme_usage(user_id):
    data = load_engagement()
    user = get_user(data, user_id)
    today = today_str()

    if user["checkme_last_date"] != today:
        user["checkme_last_date"] = today
        user["checkme_count_today"] = 0

    user["checkme_count_today"] += 1
    user["checkme_uses"] += 1

    daily = data.setdefault("checkme_daily", {})
    daily[today] = daily.get(today, 0) + 1

    save_engagement(data)


def pick_fresh(options, recent):
    fresh = [option for option in options if option not in recent]
    return random.choice(fresh or options)


def remember(recent, value, limit):
    return ([item for item in recent if item != value] + [value])[-limit:]


def new_focus(recent_levels, recent_grammar, recent_themes):
    level = LEVELS_BY_NAME[pick_fresh(list(LEVELS_BY_NAME), recent_levels)]
    return {
        "level": level["name"],
        "grammar": pick_fresh(level["grammar"], recent_grammar),
        "theme": pick_fresh(VOCAB_THEMES, recent_themes),
    }


def pick_daily_focus(state_file):
    """Pick today's level, grammar point and theme, avoiding the most recent picks."""
    previous = load_state(state_file)
    levels = previous.get("recent_levels", [])
    grammar = previous.get("recent_grammar", [])
    themes = previous.get("recent_themes", [])

    focus = new_focus(levels, grammar, themes)
    history = {
        "recent_levels": remember(levels, focus["level"], FOCUS_HISTORY["recent_levels"]),
        "recent_grammar": remember(grammar, focus["grammar"], FOCUS_HISTORY["recent_grammar"]),
        "recent_themes": remember(themes, focus["theme"], FOCUS_HISTORY["recent_themes"]),
    }
    return focus, history


def level_config(focus):
    return LEVELS_BY_NAME.get(focus.get("level"), FLUENCY_LEVELS[1])


def format_key_word(word):
    kanji = word["word"].strip()
    reading = word["reading"].strip()
    head = f"{kanji} ({reading})" if reading and reading != kanji else kanji
    return f"{head} — {word['meaning'].strip()}"


def generate_english_sentence(focus):
    level = level_config(focus)
    response = client_ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": f"""Generate one natural, conversational English sentence for a Japanese learning community to translate.

Rules:
- It should feel like something a real person would say in daily life.
- The sentence should be about {focus['theme']}.
- Difficulty: {level['english']}
- Translated into Japanese, it should naturally call for this grammar structure: {focus['grammar']}. Write the English so that structure is the obvious way to say it, but do NOT mention the grammar point.
- Do NOT include the Japanese translation
- Return ONLY the sentence, nothing else"""
        }]
    )
    return response.content[0].text.strip()


def generate_japanese_package(sentence, focus):
    """Translate the sentence and pull out the day's grammar note and three key words."""
    level = level_config(focus)
    response = client_ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        output_config={"format": {"type": "json_schema", "schema": JAPANESE_SCHEMA}},
        messages=[{
            "role": "user",
            "content": f"""You are a native Japanese speaker in your 20s living in Tokyo.
            You made a new friend who came from America and is studying Japanese. They asked you how to say this English sentence in Japanese-
            how would you naturally say it in Japanese?
            "{sentence}"

Rules:
- Write exactly how you would say it out loud to a new friend you just made, not how you would say it to a friend you have known for many years nor how a textbook would phrase it.
- {level['japanese']}
- Use this grammar structure in the translation: {focus['grammar']}.
- Do NOT use contractions like んだ、ちゃう、てる, etc.
- Do NOT end sentences with よね、かな、じゃん、けど etc. unless it is the number one most prevalent way to say that sentence in Japan.
- If there are multiple natural ways to say it, pick the most conversational one.
- Avoid でございます、～いたします or any keigo unless the sentence specifically calls for it.
- For "note", write at most two sentences explaining anything nuanced about the phrasing or any slang used.
- For "grammar_focus", write at most two sentences explaining how {focus['grammar']} works in this sentence, in English.
- For "key_words", give exactly three words or phrases that appear in your translation — the ones a learner would most need to attempt this sentence themselves. Keep the meanings to a few words each."""
        }]
    )
    text = next(block.text for block in response.content if block.type == "text")
    package = json.loads(text)
    package["key_words"] = package["key_words"][:3]
    return package


def format_reveal(package, focus):
    key_words = "\n".join(f"- {format_key_word(word)}" for word in package["key_words"])
    return (
        f"**Japanese:**\n{package['japanese']}\n\n"
        f"**Reading:**\n{package['reading']}\n\n"
        f"**Romaji:**\n{package['romaji']}\n\n"
        f"**Note:**\n{package['note']}\n\n"
        f"**Grammar focus: {focus['grammar']}**\n{package['grammar_focus']}\n\n"
        f"**Key words (this morning's spoilers):**\n{key_words}"
    )


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
    if not message.author.bot and message.channel.id == CHANNEL_ID and is_within_active_window():
        voice = is_voice_message(message)
        if voice or contains_japanese(message.content):
            record_participation(message.author.id)
            log_event("participation", message.author.id, method="voice" if voice else "text")
    await bot.process_commands(message)


async def run_morning(state_file=STATE_FILE, channel_id=CHANNEL_ID):
    channel = bot.get_channel(channel_id)
    if not channel:
        print("Channel not found")
        return

    focus, history = pick_daily_focus(state_file)
    sentence = generate_english_sentence(focus)

    try:
        package = generate_japanese_package(sentence, focus)
    except Exception as error:
        package = None
        print(f"Could not prepare the reveal this morning, will generate it tonight: {error}")

    hint = ""
    if package:
        spoilers = "\n".join(f"||{format_key_word(word)}||" for word in package["key_words"])
        hint = f"**New here? Tap a spoiler for a head start:**\n{spoilers}\n\n"

    message = await channel.send(
        f"# 🌟 SAY IT IN JAPANESE 🌟\n\n"
        f"Hi <@&{BETA_TESTERS_ROLE_ID}> !!\n"
        f"How would you say this sentence in Japanese? Send a quick voice memo or drop your translation below\n\n"
        f"**Sentence of the day** ({level_config(focus)['label']}):\n> {sentence}\n\n"
        f"{hint}"
        f"Give each other feedback! If a conversation gets going, start a public thread on this message instead of "
        f"replying in the channel — right-click (or long-press) this message → Create Thread. "
        f"It keeps everything easy to follow and lets more people join in. Come back in 12 hours for the reveal 😎\n\n"
        f"-# Want private feedback on your attempt? Right-click (or long-press) your message → Apps → Check my Japanese. Type `/streak` to see your streak."
    )

    state = {
        "sentence": sentence,
        "message_id": message.id,
        "posted_at": datetime.now(TIMEZONE).isoformat(),
        "focus": focus,
    }
    if package:
        state["japanese"] = package
    state.update(history)
    save_state(state, state_file)

    log_event("morning_posted", None, sentence=sentence, channel_id=channel_id, **focus)
    print(f"Morning drop sent ({focus['level']}, {focus['theme']}, {focus['grammar']}): {sentence}")


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

    focus = state.get("focus") or new_focus([], [], [])
    package = state.get("japanese") or generate_japanese_package(state["sentence"], focus)

    reveal_text = (
        f"# ✨ TRANSLATION REVEAL ✨\n\n"
        f"Hi <@&{BETA_TESTERS_ROLE_ID}> !!\n"
        f"How did you do??\n\n"
        f"**The sentence was:**\n"
        f"*{state['sentence']}*\n\n"
        f"{format_reveal(package, focus)}\n\n"
    )

    if original_message:
        await original_message.reply(reveal_text)
    else:
        await channel.send(reveal_text)

    state["revealed_at"] = datetime.now(TIMEZONE).isoformat()
    save_state(state, state_file)

    log_event("evening_revealed", None, sentence=state["sentence"], channel_id=channel_id, **focus)
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
    log_event("checkme", interaction.user.id)


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
    log_event("streak_check", interaction.user.id)

    data = load_engagement()
    user = get_user(data, interaction.user.id)
    if user["total_days"] == 0:
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