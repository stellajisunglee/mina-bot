import discord
from discord.ext import commands, tasks
from anthropic import Anthropic
from dotenv import load_dotenv
import os
import json
from datetime import time
import pytz

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

TIMEZONE = pytz.timezone("America/Los_Angeles")
MORNING_TIME = time(9, 0)
EVENING_TIME = time(18, 0)

STATE_FILE = "state.json"

client_ai = Anthropic(api_key=ANTHROPIC_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)


def generate_english_sentence():
    response = client_ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": """Generate one natural, conversational English sentence for a Japanese learning community to translate.

Rules:
- It should feel like something a real person would say in daily life
- Vary the theme each day: emotions, food, plans, observations, small talk, pop culture, etc.
- Avoid overly simple sentences (not "I eat rice") but keep it accessible to beginner and intermediate learners
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
A new friend you made just said this to you in English — how would you naturally say it in Japanese at the N5, N4, or N3 level?

"{sentence}"

Rules:
- Write exactly how you would say it out loud to a new friend you just made, not how you would say it to a friend you have known for many years nor how a textbook would phrase it
- Do NOT use contractions like んだ、ちゃう、てる
- Do NOT end sentences with よね、かな、じゃん、けど etc. unless it is the most prevalent way to say it in Japan
- If there are multiple natural ways to say it, pick the most conversational one
- Avoid でございます、～いたします or any keigo unless the sentence specifically calls for it
- Provide the Japanese in three forms: kanji/kana, hiragana reading, and romaji
- Add a maximum two-sentence note explaining anything nuanced about the phrasing or any slang used
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


@tasks.loop(time=MORNING_TIME)
async def daily_morning():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print("Channel not found")
        return

    sentence = generate_english_sentence()

    message = await channel.send(
        f"# 🌟 SAY IT IN JAPANESE 🌟\n\n"
        f"**today's sentence: **\n> {sentence}\n\n"
        f"how would you say this sentence in japanese? send a quick voice memo or drop your translation below\n\n"
        f"feel free to give each other feedback! come back in 12 hours for the reveal 😎"
    )

    save_state({
        "sentence": sentence,
        "message_id": message.id
    })

    print(f"Morning drop sent: {sentence}")


@tasks.loop(time=EVENING_TIME)
async def daily_evening():
    state = load_state()
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
        f"**the sentence was:**\n\n"
        f"*{state['sentence']}*\n\n"
        f"{japanese}\n\n"
        f"how did you do??"
    )

    if original_message:
        await original_message.reply(reveal_text)
    else:
        await channel.send(reveal_text)

    print("Evening reveal sent")


@bot.command(name="testmorning")
@commands.check(lambda ctx: ctx.author.guild_permissions.administrator or 
    any(role.name in ["moderator", "trial moderator"] for role in ctx.author.roles))
async def test_morning(ctx):
    await daily_morning()
    import asyncio
    await asyncio.sleep(1)
    await ctx.message.delete()

@bot.command(name="testevening")
@commands.check(lambda ctx: ctx.author.guild_permissions.administrator or 
    any(role.name in ["moderator", "trial moderator"] for role in ctx.author.roles))
async def test_evening(ctx):
    await daily_evening()
    import asyncio
    await asyncio.sleep(1)
    await ctx.message.delete()


bot.run(DISCORD_TOKEN)