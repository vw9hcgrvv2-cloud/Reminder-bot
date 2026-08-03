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
intents.members = True  # ⚠️ ENABLE in Discord Developer Portal!

ALLOWED_MENTIONS_EVERYONE = discord.AllowedMentions(everyone=True, roles=True)
ALLOWED_MENTIONS_ROLES = discord.AllowedMentions(everyone=False, roles=True)

REMINDER_MINUTES = [15, 30, 45]
TOMORROW_POLL_TITLE = "Tomorrow's 3PM Tournament Attendance Poll"
TODAY_POLL_TITLE = "Today's Tournament Attendance Poll"


# ─── STATE MANAGER ───
def _to_int_id(value):
    return None if value in (None, "") else int(value) if str(value).strip().isdigit() else None

def _save_sync(state_dict):
    with open(f"{STATE_FILE}.tmp", "w", encoding="utf-8") as f:
        json.dump(state_dict, f, indent=2)
    os.replace(f"{STATE_FILE}.tmp", STATE_FILE)

class StateManager:
    def __init__(self):
        self.state = {
            "poll_session_date": None, "current_poll_msg_id": None, "voted_users": {},
            "allowed_to_disable": [], "opted_out_users": [],
            "last_1400_run_date": None, "last_1400_status": None,
            "last_1500_reset_date": None, "last_1500_reset_status": None,
            "last_1800_poll_date": None, "last_1800_poll_status": None,
            "sent_reminders": {}, "tomorrow_poll_msg_id": None,
            "tomorrow_poll_created_date": None, "tomorrow_voted_users": {}
        }
        self.load()

    def load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    self.state.update(json.load(f))
                self.state["current_poll_msg_id"] = _to_int_id(self.state.get("current_poll_msg_id"))
                self.state["tomorrow_poll_msg_id"] = _to_int_id(self.state.get("tomorrow_poll_msg_id"))
                if "opted_out_users" not in self.state:
                    self.state["opted_out_users"] = []
            except Exception as e:
                logger.error(f"Load error: {e}")

    async def save(self):
        try:
            await asyncio.to_thread(_save_sync, self.state.copy())
        except Exception as e:
            logger.error(f"Save error: {e}")

    def get_today_key(self):
        return datetime.now(LONDON_TZ).strftime("%Y-%m-%d")
    def get_tomorrow_key(self):
        return (datetime.now(LONDON_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    def get_reminder_key(self, dt):
        return f"{dt.strftime('%Y-%m-%d')}-{dt.hour:02d}-{dt.minute:02d}"

    async def cleanup_old_reminders(self):
        cutoff = (datetime.now(LONDON_TZ) - timedelta(days=REMINDER_KEEP_DAYS)).strftime("%Y-%m-%d")
        self.state["sent_reminders"] = {k:v for k,v in self.state["sent_reminders"].items() if not k.startswith(cutoff)}
        await self.save()

state = StateManager()


# ─── ROLE HELPER ───
async def _update_role(member, role, add: bool):
    try:
        if add:
            await asyncio.wait_for(member.add_roles(role), timeout=2.0)
        else:
            await asyncio.wait_for(member.remove_roles(role), timeout=2.0)
        return True
    except Exception as e:
        logger.debug(f"Role fail: {e}")
        return False


# ─── ⚡ POLL VIEWS ───
class PollView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Attending", style=discord.ButtonStyle.green, custom_id="poll:attending")
    async def attending(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ⚡ FIRST LINE — ACKNOWLEDGE BUTTON CLICK INSTANTLY
        if not interaction.response.is_done():
            await interaction.response.defer_update()
        await self._do_vote(interaction, "attending")

    @discord.ui.button(label="❓ Maybe", style=discord.ButtonStyle.secondary, custom_id="poll:maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.response.is_done():
            await interaction.response.defer_update()
        await self._do_vote(interaction, "maybe")

    @discord.ui.button(label="❌ Not attending", style=discord.ButtonStyle.red, custom_id="poll:not_attending")
    async def not_attending(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.response.is_done():
            await interaction.response.defer_update()
        await self._do_vote(interaction, "not_attending")

    async def _do_vote(self, interaction, vote_type):
        user_id = str(interaction.user.id)
        today = state.get_today_key()
        guild = interaction.guild
        attendee_role = discord.utils.get(guild.roles, name=ATTENDEE_ROLE_NAME) if guild else None

        # Validate
        if not guild or state.state.get("poll_session_date") != today:
            await interaction.followup.send("Poll expired.", ephemeral=True)
            return
        if interaction.message and state.state.get("current_poll_msg_id") != interaction.message.id:
            await interaction.followup.send("Wrong poll.", ephemeral=True)
            return

        # Save vote
        if vote_type in ("attending", "maybe"):
            state.state["voted_users"][user_id] = vote_type
            if user_id not in state.state["allowed_to_disable"]:
                state.state["allowed_to_disable"].append(user_id)
            if user_id in state.state["opted_out_users"]:
                state.state["opted_out_users"].remove(user_id)
        else:
            state.state["voted_users"].pop(user_id, None)
            state.state["allowed_to_disable"] = [u for u in state.state["allowed_to_disable"] if u != user_id]
        
        await state.save()
        await interaction.followup.send(f"✅ Vote: {vote_type.replace('_',' ').title()}", ephemeral=True)

        # 🧵 ROLE UPDATE — FIRE AND FORGET (NO BLOCKING)
        if attendee_role:
            if vote_type in ("attending", "maybe"):
                if attendee_role not in interaction.user.roles:
                    asyncio.create_task(_update_role(interaction.user, attendee_role, add=True))
            else:
                if attendee_role in interaction.user.roles:
                    asyncio.create_task(_update_role(interaction.user, attendee_role, add=False))


class TomorrowPollView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Attending", style=discord.ButtonStyle.green, custom_id="tomorrow_poll:attending")
    async def attending(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.response.is_done():
            await interaction.response.defer_update()
        await self._do_vote(interaction, "attending")

    @discord.ui.button(label="❓ Maybe", style=discord.ButtonStyle.secondary, custom_id="tomorrow_poll:maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.response.is_done():
            await interaction.response.defer_update()
        await self._do_vote(interaction, "maybe")

    @discord.ui.button(label="❌ Not attending", style=discord.ButtonStyle.red, custom_id="tomorrow_poll:not_attending")
    async def not_attending(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.response.is_done():
            await interaction.response.defer_update()
        await self._do_vote(interaction, "not_attending")

    async def _do_vote(self, interaction, vote_type):
        user_id = str(interaction.user.id)
        tomorrow = state.get_tomorrow_key()
        guild = interaction.guild
        attendee_role = discord.utils.get(guild.roles, name=ATTENDEE_ROLE_NAME) if guild else None

        if not guild or state.state.get("tomorrow_poll_created_date") != tomorrow:
            await interaction.followup.send("Poll expired.", ephemeral=True)
            return
        if interaction.message and state.state.get("tomorrow_poll_msg_id") != interaction.message.id:
            await interaction.followup.send("Wrong poll.", ephemeral=True)
            return

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
        await interaction.followup.send(f"✅ Vote: {vote_type.replace('_',' ').title()}", ephemeral=True)

        if attendee_role:
            if vote_type in ("attending", "maybe"):
                if attendee_role not in interaction.user.roles:
                    asyncio.create_task(_update_role(interaction.user, attendee_role, add=True))
            else:
                if attendee_role in interaction.user.roles:
                    asyncio.create_task(_update_role(interaction.user, attendee_role, add=False))


class DisablePingView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Disable Event Pings", style=discord.ButtonStyle.gray, custom_id="disable_pings")
    async def disable_pings(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.response.is_done():
            await interaction.response.defer_update()
        
        user_id = str(interaction.user.id)
        today = state.get_today_key()
        tomorrow = state.get_tomorrow_key()
        voted_today = user_id in state.state["voted_users"] and state.state.get("poll_session_date") == today
        voted_tomorrow = user_id in state.state["tomorrow_voted_users"] and state.state.get("tomorrow_poll_created_date") == tomorrow

        if not voted_today and not voted_tomorrow:
            await interaction.followup.send("Vote first!", ephemeral=True)
            return

        ping_role = discord.utils.get(interaction.guild.roles, name=PING_ROLE_NAME) if interaction.guild else None
        if not ping_role or ping_role not in interaction.user.roles:
            await interaction.followup.send("No role to remove.", ephemeral=True)
            return

        ok = await _update_role(interaction.user, ping_role, add=False)
        if not ok:
            await interaction.followup.send("❌ Failed to remove role.", ephemeral=True)
            return

        if user_id not in state.state["opted_out_users"]:
            state.state["opted_out_users"].append(user_id)
            await state.save()

        await interaction.followup.send("✅ Pings disabled.", ephemeral=True)


# ─── BOT CLASS — FIXED VIEW CREATION ───
class TournamentBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        # ✅ DECLARE PLACEHOLDERS ONLY — NO VIEW CREATION HERE
        self.poll_view = None
        self.tomorrow_poll_view = None
        self.disable_view = None
        self._scheduler_started = False

    async def setup_hook(self):
        # ✅ CREATE + REGISTER VIEWS HERE — EVENT LOOP EXISTS NOW
        self.poll_view = PollView()
        self.tomorrow_poll_view = TomorrowPollView()
        self.disable_view = DisablePingView()
        
        self.add_view(self.poll_view)
        self.add_view(self.tomorrow_poll_view)
        self.add_view(self.disable_view)
        logger.info("✅ Persistent views registered")

    async def on_ready(self):
        logger.info(f"✅ Bot ready: {self.user}")
        if not self._scheduler_started:
            await self.wait_until_ready()
            self._scheduler_started = True
            asyncio.create_task(self._startup_poll())
            asyncio.create_task(self._scheduler())

    async def _get_event_ch(self):
        return self.get_channel(EVENT_CHANNEL_ID) or await self.fetch_channel(EVENT_CHANNEL_ID)
    async def _get_remind_ch(self):
        return self.get_channel(REMINDER_CHANNEL_ID) or await self.fetch_channel(REMINDER_CHANNEL_ID)

    async def _startup_poll(self):
        try:
            tomorrow = state.get_tomorrow_key()
            ch = await self._get_event_ch()
            if not ch: return

            stored_id = state.state.get("tomorrow_poll_msg_id")
            stored_date = state.state.get("tomorrow_poll_created_date")
            valid = False

            if stored_id and stored_date == tomorrow:
                try:
                    await ch.fetch_message(stored_id)
                    valid = True
                except:
                    state.state["tomorrow_poll_msg_id"] = None
                    state.state["tomorrow_poll_created_date"] = None
                    await state.save()

            if not valid:
                async for msg in ch.history(limit=20):
                    if msg.author == self.user and msg.embeds and msg.embeds[0].title == TOMORROW_POLL_TITLE:
                        if msg.created_at.astimezone(LONDON_TZ).strftime("%Y-%m-%d") == tomorrow:
                            valid = True
                            state.state["tomorrow_poll_msg_id"] = msg.id
                            state.state["tomorrow_poll_created_date"] = tomorrow
                            await state.save()
                            break

            if valid: return

            embed = discord.Embed(title=TOMORROW_POLL_TITLE, color=discord.Color.gold())
            msg = await ch.send(content="@everyone", embed=embed, view=self.tomorrow_poll_view,
                                allowed_mentions=ALLOWED_MENTIONS_EVERYONE)
            state.state["tomorrow_poll_msg_id"] = msg.id
            state.state["tomorrow_poll_created_date"] = tomorrow
            state.state["tomorrow_voted_users"] = {}
            await state.save()
            logger.info("✅ Startup poll created")
        except Exception as e:
            logger.error(f"Startup poll fail: {e}")

    async def _send_remind(self):
        try:
            ch = await self._get_remind_ch()
            ping_role = discord.utils.get(ch.guild.roles, name=PING_ROLE_NAME)
            mention = ping_role.mention if ping_role else "@everyone"
            await ch.send(f"{mention} Tournament today at 3PM!", allowed_mentions=ALLOWED_MENTIONS_ROLES)
            return True
        except Exception as e:
            logger.error(f"Remind fail: {e}")
            return False

    async def _1400_task(self):
        try:
            ch = await self._get_remind_ch()
            ping_role = discord.utils.get(ch.guild.roles, name=PING_ROLE_NAME)
            mention = ping_role.mention if ping_role else "@everyone"
            await ch.send(f"{mention} Tournament starts in 1 hour!", allowed_mentions=ALLOWED_MENTIONS_ROLES)
        except Exception as e:
            logger.error(f"14:00 fail: {e}")

    async def _1500_task(self):
        state.state["voted_users"] = {}
        state.state["allowed_to_disable"] = []
        state.state["poll_session_date"] = None
        state.state["current_poll_msg_id"] = None
        await state.save()

    async def _assign_ping_roles(self, guild):
        ping_role = discord.utils.get(guild.roles, name=PING_ROLE_NAME)
        if not ping_role or not guild.me.guild_permissions.manage_roles:
            return
        opted = set(state.state.get("opted_out_users", []))
        for m in guild.members:
            if m.bot or str(m.id) in opted or ping_role in m.roles:
                continue
            if ping_role.position >= guild.me.top_role.position:
                continue
            asyncio.create_task(_update_role(m, ping_role, add=True))

    async def _1800_task(self):
        today = state.get_today_key()
        ch = await self._get_event_ch()
        embed = discord.Embed(title=TODAY_POLL_TITLE, color=discord.Color.blue())
        msg = await ch.send(content="@everyone", embed=embed, view=self.poll_view,
                            allowed_mentions=ALLOWED_MENTIONS_EVERYONE)
        state.state["current_poll_msg_id"] = msg.id
        state.state["poll_session_date"] = today
        state.state["voted_users"] = {}
        await state.save()
        for g in self.guilds:
            asyncio.create_task(self._assign_ping_roles(g))

    async def _scheduler(self):
        logger.info("✅ Scheduler running")
        while not self.is_closed():
            try:
                now = datetime.now(LONDON_TZ)
                today = now.strftime("%Y-%m-%d")
                await state.cleanup_old_reminders()

                t14 = now.replace(hour=14, minute=0, second=0, microsecond=0)
                if now >= t14 and state.state["last_1400_run_date"] != today:
                    state.state["last_1400_run_date"] = today
                    await self._1400_task()
                    state.state["last_1400_status"] = "completed"
                    await state.save()

                t1500 = now.replace(hour=15, minute=0, second=0, microsecond=0)
                if now.hour == 14 and now.minute in REMINDER_MINUTES and now < t1500:
                    key = state.get_reminder_key(now.replace(second=0, microsecond=0))
                    if key not in state.state["sent_reminders"]:
                        state.state["sent_reminders"][key] = True
                        await state.save()
                        asyncio.create_task(self._send_remind())

                t15 = now.replace(hour=15, minute=0, second=0, microsecond=0)
                if now >= t15 and state.state["last_1500_reset_date"] != today:
                    state.state["last_1500_reset_date"] = today
                    await self._1500_task()
                    state.state["last_1500_reset_status"] = "completed"
                    await state.save()

                t18 = now.replace(hour=18, minute=0, second=0, microsecond=0)
                if now >= t18 and state.state["last_1800_poll_date"] != today:
                    state.state["last_1800_poll_date"] = today
                    await self._1800_task()
                    state.state["last_1800_poll_status"] = "completed"
                    await state.save()

                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Schd error: {e}")
                await asyncio.sleep(60)


if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    if not TOKEN:
        logger.error("❌ TOKEN NOT SET")
        exit(1)
    bot = TournamentBot()
    bot.run(TOKEN)
