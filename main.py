import asyncio
import logging
import os
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from discord import app_commands

# ===== User-configurable URLs (replace easily) =====
REMINDER_IMAGE_URL = ""
POLL_IMAGE_URL = ""

# ===== Environment =====
TOKEN = os.getenv("TOKEN")

# ===== Timezone =====
LONDON_TZ = ZoneInfo("Europe/London")

# ===== Channel and role constants (adjust IDs if needed) =====
REMINDER_CHANNEL_ID = 1528940157024206899
REAL_POLL_CHANNEL_ID = 1524445184853803069
# The real poll channel's name per your note can be checked if needed

EVENT_PING_ROLE_NAME = "Event Ping!"
EVENT_ATTEND_ROLE_NAME = "Event Attendee"

# Ephemeral messages
EPHEMERAL_ATTENDING = "I've marked you as attending."
EPHEMERAL_MAYBE = "I've marked you as maybe."
EPHEMERAL_NOT_COMING = "I've removed your event role."

# Test flag file (persists across restarts)
# Removed test-related constants and file usage per request

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger("event_bot")

# Intents
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

# Bot
bot = commands.Bot(command_prefix="!", intents=intents)

# ===== Persistent Views =====
class DisablePingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔔 Disable Event Pings", style=discord.ButtonStyle.primary, custom_id="disable_event_pings")
    async def enable_pings(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user
        if not guild:
            await interaction.response.send_message("This action must be used in a server.", ephemeral=True)
            return

        role = discord.utils.get(guild.roles, name=EVENT_PING_ROLE_NAME)
        if not role:
            await interaction.response.send_message("Event Ping! role not found.", ephemeral=True)
            return

        if role in member.roles:
            try:
                await member.remove_roles(role, reason="User decided to disable event pings")
            except discord.Forbidden:
                await interaction.response.send_message("I can't modify your roles due to permissions.", ephemeral=True)
                return
            except Exception as e:
                logger.exception("Error removing role for %s: %s", member, e)
                await interaction.response.send_message("An error occurred while updating your roles.", ephemeral=True)
                return
            await interaction.response.send_message("Event reminders disabled for you.", ephemeral=True)
        else:
            await interaction.response.send_message("You already have reminders disabled.", ephemeral=True)

class AttendanceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ I’ll be there", style=discord.ButtonStyle.success, custom_id="attend_yes")
    async def attend_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await update_attendance_and_respond(interaction, "yes")

    @discord.ui.button(label="❓ Maybe", style=discord.ButtonStyle.secondary, custom_id="attend_maybe")
    async def attend_maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await update_attendance_and_respond(interaction, "maybe")

    @discord.ui.button(label="❌ I can’t do it", style=discord.ButtonStyle.danger, custom_id="attend_no")
    async def attend_no(self, interaction: discord.Interaction, button):
        await update_attendance_and_respond(interaction, "no")

# In-memory attendance tracking (per-running process)
_attendance_yes = set()
_attendance_maybe = set()
_attendance_no = set()

def format_attendance_list(attendees):
    if not attendees:
        return ""
    return "\n".join(sorted(attendees))

async def _build_poll_embed():
    embed = discord.Embed(
        title="Event Attendance",
        description="Tomorrow’s Event at 3:00 PM",
        color=0x1E90FF
    )
    if POLL_IMAGE_URL:
        embed.set_image(url=POLL_IMAGE_URL)

    yes_list = format_attendance_list(_attendance_yes)
    maybe_list = format_attendance_list(_attendance_maybe)
    no_list = format_attendance_list(_attendance_no)

    embed.add_field(name="✅ Attending", value=yes_list or "_No attendees yet_", inline=False)
    embed.add_field(name="❓ Maybe", value=maybe_list or "_No attendees yet_", inline=False)
    embed.add_field(name="❌ Can’t Attend", value=no_list or "_No attendees yet_", inline=False)

    return embed

async def update_attendance_and_respond(interaction: discord.Interaction, category: str):
    user_name = interaction.user.display_name

    # Move the user from all lists to the selected category
    global _attendance_yes, _attendance_maybe, _attendance_no
    # Remove from all if present
    _attendance_yes.discard(user_name)
    _attendance_maybe.discard(user_name)
    _attendance_no.discard(user_name)

    # Manage Event Attendee role based on selection
    guild = interaction.guild
    if guild:
        attendee_role = discord.utils.get(guild.roles, name=EVENT_ATTEND_ROLE_NAME)
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if attendee_role and member:
            try:
                if category in ("yes", "maybe"):
                    if attendee_role not in member.roles:
                        await member.add_roles(attendee_role, reason="Assigned Event Attendee role via poll")
                elif category == "no":
                    if attendee_role in member.roles:
                        await member.remove_roles(attendee_role, reason="Removed Event Attendee role via poll")
            except Exception as e:
                logger.exception("Failed to modify Event Attendee role for %s: %s", member, e)

    if category == "yes":
        _attendance_yes.add(user_name)
        response = EPHEMERAL_ATTENDING
    elif category == "maybe":
        _attendance_maybe.add(user_name)
        response = EPHEMERAL_MAYBE
    else:
        _attendance_no.add(user_name)
        response = EPHEMERAL_NOT_COMING

    # Edit the original poll message to reflect updated lists
    try:
        if interaction.message:
            new_embed = await _build_poll_embed()
            await interaction.message.edit(embed=new_embed, view=AttendanceView())
        await interaction.response.send_message(response, ephemeral=True)
    except Exception as e:
        logger.exception("Failed to update poll message after attendance change: %s", e)
        await interaction.response.send_message("Update failed, please try again.", ephemeral=True)

async def ensure_role(guild: discord.Guild, role_name: str) -> discord.Role:
    role = discord.utils.get(guild.roles, name=role_name)
    if role is None:
        try:
            role = await guild.create_role(name=role_name, reason="Auto-create for scheduled events")
            logger.info("Created role %s in guild %s", role_name, guild.name)
        except Exception as e:
            logger.exception("Failed to create role %s in %s: %s", role_name, guild.name, e)
            return None
    return role

async def mass_assign_event_ping():
    for guild in bot.guilds:
        role = await ensure_role(guild, EVENT_PING_ROLE_NAME)
        if role is None:
            continue

        me = guild.me
        if role.position >= me.top_role.position:
            logger.warning("Cannot assign role in %s due to role hierarchy.", guild.name)
            continue
        if not guild.me.guild_permissions.manage_roles:
            logger.warning("Missing Manage Roles in %s.", guild.name)
            continue

        for member in guild.members:
            if member.bot:
                continue
            if role not in member.roles:
                try:
                    await member.add_roles(role, reason="Daily mass assignment of Event Ping! role")
                except discord.Forbidden:
                    logger.warning("Permission denied adding Event Ping! to %s in %s", member, guild.name)
                except Exception as e:
                    logger.exception("Error assigning Event Ping! to %s in %s: %s", member, guild.name, e)

async def post_daily_poll():
    channel = bot.get_channel(REAL_POLL_CHANNEL_ID)
    if channel is None:
        logger.warning("Poll channel with ID %s not found.", REAL_POLL_CHANNEL_ID)
        return

    # Build and send the poll embed with current attendee lists
    new_embed = await _build_poll_embed()
    await channel.send("@everyone", embed=new_embed, view=AttendanceView())

async def mass_role_once_on_start():
    await mass_assign_event_ping()

async def wait_until_next_london_time(target_hour: int, target_minute: int) -> None:
    now = datetime.now(tz=LONDON_TZ)
    target_today = now.date()
    target_dt = datetime.combine(target_today, time(target_hour, target_minute, tzinfo=LONDON_TZ))
    if now >= target_dt:
        target_dt = datetime.combine(target_today, time(target_hour, target_minute, tzinfo=LONDON_TZ)) + timedelta(days=1)
    delta = (target_dt - now).total_seconds()
    if delta > 0:
        await asyncio.sleep(delta)

class Scheduler:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._poll_loop_task = None
        self._reminder_loop_task = None
        # Removed _first_reminder_sent and related startup flags

    async def start(self):
        self._poll_loop_task = self.bot.loop.create_task(self._poll_loop())
        self._reminder_loop_task = self.bot.loop.create_task(self._reminder_loop())

        asyncio.create_task(self._startup_poll_and_mass())

    async def _startup_poll_and_mass(self):
        # Ensure this runs once on startup
        try:
            await mass_assign_event_ping()
            await post_daily_poll()
        except Exception as e:
            logger.exception("Error during startup poll/mass actions: %s", e)

    async def _poll_loop(self):
        while True:
            try:
                await wait_until_next_london_time(18, 0)
                await post_daily_poll()
            except Exception as e:
                logger.exception("Error in poll loop: %s", e)
            await asyncio.sleep(24 * 60 * 60)

    async def _reminder_loop(self):
        # 15-minute reminders in REMINDER_CHANNEL_ID
        while True:
            try:
                now = datetime.now(tz=LONDON_TZ)

                # Compute next reminder time anchored to current time
                if now.time() < time(0, 45):
                    # First reminder targets 00:45 today
                    first_target = datetime.combine(now.date(), time(0, 45, tzinfo=LONDON_TZ))
                    delta = (first_target - now).total_seconds()
                    if delta < 0:
                        delta = 0
                else:
                    # Next 15-minute boundary after now
                    minute = (now.minute // 15) * 15
                    next_run = now.replace(minute=minute, second=0, microsecond=0) + timedelta(minutes=15)
                    delta = (next_run - now).total_seconds()
                    if delta < 0:
                        delta = 0

                await asyncio.sleep(delta)

                channel = bot.get_channel(REMINDER_CHANNEL_ID)
                if channel:
                    embed_title = "Event Reminders"
                    # Do not include channel mentions in title or description
                    role = None
                    if channel.guild:
                        role = discord.utils.get(channel.guild.roles, name=EVENT_PING_ROLE_NAME)
                    role_mention = role.mention if role is not None else ""

                    embed = discord.Embed(
                        title=embed_title,
                        description="Remember to vote in <#1524445184853803069>",
                        color=0xFFA500
                    )
                    if REMINDER_IMAGE_URL:
                        embed.set_image(url=REMINDER_IMAGE_URL)
                    view = DisablePingsView()
                    # Send with role mention in content only
                    content = role_mention
                    # Restore missing content variable as required
                    await channel.send(content, embed=embed, view=view, allowed_mentions=discord.AllowedMentions(roles=True))
            except Exception as e:
                logger.exception("Error in reminder loop: %s", e)

            # Short sleep to yield control between iterations
            await asyncio.sleep(0)

@bot.event
async def on_ready():
    global scheduler
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if 'scheduler' not in globals() or scheduler is None:
        scheduler = Scheduler(bot)
        await scheduler.start()

@bot.command(name="massping")
@commands.has_permissions(manage_roles=True)
async def cmd_mass_ping(ctx: commands.Context):
    await mass_assign_event_ping()
    await ctx.send("Event Ping! role mass-assignment executed.")

def main():
    if not TOKEN:
        raise SystemExit("TOKEN environment variable not set.")
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
