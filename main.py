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
intents.members = True  # ⚠️ ALSO enable in Discord Developer Portal → Bot → Privileged Intents

ALLOWED_MENTIONS_EVERYONE = discord.AllowedMentions(everyone=True, roles=True)
ALLOWED_MENTIONS_ROLES = discord.AllowedMentions(everyone=False, roles=True)
ALLOWED_MENTIONS_NONE = discord.AllowedMentions(everyone=False, roles=False, users=False)

REMINDER_MINUTES = [15, 30, 45]
TOMORROW_POLL_TITLE = "Tomorrow's 3PM Tournament Attendance Poll"
TODAY_POLL_TITLE = "Today's Tournament Attendance Poll"


# ─── SAFE INTERACTION RESPONSE HELPERS ───
async def _safe_defer(interaction: discord.Interaction, ephemeral: bool = True) -> bool:
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral)
            logger.debug("Interaction deferred")
        return True
    except (discord.InteractionResponded, discord.NotFound, discord.HTTPException) as e:
        logger.warning(f"Defer skipped/failed: {type(e).__name__}: {e}")
        return False

async def _safe_followup(interaction: discord.Interaction, content: str, **kwargs):
    kwargs.setdefault("ephemeral", True)
    try:
        await interaction.followup.send(content, **kwargs)
    except discord.NotFound:
        logger.warning("Interaction token expired — cannot send followup")
    except discord.InteractionResponded:
        logger.warning("Interaction already responded — skipping followup")
    except Exception as e:
        logger.error(f"Followup send failed: {type(e).__name__}: {e}")


# ─── STATE MANAGER ───
def _to_int_id(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None

def _save_state_sync(state_dict):
    """Synchronous file write — runs in background thread."""
    temp_file = f"{STATE_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(state_dict, f, indent=2)
    os.replace(temp_file, STATE_FILE)

class StateManager:
    def __init__(self):
        self.state = {
            "poll_session_date": None,
            "current_poll_msg_id": None,
            "voted_users": {},
            "allowed_to_disable": [],
            "opted_out_users": [],
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
                    loaded = json.load(f)
                    self.state.update(loaded)
                self.state["current_poll_msg_id"] = _to_int_id(self.state.get("current_poll_msg_id"))
                self.state["tomorrow_poll_msg_id"] = _to_int_id(self.state.get("tomorrow_poll_msg_id"))
                if "opted_out_users" not in self.state:
                    self.state["opted_out_users"] = []
            except Exception as e:
                logger.error(f"State load error: {e}")

    async def save(self):
        """✅ Non-blocking save — runs file I/O in background thread."""
        try:
            await asyncio.to_thread(_save_state_sync, self.state.copy())
        except Exception as e:
            logger.error(f"State save error: {e}")

    def get_today_key(self):
        return datetime.now(LONDON_TZ).strftime("%Y-%m-%d")

    def get_tomorrow_key(self):
        return (datetime.now(LONDON_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")

    def get_reminder_key(self, dt):
        return f"{dt.strftime('%Y-%m-%d')}-{dt.hour:02d}-{dt.minute:02d}"

    async def cleanup_old_reminders(self):
        cutoff = (datetime.now(LONDON_TZ) - timedelta(days=REMINDER_KEEP_DAYS)).strftime("%Y-%m-%d")
        self.state["sent_reminders"] = {
            k: v for k, v in self.state["sent_reminders"].items()
            if not k.startswith(cutoff)
        }
        await self.save()


state = StateManager()


# ─── SAFE ROLE MANAGEMENT ───
async def _manage_role_safe(member, role, add: bool, timeout_sec: float = 2.5):
    guild = member.guild
    bot_member = guild.me

    if not bot_member.guild_permissions.manage_roles:
        logger.error("Manage Roles permission missing")
        return False, "I need the 'Manage Roles' permission"

    if role.position >= bot_member.top_role.position:
        logger.error(f"Role '{role.name}' is above my highest role")
        return False, f"'{role.name}' is too high for me to manage"

    try:
        if add:
            await asyncio.wait_for(member.add_roles(role), timeout=timeout_sec)
        else:
            await asyncio.wait_for(member.remove_roles(role), timeout=timeout_sec)
        return True, None
    except asyncio.TimeoutError:
        logger.warning("Role update timed out")
        return False, "Server took too long to respond"
    except discord.Forbidden:
        return False, "I don't have permission to modify roles"
    except Exception as e:
        logger.error(f"Role update error: {type(e).__name__}: {e}")
        return False, f"Error: {type(e).__name__}"


# ─── POLL VIEWS ───
class PollView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Attending", style=discord.ButtonStyle.green, custom_id="poll:attending")
    async def attending(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info(f"Button clicked | User: {interaction.user} | Attending")
        await _safe_defer(interaction)
        await self.handle_vote(interaction, "attending")

    @discord.ui.button(label="❓ Maybe", style=discord.ButtonStyle.secondary, custom_id="poll:maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info(f"Button clicked | User: {interaction.user} | Maybe")
        await _safe_defer(interaction)
        await self.handle_vote(interaction, "maybe")

    @discord.ui.button(label="❌ Not attending", style=discord.ButtonStyle.red, custom_id="poll:not_attending")
    async def not_attending(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info(f"Button clicked | User: {interaction.user} | Not attending")
        await _safe_defer(interaction)
        await self.handle_vote(interaction, "not_attending")

    async def handle_vote(self, interaction: discord.Interaction, vote_type: str):
        user_id = str(interaction.user.id)
        today = state.get_today_key()
        guild = interaction.guild

        # ✅ VALIDATE EARLY — before any slow work
        if not guild:
            await _safe_followup(interaction, "Guild not found.")
            return

        if state.state.get("poll_session_date") != today:
            await _safe_followup(interaction, "This poll has expired. Please wait for today's new poll.")
            return

        if interaction.message and state.state.get("current_poll_msg_id") != interaction.message.id:
            await _safe_followup(interaction, "This poll is no longer active.")
            return

        attendee_role = discord.utils.get(guild.roles, name=ATTENDEE_ROLE_NAME)

        # ✅ SAVE STATE
        if vote_type in ("attending", "maybe"):
            state.state["voted_users"][user_id] = vote_type
            if user_id not in state.state["allowed_to_disable"]:
                state.state["allowed_to_disable"].append(user_id)
            if user_id in state.state["opted_out_users"]:
                state.state["opted_out_users"].remove(user_id)
        else:
            state.state["voted_users"].pop(user_id, None)
            state.state["allowed_to_disable"] = [u for u in state.state["allowed_to_disable"] if u != user_id]
        
        # ✅ ASYNC SAVE — does NOT block the interaction
        await state.save()
        logger.info(f"Vote saved | User ID: {user_id} | Vote: {vote_type}")

        # ✅ RESPOND TO USER FIRST — within Discord's 3-second limit
        resp_msg = f"Vote recorded: {vote_type.replace('_', ' ').title()}"
        await _safe_followup(interaction, resp_msg)

        # ✅ ROLE UPDATES HAPPEN AFTER RESPONSE — no longer count toward 3s limit
        if attendee_role:
            try:
                if vote_type in ("attending", "maybe"):
                    if attendee_role not in interaction.user.roles:
                        await _manage_role_safe(interaction.user, attendee_role, add=True)
                else:
                    if attendee_role in interaction.user.roles:
                        await _manage_role_safe(interaction.user, attendee_role, add=False)
            except Exception as e:
                logger.warning(f"Background role update failed: {e}")


class TomorrowPollView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Attending", style=discord.ButtonStyle.green, custom_id="tomorrow_poll:attending")
    async def attending(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info(f"Tomorrow poll | User: {interaction.user} | Attending")
        await _safe_defer(interaction)
        await self.handle_vote(interaction, "attending")

    @discord.ui.button(label="❓ Maybe", style=discord.ButtonStyle.secondary, custom_id="tomorrow_poll:maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info(f"Tomorrow poll | User: {interaction.user} | Maybe")
        await _safe_defer(interaction)
        await self.handle_vote(interaction, "maybe")

    @discord.ui.button(label="❌ Not attending", style=discord.ButtonStyle.red, custom_id="tomorrow_poll:not_attending")
    async def not_attending(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info(f"Tomorrow poll | User: {interaction.user} | Not attending")
        await _safe_defer(interaction)
        await self.handle_vote(interaction, "not_attending")

    async def handle_vote(self, interaction: discord.Interaction, vote_type: str):
        user_id = str(interaction.user.id)
        tomorrow = state.get_tomorrow_key()
        guild = interaction.guild

        # ✅ VALIDATE EARLY
        if not guild:
            await _safe_followup(interaction, "Guild not found.")
            return

        if state.state.get("tomorrow_poll_created_date") != tomorrow:
            await _safe_followup(interaction, "This poll has expired.")
            return

        if interaction.message and state.state.get("tomorrow_poll_msg_id") != interaction.message.id:
            await _safe_followup(interaction, "This poll is no longer active.")
            return

        attendee_role = discord.utils.get(guild.roles, name=ATTENDEE_ROLE_NAME)

        # ✅ SAVE STATE
        if vote_type in ("attending", "maybe"):
            state.state["tomorrow_voted_users"][user_id] = vote_type
            if user_id not in state.state["allowed_to_disable"]:
                state.state["allowed_to_disable"].append(user_id)
            if user_id in state.state["opted_out_users"]:
                state.state["opted_out_users"].remove(user_id)
        else:
            state.state["tomorrow_voted_users"].pop(user_id, None)
            state.state["allowed_to_disable"] = [u for u in state.state["allowed_to_disable"] if u != user_id]
        
        await state.save()
        logger.info(f"Tomorrow vote saved | User ID: {user_id} | Vote: {vote_type}")

        # ✅ RESPOND FIRST — within 3 seconds
        await _safe_followup(interaction,
            f"Vote recorded: {vote_type.replace('_', ' ').title()}")

        # ✅ ROLE UPDATES IN BACKGROUND — no timeout risk
        if attendee_role:
            try:
                if vote_type in ("attending", "maybe"):
                    if attendee_role not in interaction.user.roles:
                        await _manage_role_safe(interaction.user, attendee_role, add=True)
                else:
                    if attendee_role in interaction.user.roles:
                        await _manage_role_safe(interaction.user, attendee_role, add=False)
            except Exception as e:
                logger.warning(f"Background role update failed: {e}")


class DisablePingView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Disable Event Pings", style=discord.ButtonStyle.gray, custom_id="disable_pings")
    async def disable_pings(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info(f"Disable pings | User: {interaction.user}")
        await _safe_defer(interaction)
        await self.handle_disable(interaction)

    async def handle_disable(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        today = state.get_today_key()
        tomorrow = state.get_tomorrow_key()
        poll_date = state.state.get("poll_session_date")
        tomorrow_poll_date = state.state.get("tomorrow_poll_created_date")
        guild = interaction.guild

        if not guild:
            await _safe_followup(interaction, "Guild not found.")
            return

        voted_today = user_id in state.state["voted_users"] and poll_date == today
        voted_tomorrow = user_id in state.state["tomorrow_voted_users"] and tomorrow_poll_date == tomorrow

        if not voted_today and not voted_tomorrow:
            await _safe_followup(interaction, "You must vote in a poll before disabling pings!")
            return

        ping_role = discord.utils.get(guild.roles, name=PING_ROLE_NAME)
        
        # ✅ RESPOND FIRST
        if not (ping_role and ping_role in interaction.user.roles):
            await _safe_followup(interaction, "You don't have the Event Ping! role.")
            return

        success, err_msg = await _manage_role_safe(interaction.user, ping_role, add=False)
        if not success:
            await _safe_followup(interaction, f"Could not remove role: {err_msg}")
            return

        if user_id not in state.state["opted_out_users"]:
            state.state["opted_out_users"].append(user_id)
            await state.save()

        await _safe_followup(interaction,
            "✅ Event pings disabled. You will be re-enrolled when you vote in the next poll.")


# ─── BOT CLASS ───
class TournamentBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.poll_view = PollView()
        self.tomorrow_poll_view = TomorrowPollView()
        self.disable_view = DisablePingView()
        self._scheduler_started = False

    async def setup_hook(self):
        self.add_view(self.poll_view)
        self.add_view(self.tomorrow_poll_view)
        self.add_view(self.disable_view)
        logger.info("Persistent views registered")

    async def on_ready(self):
        logger.info(f"Bot ready as {self.user}")
        if not self._scheduler_started:
            await self.wait_until_ready()
            self._scheduler_started = True
            asyncio.create_task(self.create_startup_poll())
            asyncio.create_task(self.scheduler_loop())

    async def get_event_channel(self):
        channel = self.get_channel(EVENT_CHANNEL_ID)
        if not channel:
            try:
                channel = await self.fetch_channel(EVENT_CHANNEL_ID)
            except Exception as e:
                logger.error(f"Cannot find event channel: {e}")
        return channel

    async def get_reminder_channel(self):
        channel = self.get_channel(REMINDER_CHANNEL_ID)
        if not channel:
            try:
                channel = await self.fetch_channel(REMINDER_CHANNEL_ID)
            except Exception as e:
                logger.error(f"Cannot find reminder channel: {e}")
        return channel

    async def create_startup_poll(self):
        logger.info("Startup poll check started")
        try:
            tomorrow = state.get_tomorrow_key()
            channel = await self.get_event_channel()
            if not channel:
                logger.error("Event channel NOT found")
                return

            stored_id = state.state.get("tomorrow_poll_msg_id")
            stored_date = state.state.get("tomorrow_poll_created_date")
            valid_exists = False

            if stored_id and stored_date == tomorrow:
                try:
                    await channel.fetch_message(stored_id)
                    valid_exists = True
                    logger.info("Existing poll found")
                except (discord.NotFound, discord.Forbidden, ValueError, TypeError):
                    logger.warning("Stored poll missing — clearing old ID")
                    state.state["tomorrow_poll_msg_id"] = None
                    state.state["tomorrow_poll_created_date"] = None
                    await state.save()

            if not valid_exists:
                async for msg in channel.history(limit=20):
                    if msg.author == self.user and msg.embeds and msg.embeds[0].title == TOMORROW_POLL_TITLE:
                        msg_date = msg.created_at.astimezone(LONDON_TZ).strftime("%Y-%m-%d")
                        if msg_date == tomorrow:
                            valid_exists = True
                            state.state["tomorrow_poll_msg_id"] = msg.id
                            state.state["tomorrow_poll_created_date"] = tomorrow
                            await state.save()
                            logger.info("Existing poll found via history")
                            break

            if valid_exists:
                return

            logger.info("Creating new startup poll")
            embed = discord.Embed(title=TOMORROW_POLL_TITLE, color=discord.Color.gold())
            msg = await channel.send(content="@everyone", embed=embed, view=self.tomorrow_poll_view,
                                     allowed_mentions=ALLOWED_MENTIONS_EVERYONE)
            state.state["tomorrow_poll_msg_id"] = msg.id
            state.state["tomorrow_poll_created_date"] = tomorrow
            state.state["tomorrow_voted_users"] = {}
            await state.save()
            logger.info("Startup poll created successfully")

        except Exception as e:
            logger.error(f"Startup poll failed: {type(e).__name__}: {e}")

    async def send_reminder_message(self):
        try:
            channel = await self.get_reminder_channel()
            if not channel:
                logger.warning("Reminder channel not found")
                return False
            ping_role = discord.utils.get(channel.guild.roles, name=PING_ROLE_NAME)
            mention = ping_role.mention if ping_role else "@everyone"
            await channel.send(f"{mention} Don't forget the tournament today at 3PM!",
                               allowed_mentions=ALLOWED_MENTIONS_ROLES)
            logger.info("Reminder sent")
            return True
        except Exception as e:
            logger.error(f"Reminder failed: {e}")
            return False

    async def daily_1400_reminder(self):
        try:
            channel = await self.get_reminder_channel()
            if not channel:
                logger.warning("Reminder channel not found for 14:00 reminder")
                return
            ping_role = discord.utils.get(channel.guild.roles, name=PING_ROLE_NAME)
            mention = ping_role.mention if ping_role else "@everyone"
            await channel.send(f"{mention} Tournament starts in 1 hour!",
                               allowed_mentions=ALLOWED_MENTIONS_ROLES)
            logger.info("14:00 reminder sent")
        except Exception as e:
            logger.error(f"14:00 reminder failed: {e}")
            raise

    async def daily_1500_reset(self):
        state.state["voted_users"] = {}
        state.state["allowed_to_disable"] = []
        state.state["poll_session_date"] = None
        state.state["current_poll_msg_id"] = None
        await state.save()
        logger.info("Daily reset complete")

    async def assign_ping_role_to_eligible(self, guild):
        ping_role = discord.utils.get(guild.roles, name=PING_ROLE_NAME)
        if not ping_role:
            logger.warning("Event Ping! role not found — cannot assign")
            return

        bot_member = guild.me
        if not bot_member.guild_permissions.manage_roles:
            logger.error("Cannot assign Event Ping! — missing Manage Roles permission")
            return
        if ping_role.position >= bot_member.top_role.position:
            logger.error("Cannot assign Event Ping! — role is above bot hierarchy")
            return

        opted_out = set(state.state.get("opted_out_users", []))
        assigned_count = 0

        for member in guild.members:
            if member.bot:
                continue
            if str(member.id) in opted_out:
                continue
            if ping_role in member.roles:
                continue
            try:
                success, _ = await _manage_role_safe(member, ping_role, add=True)
                if success:
                    assigned_count += 1
            except Exception as e:
                logger.debug(f"Could not assign role to {member.id}: {e}")

        logger.info(f"Event Ping! role assigned to {assigned_count} eligible members")

    async def daily_1800_poll(self):
        today = state.get_today_key()
        channel = await self.get_event_channel()
        if not channel:
            logger.warning("No channel for daily poll")
            return
        embed = discord.Embed(title=TODAY_POLL_TITLE, color=discord.Color.blue())
        msg = await channel.send(content="@everyone", embed=embed, view=self.poll_view,
                                 allowed_mentions=ALLOWED_MENTIONS_EVERYONE)
        state.state["current_poll_msg_id"] = msg.id
        state.state["poll_session_date"] = today
        state.state["voted_users"] = {}
        await state.save()
        logger.info("Daily poll created — assigning Event Ping! roles...")

        for guild in self.guilds:
            await self.assign_ping_role_to_eligible(guild)

    async def scheduler_loop(self):
        logger.info("Scheduler started")
        while not self.is_closed():
            try:
                now = datetime.now(LONDON_TZ)
                today = now.strftime("%Y-%m-%d")
                await state.cleanup_old_reminders()

                # ─── 14:00 Main Reminder ───
                t14 = now.replace(hour=14, minute=0, second=0, microsecond=0)
                if now >= t14 and state.state["last_1400_run_date"] != today:
                    if state.state["last_1400_run_date"] == today:
                        await asyncio.sleep(60)
                        continue
                    state.state["last_1400_run_date"] = today
                    state.state["last_1400_status"] = "pending"
                    await state.save()
                    try:
                        await self.daily_1400_reminder()
                        state.state["last_1400_status"] = "completed"
                    except Exception as e:
                        logger.error(f"14:00 task failed: {e}")
                        state.state["last_1400_status"] = "failed"
                    await state.save()

                # ─── 14:15, 14:30, 14:45 Reminders ───
                t1500 = now.replace(hour=15, minute=0, second=0, microsecond=0)
                if now.hour == 14 and now.minute in REMINDER_MINUTES and now < t1500:
                    rem_key = state.get_reminder_key(now.replace(second=0, microsecond=0))
                    if rem_key not in state.state["sent_reminders"]:
                        state.state["sent_reminders"][rem_key] = True
                        await state.save()
                        await self.send_reminder_message()

                # ─── 15:00 Daily Reset ───
                t15 = now.replace(hour=15, minute=0, second=0, microsecond=0)
                if now >= t15 and state.state["last_1500_reset_date"] != today:
                    if state.state["last_1500_reset_date"] == today:
                        await asyncio.sleep(60)
                        continue
                    state.state["last_1500_reset_date"] = today
                    state.state["last_1500_reset_status"] = "pending"
                    await state.save()
                    try:
                        await self.daily_1500_reset()
                        state.state["last_1500_reset_status"] = "completed"
                    except Exception as e:
                        logger.error(f"15:00 task failed: {e}")
                        state.state["last_1500_reset_status"] = "failed"
                    await state.save()

                # ─── 18:00 Daily Poll ───
                t18 = now.replace(hour=18, minute=0, second=0, microsecond=0)
                if now >= t18 and state.state["last_1800_poll_date"] != today:
                    if state.state["last_1800_poll_date"] == today:
                        await asyncio.sleep(60)
                        continue
                    state.state["last_1800_poll_date"] = today
                    state.state["last_1800_poll_status"] = "pending"
                    await state.save()
                    try:
                        await self.daily_1800_poll()
                        state.state["last_1800_poll_status"] = "completed"
                    except Exception as e:
                        logger.error(f"18:00 task failed: {e}")
                        state.state["last_1800_poll_status"] = "failed"
                    await state.save()

                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                await asyncio.sleep(60)


if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    if not TOKEN:
        logger.error("DISCORD_BOT_TOKEN environment variable not set")
        exit(1)
    bot = TournamentBot()
    bot.run(TOKEN)
