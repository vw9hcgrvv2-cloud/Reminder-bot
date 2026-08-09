import discord
from discord import NotFound, Forbidden, HTTPException
from discord.ui import View, Button
import json
import os
from datetime import datetime, timedelta
import shutil
import pytz
import asyncio
import logging
from typing import Optional, Dict, List, Any, Set

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LONDON_TZ = pytz.timezone("Europe/London")
EVENT_CHANNEL_ID = 1524445184853803069
REMINDER_CHANNEL_ID = 1528940157024206899
ATTENDEE_ROLE_NAME = "Event Attendee"
PING_ROLE_NAME = "Event Ping!"
STATE_FILE = "bot_state.json"
REMINDER_KEEP_DAYS = 7
ROLE_BATCH_SIZE = 20
ALLOWED_MENTIONS_EVERYONE = discord.AllowedMentions(everyone=True, roles=True, users=True)
ALLOWED_MENTIONS_NONE = discord.AllowedMentions(everyone=False, roles=False, users=False)


class StateManager:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.lock = asyncio.Lock()
        self.state: Dict[str, Any] = self._load()

    def _default_state(self) -> Dict[str, Any]:
        return {
            "voted_users": {},
            "allowed_to_disable": [],
            "opted_out_users": [],
            "sent_reminders": {},
            "last_reminder_cleanup": None,
            "poll_message_id": None,
            "poll_session_date": None,
            "tomorrow_poll_message_id": None,
            "tomorrow_poll_date": None,
            "last_daily_reset": None
        }

    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self.file_path):
            logger.info("No state file found, creating new state.")
            return self._default_state()
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return self._validate_data(data)
        except json.JSONDecodeError as e:
            logger.error(f"State file corrupted: {e}, resetting.")
            return self._default_state()

    def _validate_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        defaults = self._default_state()
        for key, default_val in defaults.items():
            if key not in data or not isinstance(data[key], type(default_val)):
                data[key] = default_val
        return data

    async def save(self):
        async with self.lock:
            temp_path = f"{self.file_path}.tmp"
            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(self.state, f, indent=2, ensure_ascii=False)
                shutil.move(temp_path, self.file_path)
                logger.debug("State saved successfully.")
            except Exception as e:
                logger.error(f"Failed to save state: {e}", exc_info=True)
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise


class DisablePingView(View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        self.disable_ping = Button(
            label="Disable Event Pings",
            style=discord.ButtonStyle.secondary,
            custom_id="disable_ping_btn"
        )
        self.disable_ping.callback = self.disable_pings
        self.add_item(self.disable_ping)

    async def disable_pings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        logger.info(f"Disable pings pressed by user {interaction.user.id}")

        state = self.bot.state
        user_id = interaction.user.id

        # Fixed logic: Check if user has voted in ANY valid poll (today or tomorrow)
        has_voted = False
        today_str = datetime.now(LONDON_TZ).strftime("%Y-%m-%d")

        if "voted_users" in state.state:
            if str(user_id) in state.state["voted_users"]:
                vote_data = state.state["voted_users"][str(user_id)]
                vote_date = vote_data.get("date")
                if vote_date == today_str or vote_date == (datetime.now(LONDON_TZ) + timedelta(days=1)).strftime("%Y-%m-%d"):
                    has_voted = True

        if not has_voted:
            await interaction.followup.send(
                "❌ You must vote in the active tournament poll first!",
                ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ Could not access server.", ephemeral=True)
            return

        ping_role = discord.utils.get(guild.roles, name=PING_ROLE_NAME)
        if not ping_role:
            await interaction.followup.send("❌ Event Ping role not found.", ephemeral=True)
            return

        try:
            if user_id not in state.state["opted_out_users"]:
                state.state["opted_out_users"].append(user_id)
                await state.save()

            if ping_role in interaction.user.roles:
                await interaction.user.remove_roles(ping_role, reason="User disabled event pings")
                await interaction.followup.send("✅ Event pings disabled successfully!", ephemeral=True)
            else:
                await interaction.followup.send("ℹ️ You already have pings disabled.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error disabling pings for {user_id}: {e}", exc_info=True)
            await interaction.followup.send("⚠️ Failed to update roles, please try again later.", ephemeral=True)


class PollView(View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        self.attendee_btn = Button(
            label="✅ Attending",
            style=discord.ButtonStyle.green,
            custom_id="poll_attendee"
        )
        self.maybe_btn = Button(
            label="❓ Maybe",
            style=discord.ButtonStyle.secondary,
            custom_id="poll_maybe"
        )
        self.absent_btn = Button(
            label="❌ Not attending",
            style=discord.ButtonStyle.red,
            custom_id="poll_absent"
        )
        self.attendee_btn.callback = self.handle_vote
        self.maybe_btn.callback = self.handle_vote
        self.absent_btn.callback = self.handle_vote
        self.add_item(self.attendee_btn)
        self.add_item(self.maybe_btn)
        self.add_item(self.absent_btn)

    async def handle_vote(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        username = str(interaction.user)
        custom_id = interaction.data.get("custom_id")
        logger.info(f"Vote received: user={user_id}({username}), btn={custom_id}")

        state = self.bot.state
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ Server not found.", ephemeral=True)
            return

        attendee_role = discord.utils.get(guild.roles, name=ATTENDEE_ROLE_NAME)
        if not attendee_role:
            await interaction.followup.send("❌ Event Attendee role missing.", ephemeral=True)
            return

        today_str = datetime.now(LONDON_TZ).strftime("%Y-%m-%d")
        vote_choice = {
            "poll_attendee": "attending",
            "poll_maybe": "maybe",
            "poll_absent": "absent"
        }.get(custom_id, None)

        if not vote_choice:
            await interaction.followup.send("❌ Invalid selection.", ephemeral=True)
            return

        try:
            state.state["voted_users"][str(user_id)] = {
                "choice": vote_choice,
                "date": today_str
            }
            if user_id not in state.state["allowed_to_disable"]:
                state.state["allowed_to_disable"].append(user_id)
            await state.save()

            try:
                if vote_choice in ["attending", "maybe"]:
                    if attendee_role not in interaction.user.roles:
                        await asyncio.wait_for(interaction.user.add_roles(attendee_role), timeout=5)
                else:
                    if attendee_role in interaction.user.roles:
                        await asyncio.wait_for(interaction.user.remove_roles(attendee_role), timeout=5)
            except asyncio.TimeoutError:
                logger.warning(f"Role update timed out for user {user_id}")
                await interaction.followup.send("✅ Vote saved! Role update may take a moment.", ephemeral=True)
                return
            except (Forbidden, HTTPException) as e:
                logger.error(f"Role update failed: {e}")

            response_msg = {
                "attending": "✅ Vote recorded: **Attending**\nYou can now disable pings using the button in reminders.",
                "maybe": "✅ Vote recorded: **Maybe**\nYou can now disable pings using the button in reminders.",
                "absent": "✅ Vote recorded: **Not Attending**"
            }
            await interaction.followup.send(response_msg[vote_choice], ephemeral=True)
        except Exception as e:
            logger.error(f"Vote handling error: {e}", exc_info=True)
            await interaction.followup.send("⚠️ Error processing vote. Please try again.", ephemeral=True)


class TomorrowPollView(PollView):
    def __init__(self, bot):
        super().__init__(bot)
        self.attendee_btn.custom_id = "tmr_poll_attendee"
        self.maybe_btn.custom_id = "tmr_poll_maybe"
        self.absent_btn.custom_id = "tmr_poll_absent"

    async def handle_vote(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        username = str(interaction.user)
        custom_id = interaction.data.get("custom_id")
        logger.info(f"Tomorrow poll vote: user={user_id}({username}), btn={custom_id}")

        state = self.bot.state
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ Server not found.", ephemeral=True)
            return

        attendee_role = discord.utils.get(guild.roles, name=ATTENDEE_ROLE_NAME)
        if not attendee_role:
            await interaction.followup.send("❌ Event Attendee role missing.", ephemeral=True)
            return

        tomorrow_str = (datetime.now(LONDON_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        vote_map = {
            "tmr_poll_attendee": "attending",
            "tmr_poll_maybe": "maybe",
            "tmr_poll_absent": "absent"
        }
        vote_choice = vote_map.get(custom_id)

        if not vote_choice:
            await interaction.followup.send("❌ Invalid selection.", ephemeral=True)
            return

        try:
            state.state["voted_users"][str(user_id)] = {
                "choice": vote_choice,
                "date": tomorrow_str
            }
            if user_id not in state.state["allowed_to_disable"]:
                state.state["allowed_to_disable"].append(user_id)
            await state.save()

            try:
                if vote_choice in ["attending", "maybe"]:
                    if attendee_role not in interaction.user.roles:
                        await asyncio.wait_for(interaction.user.add_roles(attendee_role), timeout=5)
                else:
                    if attendee_role in interaction.user.roles:
                        await asyncio.wait_for(interaction.user.remove_roles(attendee_role), timeout=5)
            except asyncio.TimeoutError:
                logger.warning(f"Role update timed out for tomorrow poll user {user_id}")
                await interaction.followup.send("✅ Vote saved! Role update pending.", ephemeral=True)
                return
            except Exception as e:
                logger.error(f"Tomorrow poll role error: {e}")

            resp = {
                "attending": "✅ Vote recorded for **Tomorrow: Attending**",
                "maybe": "✅ Vote recorded for **Tomorrow: Maybe**",
                "absent": "✅ Vote recorded for **Tomorrow: Not Attending**"
            }
            await interaction.followup.send(resp[vote_choice], ephemeral=True)
        except Exception as e:
            logger.error(f"Tomorrow poll error: {e}", exc_info=True)
            await interaction.followup.send("⚠️ Error processing vote.", ephemeral=True)


class TournamentBot(discord.Bot):
    def __init__(self, token: str):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.token = token
        self.state = StateManager(STATE_FILE)
        self.scheduler_running = False

    async def setup_hook(self):
        self.add_view(PollView(self))
        self.add_view(TomorrowPollView(self))
        self.add_view(DisablePingView(self))
        logger.info("Persistent views registered.")

    async def on_ready(self):
        logger.info(f"Bot logged in as {self.user} (ID: {self.user.id})")
        if not self.scheduler_running:
            self.scheduler_running = True
            self.loop.create_task(self.scheduler())
            await self.create_startup_poll()
        await self.cleanup_old_reminders()

    async def get_role_safe(self, guild: discord.Guild, name: str) -> Optional[discord.Role]:
        return discord.utils.get(guild.roles, name=name)

    async def send_reminder_message(self, channel_id: int, message: str, view: Optional[View] = None):
        channel = self.get_channel(channel_id)
        if not channel:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception as e:
                logger.error(f"Could not find channel {channel_id}: {e}")
                return

        guild = channel.guild
        ping_role = await self.get_role_safe(guild, PING_ROLE_NAME)
        mention_obj = ping_role if ping_role else "@everyone"

        try:
            await channel.send(
                content=f"{mention_obj}\n{message}",
                view=view,
                allowed_mentions=ALLOWED_MENTIONS_EVERYONE if ping_role else ALLOWED_MENTIONS_EVERYONE
            )
        except Exception as e:
            logger.error(f"Reminder send failed: {e}", exc_info=True)
            raise

    async def create_daily_poll(self):
        logger.info("Creating daily tournament poll...")
        channel = self.get_channel(EVENT_CHANNEL_ID)
        if not channel:
            channel = await self.fetch_channel(EVENT_CHANNEL_ID)

        embed = discord.Embed(
            title="Today’s Tournament Attendance Poll",
            description="Please vote below to confirm your status for today's tournament.",
            color=discord.Color.blue(),
            timestamp=datetime.now(LONDON_TZ)
        )
        embed.set_footer(text="Voting helps us prepare for the tournament")

        view = PollView(self)
        msg = await channel.send(
            content="@everyone",
            embed=embed,
            view=view,
            allowed_mentions=ALLOWED_MENTIONS_EVERYONE
        )
        self.state.state["poll_message_id"] = msg.id
        self.state.state["poll_session_date"] = datetime.now(LONDON_TZ).strftime("%Y-%m-%d")
        await self.state.save()
        logger.info(f"Daily poll created (ID: {msg.id})")

    async def create_startup_poll(self):
        logger.info("Checking startup tomorrow poll...")
        channel = self.get_channel(EVENT_CHANNEL_ID)
        if not channel:
            try:
                channel = await self.fetch_channel(EVENT_CHANNEL_ID)
            except Exception as e:
                logger.error(f"Startup poll channel error: {e}")
                return

        tomorrow_str = (datetime.now(LONDON_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        existing_id = self.state.state.get("tomorrow_poll_message_id")
        poll_exists = False

        if existing_id:
            try:
                await channel.fetch_message(existing_id)
                poll_exists = True
                logger.info("Existing tomorrow poll found.")
            except (NotFound, Forbidden):
                logger.info("Stored tomorrow poll missing, will recreate.")

        if not poll_exists:
            try:
                embed = discord.Embed(
                    title="Tomorrow’s Tournament Attendance Poll",
                    description="Confirm your status for tomorrow's 15:00 tournament.",
                    color=discord.Color.green(),
                    timestamp=datetime.now(LONDON_TZ)
                )
                view = TomorrowPollView(self)
                msg = await channel.send(
                    content="@everyone",
                    embed=embed,
                    view=view,
                    allowed_mentions=ALLOWED_MENTIONS_EVERYONE
                )
                self.state.state["tomorrow_poll_message_id"] = msg.id
                self.state.state["tomorrow_poll_date"] = tomorrow_str
                await self.state.save()
                logger.info(f"Tomorrow poll created (ID: {msg.id})")
            except Exception as e:
                logger.error(f"Startup poll failed: {e}", exc_info=True)

    async def daily_reset(self):
        now = datetime.now(LONDON_TZ)
        last_reset = self.state.state.get("last_daily_reset")
        if last_reset == now.strftime("%Y-%m-%d"):
            return

        logger.info("Running daily reset...")
        self.state.state["allowed_to_disable"] = []
        self.state.state["last_daily_reset"] = now.strftime("%Y-%m-%d")
        await self.state.save()
        await self.cleanup_old_reminders()

    async def cleanup_old_reminders(self):
        cutoff = datetime.now(LONDON_TZ) - timedelta(days=REMINDER_KEEP_DAYS)
        to_remove = []

        for key in list(self.state.state["sent_reminders"].keys()):
            try:
                r_date_str = key.split("_")[0]
                r_date = datetime.strptime(r_date_str, "%Y-%m-%d").replace(tzinfo=LONDON_TZ)
                if r_date < cutoff:
                    to_remove.append(key)
            except Exception as e:
                logger.debug(f"Invalid reminder key: {key} - {e}")
                to_remove.append(key)

        for key in to_remove:
            del self.state.state["sent_reminders"][key]
        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} old reminders")
            await self.state.save()

    async def scheduler(self):
        logger.info("Scheduler started.")
        reminder_view = DisablePingView(self)

        while True:
            now = datetime.now(LONDON_TZ)
            current_minute = now.minute
            current_hour = now.hour
            today_str = now.strftime("%Y-%m-%d")

            # --------------------------
            # 14:00 Reminder
            # --------------------------
            rem_key_14 = f"{today_str}_1400"
            if current_hour == 14 and current_minute == 0 and rem_key_14 not in self.state.state["sent_reminders"]:
                try:
                    await self.send_reminder_message(
                        REMINDER_CHANNEL_ID,
                        "Tournament starts in 1 hour!",
                        view=reminder_view
                    )
                    self.state.state["sent_reminders"][rem_key_14] = now.isoformat()
                    await self.state.save()
                    logger.info("14:00 reminder sent.")
                except Exception as e:
                    logger.error(f"14:00 reminder failed: {e}")

            # --------------------------
            # 14:15, 14:30, 14:45 Reminders
            # --------------------------
            if current_hour == 14 and current_minute in (15, 30, 45):
                rem_key_q = f"{today_str}_14{current_minute}"
                if rem_key_q not in self.state.state["sent_reminders"]:
                    try:
                        await self.send_reminder_message(
                            REMINDER_CHANNEL_ID,
                            f"Tournament reminder: {current_minute} past the hour.",
                            view=reminder_view
                        )
                        self.state.state["sent_reminders"][rem_key_q] = now.isoformat()
                        await self.state.save()
                        logger.info(f"14:{current_minute} reminder sent.")
                    except Exception as e:
                        logger.error(f"Quarterly reminder failed @14:{current_minute}: {e}")

            # --------------------------
            # Daily Reset @15:00
            # --------------------------
            if current_hour == 15 and current_minute == 0:
                await self.daily_reset()

            # --------------------------
            # Daily Poll @18:00
            # --------------------------
            if current_hour == 18 and current_minute == 0:
                poll_key = f"{today_str}_daily_poll"
                if poll_key not in self.state.state["sent_reminders"]:
                    await self.create_daily_poll()
                    self.state.state["sent_reminders"][poll_key] = now.isoformat()
                    await self.create_startup_poll()
                    await self.state.save()

            # --------------------------
            # Every 60 Minutes Reminders (as requested)
            # --------------------------
            if current_minute == 0:
                hour_key = f"{today_str}_{current_hour}_hourly"
                if hour_key not in self.state.state["sent_reminders"]:
                    try:
                        await self.send_reminder_message(
                            REMINDER_CHANNEL_ID,
                            f"⏰ Hourly reminder: {current_hour}:00 London time.",
                            view=reminder_view
                        )
                        self.state.state["sent_reminders"][hour_key] = now.isoformat()
                        await self.state.save()
                        logger.info(f"Hourly reminder sent @{current_hour}:00")
                    except Exception as e:
                        logger.error(f"Hourly reminder failed: {e}")

            await asyncio.sleep(30)


if __name__ == "__main__":
    BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    if not BOT_TOKEN:
        logger.critical("DISCORD_BOT_TOKEN environment variable not set!")
        exit(1)

    bot = TournamentBot(BOT_TOKEN)
    bot.run(BOT_TOKEN)
