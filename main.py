import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None  # fallback if ZoneInfo not available

# CONFIG: replace these placeholders with your actual IDs
GUILD_ID = 123456789012345678            # your guild/server ID
POLL_CHANNEL_ID = 987654321098765432      # channel where the poll will be posted
REMINDER_CHANNEL_ID = 111111111111111111 # channel for reminders

POLL_MESSAGE_ID = None  # will be set after posting the poll

# Role IDs (adjust names to your server)
EVENT_ROLE_ID = 222222222222222222       # The Event role (granted on Yes/Maybe, removed on No)
EVENT_PING_ROLE_ID = 333333333333333333  # The Event Ping! role (opt-out/removal from reminders, daily restoration)

# Poll text (exact text the poll should display)
POLL_TEXT = "@everyone The Next Practice Tournament will be hosted tomorrow at 3pm please vote to show your attendance"

# Emojis for voting
YES_EMOJI = "👍"
MAYBE_EMOJI = "🤔"
NO_EMOJI = "👎"

# Timezone for scheduling
TIMEZONE = "Europe/London"

intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # needed to read message content in some setups
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # Schedule daily poll at 18:00 Europe/London, and reminder every 15 minutes
    schedule_daily_poll.start()
    reminder_loop.start()

def get_guild():
    return bot.get_guild(GUILD_ID)

async def ensure_ids():
    g = get_guild()
    if g is None:
        return False
    return True

def is_today(target_dt):
    now = datetime.now(ZoneInfo(TIMEZONE) if ZoneInfo else timezone.utc)
    # Compare date in London time
    if ZoneInfo and isinstance(target_dt, datetime):
        london_now = datetime.now(ZoneInfo(TIMEZONE))
        return london_now.date() == now.date()
    return True

def next_time_today(hour, minute):
    # Return next datetime in London time for today at hour:minute
    try:
        london_tz = ZoneInfo(TIMEZONE) if ZoneInfo else None
        if london_tz:
            now = datetime.now(london_tz)
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate
    except Exception:
        pass
    # Fallback to UTC
    now = datetime.utcnow()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate

@tasks.loop(hours=24)
async def schedule_daily_poll():
    # This loop runs daily; it computes next London 18:00 and schedules a one-off poll
    await bot.wait_until_ready()
    g = bot.get_guild(GUILD_ID)
    if g is None:
        return
    poll_ch = g.get_channel(POLL_CHANNEL_ID)
    if poll_ch is None:
        return

    london_target = None
    if ZoneInfo:
        london_target = datetime.now(ZoneInfo(TIMEZONE)).replace(hour=18, minute=0, second=0, microsecond=0)
        if london_target < datetime.now(ZoneInfo(TIMEZONE)):
            london_target += timedelta(days=1)
        delay = (london_target - datetime.now(ZoneInfo(TIMEZONE))).total_seconds()
    else:
        # fallback to UTC
        london_target = datetime.utcnow().replace(hour=18, minute=0, second=0, microsecond=0)
        if london_target < datetime.utcnow():
            london_target += timedelta(days=1)
        delay = (london_target - datetime.utcnow()).total_seconds()

    # Sleep until the target time, then post the poll
    await asyncio_sleep(int(delay))
    await post_poll()

@tasks.loop(minutes=15)
async def reminder_loop():
    # Post reminder every 15 minutes in REMINDER_CHANNEL_ID
    await bot.wait_until_ready()
    g = bot.get_guild(GUILD_ID)
    if g is None:
        return
    ch = g.get_channel(REMINDER_CHANNEL_ID)
    if ch is None:
        return
    # Post a reminder message with a bell reaction to opt out of Event Ping!
    reminder_msg = await ch.send("Reminder: Event reminders are active. React with 🔔 to opt out of Event Ping! reminders.")
    await reminder_msg.add_reaction("🔔")

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
    embed.set_footer(text="Poll - live voter lists")

    poll_message = await ch.send(embed=embed)
    POLL_MESSAGE_ID = poll_message.id
    await poll_message.add_reaction(YES_EMOJI)
    await poll_message.add_reaction(MAYBE_EMOJI)
    await poll_message.add_reaction(NO_EMOJI)

async def update_poll_votes(poll_message):
    # Rebuild voter lists from reactions on the poll message
    yes_users = []
    maybe_users = []
    no_users = []
    for r in poll_message.reactions:
        if r.emoji == YES_EMOJI:
            users = await r.users().flatten()
            yes_users = [u.mention if isinstance(u, discord.User) or isinstance(u, discord.Member) else str(u) for u in users if not u.bot]
        elif r.emoji == MAYBE_EMOJI:
            users = await r.users().flatten()
            maybe_users = [u.mention if isinstance(u, discord.User) or isinstance(u, discord.Member) else str(u) for u in users if not u.bot]
        elif r.emoji == NO_EMOJI:
            users = await r.users().flatten()
            no_users = [u.mention if isinstance(u, discord.User) or isinstance(u, discord.Member) else str(u) for u in users if not u.bot]

    embed = poll_message.embeds[0]
    embed.set_field_at(0, name="Yes", value="\n".join(yes_users) if yes_users else "(no voters)", inline=True)
    embed.set_field_at(1, name="Maybe", value="\n".join(maybe_users) if maybe_users else "(no voters)", inline=True)
    embed.set_field_at(2, name="No", value="\n".join(no_users) if no_users else "(no voters)", inline=True)
    await poll_message.edit(embed=embed)

async def apply_event_role_to_votes(poll_message, action: str):
    # Simple helper to grant/remove Event role based on votes (Yes/Maybe -> grant; No -> remove)
    g = poll_message.guild
    event_role = g.get_role(EVENT_ROLE_ID)
    if event_role is None:
        return
    yes_or_maybe_users = set()
    for r in poll_message.reactions:
        if r.emoji in (YES_EMOJI, MAYBE_EMOJI):
            users = await r.users().flatten()
            yes_or_maybe_users.update({u.id for u in users if not isinstance(u, discord.User) and not u.bot})
            # above line ensures IDs from Members

    # For No votes, remove
    no_users = set()
    for r in poll_message.reactions:
        if r.emoji == NO_EMOJI:
            users = await r.users().flatten()
            no_users.update({u.id for u in users if not u.bot})

    target_ids = yes_or_maybe_users
    for member in g.members:
        if member.id in target_ids:
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

@bot.event
async def on_raw_reaction_add(payload):
    if payload.guild_id != GUILD_ID:
        return
    if str(payload.emoji) not in (YES_EMOJI, MAYBE_EMOJI, NO_EMOJI):
        return
    g = bot.get_guild(GUILD_ID)
    if not g:
        return
    if payload.message_id != POLL_MESSAGE_ID:
        return
    ch = g.get_channel(payload.channel_id)
    poll_message = await ch.fetch_message(payload.message_id)
    await update_poll_votes(poll_message)
    await apply_event_role_to_votes(poll_message, "update")

@bot.event
async def on_raw_reaction_remove(payload):
    if payload.guild_id != GUILD_ID:
        return
    if str(payload.emoji) not in (YES_EMOJI, MAYBE_EMOJI, NO_EMOJI):
        return
    g = bot.get_guild(GUILD_ID)
    if not g:
        return
    if payload.message_id != POLL_MESSAGE_ID:
        return
    ch = g.get_channel(payload.channel_id)
    poll_message = await ch.fetch_message(payload.message_id)
    await update_poll_votes(poll_message)
    await apply_event_role_to_votes(poll_message, "update")

async def asyncio_sleep(seconds: int):
    # small wrapper to avoid importing asyncio at top scope
    import asyncio
    await asyncio.sleep(seconds)

# Helper: ensure we start after token is set
@bot.event
async def on_connect():
    pass

# Start the bot with your token
TOKEN = "YOUR_DISCORD_BOT_TOKEN"  # replace with your token
bot.run(TOKEN)
