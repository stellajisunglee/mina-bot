import discord
from discord.ext import commands, tasks
from anthropic import Anthropic
from dotenv import load_dotenv
import os
import json
from datetime import time
from zoneinfo import ZoneInfo

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

TIMEZONE = ZoneInfo("America/Los_Angeles")
MORNING_TIME = time(6, 0, tzinfo=TIMEZONE)
EVENING_TIME = time(18, 0, tzinfo=TIMEZONE)

STATE_FILE = "state.json"
TEST_STATE_FILE = "test_state.json"

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

**japanese:**
[kanji/kana version]

**reading:**
[hiragana]

**romaji:**
[romaji]

**note:**
[short cultural or nuance note]"""
        }]
    )
    return response.content[0].text.strip()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    daily_morning.start()
    daily_evening.start()


async def run_morning(state_file=STATE_FILE):
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print("Channel not found")
        return

    sentence = generate_english_sentence()

    message = await channel.send(
        f"# 🌟 SAY IT IN JAPANESE 🌟\n\n"
        f"**sentence of the day: **\n> {sentence}\n\n"
        f"how would you say this sentence in japanese? send a quick voice memo or drop your translation below\n"
        f"feel free to give each other feedback and come back in 12 hours for the reveal 😎"
    )

    save_state({
        "sentence": sentence,
        "message_id": message.id
    }, state_file)

    print(f"Morning drop sent: {sentence}")


async def run_evening(state_file=STATE_FILE):
    state = load_state(state_file)
    if not state:
        print("No state found for evening reveal")
        return

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    try:
        original_message = await channel.fetch_message(state["message_id"])
    except discord.NotFound:
        original_message = None

    japanese = generate_japanese_translation(state["sentence"])

    reveal_text = (
        f"# ✨ TRANSLATION REVEAL ✨\n\n"
        f"**the sentence was:**\n"
        f"*{state['sentence']}*\n\n"
        f"{japanese}\n\n"
        f"how did you do??"
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
@commands.check(lambda ctx: ctx.author.guild_permissions.administrator or
    any(role.name in ["moderator", "trial moderator"] for role in ctx.author.roles))
async def test_morning(ctx):
    await run_morning(TEST_STATE_FILE)
    import asyncio
    await asyncio.sleep(1)
    await ctx.message.delete()

@bot.command(name="testevening")
@commands.check(lambda ctx: ctx.author.guild_permissions.administrator or
    any(role.name in ["moderator", "trial moderator"] for role in ctx.author.roles))
async def test_evening(ctx):
    await run_evening(TEST_STATE_FILE)
    import asyncio
    await asyncio.sleep(1)
    await ctx.message.delete()


bot.run(DISCORD_TOKEN)