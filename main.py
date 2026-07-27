import os
import asyncio
from datetime import datetime, timedelta
import discord
from discord.ext import commands, tasks

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None  # Fallback if ZoneInfo not available

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.environ.get("GUILD_ID", "0"))
POLL_CHANNEL_ID = int(os.environ.get("POLL_CHANNEL_ID", "0"))
EVENT_ROLE_ID = int(os.environ.get("EVENT_ROLE_ID", "0"))
TIMEZONE = os.environ.get("TIMEZONE", "Europe/London")

YES_EMOJI = "👍"
MAYBE_EMOJI = "🤔"
NO_EMOJI = "👎"

POLL_TEXT = "@everyone The Next Practice Tournament will be hosted tomorrow at 3pm please vote to show your attendance"

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

POLL_MESSAGE_ID = None

def to_london(dt: datetime) -> datetime:
    if ZoneInfo:
        tz = ZoneInfo(TIMEZONE)
        return dt.astimezone(tz)
    return dt

def next_london_1830_utc() -> datetime:
    now_london = datetime.now(ZoneInfo(TIMEZONE)) if ZoneInfo else datetime.utcnow()
    target = now_london.replace(hour=18, minute=0, second=0, microsecond=0)
    if target <= now_london:
        target += timedelta(days=1)
    if ZoneInfo:
        return target.astimezone(timedelta(0).tzinfo)
    # naive fallback in UTC
    return target

async def post_poll():
    global POLL_MESSAGE_ID
    g = bot.get_guild(GUILD_ID)
    if g is None:
        return
    ch = g.get_channel(POLL_CHANNEL_ID)
    if ch is None:
        return

    embed = discord.Embed(title="Attendance", description=POLL_TEXT, color=0x00ff00)
    embed.add_field(name="Yes", value="(no voters yet)", inline=True)
    embed.add_field(name="Maybe", value="(no voters yet)", inline=True)
    embed.add_field(name="No", value="(no voters yet)", inline=True)
    poll_message = await ch.send(embed=embed)
    POLL_MESSAGE_ID = poll_message.id
    await poll_message.add_reaction(YES_EMOJI)
    await poll_message.add_reaction(MAYBE_EMOJI)
    await poll_message.add_reaction(NO_EMOJI)

@tasks.loop(hours=24)
async def schedule_daily_poll():
    await bot.wait_until_ready()
    london_time = next_london_1830_utc()
    now = datetime.utcnow()
    delay = (london_time - now).total_seconds()
    if delay <= 0:
        delay = 1
    await asyncio.sleep(int(delay))
    await post_poll()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if GUILD_ID == 0 or POLL_CHANNEL_ID == 0:
        print("Warning: GUILD_ID or POLL_CHANNEL_ID not set. Set environment variables.")
    schedule_daily_poll.start()

@bot.event
async def on_raw_reaction_add(payload):
    # Placeholder for voting logic if needed
    pass

@bot.event
async def on_raw_reaction_remove(payload):
    # Placeholder for voting logic if needed
    pass

bot.run(TOKEN)
