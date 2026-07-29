import asyncio
from datetime import datetime, timedelta
import pytz
import discord
from discord.ext import commands

# ----------------------------
# Configuration (adjust IDs accordingly)
# ----------------------------
 TOKEN = "YOUR_BOT_TOKEN"

 TZ = pytz.timezone("Europe/London")

 POLL_CHANNEL_ID = 123456789012345678  # Channel where polls are posted
 REMINDER_CHANNEL_ID = 123456789012345679  # Channel where reminders are posted

 ROLE_EVENT_PING_NAME = "Event Ping!"
 ROLE_EVENT_ATTENDEE_NAME = "Event Attendee"

 # If you have actual role IDs, you can resolve by name as fallback
# ----------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Simple in-memory poll state; reset daily when poll is recreated
poll_state = {
    "poll_message_id": None,
    "votes": {},  # user_id -> "yes"/"maybe"/"no"
}

# Helpers
def get_role_by_name(guild: discord.Guild, name: str) -> discord.Role | None:
    for r in guild.roles:
        if r.name == name:
            return r
    return None

async def ensure_event_roles(guild: discord.Guild):
    # Ensure both roles exist; do not mass-assign on startup
    ping = get_role_by_name(guild, ROLE_EVENT_PING_NAME)
    attendee = get_role_by_name(guild, ROLE_EVENT_ATTENDEE_NAME)
    return ping, attendee

def next_quarter_hour(dt: datetime) -> datetime:
    # Returns the next 15-minute boundary (00, 15, 30, 45)
    minute = (dt.minute // 15) * 15
    # Move to the next boundary
    if dt.minute % 15 == 0 and dt.second == 0 and dt.microsecond == 0:
        minute = dt.minute + 15
    else:
        minute = minute + 15
    next_dt = dt.replace(second=0, microsecond=0, minute=minute)
    if next_dt <= dt:
        next_dt += timedelta(minutes=15)
    return next_dt

# UI View for poll with buttons
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
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_vote(interaction, "no")

async def handle_vote(interaction: discord.Interaction, choice: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Votes must be cast in a server.", ephemeral=True)
        return

    user_id = interaction.user.id
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
            pass

    await interaction.response.send_message(f"Vote recorded: {choice}.", ephemeral=True)

# Daily tasks: 14:00 reminder and 18:00 poll creation
async def daily_14_reminder():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(TZ)
        next_run = now.replace(hour=14, minute=0, second=0, microsecond=0)
        next_run = TZ.localize(next_run) if next_run.tzinfo is None else next_run
        if next_run <= now:
            next_run += timedelta(days=1)
        wait = (next_run - now).total_seconds()
        await asyncio.sleep(wait)

        channel = bot.get_channel(POLL_CHANNEL_ID)
        if channel and channel.guild:
            ping_role = get_role_by_name(channel.guild, ROLE_EVENT_PING_NAME)
            content = ""
            if ping_role:
                content = f"{ping_role.mention} Tournament starts in one hour!"
            else:
                content = "@everyone Tournament starts in one hour!"
            await channel.send(content, allowed_mentions=discord.AllowedMentions(roles=True, everyone=True, users=False))

async def daily_18_poll():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(TZ)
        next_run = now.replace(hour=18, minute=0, second=0, microsecond=0)
        next_run = TZ.localize(next_run) if next_run.tzinfo is None else next_run
        if next_run <= now:
            next_run += timedelta(days=1)
        wait = (next_run - now).total_seconds()
        await asyncio.sleep(wait)

        channel = bot.get_channel(POLL_CHANNEL_ID)
        if not channel:
            continue

        # Reset poll state
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
        try:
            poll_msg = await channel.send(poll_text, view=view)
            poll_state["poll_message_id"] = poll_msg.id
        except Exception:
            pass

# Reminder loop: align to next 15-minute boundary and repeat forever
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
                pass
        # Loop continues for the next 15-minute boundary

# Startup / Ready
@bot.event
async def on_ready():
    # Register the persistent view for poll buttons
    bot.add_view(PollView())

    # Start background tasks
    bot.loop.create_task(daily_14_reminder())
    bot.loop.create_task(daily_18_poll())
    bot.loop.create_task(reminder_sequence_loop())

    print(f"Logged in as {bot.user} - ready.")

# Admin command for manual Event Ping! (no startup mass-assign)
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

# Optional: a simple command to force-create a new poll (keeps behavior minimal)
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
    await ctx.send("New poll created.")

bot.run(TOKEN)
