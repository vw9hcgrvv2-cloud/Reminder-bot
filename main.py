import discord
from discord.ui import View, Button
import json
import os
from datetime import datetime, timedelta
import pytz
import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LONDON_TZ = pytz.timezone("Europe/London")
EVENT_CHANNEL_ID = 1524445184853803069
REMINDER_CHANNEL_ID = 1528940157024206899
ATTENDEE_ROLE_NAME = "Event Attendee"
PING_ROLE_NAME = "Event Ping!"
STATE_FILE = "bot_state.json"
REMINDER_KEEP_DAYS = 3

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

ALLOWED_MENTIONS_EVERYONE = discord.AllowedMentions(everyone=True, roles=True)
ALLOWED_MENTIONS_ROLES = discord.AllowedMentions(everyone=False, roles=True)
ALLOWED_MENTIONS_NONE = discord.AllowedMentions(everyone=False, roles=False, users=False)

QUARTER_HOURS = [0, 15, 30, 45]
TOMORROW_POLL_TITLE = "Tomorrow's 3PM Tournament Attendance Poll"
TODAY_POLL_TITLE = "Today's Tournament Attendance Poll"

class StateManager:
    def __init__(self):
        self.state = {
            "poll_session_date": None,
            "current_poll_msg_id": None,
            "voted_users": {},
            "allowed_to_disable": [],
            "last_1400_run_date": None,
            "last_1400_status": None,
            "last_1500_reset_date": None,
            "last_1500_reset_status": None,
            "last_1800_poll_date": None,
            "last_1800_poll_status": None,
            "sent_reminders": {},
            "tomorrow_poll_msg_id": None,
            "tomorrow_poll_created_date": None,
            "tomorrow_poll_status": None,
            "tomorrow_voted_users": {}
        }
        self.load()

    def load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    self.state.update(json.load(f))
            except Exception as e:
                logger.error(f"State load error: {e}")

    def save(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"State save error: {e}")

    def get_today_key(self):
        return datetime.now(LONDON_TZ).strftime("%Y-%m-%d")

    def get_tomorrow_key(self):
        return (datetime.now(LONDON_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")

    def get_reminder_key(self, dt):
        return f"{dt.strftime('%Y-%m-%d')}-{dt.hour:02d}-{dt.minute:02d}"

    def cleanup_old_reminders(self):
        today = datetime.now(LONDON_TZ).date()
        kept = {}
        for key in self.state["sent_reminders"]:
            try:
                date_part = key[:10]
                key_date = datetime.strptime(date_part, "%Y-%m-%d").date()
                days_old = (today - key_date).days
                if days_old <= REMINDER_KEEP_DAYS:
                    kept[key] = self.state["sent_reminders"][key]
            except Exception:
                kept[key] = True
        self.state["sent_reminders"] = kept
        self.save()

state = StateManager()

class PollView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Attending", style=discord.ButtonStyle.Green, custom_id="poll:attending")
    async def attending(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "attending")

    @discord.ui.button(label="❓ Maybe", style=discord.ButtonStyle.Secondary, custom_id="poll:maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "maybe")

    @discord.ui.button(label="❌ Not attending", style=discord.ButtonStyle.Red, custom_id="poll:not_attending")
    async def not_attending(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "not_attending")

    async def handle_vote(self, interaction: discord.Interaction, vote_type: str):
        try:
            today = state.get_today_key()
            user_id = str(interaction.user.id)
            guild = interaction.guild
            if not guild:
                await interaction.response.send_message("Guild not found.", ephemeral=True, allowed_mentions=ALLOWED_MENTIONS_NONE)
                return

            if state.state.get("poll_session_date") != today:
                await interaction.response.send_message("This poll has expired. Please wait for today's new poll.", ephemeral=True, allowed_mentions=ALLOWED_MENTIONS_NONE)
                return

            if interaction.message and state.state.get("current_poll_msg_id") != interaction.message.id:
                await interaction.response.send_message("This poll is no longer active.", ephemeral=True, allowed_mentions=ALLOWED_MENTIONS_NONE)
                return

            attendee_role = discord.utils.get(guild.roles, name=ATTENDEE_ROLE_NAME)

            if vote_type in ("attending", "maybe"):
                if attendee_role and attendee_role not in interaction.user.roles:
                    try:
                        await interaction.user.add_roles(attendee_role)
                    except Exception as e:
                        logger.error(f"Add role error: {e}")
                state.state["voted_users"][user_id] = vote_type
                if user_id not in state.state["allowed_to_disable"]:
                    state.state["allowed_to_disable"].append(user_id)
            else:
                if attendee_role and attendee_role in interaction.user.roles:
                    try:
                        await interaction.user.remove_roles(attendee_role)
                    except Exception as e:
                        logger.error(f"Remove role error: {e}")
                state.state["voted_users"].pop(user_id, None)
                state.state["allowed_to_disable"] = [u for u in state.state["allowed_to_disable"] if u != user_id]

            state.save()
            await interaction.response.send_message(f"Vote recorded: {vote_type.replace('_', ' ').title()}", ephemeral=True, allowed_mentions=ALLOWED_MENTIONS_NONE)
        except Exception as e:
            logger.error(f"Vote handling error: {e}")
            await interaction.response.send_message("An error occurred while recording your vote.", ephemeral=True, allowed_mentions=ALLOWED_MENTIONS_NONE)

class TomorrowPollView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Attending", style=discord.ButtonStyle.Green, custom_id="tomorrow_poll:attending")
    async def attending(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "attending")

    @discord.ui.button(label="❓ Maybe", style=discord.ButtonStyle.Secondary, custom_id="tomorrow_poll:maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "maybe")

    @discord.ui.button(label="❌ Not attending", style=discord.ButtonStyle.Red, custom_id="tomorrow_poll:not_attending")
    async def not_attending(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "not_attending")

    async def handle_vote(self, interaction: discord.Interaction, vote_type: str):
        try:
            tomorrow = state.get_tomorrow_key()
            user_id = str(interaction.user.id)
            guild = interaction.guild
            if not guild:
                await interaction.response.send_message("Guild not found.", ephemeral=True, allowed_mentions=ALLOWED_MENTIONS_NONE)
                return

            if state.state.get("tomorrow_poll_created_date") != tomorrow:
                await interaction.response.send_message("This poll has expired.", ephemeral=True, allowed_mentions=ALLOWED_MENTIONS_NONE)
                return

            if interaction.message and state.state.get("tomorrow_poll_msg_id") != interaction.message.id:
                await interaction.response.send_message("This poll is no longer active.", ephemeral=True, allowed_mentions=ALLOWED_MENTIONS_NONE)
                return

            attendee_role = discord.utils.get(guild.roles, name=ATTENDEE_ROLE_NAME)

            if vote_type in ("attending", "maybe"):
                if attendee_role and attendee_role not in interaction.user.roles:
                    try:
                        await interaction.user.add_roles(attendee_role)
                    except Exception as e:
                        logger.error(f"Add role error: {e}")
                state.state["tomorrow_voted_users"][user_id] = vote_type
                if user_id not in state.state["allowed_to_disable"]:
                    state.state["allowed_to_disable"].append(user_id)
            else:
                if attendee_role and attendee_role in interaction.user.roles:
                    try:
                        await interaction.user.remove_roles(attendee_role)
                    except Exception as e:
                        logger.error(f"Remove role error: {e}")
                state.state["tomorrow_voted_users"].pop(user_id, None)
                state.state["allowed_to_disable"] = [u for u in state.state["allowed_to_disable"] if u != user_id]

            state.save()
            await interaction.response.send_message(f"Vote recorded: {vote_type.replace('_', ' ').title()}", ephemeral=True, allowed_mentions=ALLOWED_MENTIONS_NONE)
        except Exception as e:
            logger.error(f"Tomorrow poll vote error: {e}")
            await interaction.response.send_message("An error occurred while recording your vote.", ephemeral=True, allowed_mentions=ALLOWED_MENTIONS_NONE)

class DisablePingView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Disable Event Pings", style=discord.ButtonStyle.Gray, custom_id="disable_pings")
    async def disable_pings(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            today = state.get_today_key()
            tomorrow = state.get_tomorrow_key()
            poll_date = state.state.get("poll_session_date")
            tomorrow_poll_date = state.state.get("tomorrow_poll_created_date")
            user_id = str(interaction.user.id)
            guild = interaction.guild
            if not guild:
                await interaction.response.send_message("Guild not found.", ephemeral=True, allowed_mentions=ALLOWED_MENTIONS_NONE)
                return

            voted_today = user_id in state.state["voted_users"] and poll_date == today
            voted_tomorrow = user_id in state.state["tomorrow_voted_users"] and tomorrow_poll_date == tomorrow

            if not voted_today and not voted_tomorrow:
                await interaction.response.send_message(
                    "You must vote in a tournament poll before you can disable pings!",
                    ephemeral=True,
                    allowed_mentions=ALLOWED_MENTIONS_NONE
                )
                return

            ping_role = discord.utils.get(guild.roles, name=PING_ROLE_NAME)
            if ping_role and ping_role in interaction.user.roles:
                try:
                    await interaction.user.remove_roles(ping_role)
                    await interaction.response.send_message("✅ Event pings disabled for your account.", ephemeral=True, allowed_mentions=ALLOWED_MENTIONS_NONE)
                except Exception as e:
                    logger.error(f"Disable ping error: {e}")
                    await interaction.response.send_message("Error removing role.", ephemeral=True, allowed_mentions=ALLOWED_MENTIONS_NONE)
            else:
                await interaction.response.send_message("You don't have the Event Ping! role.", ephemeral=True, allowed_mentions=ALLOWED_MENTIONS_NONE)
        except Exception as e:
            logger.error(f"Disable ping button error: {e}")
            await interaction.response.send_message("An error occurred.", ephemeral=True, allowed_mentions=ALLOWED_MENTIONS_NONE)

class TournamentBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.poll_view = PollView()
        self.tomorrow_poll_view = TomorrowPollView()
        self.disable_view = DisablePingView()
        self._scheduler_started = False
        self._startup_poll_done = False

    async def setup_hook(self):
        self.add_view(self.poll_view)
        self.add_view(self.tomorrow_poll_view)
        self.add_view(self.disable_view)

    async def get_channel_safe(self, channel_id):
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception as e:
                logger.warning(f"fetch_channel failed for {channel_id}: {e}")
        return channel

    async def on_ready(self):
        logger.info(f"Logged in as {self.user}")
        if not self._startup_poll_done:
            await self.wait_until_ready()
            self._startup_poll_done = True
            asyncio.create_task(self.create_startup_poll())
        if not self._scheduler_started:
            self._scheduler_started = True
            asyncio.create_task(self.scheduler())
            logger.info("Scheduler started after bot ready")

    async def find_existing_tomorrow_poll(self, channel, tomorrow_date):
        """Search channel for a valid existing tomorrow poll"""
        async for msg in channel.history(limit=20):
            if msg.author == self.user and msg.embeds:
                embed = msg.embeds[0]
                if embed.title == TOMORROW_POLL_TITLE:
                    return msg
        return None

    async def create_startup_poll(self):
        try:
            tomorrow = state.get_tomorrow_key()
            stored_msg_id = state.state.get("tomorrow_poll_msg_id")
            stored_date = state.state.get("tomorrow_poll_created_date")
            valid_msg = None

            channel = await self.get_channel_safe(EVENT_CHANNEL_ID)
            if not channel:
                logger.warning("Event channel not found for startup poll")
                return

            if stored_msg_id and stored_date == tomorrow:
                try:
                    msg = await channel.fetch_message(stored_msg_id)
                    if msg.embeds and msg.embeds[0].title == TOMORROW_POLL_TITLE:
                        valid_msg = msg
                        logger.info("Stored tomorrow poll is valid — reusing existing poll")
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    logger.info("Stored tomorrow poll message missing/invalid — searching channel")

            if not valid_msg:
                valid_msg = await self.find_existing_tomorrow_poll(channel, tomorrow)

            if valid_msg:
                state.state["tomorrow_poll_msg_id"] = valid_msg.id
                state.state["tomorrow_poll_created_date"] = tomorrow
                state.save()
                return

            embed = discord.Embed(title=TOMORROW_POLL_TITLE, color=discord.Color.gold())
            msg = await channel.send(
                content="@everyone",
                embed=embed,
                view=self.tomorrow_poll_view,
                allowed_mentions=ALLOWED_MENTIONS_EVERYONE
            )
            state.state["tomorrow_poll_msg_id"] = msg.id
            state.state["tomorrow_poll_created_date"] = tomorrow
            state.state["tomorrow_poll_status"] = "created"
            state.state["tomorrow_voted_users"] = {}
            state.save()
            logger.info("New tomorrow poll created")
        except Exception as e:
            logger.error(f"Startup poll creation error: {e}")

    async def send_reminder_message(self):
        try:
            channel = await self.get_channel_safe(REMINDER_CHANNEL_ID)
            if not channel:
                logger.warning("Reminder channel not found")
                return False
            ping_role = discord.utils.get(channel.guild.roles, name=PING_ROLE_NAME)
            event_chan_mention = f"<#{EVENT_CHANNEL_ID}>"
            role_mention = ping_role.mention if ping_role else "@Event Ping!"
            content = f"{role_mention} Remember to vote in the upcoming tournament poll in {event_chan_mention}"
            await channel.send(content, view=self.disable_view, allowed_mentions=ALLOWED_MENTIONS_ROLES)
            logger.info("15-minute reminder sent")
            return True
        except Exception as e:
            logger.error(f"Send reminder error: {e}")
            return False

    async def daily_1400_reminder(self):
        channel = await self.get_channel_safe(EVENT_CHANNEL_ID)
        if not channel:
            logger.warning("Event channel not found for 14:00 reminder")
            return
        await channel.send("@everyone Tournament starts in 1 hour!", allowed_mentions=ALLOWED_MENTIONS_EVERYONE)
        logger.info("14:00 reminder sent")

    async def daily_1500_reset(self):
        state.state["voted_users"] = {}
        state.state["tomorrow_voted_users"] = {}
        state.state["allowed_to_disable"] = []
        state.state["poll_session_date"] = None
        state.state["tomorrow_poll_created_date"] = None
        state.state["tomorrow_poll_msg_id"] = None
        state.save()
        logger.info("15:00 daily reset complete — all permissions cleared")

    async def daily_1800_poll(self):
        today = state.get_today_key()
        channel = await self.get_channel_safe(EVENT_CHANNEL_ID)
        if not channel:
            logger.warning("Event channel not found for 18:00 poll")
            return
        embed = discord.Embed(title=TODAY_POLL_TITLE, color=discord.Color.blue())
        msg = await channel.send(
            content="@everyone",
            embed=embed,
            view=self.poll_view,
            allowed_mentions=ALLOWED_MENTIONS_EVERYONE
        )
        state.state["current_poll_msg_id"] = msg.id
        state.state["voted_users"] = {}
        state.state["allowed_to_disable"] = []
        state.state["poll_session_date"] = today
        state.save()
        logger.info("18:00 poll created")

    async def assign_ping_role(self):
        for guild in self.guilds:
            ping_role = discord.utils.get(guild.roles, name=PING_ROLE_NAME)
            if not ping_role:
                logger.warning("Event Ping! role not found")
                continue
            for member in guild.members:
                if not member.bot and ping_role not in member.roles:
                    try:
                        await member.add_roles(ping_role)
                    except Exception as e:
                        logger.error(f"Role assign error for {member.id}: {e}")
        logger.info("Ping roles assigned")

    def get_next_reminder_time(self):
        """Get the next quarter-hour mark. If exactly on one, return it immediately."""
        now = datetime.now(LONDON_TZ)
        minute = now.minute
        second = now.second
        for q in QUARTER_HOURS:
            if minute < q or (minute == q and second == 0):
                next_time = now.replace(minute=q, second=0, microsecond=0)
                if next_time <= now:
                    next_time += timedelta(hours=1)
                return next_time
        next_time = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        return next_time

    async def scheduler(self):
        logger.info("Robust scheduler started — checking tasks every 5 seconds")
        while True:
            try:
                await asyncio.sleep(5)
                now = datetime.now(LONDON_TZ)
                today = state.get_today_key()

                t14 = now.replace(hour=14, minute=0, second=0, microsecond=0)
                if now >= t14 and state.state["last_1400_run_date"] != today:
                    state.state["last_1400_run_date"] = today
                    state.state["last_1400_status"] = "pending"
                    state.save()
                    try:
                        await self.daily_1400_reminder()
                        state.state["last_1400_status"] = "completed"
                        state.save()
                    except Exception as e:
                        logger.error(f"14:00 task failed: {e}")
                        state.state["last_1400_status"] = "failed"
                        state.save()

                t15 = now.replace(hour=15, minute=0, second=0, microsecond=0)
                if now >= t15 and state.state["last_1500_reset_date"] != today:
                    state.state["last_1500_reset_date"] = today
                    state.state["last_1500_reset_status"] = "pending"
                    state.save()
                    try:
                        await self.daily_1500_reset()
                        state.state["last_1500_reset_status"] = "completed"
                        state.save()
                    except Exception as e:
                        logger.error(f"15:00 reset failed: {e}")
                        state.state["last_1500_reset_status"] = "failed"
                        state.save()

                t18 = now.replace(hour=18, minute=0, second=0, microsecond=0)
                if now >= t18 and state.state["last_1800_poll_date"] != today:
                    state.state["last_1800_poll_date"] = today
                    state.state["last_1800_poll_status"] = "pending"
                    state.save()
                    try:
                        await self.daily_1800_poll()
                        await self.assign_ping_role()
                        state.state["last_1800_poll_status"] = "completed"
                        state.save()
                    except Exception as e:
                        logger.error(f"18:00 poll task failed: {e}")
                        state.state["last_1800_poll_status"] = "failed"
                        state.save()

                rem_key = state.get_reminder_key(now.replace(second=0, microsecond=0))
                if now.second < 10 and now.minute in QUARTER_HOURS:
                    if rem_key not in state.state["sent_reminders"]:
                        success = await self.send_reminder_message()
                        if success:
                            state.state["sent_reminders"][rem_key] = True
                            state.save()

                state.cleanup_old_reminders()

            except Exception as e:
                logger.error(f"Scheduler loop error: {e}")
                await asyncio.sleep(30)

if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    if not TOKEN:
        logger.error("No token found in environment variable DISCORD_BOT_TOKEN")
        exit(1)
    bot = TournamentBot()
    bot.run(TOKEN)
