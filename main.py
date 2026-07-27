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
REMINDER_CHANNEL_ID = int(os.environ.get("REMINDER_CHANNEL_ID", "0"))
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

POLL_MESSAGE_ID = None  # will be set after posting the poll

def to_london(dt: datetime) -> datetime:
    if ZoneInfo:
        tz = ZoneInfo(TIMEZONE)
        return dt.astimezone(tz)
    return dt

def london_now():
    if ZoneInfo:
        return datetime.now(ZoneInfo(TIMEZONE))
    return datetime.utcnow()

def next_london_1830():
    now_london = london_now()
    target = now_london.replace(hour=18, minute=0, second=0, microsecond=0)
    if target <= now_london:
        target += timedelta(days=1)
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

async def update_poll_votes(poll_message):
    yes_users = []
    maybe_users = []
    no_users = []
    for r in poll_message.reactions:
        if r.emoji == YES_EMOJI:
            users = await r.users().flatten()
            yes_users = [u.mention for u in users if not u.bot]
        elif r.emoji == MAYBE_EMOJI:
            users = await r.users().flatten()
            maybe_users = [u.mention for u in users if not u.bot]
        elif r.emoji == NO_EMOJI:
            users = await r.users().flatten()
            no_users = [u.mention for u in users if not u.bot]

    embed = poll_message.embeds[0]
    embed.set_field_at(0, name="Yes", value="\n".join(yes_users) if yes_users else "(no voters)", inline=True)
    embed.set_field_at(1, name="Maybe", value="\n".join(maybe_users) if maybe_users else "(no voters)", inline=True)
    embed.set_field_at(2, name="No", value="\n".join(no_users) if no_users else "(no voters)", inline=True)
    await poll_message.edit(embed=embed)

def get_member_by_id(guild, member_id):
    for m in guild.members:
        if m.id == member_id:
            return m
    return None

@bot.event
async def on_raw_reaction_add(payload):
    if payload.guild_id != GUILD_ID:
        return
    if str(payload.emoji) not in (YES_EMOJI, MAYBE_EMOJI, NO_EMOJI):
        return
    if payload.message_id != POLL_MESSAGE_ID:
        return
    g = bot.get_guild(GUILD_ID)
    if not g:
        return
    ch = g.get_channel(payload.channel_id)
    if not ch:
        return
    poll_message = await ch.fetch_message(payload.message_id)
    await update_poll_votes(poll_message)
    # apply role logic
    await apply_event_role_to_votes(poll_message)

@bot.event
async def on_raw_reaction_remove(payload):
    if payload.guild_id != GUILD_ID:
        return
    if str(payload.emoji) not in (YES_EMOJI, MAYBE_EMOJI, NO_EMOJI):
        return
    if payload.message_id != POLL_MESSAGE_ID:
        return
    g = bot.get_guild(GUILD_ID)
    if not g:
        return
    ch = g.get_channel(payload.channel_id)
    if not ch:
        return
    poll_message = await ch.fetch_message(payload.message_id)
    await update_poll_votes(poll_message)
    await apply_event_role_to_votes(poll_message)

async def apply_event_role_to_votes(poll_message):
    g = poll_message.guild
    event_role = g.get_role(EVENT_ROLE_ID)
    if event_role is None:
        return
    yes_or_maybe_ids = set()
    for r in poll_message.reactions:
        if r.emoji in (YES_EMOJI, MAYBE_EMOJI):
            users = await r.users().flatten()
            yes_or_maybe_ids.update({u.id for u in users if not u.bot})

    no_ids = set()
    for r in poll_message.reactions:
        if r.emoji == NO_EMOJI:
            users = await r.users().flatten()
            no_ids.update({u.id for u in users if not u.bot})

    for member in g.members:
        if member.id in yes_or_maybe_ids:
            if event_role not in member.roles:
                try:
                    await member.add_roles(event_role)
                except Exception:
                    pass
        else:
            if event_role in member.roles:
                try:
                    await member.remove_roles(event_role)
                except Exception:
                    pass

async def reminder_loop():
    await bot.wait_until_ready()
    g = bot.get_guild(GUILD_ID)
    if g is None:
        return
    ch = g.get_channel(REMINDER_CHANNEL_ID)
    if ch is None:
        return
    reminder_msg = await ch.send("Reminder: Event reminders are active. React with 🔔 to opt out of Event Ping! reminders.")
    await reminder_msg.add_reaction("🔔")

@tasks.loop(seconds=60)
async def schedule_daily_poll():
    await bot.wait_until_ready()
    target = next_london_1830()
    now = datetime.utcnow()
    delay = (target - now).total_seconds()
    if delay > 0:
        await asyncio.sleep(int(delay))
    await post_poll()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    schedule_daily_poll.start()
    bot.loop.create_task(reminder_loop())

bot.run(TOKEN)
