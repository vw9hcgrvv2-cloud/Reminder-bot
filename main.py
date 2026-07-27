import os
import asyncio
import discord
from discord.ext import commands, tasks
from datetime import datetime, time as dt_time, timedelta

TOKEN = os.getenv("TOKEN")

# IDs and names (keep or adjust as needed)
POST_CHANNEL_ID = 1528940157024206899       # Channel to post the reminder message
VOTING_CHANNEL_ID = 1524445184853803069     # Channel where voting happens
EVENT_ROLE_NAME = "Event Ping!"

# Timings (UTC)
TEST_POLL_TIME = dt_time(hour=0, minute=30, second=0)   # 00:30
DAILY_EVENT_TIME = dt_time(hour=0, minute=30, second=0) # 00:30 (mass grant time)

# Optional: image URLs for embeds (replace with your actual URLs)
# IMAGE_URL_DAY = "https://example.com/day_poll_banner.png"
# IMAGE_URL_TEST = "https://example.com/test_poll_banner.png"

intents = discord.Intents.default()
intents.members = True  # Needed for mass role grant
bot = commands.Bot(command_prefix="!", intents=intents)


def build_embed(title: str, description: str, image_url: str) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=discord.Color.blue())
    if image_url:
        embed.set_image(url=image_url)
    return embed


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

    # Start scheduled tasks if not already running
    if not post_message.is_running():
        post_message.start()
    if not test_poll_job.is_running():
        test_poll_job.start()
    if not grant_event_role_to_all.is_running():
        grant_event_role_to_all.start()


@tasks.loop(minutes=15)
async def post_message():
    channel = bot.get_channel(POST_CHANNEL_ID)
    if channel is None:
        return

    guild = channel.guild
    role = discord.utils.get(guild.roles, name=EVENT_ROLE_NAME)
    voting_channel = guild.get_channel(VOTING_CHANNEL_ID)

    if role is None or voting_channel is None:
        return

    try:
        await channel.send(
            f"{role.mention} @here\n\n"
            f"Remember to vote in {voting_channel.mention}.\n\n"
            "Once you've finished voting, click the 🔔 emoji."
        )
    except Exception:
        pass


@post_message.before_loop
async def before_post():
    await bot.wait_until_ready()


# Mass role grant at 00:30 UTC daily
@tasks.loop(hours=24)
async def grant_event_role_to_all():
    now = datetime.utcnow()
    target = datetime.combine(now.date(), dt_time(hour=0, minute=30, second=0))
    if now.time() > dt_time(hour=0, minute=30, second=0):
        target = datetime.combine(now.date() + timedelta(days=1), dt_time(hour=0, minute=30, second=0))
    wait = (target - now).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)

    for guild in bot.guilds:
        role = discord.utils.get(guild.roles, name=EVENT_ROLE_NAME)
        if role is None:
            continue
        for member in guild.members:
            if role not in member.roles:
                try:
                    await member.add_roles(role, reason="Daily grant of Event Ping! Role at 00:30 UTC")
                except Exception:
                    pass


# Test poll and daily poll embeds (replace IMAGE_URLs with real images)
@tasks.loop(hours=24)
async def test_poll_job():
    now = datetime.utcnow()
    target = datetime.combine(now.date(), TEST_POLL_TIME)
    if now.time() > TEST_POLL_TIME:
        target = datetime.combine(now.date() + timedelta(days=1), TEST_POLL_TIME)
    wait = (target - now).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)

    guild = bot.guilds[0] if bot.guilds else None
    if not guild:
        return

    channel = guild.get_channel(VOTING_CHANNEL_ID)
    if channel is None:
        return

    embed = build_embed(
        title="Event Poll (Test)",
        description="",
        image_url=None  # Replace with IMAGE_URL_TEST if you have one
    )
    try:
        msg = await channel.send(embed=embed)
        # If you use a view with Yes/No/Maybe buttons, attach here
        # view = EventPollView(guild, channel)
        # await msg.edit(embed=embed, view=view)
    except Exception:
        pass


@test_poll_job.before_loop
async def before_test():
    await bot.wait_until_ready()


# Ensure the bot starts these tasks on ready
@grant_event_role_to_all.before_loop
async def before_grant():
    await bot.wait_until_ready()


@bot.event
async def on_disconnect():
    # Basic safety to avoid crashing on disconnect
    pass


bot.run(TOKEN)
