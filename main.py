import asyncio
import logging
import os
from datetime import datetime, timedelta
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
TEST_DONE_FILE = "test_done.flag"
TEST_TIME_HOUR = 1   # 01:00
TEST_TIME_MINUTE = 0  # 01:00

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
    async def disable_pings(self, interaction: discord.Interaction, button: discord.ui.Button):
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
                await member.remove_roles(role, reason="User disabled event pings")
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
        await _handle_attendance(interaction, add=True, category="Yes")

    @discord.ui.button(label="❓ Maybe", style=discord.ButtonStyle.secondary, custom_id="attend_maybe")
    async def attend_maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_attendance(interaction, add=True, category="Maybe")

    @discord.ui.button(label="❌ I can’t do it", style=discord.ButtonStyle.danger, custom_id="attend_no")
    async def attend_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_attendance(interaction, add=False, category="No")

async def _format_attendance_embed(mapping, guilds):
    yes_list = []
    maybe_list = []
    no_list = []

    for user_id, cat in mapping.items():
        member_name = None
        # Try to find member name from any guild; prefer current guild if available
        for g in guilds:
            member = g.get_member(int(user_id))
            if member:
                member_name = member.display_name
                break
        if not member_name:
            member_name = f"User {user_id}"
        if cat == "Yes":
            yes_list.append(member_name)
        elif cat == "Maybe":
            maybe_list.append(member_name)
        elif cat == "No":
            no_list.append(member_name)

    def make_section(title, items):
        lines = []
        for n in items:
            lines.append(f"• {n}")
        body = "\n".join(lines) if lines else ""
        return f"{title}\n{body}" if body else f"{title}\n"

    sections = [
        make_section("✅ Yes", yes_list),
        "",
        make_section("❓ Maybe", maybe_list),
        "",
        make_section("❌ No", no_list),
    ]
    description = "\n".join([s for s in sections if s is not None])
    embed = discord.Embed(title="🏆 Tournament Tomorrow at 3 PM", color=0x3498db)
    embed.description = "Will you be joining us?\n\n" + description if description.strip() else "Will you be joining us?"
    return embed

async def _handle_attendance(interaction: discord.Interaction, add: bool, category: str):
    guild = interaction.guild
    member = interaction.user
    if not guild:
        await interaction.response.send_message("This action must be used in a server.", ephemeral=True)
        return

    # Initialize mapping store on the bot object if not present
    if not hasattr(interaction.client, "attend_mapping"):
        interaction.client.attend_mapping = {}

    mapping = interaction.client.attend_mapping

    # Update mapping based on action
    if add:
        mapping[str(member.id)] = category
        action_msg = EPHEMERAL_ATTENDING
    else:
        # Remove the user from the list if present
        if str(member.id) in mapping:
            mapping.pop(str(member.id))
        action_msg = EPHEMERAL_NOT_COMING

    # Maintain Event Attendee role similarly to previous behavior
    role = discord.utils.get(guild.roles, name=EVENT_ATTEND_ROLE_NAME)
    try:
        if add:
            if role and role not in member.roles:
                await member.add_roles(role, reason="Attendance via poll")
        else:
            if role and role in member.roles:
                await member.remove_roles(role, reason="User cannot attend")
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to modify roles.", ephemeral=True)
        return
    except Exception as e:
        logger.exception("Error updating attendance role: %s", e)
        await interaction.response.send_message("An error occurred.", ephemeral=True)
        return

    # Edit the poll embed to reflect current sign-up status
    try:
        new_embed = await _format_attendance_embed(mapping, interaction.client.guilds)
        await interaction.message.edit(embed=new_embed, view=AttendanceView())
        await interaction.response.send_message(action_msg, ephemeral=True)
    except Exception as e:
        logger.exception("Failed to update attendance embed: %s", e)
        await interaction.response.send_message("An error occurred while updating the sign-up list.", ephemeral=True)

# ===== Scheduling helpers =====
class Scheduler:
    def __init__(self, bot_instance: commands.Bot):
        self.bot = bot_instance
        self.started = False
        self._reminder_task = None
        self._real_poll_task = None
        self._test_poll_task = None
        self._test_done = False

        # In-memory sign-up mapping: user_id (str) -> category ("Yes"/"Maybe"/"No")
        self.attend_mapping = {}

    async def start(self):
        if self.started:
            return
        self.started = True
        self._reminder_task = self.bot.loop.create_task(self._reminder_loop())
        self._real_poll_task = self.bot.loop.create_task(self._real_poll_loop())
        self._test_poll_task = self.bot.loop.create_task(self._test_poll_loop())

    async def _wait_until_next_london_time(self, hour: int, minute: int) -> None:
        now = datetime.now(tz=LONDON_TZ)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        wait = (target - now).total_seconds()
        await asyncio.sleep(max(0, wait))

    async def _send_reminder(self):
        channel = self.bot.get_channel(REMINDER_CHANNEL_ID)
        if not channel or not isinstance(channel, discord.abc.Messageable):
            logger.warning("Reminder channel not found.")
            return

        content = ""
        role = None
        for g in self.bot.guilds:
            r = discord.utils.get(g.roles, name=EVENT_PING_ROLE_NAME)
            if r:
                role = r
                break
        if role:
            content = role.mention

        embed = discord.Embed(title="Event Reminder", color=0x3498db)
        if REMINDER_IMAGE_URL:
            embed.set_image(url=REMINDER_IMAGE_URL)

        try:
            await channel.send(content=content, embed=embed, view=DisablePingsView())
            logger.info("Posted reminder in %s", channel)
        except Exception as e:
            logger.exception("Failed to post reminder: %s", e)

    async def _reminder_loop(self):
        # First reminder at 01:10 AM Europe/London, then every 15 minutes
        await self._wait_until_next_london_time(1, 10)
        while True:
            await self._send_reminder()
            await asyncio.sleep(15 * 60)

    async def _send_real_poll(self):
        channel = self.bot.get_channel(REAL_POLL_CHANNEL_ID)
        if not channel or not isinstance(channel, discord.abc.Messageable):
            logger.warning("Real poll channel not found.")
            return

        # Live sign-up embed with Yes / Maybe / No sections
        embed = await _format_attendance_embed(self.attend_mapping, self.bot.guilds)
        if POLL_IMAGE_URL:
            embed.set_image(url=POLL_IMAGE_URL)

        content = "@everyone"

        view = AttendanceView()  # persistent
        try:
            await channel.send(content=content, embed=embed, view=view)
            logger.info("Posted real poll in %s", channel)
        except Exception as e:
            logger.exception("Failed to post real poll: %s", e)

    async def _real_poll_loop(self):
        while True:
            await self._wait_until_next_london_time(18, 0)  # 18:00 local
            await self._send_real_poll()
            # Wait 24h for next day's poll
            await asyncio.sleep(24 * 60 * 60)

    async def _run_test_once(self):
        # Run only once; controlled by a flag file
        if os.path.exists(TEST_DONE_FILE):
            return

        channel = self.bot.get_channel(REAL_POLL_CHANNEL_ID)
        if not channel or not isinstance(channel, discord.abc.Messageable):
            logger.warning("Test poll channel not found.")
            return

        embed = discord.Embed(
            title="🏆 Tournament Tomorrow at 3 PM",
            description="Will you be joining us?",
            color=0x2ecc71,
        )
        if POLL_IMAGE_URL:
            embed.set_image(url=POLL_IMAGE_URL)

        content = "@everyone"

        # Use the same live sign-up view, but this is a test poll; ensure mapping is updated too
        view = AttendanceView()
        try:
            await channel.send(content=content, embed=embed, view=view)
            logger.info("Posted test poll in %s", channel)
        except Exception as e:
            logger.exception("Failed to post test poll: %s", e)
            return

        # Grant Event Ping! role to everyone if possible
        for guild in self.bot.guilds:
            role = discord.utils.get(guild.roles, name=EVENT_PING_ROLE_NAME)
            if not role:
                try:
                    role = await guild.create_role(name=EVENT_PING_ROLE_NAME, reason="Test: ensure Event Ping! exists")
                except Exception as e:
                    logger.exception("Could not create Event Ping! role in %s: %s", guild.name, e)
                    continue
            for member in guild.members:
                if role not in member.roles:
                    try:
                        await member.add_roles(role, reason="Test: grant Event Ping!")
                    except discord.Forbidden:
                        logger.warning("Missing permissions to add role in %s for %s", guild.name, member)
                    except Exception as e:
                        logger.exception("Error adding role to %s: %s", member, e)

        # Mark test as done
        try:
            with open(TEST_DONE_FILE, "w") as f:
                f.write("done")
        except Exception as e:
            logger.exception("Could not write test_done flag: %s", e)

    async def _test_poll_loop(self):
        # Run once at 01:10 Europe/London
        await self._wait_until_next_london_time(1, 10)
        await self._run_test_once()
        # After running once, keep the loop alive; the test is designed to run once
        while True:
            await asyncio.sleep(60)

# ===== Slash command: /ping for testing =====
@bot.tree.command(name="ping", description="Test ping to verify the bot is online.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!", ephemeral=True)

# ===== Bot events =====
@bot.event
async def on_ready():
    logger.info("Bot ready as %s (ID: %s)", bot.user, bot.user.id)

# ===== Main =====
scheduler = Scheduler(bot)

@bot.event
async def on_connect():
    # Ensure only a single background task is running on reconnect
    if not scheduler.started:
        await scheduler.start()

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("TOKEN environment variable is not set.")

    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.exception("Bot failed to start: %s", e)
