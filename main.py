import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

import discord
import pytz
from discord.ext import commands

# ----------------------------
# Logging setup
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("discord_bot_prod")

# ----------------------------
# Timezone
# ----------------------------
TZ = pytz.timezone("Europe/London")

# ----------------------------
# Config (environment variables)
# ----------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
POLL_CHANNEL_ID = os.getenv("POLL_CHANNEL_ID")
REMINDER_CHANNEL_ID = os.getenv("REMINDER_CHANNEL_ID")
ROLE_EVENT_ATTENDEE_NAME = os.getenv("ROLE_EVENT_ATTENDEE", "Event Attendee")
ROLE_EVENT_PING_NAME = os.getenv("ROLE_EVENT_PING", "Event Ping!")
ENABLE_DAILY_PING_ASSIGNMENT = os.getenv("ENABLE_DAILY_PING_ASSIGNMENT", "false").lower() in ("1", "true", "yes")

def to_int(val, name):
    try:
        return int(val)
    except Exception:
        logger.exception(f"Invalid integer for {name}: {val}")
        raise SystemExit(1)

if not TOKEN:
    logger.error("DISCORD_BOT_TOKEN is not set.")
    raise SystemExit(1)

if not POLL_CHANNEL_ID:
    logger.error("POLL_CHANNEL_ID is not set.")
    raise SystemExit(1)
POLL_CHANNEL_ID = to_int(POLL_CHANNEL_ID, "POLL_CHANNEL_ID")

if not REMINDER_CHANNEL_ID:
    logger.error("REMINDER_CHANNEL_ID is not set.")
    raise SystemExit(1)
REMINDER_CHANNEL_ID = to_int(REMINDER_CHANNEL_ID, "REMINDER_CHANNEL_ID")

# ----------------------------
# State persistence
# ----------------------------
STATE_FILE = "poll_state.json"

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"poll_message_id": None, "votes": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception("Failed to load poll state file.")
        return {"poll_message_id": None, "votes": {}}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        logger.exception("Failed to save poll state.")

# ----------------------------
# Bot setup
# ----------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

poll_state = load_state()
background_tasks_started = False
start_lock = asyncio.Lock()

# ----------------------------
# Helpers
# ----------------------------
def get_role_by_name(guild: discord.Guild, name: str) -> discord.Role | None:
    for r in guild.roles:
        if r.name == name:
            return r
    return None

# Ensure we keep a persistent view registered
class PollView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ I'll be there", style=discord.ButtonStyle.success, custom_id="poll_yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_vote(interaction, "yes")

    @discord.ui.button(label="❓ Maybe", style=discord.ButtonStyle.primary, custom_id="poll_maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_vote(interaction, "maybe")

    @discord.ui.button(label="❌ Can't do it", style=discord.ButtonStyle.danger, custom_id="poll_no")
    async def no(self, interaction: discord.Interaction, button):
        await handle_vote(interaction, "no")

async def handle_vote(interaction: discord.Interaction, choice: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Votes must be cast in a server.", ephemeral=True)
        return

    user_id = str(interaction.user.id)
    poll_state.setdefault("votes", {})
    poll_state["votes"][user_id] = choice

    attendee_role = get_role_by_name(interaction.guild, ROLE_EVENT_ATTENDEE_NAME)
    if attendee_role:
        try:
            if choice in ("yes", "maybe"):
                if attendee_role not in interaction.user.roles:
                    await interaction.user.add_roles(attendee_role, reason="Event poll: attending")
            else:
                if attendee_role in interaction.user.roles:
                    await interaction.user.remove_roles(attendee_role, reason="Event poll: not attending")
        except Exception:
            logger.exception("Failed to modify attendee role on vote.")
    save_state(poll_state)
    await interaction.response.send_message(f"Vote recorded: {choice}.", ephemeral=True)

# ----------------------------
# Background tasks
# ----------------------------
async def start_background_tasks_once():
    global background_tasks_started
    async with start_lock:
        if background_tasks_started:
            return
        background_tasks_started = True
        asyncio.create_task(daily_14_reminder())
        asyncio.create_task(daily_18_poll())
        asyncio.create_task(reminder_sequence_loop())
        if ENABLE_DAILY_PING_ASSIGNMENT:
            asyncio.create_task(daily_ping_assigner())

async def daily_14_reminder():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(TZ)
        next_run = now.replace(hour=14, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        wait = (next_run - now).total_seconds()
        await asyncio.sleep(wait)

        channel = bot.get_channel(POLL_CHANNEL_ID)
        if channel:
            content = ""
            content = "@everyone Tournament starts in one hour!"  # simple, fans can modify per server needs
            try:
                await channel.send(content, allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=False))
            except Exception:
                logger.exception("Failed to send 14:00 reminder.")
        else:
            logger.warning("14:00 reminder: Poll channel not found.")

async def daily_18_poll():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(TZ)
        next_run = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        wait = (next_run - now).total_seconds()
        await asyncio.sleep(wait)

        channel = bot.get_channel(POLL_CHANNEL_ID)
        if not channel:
            logger.warning("18:00 poll: Poll channel not found.")
            continue

        # Reset poll state for new poll
        poll_state["poll_message_id"] = None
        poll_state["votes"] = {}
        save_state(poll_state)

        poll_text = (
            "Poll: Will you attend the event?\n\n"
            "Use the buttons to vote.\n"
            "✅ I'll be there\n"
            "❓ Maybe\n"
            "❌ Can't do it"
        )
        view = PollView()
        try:
            prev_id = poll_state.get("poll_message_id")
            if prev_id:
                try:
                    prev_msg = await channel.fetch_message(prev_id)
                    if prev_msg:
                        await prev_msg.delete()
                except Exception:
                    logger.debug("Could not delete previous poll message (may be too old or missing).")
            poll_msg = await channel.send(poll_text, view=view)
            poll_state["poll_message_id"] = poll_msg.id
            save_state(poll_state)
        except Exception:
            logger.exception("Failed to create attendance poll at 18:00.")

async def reminder_sequence_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(TZ)
        next_boundary = next_quarter_hour(now)
        sleep_delta = (next_boundary - now).total_seconds()
        if sleep_delta > 0:
            await asyncio.sleep(sleep_delta)

        channel = bot.get_channel(REMINDER_CHANNEL_ID)
        if channel and channel.guild:
            ping_role = get_role_by_name(channel.guild, ROLE_EVENT_PING_NAME)
            if ping_role:
                content = f"{ping_role.mention} Tournament reminder: starting soon."
                allowed = discord.AllowedMentions(roles=True, everyone=False, users=False)
            else:
                content = "Tournament reminder: starting soon."
                allowed = discord.AllowedMentions.none()
            try:
                await channel.send(content, allowed_mentions=allowed)
            except Exception:
                logger.exception("Failed to send 15-minute reminder.")
        else:
            logger.warning("Reminder channel not found or not in a guild.")

async def daily_ping_assigner():
    """Optional: assign Event Ping! role to all members at 18:00 daily."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(TZ)
        next_run = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        wait = (next_run - now).total_seconds()
        await asyncio.sleep(wait)

        channel = bot.get_channel(REMINDER_CHANNEL_ID)
        if not channel or not channel.guild:
            logger.warning("Ping assigner: channel or guild not found.")
            continue

        role = get_role_by_name(channel.guild, ROLE_EVENT_PING_NAME)
        if not role:
            logger.info("Ping assigner: role not found; skipping.")
            continue

        try:
            for member in channel.guild.members:
                if member.bot:
                    continue
                if role not in member.roles:
                    try:
                        await member.add_roles(role, reason="Daily 18:00 mass ping assignment")
                    except Exception:
                        pass  # Individual failures should be logged
            logger.info("Daily ping assignment completed.")
        except Exception:
            logger.exception("Failed during daily ping assignment.")

def next_quarter_hour(dt: datetime) -> datetime:
    """Return the next 15-minute boundary (00, 15, 30, 45) in the same timezone."""
    if dt.minute % 15 == 0 and dt.second == 0 and dt.microsecond == 0:
        next_min = dt.minute + 15
    else:
        next_min = ((dt.minute // 15) + 1) * 15
    next_dt = dt.replace(second=0, microsecond=0, minute=next_min)
    if next_dt <= dt:
        next_dt += timedelta(minutes=15)
    return next_dt

# ----------------------------
# Events
# ----------------------------
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} - ready. Guilds: {len(bot.guilds)}")

    # Restore persistent poll view if a poll message exists
    if POLL_CHANNEL_ID:
        try:
            channel = bot.get_channel(POLL_CHANNEL_ID)
            if channel and poll_state.get("poll_message_id"):
                try:
                    poll_msg = await channel.fetch_message(poll_state["poll_message_id"])
                    if poll_msg:
                        bot.add_view(PollView())
                        logger.info("Persistent poll view attached to existing message.")
                except Exception:
                    logger.debug("Could not attach poll view to existing message (may be too old or missing).")
        except Exception:
            logger.exception("Error while attempting to restore persistent poll view.")

    await start_background_tasks_once()

# ----------------------------
# Admin or utility commands
# ----------------------------
@bot.command()
@commands.has_permissions(administrator=True)
async def event_ping(ctx):
    """Manually ping the Event Ping! role to announce a tournament."""
    guild = ctx.guild
    if not guild:
        await ctx.send("This command can only be used in a server.")
        return
    role = get_role_by_name(guild, ROLE_EVENT_PING_NAME)
    if not role:
        await ctx.send(f"Role '{ROLE_EVENT_PING_NAME}' not found.")
        return
    await ctx.send(role.mention, allowed_mentions=discord.AllowedMentions(roles=True))

@bot.command()
@commands.has_permissions(manage_guild=True)
async def poll_now(ctx):
    channel = bot.get_channel(POLL_CHANNEL_ID)
    if not channel:
        await ctx.send("Poll channel not found.")
        return
    poll_state["poll_message_id"] = None
    poll_state["votes"] = {}
    poll_text = (
        "Poll: Will you attend the event?\n\n"
        "Use the buttons to vote.\n"
        "✅ I'll be there\n"
        "❓ Maybe\n"
        "❌ Can't do it"
    )
    view = PollView()
    poll_msg = await channel.send(poll_text, view=view)
    poll_state["poll_message_id"] = poll_msg.id
    save_state(poll_state)
    await ctx.send("New poll created.")

# ----------------------------
# Run
# ----------------------------
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        logger.info("Shutting down due to KeyboardInterrupt.")
    except Exception:
        logger.exception("Unhandled exception in bot run.")
