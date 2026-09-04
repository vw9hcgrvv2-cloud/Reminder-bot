import os
import re
import json
import logging
import asyncio
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple, Any, Set
from dataclasses import dataclass, field
from collections import defaultdict

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

# -----------------------------------------------------------------------------
# CONFIGURATION & ENVIRONMENT VARIABLES
# -----------------------------------------------------------------------------
TIMEZONE = ZoneInfo("Europe/London")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# EXACT CHANNEL IDs — FIXED
DAILY_POLL_CHANNEL_ID = 1545264348295987250
EVENT_CHANNEL_ID_A = 1545264346265952367
EVENT_CHANNEL_ID_B = 1545264350393008169
EVENT_CHANNEL_IDS = [EVENT_CHANNEL_ID_A, EVENT_CHANNEL_ID_B]

DAILY_POLL_HOUR = int(os.getenv("DAILY_POLL_HOUR", "10"))
DAILY_POLL_MINUTE = int(os.getenv("DAILY_POLL_MINUTE", "0"))
VOTING_DURATION_MINUTES = int(os.getenv("VOTING_DURATION_MINUTES", "60"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))
STATE_FILE = "state.json"
EVENT_ROLE_NAME = "Event"
REMINDER_INTERVAL_MINUTES = 45
FINAL_REMINDER_MINUTES_BEFORE = 15
MIN_TIME_LEAD_MINUTES = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# DATA STRUCTURES
# -----------------------------------------------------------------------------
@dataclass
class UserVote:
    user_id: int
    username: str
    vote: Optional[bool] = None
    submitted_time: Optional[str] = None
    time_minutes: Optional[int] = None

@dataclass
class AttendanceRecord:
    user_id: int
    username: str
    status: str  # yes / maybe / no

@dataclass
class BotState:
    date_str: str = ""
    poll_message_id: Optional[int] = None
    poll_channel_id: int = DAILY_POLL_CHANNEL_ID
    voting_deadline: Optional[float] = None
    votes: Dict[int, UserVote] = field(default_factory=dict)
    event_confirmed: bool = False
    event_time_minutes: Optional[int] = None
    event_time_str: Optional[str] = None
    attendance_message_ids: List[int] = field(default_factory=list)
    attendance: Dict[int, AttendanceRecord] = field(default_factory=dict)
    reminders_started: bool = False
    last_reminder_sent_minutes: Optional[int] = None
    final_reminder_sent: bool = False
    event_started: bool = False
    daily_reset_done: bool = False

    def reset_daily(self):
        self.date_str = self._today_str()
        self.poll_message_id = None
        self.poll_channel_id = DAILY_POLL_CHANNEL_ID
        self.voting_deadline = None
        self.votes = {}
        self.event_confirmed = False
        self.event_time_minutes = None
        self.event_time_str = None
        self.attendance_message_ids = []
        self.attendance = {}
        self.reminders_started = False
        self.last_reminder_sent_minutes = None
        self.final_reminder_sent = False
        self.event_started = False
        self.daily_reset_done = False

    def _today_str(self) -> str:
        return datetime.now(TIMEZONE).strftime("%Y-%m-%d")

    def is_today(self) -> bool:
        return self.date_str == self._today_str()

# -----------------------------------------------------------------------------
# STATE MANAGEMENT
# -----------------------------------------------------------------------------
class StateManager:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.state = BotState()
        self._lock = asyncio.Lock()

    async def load(self):
        async with self._lock:
            if not os.path.exists(self.file_path):
                logger.info("No state file found, starting fresh.")
                self.state = BotState()
                return
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._load_from_dict(data)
                logger.info("State loaded successfully.")
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.error(f"Corrupted state file: {e}, starting fresh.")
                self.state = BotState()

    async def save(self):
        async with self._lock:
            try:
                data = self._dump_to_dict()
                with open(self.file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception as e:
                logger.error(f"Failed to save state: {e}")

    def _dump_to_dict(self) -> dict:
        return {
            "date_str": self.state.date_str,
            "poll_message_id": self.state.poll_message_id,
            "poll_channel_id": self.state.poll_channel_id,
            "voting_deadline": self.state.voting_deadline,
            "votes": {
                str(uid): {
                    "user_id": v.user_id,
                    "username": v.username,
                    "vote": v.vote,
                    "submitted_time": v.submitted_time,
                    "time_minutes": v.time_minutes
                }
                for uid, v in self.state.votes.items()
            },
            "event_confirmed": self.state.event_confirmed,
            "event_time_minutes": self.state.event_time_minutes,
            "event_time_str": self.state.event_time_str,
            "attendance_message_ids": self.state.attendance_message_ids,
            "attendance": {
                str(uid): {
                    "user_id": a.user_id,
                    "username": a.username,
                    "status": a.status
                }
                for uid, a in self.state.attendance.items()
            },
            "reminders_started": self.state.reminders_started,
            "last_reminder_sent_minutes": self.state.last_reminder_sent_minutes,
            "final_reminder_sent": self.state.final_reminder_sent,
            "event_started": self.state.event_started,
            "daily_reset_done": self.state.daily_reset_done
        }

    def _load_from_dict(self, data: dict):
        self.state = BotState()
        self.state.date_str = data.get("date_str", "")
        self.state.poll_message_id = data.get("poll_message_id")
        self.state.poll_channel_id = data.get("poll_channel_id", DAILY_POLL_CHANNEL_ID)
        self.state.voting_deadline = data.get("voting_deadline")
        self.state.event_confirmed = data.get("event_confirmed", False)
        self.state.event_time_minutes = data.get("event_time_minutes")
        self.state.event_time_str = data.get("event_time_str")
        self.state.attendance_message_ids = data.get("attendance_message_ids", [])
        self.state.reminders_started = data.get("reminders_started", False)
        self.state.last_reminder_sent_minutes = data.get("last_reminder_sent_minutes")
        self.state.final_reminder_sent = data.get("final_reminder_sent", False)
        self.state.event_started = data.get("event_started", False)
        self.state.daily_reset_done = data.get("daily_reset_done", False)

        votes_data = data.get("votes", {})
        for uid_str, vd in votes_data.items():
            uid = int(uid_str)
            self.state.votes[uid] = UserVote(
                user_id=uid,
                username=vd.get("username", "Unknown"),
                vote=vd.get("vote"),
                submitted_time=vd.get("submitted_time"),
                time_minutes=vd.get("time_minutes")
            )

        att_data = data.get("attendance", {})
        for uid_str, ad in att_data.items():
            uid = int(uid_str)
            self.state.attendance[uid] = AttendanceRecord(
                user_id=uid,
                username=ad.get("username", "Unknown"),
                status=ad.get("status", "no")
            )

state_mgr = StateManager(STATE_FILE)

# -----------------------------------------------------------------------------
# TIME PARSING & FORMATTING
# -----------------------------------------------------------------------------
def parse_time_str(time_str: str) -> Optional[int]:
    s = time_str.strip().lower()
    patterns = [
        r"^(\d{1,2}):(\d{2})\s*(am|pm)$",
        r"^(\d{1,2})\.(\d{2})\s*(am|pm)$",
        r"^(\d{1,2})\s*(am|pm)$",
        r"^(\d{1,2}):(\d{2})$",
        r"^(\d{2}):(\d{2})$",
        r"^(\d{1,2})$",
    ]
    for pat in patterns:
        m = re.match(pat, s)
        if not m:
            continue
        groups = m.groups()
        if len(groups) == 3:
            h, mi, per = groups
            h = int(h)
            mi = int(mi) if mi else 0
            if per == "pm" and h != 12:
                h += 12
            if per == "am" and h == 12:
                h = 0
        elif len(groups) == 2:
            h, mi = int(groups[0]), int(groups[1])
            if h < 0 or h > 23 or mi < 0 or mi > 59:
                continue
        elif len(groups) == 1:
            h = int(groups[0])
            mi = 0
            if h < 0 or h > 12:
                continue
        else:
            continue
        if h < 0 or h > 23 or mi < 0 or mi > 59:
            continue
        return h * 60 + mi
    return None

def format_time(minutes: int) -> str:
    h = minutes // 60
    m = minutes % 60
    if h == 0:
        return f"12:{m:02d} AM"
    elif h < 12:
        return f"{h}:{m:02d} AM" if m else f"{h} AM"
    elif h == 12:
        return f"12:{m:02d} PM" if m else "12 PM"
    else:
        return f"{h-12}:{m:02d} PM" if m else f"{h-12} PM"

def average_time(minutes_list: List[int]) -> int:
    if not minutes_list:
        return 0
    rad = [m * (2 * math.pi / 1440) for m in minutes_list]
    avg_sin = sum(math.sin(x) for x in rad) / len(rad)
    avg_cos = sum(math.cos(x) for x in rad) / len(rad)
    avg_rad = math.atan2(avg_sin, avg_cos)
    if avg_rad < 0:
        avg_rad += 2 * math.pi
    raw = (avg_rad * 1440) / (2 * math.pi)
    return round(raw)

def is_time_valid(minutes: int) -> bool:
    now = datetime.now(TIMEZONE)
    now_min = now.hour * 60 + now.minute
    return minutes >= now_min + MIN_TIME_LEAD_MINUTES and minutes <= 23 * 60 + 59

# -----------------------------------------------------------------------------
# ROLE HELPERS
# -----------------------------------------------------------------------------
async def get_event_role(guild: discord.Guild) -> Optional[discord.Role]:
    for role in guild.roles:
        if role.name == EVENT_ROLE_NAME:
            return role
    logger.warning(f"Role '{EVENT_ROLE_NAME}' not found in guild {guild.name}")
    return None

async def apply_event_role(member: discord.Member, add: bool):
    role = await get_event_role(member.guild)
    if not role:
        return
    try:
        if add and role not in member.roles:
            await member.add_roles(role, reason="Event attendance")
        elif not add and role in member.roles:
            await member.remove_roles(role, reason="No longer attending event")
    except discord.Forbidden:
        logger.error(f"Permission error managing role for {member}")
    except Exception as e:
        logger.error(f"Role update error: {e}")

async def remove_role_from_all(guild: discord.Guild):
    role = await get_event_role(guild)
    if not role:
        return
    count = 0
    for member in guild.members:
        if role in member.roles:
            try:
                await member.remove_roles(role, reason="Daily reset")
                count += 1
                if count % 10 == 0:
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Could not remove role from {member}: {e}")
    logger.info(f"Removed Event role from {count} members")

# -----------------------------------------------------------------------------
# EMBED BUILDERS
# -----------------------------------------------------------------------------
def build_poll_embed() -> discord.Embed:
    state = state_mgr.state
    yes_users: List[UserVote] = []
    pending_users: List[UserVote] = []
    no_users: List[UserVote] = []
    for v in state.votes.values():
        if v.vote is True:
            if v.time_minutes is not None:
                yes_users.append(v)
            else:
                pending_users.append(v)
        elif v.vote is False:
            no_users.append(v)
    yes_count = len(yes_users)
    no_count = len(no_users)
    embed = discord.Embed(title="🏆 TOURNAMENT / EVENT TODAY?", color=0x3498db)
    embed.description = "Would you like an event later today?"
    embed.add_field(name=f"✅ YES — {yes_count}", value="\n".join(f"• {u.username}" for u in yes_users) or "—", inline=True)
    if pending_users:
        embed.add_field(name="⏳ Pending Time", value="\n".join(f"• {u.username}" for u in pending_users), inline=True)
    embed.add_field(name=f"❌ NO — {no_count}", value="\n".join(f"• {u.username}" for u in no_users) or "—", inline=True)
    if state.voting_deadline:
        closes_at = datetime.fromtimestamp(state.voting_deadline, tz=TIMEZONE)
        embed.set_footer(text=f"Voting closes at {closes_at.strftime('%H:%M %Z')}")
    return embed

def build_attendance_embed() -> discord.Embed:
    state = state_mgr.state
    yes_list: List[str] = []
    maybe_list: List[str] = []
    no_list: List[str] = []
    for a in state.attendance.values():
        if a.status == "yes":
            yes_list.append(f"• {a.username}")
        elif a.status == "maybe":
            maybe_list.append(f"• {a.username}")
        else:
            no_list.append(f"• {a.username}")
    embed = discord.Embed(title="🏆 EVENT CONFIRMED!", color=0x2ecc71)
    embed.add_field(name="🕐 Event Time", value=state.event_time_str or "—", inline=False)
    embed.add_field(name=f"✅ YES — {len(yes_list)}", value="\n".join(yes_list) or "—", inline=True)
    embed.add_field(name=f"🤔 MAYBE — {len(maybe_list)}", value="\n".join(maybe_list) or "—", inline=True)
    embed.add_field(name=f"❌ NO — {len(no_list)}", value="\n".join(no_list) or "—", inline=True)
    return embed

# -----------------------------------------------------------------------------
# SYNCHRONIZED MESSAGE UPDATER
# -----------------------------------------------------------------------------
async def update_all_attendance_messages():
    """Update ALL attendance poll messages in both channels from shared state"""
    state = state_mgr.state
    if not state.attendance_message_ids:
        return
    embed = build_attendance_embed()
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    updated_ids = []
    for channel_id in EVENT_CHANNEL_IDS:
        channel = guild.get_channel(channel_id)
        if not channel:
            logger.warning(f"Channel {channel_id} not found")
            continue
        for msg_id in list(state.attendance_message_ids):
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(embed=embed, view=AttendanceView() if not state.event_started else None)
                updated_ids.append(msg_id)
            except discord.NotFound:
                logger.warning(f"Message {msg_id} not found in channel {channel_id} — was deleted")
            except discord.Forbidden:
                logger.error(f"Cannot edit message {msg_id} in channel {channel_id} — missing permissions")
            except Exception as e:
                logger.error(f"Failed to update message {msg_id}: {e}")
    state.attendance_message_ids = list(set(updated_ids))
    await state_mgr.save()

async def update_daily_poll_message():
    """Update the daily Yes/No poll message"""
    state = state_mgr.state
    if not state.poll_message_id:
        return
    guild = bot.get_guild(GUILD_ID)
    channel = guild.get_channel(DAILY_POLL_CHANNEL_ID) if guild else None
    if not channel:
        return
    try:
        msg = await channel.fetch_message(state.poll_message_id)
        await msg.edit(embed=build_poll_embed())
    except Exception as e:
        logger.error(f"Failed to update daily poll: {e}")

# -----------------------------------------------------------------------------
# VIEWS & MODALS
# -----------------------------------------------------------------------------
class TimeModal(ui.Modal, title="Submit Event Time"):
    time_input = ui.TextInput(
        label="What time would you like the event to start?",
        placeholder="e.g. 3:00 PM or 15:00",
        required=True,
        max_length=20
    )

    def __init__(self, user_id: int):
        super().__init__()
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        state = state_mgr.state
        if not state.is_today() or state.poll_message_id is None:
            await interaction.response.send_message("❌ This poll is no longer active.", ephemeral=True)
            return
        minutes = parse_time_str(self.time_input.value)
        if minutes is None:
            await interaction.response.send_message(
                "❌ Invalid time. Please enter a valid time such as 3:00 PM or 15:00.",
                ephemeral=True
            )
            return
        if not is_time_valid(minutes):
            await interaction.response.send_message(
                "❌ That time is too early or in the past. Please enter a later time.",
                ephemeral=True
            )
            return
        if self.user_id not in state.votes or state.votes[self.user_id].vote is not True:
            await interaction.response.send_message("❌ Please select YES first.", ephemeral=True)
            return
        state.votes[self.user_id].submitted_time = self.time_input.value.strip()
        state.votes[self.user_id].time_minutes = minutes
        await state_mgr.save()
        await interaction.response.send_message("🕐 Your preferred time has been saved.", ephemeral=True)
        await update_daily_poll_message()

class DailyPollView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        state = state_mgr.state
        if not state.is_today() or not state.voting_deadline or datetime.now(TIMEZONE).timestamp() > state.voting_deadline:
            await interaction.response.send_message("🔒 Voting is now closed.", ephemeral=True)
            return False
        return True

    @ui.button(label="✅ YES", style=discord.ButtonStyle.Green, custom_id="poll_yes")
    async def yes_btn(self, interaction: discord.Interaction, button: ui.Button):
        state = state_mgr.state
        uid = interaction.user.id
        state.votes[uid] = UserVote(
            user_id=uid,
            username=interaction.user.display_name,
            vote=True,
            submitted_time=None,
            time_minutes=None
        )
        await state_mgr.save()
        await interaction.response.send_message(
            "✅ You selected YES. Please submit your preferred event time.",
            ephemeral=True,
            view=TimeSubmitView(uid)
        )
        await update_daily_poll_message()

    @ui.button(label="❌ NO", style=discord.ButtonStyle.Red, custom_id="poll_no")
    async def no_btn(self, interaction: discord.Interaction, button: ui.Button):
        state = state_mgr.state
        uid = interaction.user.id
        state.votes[uid] = UserVote(
            user_id=uid,
            username=interaction.user.display_name,
            vote=False
        )
        await state_mgr.save()
        await interaction.response.send_message("❌ Your vote has been recorded as NO.", ephemeral=True)
        await update_daily_poll_message()

class TimeSubmitView(ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id

    @ui.button(label="🕐 Submit Event Time", style=discord.ButtonStyle.Primary)
    async def submit_time(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your button.", ephemeral=True)
            return
        await interaction.response.send_modal(TimeModal(self.user_id))

class AttendanceView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        state = state_mgr.state
        if not state.event_confirmed or state.event_started:
            await interaction.response.send_message("❌ This event is no longer active.", ephemeral=True)
            return False
        return True

    @ui.button(label="✅ YES", style=discord.ButtonStyle.Green, custom_id="att_yes")
    async def yes_btn(self, interaction: discord.Interaction, button: ui.Button):
        state = state_mgr.state
        uid = interaction.user.id
        state.attendance[uid] = AttendanceRecord(uid, interaction.user.display_name, "yes")
        await apply_event_role(interaction.user, True)
        await state_mgr.save()
        await interaction.response.send_message(
            "✅ You're marked as attending and have received the Event role.",
            ephemeral=True
        )
        await update_all_attendance_messages()

    @ui.button(label="🤔 MAYBE", style=discord.ButtonStyle.Blurple, custom_id="att_maybe")
    async def maybe_btn(self, interaction: discord.Interaction, button: ui.Button):
        state = state_mgr.state
        uid = interaction.user.id
        state.attendance[uid] = AttendanceRecord(uid, interaction.user.display_name, "maybe")
        await apply_event_role(interaction.user, True)
        await state_mgr.save()
        await interaction.response.send_message(
            "🤔 You're marked as Maybe and have received the Event role.",
            ephemeral=True
        )
        await update_all_attendance_messages()

    @ui.button(label="❌ NO", style=discord.ButtonStyle.Red, custom_id="att_no")
    async def no_btn(self, interaction: discord.Interaction, button: ui.Button):
        state = state_mgr.state
        uid = interaction.user.id
        state.attendance[uid] = AttendanceRecord(uid, interaction.user.display_name, "no")
        await apply_event_role(interaction.user, False)
        await state_mgr.save()
        await interaction.response.send_message(
            "❌ You're marked as not attending and the Event role has been removed.",
            ephemeral=True
        )
        await update_all_attendance_messages()

# -----------------------------------------------------------------------------
# BOT SETUP
# -----------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# -----------------------------------------------------------------------------
# DAILY RESET & SCHEDULER
# -----------------------------------------------------------------------------
async def perform_daily_reset(guild: discord.Guild):
    state = state_mgr.state
    if state.daily_reset_done and state.is_today():
        return
    logger.info("Performing daily reset...")
    await remove_role_from_all(guild)
    state.reset_daily()
    state.daily_reset_done = True
    await state_mgr.save()
    await create_daily_poll(guild)

async def create_daily_poll(guild: discord.Guild):
    state = state_mgr.state
    channel = guild.get_channel(DAILY_POLL_CHANNEL_ID)
    if not channel:
        logger.error(f"Daily poll channel {DAILY_POLL_CHANNEL_ID} not found")
        return
    deadline = (datetime.now(TIMEZONE) + timedelta(minutes=VOTING_DURATION_MINUTES)).timestamp()
    state.voting_deadline = deadline
    embed = build_poll_embed()
    msg = await channel.send("@everyone", embed=embed, view=DailyPollView())
    state.poll_message_id = msg.id
    state.poll_channel_id = DAILY_POLL_CHANNEL_ID
    await state_mgr.save()
    logger.info("Daily poll created")

async def close_voting():
    state = state_mgr.state
    if not state.is_today() or state.event_confirmed:
        return
    logger.info("Closing voting...")
    guild = bot.get_guild(GUILD_ID)
    channel = guild.get_channel(DAILY_POLL_CHANNEL_ID) if guild else None
    if channel and state.poll_message_id:
        try:
            msg = await channel.fetch_message(state.poll_message_id)
            await msg.edit(content="🔒 @everyone\n🔒 Voting closed.", embed=msg.embed, view=None)
        except:
            pass
    yes_valid = [v for v in state.votes.values() if v.vote is True and v.time_minutes is not None]
    no_count = sum(1 for v in state.votes.values() if v.vote is False)
    if len(yes_valid) == 0 and no_count == 0:
        if channel:
            await channel.send("❌ No votes cast. No event scheduled.")
        return
    if len(yes_valid) > no_count:
        if not yes_valid:
            if channel:
                await channel.send("❌ EVENT NOT SCHEDULED — People voted Yes but nobody submitted a valid event time.")
            return
        times = [v.time_minutes for v in yes_valid if v.time_minutes is not None]
        avg_min = average_time(times)
        state.event_time_minutes = avg_min
        state.event_time_str = format_time(avg_min)
        state.event_confirmed = True
        await state_mgr.save()
        await create_attendance_poll(guild, state.event_time_str)
    elif len(yes_valid) == no_count:
        if channel:
            await channel.send("⚖️ VOTE TIED — There wasn't a majority, so no event will be scheduled today.")
    else:
        if channel:
            await channel.send("❌ NO EVENT TODAY — The majority voted No.")

async def create_attendance_poll(guild: discord.Guild, time_str: str):
    state = state_mgr.state
    embed = build_attendance_embed()
    state.attendance_message_ids = []
    for channel_id in EVENT_CHANNEL_IDS:
        channel = guild.get_channel(channel_id)
        if not channel:
            logger.error(f"Event channel {channel_id} not found")
            continue
        msg = await channel.send("@everyone", embed=embed, view=AttendanceView())
        state.attendance_message_ids.append(msg.id)
    state.reminders_started = True
    await state_mgr.save()
    logger.info(f"Attendance polls created in {len(EVENT_CHANNEL_IDS)} channels")

async def process_reminders():
    state = state_mgr.state
    if not state.event_confirmed or not state.event_time_minutes or state.event_started:
        return
    now = datetime.now(TIMEZONE)
    now_min = now.hour * 60 + now.minute
    event_min = state.event_time_minutes
    minutes_to_start = event_min - now_min
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    role = await get_event_role(guild)
    role_mention = f"<@&{role.id}>" if role else "@Event"
    if minutes_to_start <= 0:
        if not state.event_started:
            state.event_started = True
            await state_mgr.save()
            for channel_id in EVENT_CHANNEL_IDS:
                channel = guild.get_channel(channel_id)
                if channel:
                    await channel.send(f"{role_mention}\n🏆 THE EVENT IS STARTING NOW!\nGood luck everyone! 🔥")
        return
    if minutes_to_start <= FINAL_REMINDER_MINUTES_BEFORE and not state.final_reminder_sent:
        state.final_reminder_sent = True
        await state_mgr.save()
        for channel_id in EVENT_CHANNEL_IDS:
            channel = guild.get_channel(channel_id)
            if channel:
                await channel.send(f"{role_mention}\n🚨 FINAL REMINDER!\nThe event starts in {minutes_to_start} minutes!\nGet ready!")
        return
    if state.last_reminder_sent_minutes is None or state.last_reminder_sent_minutes - minutes_to_start >= REMINDER_INTERVAL_MINUTES:
        state.last_reminder_sent_minutes = minutes_to_start
        await state_mgr.save()
        for channel_id in EVENT_CHANNEL_IDS:
            channel = guild.get_channel(channel_id)
            if channel:
                await channel.send(f"{role_mention}\n⏰ EVENT REMINDER!\nThe event starts at {state.event_time_str}.\nGet ready!")

@tasks.loop(minutes=1)
async def scheduler_loop():
    now = datetime.now(TIMEZONE)
    state = state_mgr.state
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    if now.hour == DAILY_POLL_HOUR and now.minute == DAILY_POLL_MINUTE:
        if not state.is_today() or not state.daily_reset_done:
            await perform_daily_reset(guild)
    if state.voting_deadline and now.timestamp() > state.voting_deadline and not state.event_confirmed:
        await close_voting()
    if state.event_confirmed and not state.event_started:
        await process_reminders()

@scheduler_loop.before_loop
async def before_scheduler():
    await bot.wait_until_ready()

# -----------------------------------------------------------------------------
# SLASH COMMANDS
# -----------------------------------------------------------------------------
def is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    if interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator:
        return True
    if ADMIN_ROLE_ID and any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles):
        return True
    return False

@bot.tree.command(name="eventstatus", description="Show current event status")
async def cmd_status(interaction: discord.Interaction):
    state = state_mgr.state
    status = f"📅 Date: {state.date_str or 'Not started'}\n"
    status += f"🗳️ Voting active: {'Yes' if state.voting_deadline and not state.event_confirmed else 'No'}\n"
    status += f"✅ Event confirmed: {'Yes' if state.event_confirmed else 'No'}\n"
    if state.event_confirmed:
        status += f"🕐 Event time: {state.event_time_str}\n"
        status += f"🔔 Event started: {'Yes' if state.event_started else 'No'}"
    await interaction.response.send_message(status, ephemeral=True)

@bot.tree.command(name="eventreset", description="Reset current event system (Admin)")
@app_commands.check(is_admin)
async def cmd_reset(interaction: discord.Interaction):
    state = state_mgr.state
    state.reset_daily()
    state.daily_reset_done = True
    await state_mgr.save()
    await interaction.response.send_message("✅ Event system reset. New poll will be created at 10:00.", ephemeral=True)

@bot.tree.command(name="eventstart", description="Manually start event process (Admin)")
@app_commands.check(is_admin)
async def cmd_start(interaction: discord.Interaction):
    if not interaction.guild:
        return
    state = state_mgr.state
    state.reset_daily()
    await state_mgr.save()
    await create_daily_poll(interaction.guild)
    await interaction.response.send_message("✅ Daily poll created.", ephemeral=True)

@bot.tree.command(name="eventcancel", description="Cancel current event (Admin)")
@app_commands.check(is_admin)
async def cmd_cancel(interaction: discord.Interaction):
    state = state_mgr.state
    state.event_confirmed = False
    state.event_started = True
    state.reminders_started = False
    await state_mgr.save()
    await interaction.response.send_message("✅ Event cancelled.", ephemeral=True)

@bot.tree.command(name="eventremind", description="Send immediate reminder (Admin)")
@app_commands.check(is_admin)
async def cmd_remind(interaction: discord.Interaction):
    state = state_mgr.state
    if not state.event_confirmed or not state.event_time_str:
        await interaction.response.send_message("❌ No active event.", ephemeral=True)
        return
    guild = interaction.guild
    role = await get_event_role(guild)
    mention = f"<@&{role.id}>" if role else "@Event"
    for cid in EVENT_CHANNEL_IDS:
        channel = guild.get_channel(cid)
        if channel:
            await channel.send(f"{mention}\n⏰ Reminder: Event starts at {state.event_time_str}!")
    await interaction.response.send_message("✅ Reminder sent to both channels.", ephemeral=True)

@bot.tree.command(name="eventrole", description="Check Event role status")
async def cmd_role(interaction: discord.Interaction):
    if not interaction.guild:
        return
    role = await get_event_role(interaction.guild)
    if role:
        await interaction.response.send_message(f"✅ Event role found: {role.name} (ID: {role.id})", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Event role '{EVENT_ROLE_NAME}' not found!", ephemeral=True)

@bot.tree.command(name="botstatus", description="Check bot status and time")
async def cmd_botstatus(interaction: discord.Interaction):
    now = datetime.now(TIMEZONE)
    guild = bot.get_guild(GUILD_ID)
    status = f"✅ Bot operational\n🕐 London time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    status += f"🌐 Guild: {guild.name if guild else 'Not found'}\n"
    status += f"🗳️ Daily Poll Channel: {DAILY_POLL_CHANNEL_ID}\n"
    status += f"🏟️ Event Channels: {', '.join(map(str, EVENT_CHANNEL_IDS))}\n"
    status += f"🏆 Daily event system: ACTIVE"
    await interaction.response.send_message(status, ephemeral=True)

# -----------------------------------------------------------------------------
# BOT EVENTS
# -----------------------------------------------------------------------------
@bot.event
async def on_ready():
    await state_mgr.load()
    state = state_mgr.state
    guild = bot.get_guild(GUILD_ID)
    if guild:
        role = await get_event_role(guild)
        if not role:
            logger.warning(f"⚠️ Role '{EVENT_ROLE_NAME}' not found! Create it for role management to work.")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash commands")
    except Exception as e:
        logger.error(f"Command sync failed: {e}")
    logger.info("=" * 60)
    logger.info(f"Bot online as {bot.user}")
    logger.info(f"Guild ID: {GUILD_ID}")
    logger.info(f"Timezone: Europe/London")
    logger.info(f"Daily Poll Channel: {DAILY_POLL_CHANNEL_ID}")
    logger.info(f"Event Channels: {EVENT_CHANNEL_IDS}")
    logger.info(f"Current London time: {datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Daily event system: ACTIVE")
    logger.info("=" * 60)
    if not state.is_today():
        state.reset_daily()
        await state_mgr.save()
    if state.voting_deadline and datetime.now(TIMEZONE).timestamp() < state.voting_deadline and not state.event_confirmed:
        logger.info("Resuming active daily poll...")
    if state.event_confirmed and not state.event_started:
        logger.info("Resuming active event & reminders...")
    if not scheduler_loop.is_running():
        scheduler_loop.start()

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
def main():
    if not DISCORD_TOKEN or not GUILD_ID:
        logger.error("Missing required environment variables: DISCORD_TOKEN, GUILD_ID")
        return
    try:
        bot.run(DISCORD_TOKEN)
    except discord.LoginFailure:
        logger.error("Invalid Discord token")
    except Exception as e:
        logger.error(f"Fatal error: {e}")

if __name__ == "__main__":
    main()
