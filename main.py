import os
import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.utils import get
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

# Configuration: image URLs (configurable at top)
REMINDER_IMAGE_URL = ""
POLL_IMAGE_URL = ""

# Core constants
EVENT_PING_ROLE_NAME = "Event Ping!"
EVENT_ROLE_NAME = "Event role"  # kept from initial requirements, not used directly here unless needed
POLL_CHANNEL_ID = 1524445184853803069  # channel to post poll
 london_tz = ZoneInfo("Europe/London")

# Intents
intents = discord.Intents.default()
intents.members = True  # needed for mass role assignment
intents.message_content = True

# Bot
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None, activity=None)

# Persistent view for poll buttons
class AttendanceView(discord.ui.View):
    def __init__(self, timeout=300):
        super().__init__(timeout=timeout)

    @discord.ui.button(label="I'll be there", style=discord.ButtonStyle.success, emoji="✅")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Thanks! You are marked as attending.", ephemeral=True)

    @discord.ui.button(label="Maybe", style=discord.ButtonStyle.secondary, emoji="❓")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Marked as maybe. We'll update you if needed.", ephemeral=True)

    @discord.ui.button(label="I can't do it", style=discord.ButtonStyle.danger, emoji="❌")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Noted. We'll keep you informed.", ephemeral=True)

# Helpers
async def ensure_role(guild: discord.Guild, role_name: str) -> discord.Role:
    role = get(guild.roles, name=role_name)
    if role is None:
        try:
            role = await guild.create_role(name=role_name, reason="Auto-create for scheduled events")
            print(f"Created role {role_name} in guild {guild.name}")
        except Exception as e:
            print(f"Failed to create role {role_name} in {guild.name}: {e}")
            return None
    return role

async def mass_assign_event_ping():
    for guild in bot.guilds:
        # Ensure Event Ping! role exists
        role = await ensure_role(guild, EVENT_PING_ROLE_NAME)
        if role is None:
            continue

        # Check bot permissions
        me = guild.me
        if role.position >= me.top_role.position:
            print(f"Cannot assign role in {guild.name} due to role hierarchy.")
            continue
        if not guild.me.guild_permissions.manage_roles:
            print(f"Missing Manage Roles permission in {guild.name}.")
            continue

        # Iterate members and assign
        for member in guild.members:
            if member.bot:
                continue
            if role not in member.roles:
                try:
                    await member.add_roles(role, reason="Daily mass assignment of Event Ping! role")
                    # Optional: log per-member assignment or keep silent
                except discord.Forbidden:
                    print(f"Permission denied adding role to {member} in {guild.name}")
                except Exception as e:
                    print(f"Error assigning Event Ping! to {member} in {guild.name}: {e}")

async def post_daily_poll():
    channel = bot.get_channel(POLL_CHANNEL_ID)
    if channel is None:
        print(f"Poll channel with ID {POLL_CHANNEL_ID} not found.")
        return

    # Build embed with image
    embed = discord.Embed(
        title="Event Attendance",
        description="Please indicate your attendance for today's event.",
        color=0x1E90FF
    )
    if POLL_IMAGE_URL:
        embed.set_image(url=POLL_IMAGE_URL)

    await channel.send("@everyone", embed=embed, view=AttendanceView())

async def mass_role_once_on_start():
    # Run once on startup to ensure everyone has the Event Ping! role
    await mass_assign_event_ping()

async def wait_until_next london_time_target(target_hour: int, target_minute: int) -> None:
    now = datetime.now(tz=london_tz)
    target_today = now.date()
    target_dt = datetime.combine(target_today, time(target_hour, target_minute, tzinfo=london_tz))
    if now >= target_dt:
        target_dt = datetime.combine(target_today, time(target_hour, target_minute, tzinfo=london_tz)) + timedelta(days=1)
    delta = (target_dt - now).total_seconds()
    if delta > 0:
        await asyncio.sleep(delta)

# Scheduler-like background tasks
class Scheduler:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._mass_role_task = None
        self._startup_run_done = False
        self._startup_lock = asyncio.Lock()
        self._poll_loop_task = None

    async def start(self):
        # Start background tasks
        self._mass_role_task = self.bot.loop.create_task(self._mass_role_loop())
        self._poll_loop_task = self.bot.loop.create_task(self._poll_loop())
        # Run startup mass role assignment once
        self.bot.loop.create_task(self._mass_role_startup())

    async def _mass_role_startup(self):
        async with self._startup_lock:
            if self._startup_run_done:
                return
            self._startup_run_done = True
        try:
            await mass_role_once_on_start()
        except Exception as e:
            print(f"Error during startup mass role assignment: {e}")

    async def _mass_role_loop(self):
        while True:
            try:
                await wait_until_next london_time_target(18, 0)  # 6:00 PM London
                await mass_assign_event_ping()
            except Exception as e:
                print(f"Error in mass role loop: {e}")
            await asyncio.sleep(24 * 60 * 60)  # 24 hours

    async def _poll_loop(self):
        while True:
            try:
                await wait_until_next london_time_target(18, 0)  # 6:00 PM London for poll as well
                await post_daily_poll()
            except Exception as e:
                print(f"Error in poll loop: {e}")
            await asyncio.sleep(24 * 60 * 60)

# Init and events
scheduler: Scheduler = None

@bot.event
async def on_ready():
    global scheduler
    if not hasattr(bot, "is_ready"):
        bot.is_ready = True
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # Start scheduler if not already
    if scheduler is None:
        scheduler = Scheduler(bot)
        await scheduler.start()

# Optional: command to manually trigger mass role (for testing)
@bot.command(name="massping")
@commands.has_permissions(manage_roles=True)
async def cmd_mass_ping(ctx: commands.Context):
    await mass_assign_event_ping()
    await ctx.send("Event Ping! role mass-assignment executed.", ephemeral=True)

# Basic run
def main():
    token = os.getenv("TOKEN")
    if not token:
        raise SystemExit("TOKEN environment variable not set.")
    # Optional: set a simple presence
    try:
        bot.run(token)
    except KeyboardInterrupt:
        print("Bot shutting down.")

if __name__ == "__main__":
    main()
